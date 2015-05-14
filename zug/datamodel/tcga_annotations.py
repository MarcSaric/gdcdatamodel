import re
import os
import requests
from cdisutils.log import get_logger
from datetime import datetime
from time import mktime, strptime
from uuid import UUID, uuid5
from psqlgraph import PsqlGraphDriver

from psqlgraph import Node

from gdcdatamodel.models import (
    File, Aliquot, Analyte,
    Sample, Portion, Participant,
    Annotation
)


BASE_URL = "https://tcga-data.nci.nih.gov/annotations/resources/searchannotations/json"

DATE_RE = re.compile('(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(-\d{1,2}:\d{2})')

ANNOTATION_NAMESPACE = UUID('e61d5a88-7f5c-488e-9c42-a5f32b4d1c50')


def unix_time(dt):
    epoch = datetime.utcfromtimestamp(0)
    delta = dt - epoch
    return long(delta.total_seconds())


class TCGAAnnotationSyncer(object):

    def __init__(self):
        self.graph = PsqlGraphDriver(
            os.environ["PG_HOST"],
            os.environ["PG_USER"],
            os.environ["PG_PASS"],
            os.environ["PG_NAME"],
        )
        self.log = get_logger('tcga_annotation_sync')

    def download_annotations(self, url=BASE_URL):
        """Downloads all annotations from TCGA
        """
        self.log.info('Downloading annotations from %s', url)
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.json()["dccAnnotation"]

    def go(self):
        annotations_doc = self.download_annotations()
        with self.graph.session_scope():
            for annotation_doc in annotations_doc:
                self.insert_annotation(annotation_doc)

    def insert_annotation(self, doc):
        """Insert a single annotation dict into graph
        """
        assert len(doc["items"]) == 1
        item = doc["items"][0]
        dst = self.lookup_item_node(item)
        if not dst:
            self.log.info("No item found for annotation %s, skipping", doc["id"])
            return
        for note_id, note_text in self.get_notes(doc).items():
            annotation = Annotation(
                node_id=self.generate_uuid(self.get_submitter_id(doc)+note_id),
                submitter_id=self.get_submitter_id(doc),
                category=self.get_category(doc),
                classification=self.get_classification(doc),
                creator=self.get_creator(doc),
                created_datetime=self.get_created_datetime(doc),
                status=self.get_status(doc),
                notes=note_text,
            )
            if isinstance(dst, File):
                annotation.files = [dst]
            elif isinstance(dst, Sample):
                annotation.samples = [dst]
            elif isinstance(dst, Participant):
                annotation.participants = [dst]
            elif isinstance(dst, Portion):
                annotation.portions = [dst]
            elif isinstance(dst, Analyte):
                annotation.analytes = [dst]
            elif isinstance(dst, Aliquot):
                annotation.aliquots = [dst]
            else:
                raise("annotations cannot annotate {}".format(dst))
            self.graph.current_session().merge(annotation)

    def generate_uuid(self, key):
        """UUID generated from key=(target barcode + noteID)
        """
        return str(uuid5(ANNOTATION_NAMESPACE, key))

    def get_category(self, doc):
        return doc['annotationCategory']['categoryName']

    def get_submitter_id(self, doc):
        return str(doc['id'])

    def get_classification(self, doc):
        return doc[
            'annotationCategory'][
                'annotationClassification'][
                    'annotationClassificationName']

    def get_creator(self, doc):
        return doc['createdBy']

    def get_created_datetime(self, doc):
        return self.parse_datetime(DATE_RE.match(doc['dateCreated']).group(1))

    def get_status(self, doc):
        return doc['status']

    def get_notes(self, doc):
        return {str(n['noteId']): n['noteText'] for n in doc.get('notes', [])}

    def parse_datetime(self, text):
        return unix_time(datetime.fromtimestamp(mktime(strptime(
            text, '%Y-%m-%dT%H:%M:%S'))))

    def lookup_item_node(self, item):
        """Lookup node by barcode under it's supposed label.  If we can't find
        it, check again without label constraint and complain if we
        find it that way.

        """
        cls = Node.get_subclass(item['itemType']['itemTypeName'].lower())
        node = self.graph.nodes(cls)\
                         .props({'submitter_id': item['item']})\
                         .scalar()
        return node