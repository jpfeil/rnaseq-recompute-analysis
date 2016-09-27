#!/usr/bin/env python2.7
"""
John Vivian
September, 2016
"""
import argparse
import errno
import itertools
import logging
import os
import shutil
import subprocess
import textwrap
from collections import defaultdict, Counter

import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class Analysis(object):
    """
    Represents one pairwise differential expression analysis.

    Pairwise differential expression describes the process of comparing one group of samples
    to a different sample one at a time and aggregating the results. GTEx represents normal
    human tissue, so that's our collective normal group. TCGA samples are from cancer biopsies
    and cluster together less tightly than GTEx, so we'll compare against them one at a time.
    """

    def __init__(self, tissue_df, cores, gene_map, gencode_path):
        """
        :param str tissue_df: Path to the combined tissue dataframe. See tissue_pairing.py for more information
        :param int cores: Number of cores to use when performing pairwise differential expression
        :param str gene_map: Path to the TSV that has ENSMBL gene mapping information
        :param str gencode_path: Path to the gencode annotation
        """
        self.df_path = tissue_df
        self.cores = cores
        self.gene_map = gene_map
        self.gencode_path = gencode_path
        self.tissue_dir = os.path.dirname(self.df_path)
        self.norm_counts_dir = os.path.join(self.tissue_dir, 'norm_count_tables')
        self.mds_dir = os.path.join(self.tissue_dir, 'plots_mds')
        self.ma_dir = os.path.join(self.tissue_dir, 'plots_ma')
        self.pairwise_dir = os.path.join(self.tissue_dir, 'pairwise_results')
        self.masked_dir = os.path.join(self.pairwise_dir, 'masked_results')
        self.matched_dir = os.path.join(self.pairwise_dir, 'matched_results')
        self.unmatched_dir = os.path.join(self.pairwise_dir, 'unmatched_results')
        self.results_dir = os.path.join(self.tissue_dir, 'results')
        self.edger_script = os.path.join(self.tissue_dir, 'edgeR-pairwise-DE.R')
        self.pc_path = os.path.join(self.tissue_dir, os.path.splitext(os.path.basename(self.df_path))[0] + '_pc.tsv')
        # Read in dataframe and store tcga_names
        self.df = None
        self.tcga_names = None
        # Variables used in aggregating results
        self.dfs = None
        self.num_samples = None
        # self.ranked = pd.DataFrame()
        self.genes = None
        self.pvals = defaultdict(list)
        self.fc = defaultdict(list)
        self.cpm = defaultdict(list)

    def run_pairwise_edger(self):
        """
        Run pairwise differential expression using the program edgeR

        The R script is generated then parallelized by the number of
        cores the analysis object was instantiated with
        """
        log.info('Reading in table')
        self.df = pd.read_csv(self.df_path, sep='\t', index_col=0)
        self.tcga_names = [name.replace('-', '.') for name in self.df.columns if 'TCGA' in name]
        for d in [self.mds_dir, self.ma_dir, self.pairwise_dir, self.norm_counts_dir]:
            mkdir_p(d)
        if not os.path.exists(self.pc_path):
            self._remove_nonprotein_coding_genes()
        # Write out edgeR script
        with open(self.edger_script, 'w') as f:
            f.write(self._generate_edger_script())
        # Run multiple edgeR processes
        log.info('Beginning pairwise differential expression: Using {} cores'.format(self.cores))
        with ThreadPoolExecutor(max_workers=self.cores) as executor:
            executor.map(self._run_edger, self.tcga_names)

    def _remove_nonprotein_coding_genes(self):
        """
        Removes non-protein coding genes which can skew normalization
        """
        log.info('Creating dataframe with non-protein coding genes removed.')
        pc_genes = set()
        with open(self.gencode_path, 'r') as f:
            for line in f.readlines():
                if not line.startswith('#'):
                    line = line.split()
                    if line[line.index('gene_type') + 1] == '"protein_coding";':
                        pc_genes.add(line[line.index('gene_id') + 1].split('"')[1])
        pc_genes = list(pc_genes)
        self.df = self.df.ix[pc_genes]
        self.df.to_csv(self.pc_path, sep='\t')
        return self.df

    def _run_edger(self, sample):
        """
        Method used in ThreadPoolExecutor to run Rscript

        :param str sample: TCGA sample
        """
        log.info('Running sample: ' + sample)
        p = subprocess.Popen(['Rscript', self.edger_script, sample])
        out, err = p.communicate()
        if not p.returncode == 0:
            raise RuntimeError('EdgeR run failed!\n\n\nstdout:\n{}stderr:\n{}\n\n\n'.format(out, err))
        return 'yay!'

    def read_results(self):
        """
        Read in the differential expression results, rank
        """
        for d in [self.results_dir, self.masked_dir, self.matched_dir, self.unmatched_dir]:
            mkdir_p(d)
        log.info('Compiling pairwise results')
        self.dfs = [x for x in os.listdir(self.pairwise_dir) if x.endswith('.tsv')]
        self.num_samples = len(self.dfs)
        # Find samples with matched normals
        barcodes = [x[:-7] for x in self.dfs]
        matched = {item for item, count in Counter(barcodes).items() if count == 2}
        matched = {x for x in matched if x + '.11.tsv' in self.dfs and x + '.01.tsv' in self.dfs}
        self._produce_masks(matched)
        matched = set([x+'.11.tsv' for x in matched] + [x + '.01.tsv' for x in matched])
        for unmatch in set(self.dfs) - matched:
            shutil.move(os.path.join(self.pairwise_dir, unmatch), os.path.join(self.unmatched_dir, unmatch))
        for match in matched:
            shutil.move(os.path.join(self.pairwise_dir, match), os.path.join(self.matched_dir, match))

        # Rank genes for matched and unmatched samples
        for directory in [self.masked_dir, self.unmatched_dir]:
            ranked = pd.DataFrame()
            ranked = self._rank_results(directory, ranked)
            ranked = self._add_mapped_genes(ranked)
            log.info('Saving ranked TSV file to: ' + self.results_dir)
            ranked.to_csv(os.path.join(self.results_dir, os.path.basename(directory) + '-ranked.tsv'), sep='\t')

    def _produce_masks(self, matched):
        """
        Samples with matched normals will be used to mask genes in the tumor sample
        :return:
        """
        log.info('Producing masks for matched samples')
        for match in tqdm(matched):
            log.debug('Match: ' + match)
            match_path = os.path.join(self.pairwise_dir, match)
            # try:
            df_norm = pd.read_csv(match_path + '.11.tsv', sep='\t', index_col=0)
            masked_genes = df_norm[df_norm.PValue < 0.001].index
            df_tumor = pd.read_csv(match_path + '.01.tsv', sep='\t', index_col=0)
            for gene in masked_genes:
                try:
                    df_tumor.drop(gene, inplace=True)
                except ValueError:  # DE gene in normal didn't show up in tumor
                    pass
            df_tumor.to_csv(os.path.join(self.masked_dir, match + '.01.tsv'), sep='\t')
            with open(os.path.join(self.masked_dir, match + '_masked_genes'), 'w') as f:
                f.write('\n'.join(masked_genes))
            # except IOError:  # "Norm" sample wasn't .11
            #     pass

    def _rank_results(self, directory, ranked):
        log.info('Reading in ranked tables from: ' + directory)
        for f in tqdm([x for x in os.listdir(directory) if x.endswith('.tsv')]):
            log.debug('Ranking: ' + f)
            df = pd.read_csv(os.path.join(directory, f), sep='\t', index_col=0)
            for gene in df.index:
                self.pvals[gene].append(df.loc[gene]['PValue'])
                self.fc[gene].append(df.loc[gene]['logFC'])
                self.cpm[gene].append(df.loc[gene]['logCPM'])

        log.info('Ranking results by pval < 0.001')
        self.genes = self.pvals.keys()
        ranked['pval'] = [np.median(self.pvals[x]) for x in self.genes]
        ranked['pval_counts'] = [sum([1 for y in self.pvals[x] if y < 0.001]) for x in self.genes]
        ranked['pval_std'] = [np.std(self.pvals[x]) for x in self.genes]
        ranked['fc'] = [np.median(self.fc[x]) for x in self.genes]
        ranked['fc_std'] = [np.std(self.fc[x]) for x in self.genes]
        ranked['cpm'] = [np.median(self.cpm[x]) for x in self.genes]
        ranked['cpm_std'] = [np.std(self.cpm[x]) for x in self.genes]
        ranked['num_samples'] = [len(self.pvals[x]) for x in self.genes]
        ranked['gene'] = self.genes
        ranked.index = self.genes
        ranked.sort_values('pval_counts', inplace=True, ascending=False)

        return ranked

    def _remove_unrepresented_genes(self, gene):
        log.debug('Removing underrepresented gene: ' + gene)
        if len(self.pvals[gene]) < int(self.num_samples * 0.90):
            self.pvals.pop(gene)
            self.fc.pop(gene)
            self.cpm.pop(gene)

    def _add_mapped_genes(self, df):
        log.info('Adding mapped genes')
        id_map = pd.read_table(self.gene_map, sep='\t')
        gene_mappings = {x: y for x, y in itertools.izip(id_map['geneId'], id_map['geneName'])}
        mapped_genes = []
        for gene in self.genes:
            try:
                new_gene = gene_mappings[gene]
            except KeyError:
                new_gene = gene
            mapped_genes.append(new_gene)
        df['gene_name'] = mapped_genes
        return df

    def _generate_edger_script(self):
        return textwrap.dedent("""
        plot_MDS <- function(name, f){{
            output_name = paste('{mds_dir}/', name, '.pdf', sep='')
            pdf( output_name , width = 7 , height = 7 )
            title = paste('MDS Plot for', name, '\\nNumber of Genes:', dim(f)[1])
            plotMDS( f , main = title, pch = c(rep(21, gtex_count), rep(25, tcga_count)), cex=0.75,
                    col=c(rep('blue', gtex_count), rep('red', tcga_count)))
            dev.off()
            return(f)
        }}

        # Belongs in a .R function for batching
        library(edgeR)
        args <- commandArgs(trailingOnly = TRUE)
        sample <- args[1]
        n <- read.table('{pc_path}', sep='\\t', header=1, row.names=1)
        gtex_count <- length(colnames(n[grepl('GTEX', colnames(n))]))
        tcga_count <- length(colnames(n)) - gtex_count
        cat('Sample: ', sample, '\\t')
        # Define GTEx and TCGA Dataframes
        gtex <- n[0:gtex_count]
        tcga <- n[(gtex_count + 1) : (gtex_count + tcga_count)]

        # Filtering protocol: at least 1cpm in 90% of gtex OR >= 1 in the single tcga
        # Smaller CPM thresholds are usually appropriate for larger libraries. As a general
        # rule, a good threshold can be chosen by identifying the CPM that corresponds to a
        # count of 10
        # cutoff = cpm(10, mean(y$samples$lib.size))
        # Construct DGE object, normalize, and produce MDS plot
        group <- c(rep('gtex', gtex_count), rep('tcga', 1 ))
        gtex_filter <- rowMeans(cpm(gtex)>1) >= gtex_count * .90
        tcga_filter <- rowMeans(cpm(tcga[sample])>1) >= 1
        filter <- gtex_filter + tcga_filter
        filter <- data.frame(filter)
        filter$filter <- as.logical(filter$filter)
        # Add sample from TCGA to GTEx DF
        gtex[sample] <- tcga[sample]
        # Apply filter
        gtex <- gtex[filter$filter,]
        cat('Genes after filtering: ', dim(gtex)[1], '\\n')
        y <- DGEList(counts=gtex, group=group)

        # Calculate normalization factors
        y <- calcNormFactors(y)
        f <- plot_MDS(sample, y)

        # Establish design and estimate dispersion
        # Currently the design model is a binary group: GTEx / TCGA
        design <- model.matrix(~group)
        f <- estimateDisp( f, design )

        # Generate normalized counts dataframe
        nc <- cpm(f, normalized.lib.sizes=FALSE)
        output_name <- paste('{tissue_dir}/norm_count_tables/', sample, '.tsv', sep='')
        write.table(nc, output_name, quote=FALSE, sep='\\t', col.names=NA)

        # Fit the Quasi-Likelihood GLM
        fit <- glmQLFit(f, design)
        qlf <- glmQLFTest(fit, coef=2)

        # Create QLF table
        qlf_sort <- qlf$table[order(qlf$table$logFC),]

        # Find DE Genes
        de_qlf <- rownames(qlf_sort[abs(qlf_sort$logFC) > 2,])
        summary(de <- decideTestsDGE(qlf))
        detags <- rownames(f)[as.logical(de)]

        # Generate MA plot
        title = paste('{ma_dir}/', sample, '.pdf', sep='')
        pdf(title, width=7, height=7)
        plotSmear(qlf, de.tags = detags)
        abline(h=c(-2, 2), col="blue")
        dev.off()

        # Write out table
        output_name <- paste('{pairwise_dir}/', sample, '.tsv', sep='')
        write.table(qlf_sort, output_name, quote=FALSE, sep='\t', col.names=NA)
        """.format(tissue_dir=self.tissue_dir, mds_dir=self.mds_dir, ma_dir=self.ma_dir,
                   pairwise_dir=self.pairwise_dir, pc_path=self.pc_path))


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def main():
    """
    RNA-seq Pairwise Differential Expression Methodology:

    1. Read in combined dataframe for a particular tissue
    2. Remove all genes except protein-coding genes. We'll use the gencode annotation file,
        which was used in the recompute, to pull out the set of genes that are protein coding.
        HG38 includes many non-protein coding genes which may affect how normalization is performed.
    3. Run RScript to perform differential expression
        I. Create dataframe of GTEx and single TCGA sample
        II.
    """
    parser = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tissue-df', type=str, required=True,
                        help='Tissue dataframe: genes/isoforms by sample names. GTEx columns should come first followed'
                             'by TCGA columns.')
    parser.add_argument('--gene-map', type=str, default='/mnt/metadata/attrs.tsv',
                        help='File containing map information. Must have at least 2 column names: '
                             'geneID and geneName.')
    parser.add_argument('--gencode', type=str, default='/mnt/gencode.v23.annotation.gtf',
                        help='Gencode annotation file. Version 23 was used in the rnaseq recompute.')
    parser.add_argument('--cores', default=8, type=int, help='Number of cores to use.')
    params = parser.parse_args()

    a = Analysis(tissue_df=params.tissue_df, cores=params.cores, gene_map=params.gene_map, gencode_path=params.gencode)
    a.run_pairwise_edger()
    a.read_results()
