import logging
import unittest
import os
from zug.datamodel import xml2psqlgraph, latest_urls, extract_tar, \
    bcr_xml_mapping
from zug.datamodel.prelude import create_prelude_nodes
from psqlgraph.validate import AvroNodeValidator, AvroEdgeValidator
from gdcdatamodel import node_avsc_object, edge_avsc_object

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

data_dir = os.path.dirname(os.path.realpath(__file__))

datatype = 'biospecimen'
host = 'localhost'
user = 'test'
password = 'test'
database = 'automated_test'


def initialize():

    node_validator = AvroNodeValidator(node_avsc_object)
    edge_validator = AvroEdgeValidator(edge_avsc_object)

    parser = latest_urls.LatestURLParser(
        constraints={'data_level': 'Level_1', 'platform': 'bio'},
        url_key='dcc_archive_url',
    )
    extractor = extract_tar.ExtractTar(
        regex=".*(bio).*(Level_1).*\\.xml"
    )
    converter = xml2psqlgraph.xml2psqlgraph(
        xml_mapping=bcr_xml_mapping,
        host=host,
        user=user,
        password=password,
        database=database,
        edge_validator=edge_validator,
        node_validator=node_validator,
    )
    return parser, extractor, converter


class TestTCGABiospeceminImport(unittest.TestCase):

    IGNORED_LABELS = [
        'center', 'tissue_source_site', 'tag', 'experimental_strategy',
        'platform', 'data_subtype', 'data_type', 'program', 'project',
        'data_format']

    def setUp(self):
        logging.basicConfig(level=logging.DEBUG)
        self.parser, self.extrator, self.converter = initialize()
        create_prelude_nodes(self.converter.graph)

    def tearDown(self):
        with self.converter.graph.engine.begin() as conn:
            conn.execute('delete from edges')
            conn.execute('delete from nodes')
            conn.execute('delete from voided_edges')
            conn.execute('delete from voided_nodes')
        self.converter.graph.engine.dispose()

    def test_convert_sample(self):
        with open(os.path.join(data_dir, 'sample_biospecimen.xml')) as f:
            xml = f.read()
        self.converter.xml2psqlgraph(xml)
        self.converter.export()

    def test_convert_validate_nodes_sample(self):
        self.converter.export_nodes()
        with open(os.path.join(data_dir, 'sample_biospecimen.xml')) as f:
            xml = f.read()
        self.converter.xml2psqlgraph(xml)
        self.converter.export_nodes()

    def test_convert_validate_edges_sample(self):
        self.converter.export_nodes()
        with open(os.path.join(data_dir, 'sample_biospecimen.xml')) as f:
            xml = f.read()
        self.converter.xml2psqlgraph(xml)
        self.converter.export_nodes()
        self.converter.export_edges()

    def test_versioned_idempotency(self):
        g = self.converter.graph
        self.converter.export_nodes()
        with open(os.path.join(data_dir, 'sample_biospecimen.xml')) as f:
            xml = f.read()

        self.converter.xml2psqlgraph(xml)
        self.converter.export(group_id='group1', version=1)
        v1 = {n.node_id: n for n in g.get_nodes().all()
              if n.label not in self.IGNORED_LABELS}

        self.converter.xml2psqlgraph(xml)
        self.converter.export(group_id='group1', version=2.5)
        v2 = {n.node_id: n for n in g.get_nodes().all()
              if n.label not in self.IGNORED_LABELS}

        for node_id, node in v1.iteritems():
            self.assertTrue(node_id in v2)
            self.assertTrue(v1[node_id].system_annotations == {
                'group_id': 'group1', 'version': 1})
            self.assertEqual(v1[node_id].properties, v2[node_id].properties)

        for node_id, node in v2.iteritems():
            self.assertTrue(node_id in v1)
            self.assertTrue(v2[node_id].system_annotations == {
                'group_id': 'group1', 'version': 2.5})
            self.assertEqual(v2[node_id].properties, v1[node_id].properties)

    def test_versioned_import(self):
        self.converter.export_nodes()

        g = self.converter.graph

        with open(os.path.join(data_dir, 'sample_biospecimen.xml')) as f:
            xml = f.read()
        self.converter.xml2psqlgraph(xml)
        self.converter.export(group_id='group1', version=1)
        v1 = {n.node_id: n for n in g.get_nodes().all()
              if n.label not in self.IGNORED_LABELS}

        with open(os.path.join(data_dir, 'sample_biospecimen_v2.xml')) as f:
            xml = f.read()
        self.converter.xml2psqlgraph(xml)
        self.converter.export(group_id='group1', version=2.5)
        v2 = {n.node_id: n for n in g.get_nodes().all()
              if n.label not in self.IGNORED_LABELS}

        for node_id, node in v1.iteritems():
            self.assertTrue(node_id in v2)
            self.assertTrue(v1[node_id].system_annotations == {
                'group_id': 'group1', 'version': 1})

        for node_id, node in v2.iteritems():
            self.assertTrue(node_id in v1)
            self.assertTrue(v2[node_id].system_annotations == {
                'group_id': 'group1', 'version': 2.5})
            if v1[node_id].node_id != '5fa9998b-deff-493e-8a8e-dc2422192a48':
                self.assertEqual(
                    v1[node_id].properties, v2[node_id].properties)
            else:
                self.assertFalse(v1[node_id]['is_ffpe'])
                self.assertTrue(v2[node_id]['is_ffpe'])
