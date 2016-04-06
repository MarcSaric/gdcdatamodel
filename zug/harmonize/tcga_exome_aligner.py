from queries import exome
from sqlalchemy import BigInteger
from gdcdatamodel.models import File
from gdcdatamodel.models import FileDataFromFile


from zug.harmonize.bwa_aligner import BWAAligner


class TCGAExomeAligner(BWAAligner):

    @property
    def name(self):
        return "tcga_exome_aligner"

    @property
    def source(self):
        return "tcga_exome_alignment"

    def choose_bam_by_forced_id(self):
        input_bam = self.graph.nodes(File).ids(self.config["force_input_id"]).one()
        assert input_bam.sysan["source"] == "tcga_cghub"
        assert input_bam.data_formats[0].name == "BAM"
        assert input_bam.experimental_strategies[0].name == "WXS"
        return input_bam

    @property
    def bam_files(self):
        '''targeted bam files query'''
        return exome(self.graph, 'tcga_cghub')

    @property
    def new_alignable_files(self):
        '''bam files that are not aligned'''
        currently_being_aligned = self.consul.list_locked_keys()
        alignable = self.bam_files\
                        .props(state="live")\
                        .not_sysan(alignment_data_problem=True)\
                        .filter(~File.derived_files.any())\
                        .filter(~File.node_id.in_(currently_being_aligned))
        if self.config["size_limit"]:
            alignable = alignable.filter(
                File.file_size.cast(BigInteger) < self.config["size_limit"]
            )
        if self.config["size_min"]:
            alignable = alignable.filter(
                File.file_size.cast(BigInteger) > self.config["size_min"]
            )
        
        return alignable

    @staticmethod
    def with_qc_failures(q):
        '''
        Apply a filter for selecting only those nodes that have qc failures.
        '''
        return q.sysan(qc_failed=True)

    @staticmethod
    def without_realignment(q):
        '''
        Apply a filter for selecting only non-realigned nodes.
        '''
        return q.not_sysan(qc_realigned=True)

    @property
    def realignable_files(self):
        '''
        Returns a query for BAM files that are subject to realignment.
        '''
        currently_being_aligned = self.consul.list_locked_keys()
        
        alignable = self.bam_files\
                        .props(state="live")\
                        .filter(~File.source_files.any())\
                        .filter(~File.node_id.in_(currently_being_aligned))
        
        if self.config["size_limit"]:
            alignable = alignable.filter(
                File.file_size.cast(BigInteger) < self.config["size_limit"]
            )
        
        if self.config["size_min"]:
            alignable = alignable.filter(
                File.file_size.cast(BigInteger) > self.config["size_min"]
            )
        
        alignable = self.with_qc_failures(alignable)
        alignable = self.without_realignment(alignable)
        
        return alignable

    @property
    def alignable_files(self):
        return self.new_alignable_files.union(self.realignable_files)
