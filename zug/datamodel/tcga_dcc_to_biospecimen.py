from cdisutils.log import get_logger
from psqlgraph import PsqlEdge
import os

class TCGADCCToBiospecimen(object):

    def __init__(self,file_node,pg_driver):
        '''
        DCC to biospecimen edge builder class  which tie DCC file node to 
        biospecimen nodes from file node's system annotation, if the file is
        not tied to any biospecimen from magetab parsing.

        '''
        self.file_node = file_node
        self.pg_driver = pg_driver
        self.log = get_logger('tcga_dcc_to_biospecimen_'
                    +str(os.getpid()) + '_' + self.name)

    @property
    def name(self):
        return self.file_node["file_name"]

    def _get_archive(self):
        return self.pg_driver.nodes().labels('archive').with_edge_from_node('member_of',self.file_node).first().system_annotations

    def build(self):
        '''build edges between the given file node and biospecimen nodes'''
        with self.pg_driver.session_scope() as session:
            self.archive = self._get_archive()
            self.tie_file_from_classification(self.file_node,session)

    def tie_file_from_classification(self,file_node,session):
        attrs = file_node.system_annotations
        self.log.debug(attrs) 
        for edge in  file_node.edges_out:
            if edge.label == 'data_from' and \
                edge.system_annotations['source']=='tcga_magetab':
                return
        nodes=[]
        # find aliquot or slide node that should be tied to this file
        for possible_attr in ['_aliquot','_tumor_aliquot','_control_aliquot',\
           '_slide']: 
            node=None
            if possible_attr+'_uuid' in attrs:
                node = self.pg_driver.nodes().ids(
                    [attrs[possible_attr+'_uuid']]).first()
            elif possible_attr+'_barcode' in attrs:
                node = self.pg_driver.nodes().props(
                    {'submitter_id':attrs[possible_attr+'_barcode']}).first()
            if node:
                self.log.info("find %s %s",node.label,node['submitter_id'])
                nodes.append(node)
        
        # if no biospecimen is tied to this file, find participant that should be tied to this file
        if len(nodes) == 0:
            node=None
            if '_participant_uuid' in attrs:
                node = self.pg_driver.nodes().ids(
                    [attrs['_participant_uuid']]).first()
            elif '_participant_barcode' in attrs:
                node = self.pg_driver.nodes().props(
                    {'submitter_id':attrs['_participant_barcode']}).first()
            if node:
                self.log.info("find %s %s",node.label,node['submitter_id'])
                nodes.append(node)

        for node in nodes:
            maybe_edge_to_biospecimen = self.pg_driver.edge_lookup_one(
                label='data_from',
                src_id=file_node.node_id,
                dst_id=node.node_id)
            if not maybe_edge_to_biospecimen:
                edge_to_biospecimen = PsqlEdge(label='data_from',
                    src_id=file_node.node_id,
                    dst_id=node.node_id)
                edge_to_biospecimen.system_annotations['source']='filename'
                self.pg_driver.edge_insert(edge_to_biospecimen,session=session)
 