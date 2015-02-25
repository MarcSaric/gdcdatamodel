from gdcdatamodel.mappings import (
    get_project_es_mapping, index_settings,
    annotation_tree, get_annotation_es_mapping,
    participant_tree, participant_traversal, get_participant_es_mapping,
    file_tree, file_traversal, get_file_es_mapping,
    ONE_TO_ONE, ONE_TO_MANY
)
import json
import logging
from cdisutils.log import get_logger
import networkx as nx
from psqlgraph import Edge
import re
import itertools
from sqlalchemy.orm import joinedload
from progressbar import ProgressBar, Percentage, Bar, ETA
from elasticsearch import NotFoundError

log = get_logger("psqlgraph2json")
log.setLevel(level=logging.INFO)


class PsqlGraph2JSON(object):

    """
    """

    def __init__(self, psqlgraph_driver, es=None):
        """Walks the graph to produce elasticsearch json documents.
        Assumptions include:

        """
        self.g = psqlgraph_driver
        self.G = nx.Graph()
        self.es = es
        self.index_pattern = '{base}_{n}'
        self.patch_trees()
        self.leaf_nodes = ['center', 'tissue_source_site']
        self.experimental_strategies = {}
        self.data_types = {}
        self.flatten = {
            'tag': 'name',
            'platform': 'name',
            'data_format': 'name',
            'data_subtype': 'name',
            'experimental_strategy': 'name',
            'data_level': 'name',
        }
        self.differentiated_edges = [
            ('file', 'member_of', 'archive'),
            ('archive', 'member_of', 'file'),
            ('file', 'describes', 'participant'),
            ('participant', 'describes', 'file')
        ]

    def pbar(self, title, maxval):
        pbar = ProgressBar(widgets=[
            title, Percentage(), ' ',
            Bar(marker='#', left='[', right=']'), ' ',
            ETA(), ' '], maxval=maxval)
        pbar.update(0)
        return pbar

    def es_bulk_upload(self, index, doc_type, docs, batch_size=256):
        if not self.es:
            log.error('No elasticsearch driver initialized')
        instruction = {"index": {"_index": index, "_type": doc_type}}
        pbar = self.pbar('{} upload '.format(doc_type), len(docs))

        def body():
            start = pbar.currval
            for doc in docs[start:start+batch_size]:
                yield instruction
                yield doc
                pbar.update(pbar.currval+1)
        while pbar.currval < len(docs):
            res = self.es.bulk(body=body())
            if res['errors']:
                raise RuntimeError(json.dumps([
                    d for d in res['items'] if d['index']['status'] != 100
                ], indent=2))
        pbar.finish()

    def es_put_mappings(self, index):
        if not self.es:
            log.error('No elasticsearch driver initialized')
        return [
            self.es.indices.put_mapping(
                index=index,
                doc_type="project",
                body=get_project_es_mapping()),
            self.es.indices.put_mapping(
                index=index,
                doc_type="file",
                body=get_file_es_mapping()),
            self.es.indices.put_mapping(
                index=index,
                doc_type="participant",
                body=get_participant_es_mapping()),
            self.es.indices.put_mapping(
                index=index,
                doc_type="annotation",
                body=get_annotation_es_mapping()),
        ]

    def es_index_create_and_populate(self, index, part_docs=None,
                                     file_docs=None, project_docs=None,
                                     ann_docs=None):
        self.es.indices.create(index=index, body=index_settings())
        self.es_put_mappings(index)
        if not part_docs:
            part_docs, file_docs, ann_docs = self.denormalize_participants()
        if not project_docs:
            project_docs = self.denormalize_projects()
        self.es_bulk_upload(index, 'annotation', ann_docs)
        self.es_bulk_upload(index, 'project', project_docs)
        self.es_bulk_upload(index, 'participant', part_docs)
        self.es_bulk_upload(index, 'file', file_docs)

    def swap_index(self, old_index, new_index, alias):
        self.es.indices.update_aliases({'actions': [
            {'remove': {'index': old_index, 'alias': alias}},
            {'add': {'index': new_index, 'alias': alias}}]})

    def get_next_index(self, base):
        indices = set(self.es.indices.get_aliases().keys())
        p = re.compile(self.index_pattern.format(base=base, n='(\d+)')+'$')
        matches = [p.match(index) for index in indices if p.match(index)]
        next_n = max(sorted([int(m.group(1)) for m in matches]+[0]))+1
        return self.index_pattern.format(base=base, n=next_n)

    def lookup_index_by_alias(self, alias):
        try:
            keys = self.es.indices.get_alias(alias).keys()
            if not keys:
                return None
            return keys[0]
        except NotFoundError:
            return None

    def deploy_alias(self, alias, rollback_count=5, **kwargs):
        new_index = self.get_next_index(alias)
        self.es_index_create_and_populate(new_index, **kwargs)
        old_index = self.lookup_index_by_alias(alias)
        if old_index:
            self.swap_index(old_index, new_index, alias)
        else:
            self.es.indices.put_alias(index=new_index, name=alias)

    def patch_trees(self):
        # Include files only attached to biospecimen pathway via another file
        participant_tree.file.file.corr = (ONE_TO_MANY, 'files')
        if ('file',) not in participant_traversal['file']:
            participant_traversal['file'].append(('file',))

        # Add leaves to root for things like target
        participant_tree.aliquot = participant_tree.sample\
                                                   .portion\
                                                   .analyte\
                                                   .aliquot

        # Format tree in way that allows uniform walking
        self.ptree_mapping = {'participant': participant_tree.to_dict()}
        self.ftree_mapping = {'file': file_tree.to_dict()}
        self.atree_mapping = {'annotation': annotation_tree.to_dict()}

    def cache_database(self):
        pbar = self.pbar('Caching Database: ', self.g.edges().count())
        for e in self.g.edges().options(joinedload(Edge.src))\
                               .options(joinedload(Edge.dst))\
                               .yield_per(int(1e5)):
            pbar.update(pbar.currval+1)
            needs_differentiation = ((e.src.label, e.label, e.dst.label)
                                     in self.differentiated_edges)
            if needs_differentiation and e.properties:
                self.G.add_edge(
                    e.src, e.dst, label=e.label, props=e.properties)
            elif needs_differentiation and not e.properties:
                self.G.add_edge(e.src, e.dst, label=e.label)
            elif e.properties:
                self.G.add_edge(e.src, e.dst, props=e.properties)
            else:
                self.G.add_edge(e.src, e.dst)
        pbar.finish()
        print('Cached {} nodes'.format(self.G.number_of_nodes()))

    def nodes_labeled(self, label):
        for n, p in self.G.nodes_iter(data=True):
            if n.label == label:
                yield n

    def neighbors_labeled(self, node, labels):
        labels = labels if hasattr(labels, '__iter__') else [labels]
        for n in self.G.neighbors(node):
            if n.label in labels:
                yield n

    def parse_tree(self, tree, result):
        for key in tree:
            if key != 'corr':
                result[key] = {}
                self.parse_tree(tree[key], result[key])
        return result

    def _get_base_doc(self, node):
        base = {'{}_id'.format(node.label): node.node_id}
        base.update(node.properties)
        return base

    def create_tree(self, node, mapping, tree):
        if node.label in self.leaf_nodes:
            return {}
        submap = mapping[node.label]
        corr, plural = submap['corr']
        for child in self.G.neighbors(node):
            if child.label not in submap:
                continue
            tree[child] = {}
            self.create_tree(child, submap, tree[child])
        return tree

    def walk_tree(self, node, tree, mapping, doc, level=0):
        corr, plural = mapping[node.label]['corr']
        subdoc = self._get_base_doc(node)
        for child in tree[node]:
            child_corr, child_plural = mapping[node.label][child.label]['corr']
            if child_plural not in subdoc and child_corr == ONE_TO_ONE:
                subdoc[child_plural] = {}
            elif child_plural not in subdoc:
                subdoc[child_plural] = []
            self.walk_tree(child, tree[node], mapping[node.label],
                           subdoc[child_plural], level+1)
        if corr == ONE_TO_MANY:
            doc.append(subdoc)
        else:
            doc.update(subdoc)
        return doc

    def copy_tree(self, original, new):
        for node in original:
            new[node] = {}
            self.copy_tree(original[node], new[node])
        return new

    def walk_path(self, node, path, whole=False):
        if path:
            for neighbor in self.neighbors_labeled(node, path[0]):
                if whole or (len(path) == 1 and path[0] == neighbor.label):
                    yield neighbor
                for n in self.walk_path(neighbor, path[1:], whole):
                    yield n

    def walk_paths(self, node, paths, whole=False):
        return {n for n in itertools.chain(
            *[self.walk_path(node, path, whole=whole)
              for path in paths])}

    def _cache_data_types(self):
        if len(self.data_types):
            return
        print('Caching data types')
        for data_type in self.nodes_labeled('data_type'):
            self.data_types[data_type] = set(self.walk_path(
                data_type, ['data_subtype', 'file']))

    def _cache_experimental_strategies(self):
        if len(self.experimental_strategies):
            return
        print('Caching experitmental strategies')
        for exp_strat in self.nodes_labeled('experimental_strategy'):
            self.experimental_strategies[exp_strat] = set(self.walk_path(
                exp_strat, ['file']))

    def get_exp_strats(self, files):
        self._cache_experimental_strategies()
        for exp_strat, file_list in self.experimental_strategies.iteritems():
            intersection = (file_list & files)
            if intersection:
                yield {'experimental_strategy': exp_strat['name'],
                       'file_count': len(intersection)}

    def get_data_types(self, files):
        self._cache_data_types()
        for data_type, file_list in self.data_types.iteritems():
            intersection = (file_list & files)
            if intersection:
                yield {'data_type': data_type['name'],
                       'file_count': len(intersection)}

    def get_participant_summary(self, node, files):
        return {
            'file_count': len(files),
            'file_size': sum([f['file_size'] for f in files]),
            'experimental_strategies': list(self.get_exp_strats(files)),
            'data_types': list(self.get_data_types(files)),
        }

    def reconstruct_biospecimen_paths(self, participant):
        """For each sample.aliquot, reconstruct entire path"""

        for sample in participant.get('samples', []):
            sample['portions'] = sample.get('portions', [])
            for aliquot in sample.pop('aliquots', []):
                sample['portions'].append({'portion': {'analyte': {aliquot}}})

    def get_metadata_files(self, participant):
        neighbors = self.G[participant]
        files = []
        for n in neighbors:
            if self.G[participant][n].get('label', None) == 'describes':
                files.append(self._get_base_doc(n))
        return files

    def fix_project_keys(self, root):
        root['code'] = root.pop('name')
        root['name'] = root.pop('project_name')

    def denormalize_participant(self, node):
        # Walk graph naturally for tree of node objects
        ptree = {node: self.create_tree(node, self.ptree_mapping, {})}
        # Use tree to create nested json
        participant = self.walk_tree(node, ptree, self.ptree_mapping, [])[0]
        # Walk from participant to all file leaves
        files = self.walk_paths(node, participant_traversal['file'])
        # Create participant summary
        participant['summary'] = self.get_participant_summary(node, files)
        # Take any out of place nodes and put then in correct place in tree
        self.reconstruct_biospecimen_paths(participant)
        # Get the metadatafiles that generated the participant
        participant['metadata_files'] = self.get_metadata_files(node)

        project = participant.get('project', None)
        if project:
            self.fix_project_keys(project)

        # Denormalize the participants files
        def get_file(f):
            return self.denormalize_file(f, self.copy_tree(ptree, {}))
        participant['files'] = map(get_file, files)

        annotations = [a for f in participant['files']
                       for a in f.get('annotations', [])]
        for a in annotations:
            a['project'] = project
        return participant, participant['files'], annotations

    def prune_participant(self, relevant_nodes, ptree, keys):
        """Start with whole participant tree and remove any nodes that did not
        contribute the the creation of this file.
        """
        for node in ptree.keys():
            if ptree[node]:
                self.prune_participant(relevant_nodes, ptree[node], keys)
            if node.label in keys and node not in relevant_nodes:
                ptree.pop(node)

    def add_file_neighbors(self, node, doc):
        auto_neighbors = [n for n in dict(file_tree).keys()
                          if n not in ['archive']]
        for neighbor in set(self.neighbors_labeled(node, auto_neighbors)):
            corr, label = file_tree[neighbor.label]['corr']
            if neighbor.label in self.flatten:
                base = neighbor[self.flatten[neighbor.label]]
            else:
                base = self._get_base_doc(neighbor)
            if corr == ONE_TO_ONE:
                assert label not in doc
                doc[label] = base
            else:
                if label not in doc:
                    doc[label] = []
                doc[label].append(base)

    def add_related_files(self, node, doc):
        # Get related_files
        related_files = list(self.neighbors_labeled(node, 'file'))
        if related_files:
            doc['related_files'] = map(self._get_base_doc, related_files)

    def add_archives(self, node, doc):
        archives, related_archives = [], []
        for archive in set(self.neighbors_labeled(node, 'archive')):
            if self.G[node][archive].get('label') == 'member_of':
                archives.append(self._get_base_doc(archive))
            else:
                related_archives.append(self._get_base_doc(archive))
        if archives:
            doc['archives'] = archives
        if related_archives:
            doc['related_archives'] = related_archives

    def add_data_type(self, node, doc):
        if 'data_subtype' in doc.keys():
            self._cache_data_types()
            for data_type, _files in self.data_types.iteritems():
                if node not in _files:
                    continue
                doc['data_type'] = data_type['name']

    def add_participants(self, node, ptree, doc):
        relevant = self.walk_paths(node, file_traversal.participant, True)
        self.prune_participant(relevant, ptree, [
            'sample', 'portion', 'analyte', 'aliquot', 'file'])
        doc['participants'] = map(
            lambda p: self.walk_tree(p, ptree, self.ptree_mapping, [])[0],
            ptree)
        for participant in doc['participants']:
            project = participant.get('project', None)
            if project:
                self.fix_project_keys(project)
        return relevant

    def add_annotations(self, node, relevant, doc):
        annotations = doc.pop('annotations', [])
        for parent in relevant:
            for p_annotation in self.neighbors_labeled(parent, 'annotation'):
                annotations.append(self.denormalize_annotation(p_annotation))
        if annotations:
            doc['annotations'] = annotations

    def add_acl(self, node, doc):
        if node.acl == ['open']:
            doc['access'] = 'open'
        else:
            doc['access'] = 'protected'

    def denormalize_file(self, node, ptree):
        """Given a participants tree and a file node, create the file json
        document.

        """
        doc = self._get_base_doc(node)
        self.add_file_neighbors(node, doc)
        self.add_related_files(node, doc)
        self.add_archives(node, doc)
        self.add_data_type(node, doc)
        relevant = self.add_participants(node, ptree, doc)
        self.add_annotations(node, relevant, doc)
        self.add_acl(node, doc)
        return doc

    def denormalize_project(self, p):
        """Summarize a project.

        """
        doc = self._get_base_doc(p)
        doc['code'] = doc.pop('name')
        doc['name'] = doc.pop('project_name')

        # Get programs
        program = self.neighbors_labeled(p, 'program').next()
        log.debug('Program: {}'.format(program))
        doc['program'] = self._get_base_doc(program)

        log.debug('Finding participants')
        parts = list(self.neighbors_labeled(p, 'participant'))
        log.debug('Got {} participants'.format(len(parts)))

        # Construct paths
        paths = [
            ['file'],
            ['sample', 'file'],
            ['sample', 'aliquot', 'file'],
            ['sample', 'portion', 'file'],
            ['sample', 'portion', 'analyte', 'file'],
            ['sample', 'portion', 'analyte', 'aliquot', 'file'],
        ]

        # Get files
        log.debug('Getting files')
        files = set()
        part_files = {}
        for part in parts:
            part_files[part] = self.walk_paths(part, paths)
            files = files.union(part_files[part])
        log.debug('Got {} files from {} participants'.format(
            len(files), len(part_files)))

        # Get data types
        exp_strat_summaries = []
        self._cache_experimental_strategies()
        for exp_strat in self.nodes_labeled('experimental_strategy'):
            log.debug('{} {}'.format(exp_strat, exp_strat['name']))
            exp_files = self.experimental_strategies[exp_strat]
            if not len(exp_files & files):
                continue
            participant_count = len(
                {p for p, p_files in part_files.iteritems()
                 if len(exp_files & p_files)})
            exp_strat_summaries.append({
                'participant_count': participant_count,
                'experimental_strategy': exp_strat['name'],
                'file_count': len(exp_files),
            })

        # Get data types
        data_type_summaries = []
        self._cache_data_types()
        for data_type in self.nodes_labeled('data_type'):
            log.debug('{} {}'.format(data_type, data_type['name']))
            dt_files = self.data_types[data_type]
            if not len(dt_files & files):
                continue
            participant_count = len(
                {p for p, p_files in part_files.iteritems()
                 if len(dt_files & p_files)})
            data_type_summaries.append({
                'participant_count': participant_count,
                'data_type': data_type['name'],
                'file_count': len(dt_files),
            })

        # Compile summary
        doc['summary'] = {
            'participant_count': len(parts),
            'file_count': len(files),
            'file_size': sum([f['file_size'] for f in files]),
        }
        if exp_strat_summaries:
            doc['summary']['experimental_strategies'] = exp_strat_summaries
        if data_type_summaries:
            doc['summary']['data_types'] = data_type_summaries
        return doc

    def denormalize_participants(self, participants=None):
        total_part_docs, total_file_docs, total_ann_docs = [], [], []
        if not participants:
            participants = list(self.nodes_labeled('participant'))
        pbar = self.pbar('Denormalizing participants ', len(participants))
        for n in participants:
            part_doc, file_docs, ann_docs = self.denormalize_participant(n)
            total_part_docs.append(part_doc)
            total_file_docs += file_docs
            total_ann_docs += ann_docs
            pbar.update(pbar.currval+1)
        pbar.finish()
        return total_part_docs, total_file_docs, total_ann_docs

    def denormalize_projects(self, projects=None):
        if not projects:
            projects = list(self.nodes_labeled('project'))
        project_docs = []
        pbar = self.pbar('Denormalizing projects ', len(projects))
        for project in projects:
            project_docs.append(self.denormalize_project(project))
            pbar.update(pbar.currval+1)
        pbar.finish()
        return project_docs

    def denormalize_annotation(self, node):
        ann_doc = self._get_base_doc(node)
        items = self.G.neighbors(node)
        assert len(items) == 1
        ann_doc['item_type'] = items[0].label
        ann_doc['item_id'] = items[0].node_id
        return ann_doc

    def denormalize_annotations(self, annotations=None):
        if not annotations:
            annotations = list(self.nodes_labeled('annotation'))[:100]
        annotation_docs = []
        pbar = self.pbar('Denormalizing annotations ', len(annotations))
        for annotation in annotations:
            annotation_docs.append(self.denormalize_annotation(annotation))
            pbar.update(pbar.currval+1)
        pbar.finish()
        return annotation_docs
