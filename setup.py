from setuptools import setup, find_packages

setup(
    name="zug",
    version="0.1",
    packages=find_packages(),
    package_data={
        "zug": [
            "datamodel/tcga_classification.yaml",
            "datamodel/centerCode.csv",
            "datamodel/tissueSourceSite.csv",
            "datamodel/bcr.yaml",
            "datamodel/cghub.yaml",
            "datamodel/clinical.yaml",
            "datamodel/projects.csv",
            "datamodel/cghub_file_categorization.yaml",
            "datamodel/target/barcodes.tsv",
        ]
    },
    install_requires=[
        'progressbar==2.2',
        'networkx',
        'pyyaml',
        'psqlgraph',
        'gdcdatamodel',
        'cdisutils',
        'signpostclient',
        'lockfile',
        'lxml==3.4.1',
        'requests==2.5.2',
        'apache-libcloud==0.15.1',
        'cssselect==0.9.1',
        'elasticsearch==1.4.0',
        'pandas==0.15.2',
        'xlrd==0.9.3',
        'consulate==0.4',
        'boto==2.36.0',
        'filechunkio==1.6',
        'docker-py==1.2.2',
    ],
    dependency_links=[
        'git+ssh://git@github.com/NCI-GDC/psqlgraph.git@d7e7aa1aaf37abe5e02a9f106e3f4a64f5781522#egg=psqlgraph',
        'git+ssh://git@github.com/NCI-GDC/cdisutils.git@e7feedc81ae638fcf6e4e3be1cc4eb08057b352b#egg=cdisutils',
        'git+ssh://git@github.com/NCI-GDC/gdcdatamodel.git@f839a66934d3c71d43624349a3541a410ef596f8#egg=gdcdatamodel',
        'git+ssh://git@github.com/NCI-GDC/python-signpostclient.git@4a6db7c192f65f838fad8a7efd43484b9380728f#egg=signpostclient',
    ]
)
