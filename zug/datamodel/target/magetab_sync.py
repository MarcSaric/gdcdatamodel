import os
from cStringIO import StringIO
import requests
import pandas as pd
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm.exc import NoResultFound
from psqlgraph import PsqlEdge
from psqlgraph.validate import AvroNodeValidator, AvroEdgeValidator
from gdcdatamodel import node_avsc_object, edge_avsc_object

from cdisutils.log import get_logger

from zug.datamodel.target.dcc_sync import tree_walk
from zug.datamodel.tcga_magetab_sync import group_by_protocol, cleanup_list, cleanup_row, FILE_COL_NAMES


def is_file_group(group):
    group = cleanup_list(group)
    if "Derived Data File REF" in group:
        return True
    if "Comment [Derived Data File REF]" in group:
        return True
    for name in FILE_COL_NAMES:
        if name in group:
            return True
    return False


def get_file(subrow):
    ret = None
    for name in FILE_COL_NAMES:
        if subrow.get(name):
            if not ret:
                ret = subrow[name]
            else:
                raise RuntimeError("subrow %s has more than one file column", subrow)
    return ret


def get_name_and_version(sdrf):
    parts = sdrf.replace(".sdrf.txt", "").split("_")
    date = parts.pop()
    return "_".join(parts), datetime.strptime(date, "%Y%m%d").toordinal()


class TARGETMAGETABSyncer(object):

    def __init__(self, project, graph=None, dcc_auth=None):
        self.project = project
        self.url = "https://target-data.nci.nih.gov/{}/".format(project)
        assert self.url.startswith("https://target-data.nci.nih.gov")
        self.dcc_auth = dcc_auth
        self.graph = graph
        if self.graph:
            self.graph.node_validator = AvroNodeValidator(node_avsc_object)
            self.graph.edge_validator = AvroEdgeValidator(edge_avsc_object)
        self.log = get_logger("target_magetab_sync_{}_{}".format(os.getpid(), self.project))

    def magetab_links(self):
        for link in tree_walk(self.url, auth=self.dcc_auth):
            if link.endswith(".sdrf.txt"):
                yield link

    def df_for_link(self, link):
        resp = requests.get(link, auth=self.dcc_auth)
        return pd.read_table(StringIO(resp.content))

    def aliquot_for(self, row):
        """Target magetabs appear fairly well organized, each seems to have a
        'Source Name' column, which has the participant barcode, a
        'Sample Name' column, which has the sample barcode, and an
        'Extract Name' column, which has the aliquot barcode. This
        method parses that information out.
        """
        participant = row["Source Name"]
        sample = row["Sample Name"]
        aliquot = row["Extract Name"]
        assert aliquot.startswith("TARGET-")
        assert aliquot.startswith(sample)
        assert aliquot.startswith(participant)
        return aliquot

    def compute_mappings(self):
        """Returns a dict, the keys of which are names of sdrf files that
        produced a mapping, the values ("mappings") are dicts from
        filename to aliquot barcode
        """
        self.log.info("computing mapping")
        ret = {}
        for link in self.magetab_links():
            sdrf = link.split("/")[-1]
            mapping = defaultdict(lambda: set())
            self.log.info("processing %s", link)
            df = self.df_for_link(link)
            for _, row in df.iterrows():
                aliquot = self.aliquot_for(row)
                groups = group_by_protocol(df)
                file_groups = [group for group in groups if is_file_group(group)]
                for group in file_groups:
                    subrow = row[group]
                    cleanup_row(subrow)
                    filename = get_file(subrow)
                    if not pd.notnull(filename):
                        continue
                    else:
                        mapping[filename].add(aliquot)
            ret[sdrf] = mapping
        return ret

    def sync(self):
        mappings = self.compute_mappings()
        self.insert_mappings_in_graph(mappings)

    def insert_mappings_in_graph(self, mappings):
        with self.graph.session_scope():
            for sdrf, mapping in mappings.iteritems():
                self.log.info("inserting mapping for %s", sdrf)
                self.insert_mapping_in_graph(sdrf, mapping)

    def insert_mapping_in_graph(self, sdrf_name, mapping):
        sdrf = self.graph.nodes().labels("file")\
                                 .sysan({"source": "target_dcc"})\
                                 .props({"file_name": sdrf_name}).scalar()
        if not sdrf:
            self.log.warning("sdrf %s not found", sdrf_name)
            return
        for file_name, aliquot_barcodes in mapping.iteritems():
            try:
                # note that for now this assumes filenames are
                # unique (this is the case in WT), it will blow up
                # with MultipleResultsFound exception and fail to
                # insert anything if this is not the case.
                # presumably this will need to be revisited in the future
                file = self.graph.nodes().labels("file")\
                                         .sysan({"source": "target_dcc"})\
                                         .props({"file_name": file_name}).one()
                self.log.info("found file %s as %s", file_name, file)
            except NoResultFound:
                self.log.warning("file %s not found in graph", file_name)
                continue
            self.tie_file_to_sdrf(file, sdrf)
            for barcode in aliquot_barcodes:
                self.log.info("attempting to tie file %s to aliquot %s", file_name, barcode)
                try:
                    aliquot = self.graph.nodes().labels("aliquot")\
                                                .sysan({"source": "target_sample_matrices"})\
                                                .props({"submitter_id": barcode}).one()
                    self.log.info("found aliquot %s", barcode)
                except NoResultFound:
                    self.log.warning("aliquot %s not found in graph", barcode)
                    continue
                self.tie_file_to_aliquot(file, aliquot, sdrf)
            # TODO add code to delete edges from old versions of this
            # sdrf when we insert a new one

    def tie_file_to_aliquot(self, file, aliquot, sdrf):
        maybe_edge_to_aliquot = self.graph.edges().labels("data_from")\
                                                  .src(file.node_id)\
                                                  .dst(aliquot.node_id)\
                                                  .scalar()
        sdrf_name, sdrf_version = get_name_and_version(sdrf["file_name"])
        if not maybe_edge_to_aliquot:
            edge_to_aliquot = PsqlEdge(
                label="data_from",
                src_id=file.node_id,
                dst_id=aliquot.node_id,
                system_annotations={
                    "source": "target_magetab",
                    "sdrf_name": sdrf_name,
                    "sdrf_version": sdrf_version,
                }
            )
            self.graph.edge_insert(edge_to_aliquot)

    def tie_file_to_sdrf(self, file, sdrf):
        maybe_edge = self.graph.edges().labels("related_to")\
                                       .src(sdrf.node_id)\
                                       .dst(file.node_id)\
                                       .scalar()
        if not maybe_edge:
            sdrf_name, sdrf_version = get_name_and_version(sdrf["file_name"])
            edge = PsqlEdge(
                label="related_to",
                src_id=sdrf.node_id,
                dst_id=file.node_id,
                system_annotations={
                    "source": "target_magetab",
                    "sdrf_name": sdrf_name,
                    "sdrf_version": sdrf_version,
                }
            )
            self.graph.edge_insert(edge)
