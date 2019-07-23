"""
Image-based meta-analysis estimators
"""
from __future__ import division

import logging
from os import mkdir
import os.path as op
from shutil import rmtree

import numpy as np
import nibabel as nib
from scipy import stats
from nipype.interfaces import fsl
from nilearn.masking import unmask, apply_mask

from .esma import fishers, stouffers, weighted_stouffers, rfx_glm
from ..base import MetaResult, IBMAEstimator
from ..stats import p_to_z

LGR = logging.getLogger(__name__)


class Fishers(IBMAEstimator):
    """
    An image-based meta-analytic test using t- or z-statistic images.
    Sum of -log P-values (from T/Zs converted to Ps)

    Requirements:
        - t OR z
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None
        self.results = None

    def fit(self, ids, corr='FWE', two_sided=True):
        self.ids = ids
        z_maps = self.dataset.get_images(self.ids, imtype='z')
        images = fishers(z_maps, corr=corr, two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=images)


class Stouffers(IBMAEstimator):
    """
    A t-test on z-statistic images.

    Parameters
    ----------
    dataset : :obj:`nimare.dataset.Dataset`
        Dataset to analyze.
    inference : {'rfx', 'ffx'}
        Whether to run a random- or fixed-effects model.
    null : {'theoretical', 'empirical'}
        Whether to compare test statistics to theoretical or empirical null
        distribution. Empirical null distribution is only possible when
        inference is set to 'rfx'.

    Requirements:
        - z
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None
        self.inference = None
        self.null = None
        self.n_iters = None
        self.results = None

    def fit(self, ids, inference='ffx', null='theoretical', n_iters=None,
            corr='FWE', two_sided=True):
        self.ids = ids
        self.inference = inference
        self.null = null
        self.n_iters = n_iters
        z_maps = self.dataset.get_images(self.ids, imtype='z')
        images = stouffers(z_maps, inference=inference, null=null,
                           n_iters=n_iters, corr=corr, two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=images)


class WeightedStouffers(IBMAEstimator):
    """
    An image-based meta-analytic test using z-statistic images and
    sample sizes.
    Zs from bigger studies get bigger weight

    Requirements:
        - z
        - n
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None
        self.results = None

    def fit(self, ids, two_sided=True):
        self.ids = ids
        z_maps = self.dataset.get_images(self.ids, imtype='z')
        z_maps = apply_mask(z_maps, self.mask)
        sample_sizes = self.dataset.get_metadata(self.ids, 'n')
        results = weighted_stouffers(z_maps, sample_sizes, two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=results)


class RFX_GLM(IBMAEstimator):
    """
    A t-test on contrast images.

    Requirements:
        - con
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None
        self.null = None
        self.n_iters = None
        self.results = None

    def fit(self, ids, null='theoretical', n_iters=None, corr='FWE',
            two_sided=True):
        self.ids = ids
        self.null = null
        self.n_iters = n_iters
        con_maps = self.dataset.get_images(self.ids, imtype='con')
        images = rfx_glm(con_maps, null=self.null,
                         n_iters=self.n_iters, corr=corr, two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=images)


def fsl_glm(con_maps, se_maps, sample_sizes, mask, inference, cdt=0.01, q=0.05,
            work_dir='fsl_glm', two_sided=True):
    assert con_maps.shape == se_maps.shape
    assert con_maps.shape[0] == sample_sizes.shape[0]

    if inference == 'mfx':
        run_mode = 'flame1'
    elif inference == 'ffx':
        run_mode = 'fe'
    else:
        raise ValueError('Input "inference" must be "mfx" or "ffx".')

    if 0 < cdt < 1:
        cdt_z = p_to_z(cdt, tail='two')
    else:
        cdt_z = cdt

    work_dir = op.abspath(work_dir)
    if op.isdir(work_dir):
        raise ValueError('Working directory already '
                         'exists: "{0}"'.format(work_dir))

    mkdir(work_dir)
    cope_file = op.join(work_dir, 'cope.nii.gz')
    varcope_file = op.join(work_dir, 'varcope.nii.gz')
    mask_file = op.join(work_dir, 'mask.nii.gz')
    design_file = op.join(work_dir, 'design.mat')
    tcon_file = op.join(work_dir, 'design.con')
    cov_split_file = op.join(work_dir, 'cov_split.mat')
    dof_file = op.join(work_dir, 'dof.nii.gz')

    dofs = (np.array(sample_sizes) - 1).astype(str)

    con_maps[np.isnan(con_maps)] = 0
    cope_4d_img = unmask(con_maps, mask)
    se_maps[np.isnan(se_maps)] = 0
    se_maps = se_maps ** 2  # square SE to get var
    varcope_4d_img = unmask(se_maps, mask)
    dof_maps = np.ones(con_maps.shape)
    for i in range(len(dofs)):
        dof_maps[i, :] = dofs[i]
    dof_4d_img = unmask(dof_maps, mask)

    # Covariance splitting file
    cov_data = ['/NumWaves\t1',
                '/NumPoints\t{0}'.format(con_maps.shape[0]),
                '',
                '/Matrix']
    cov_data += ['1'] * con_maps.shape[0]
    with open(cov_split_file, 'w') as fo:
        fo.write('\n'.join(cov_data))

    # T contrast file
    tcon_data = ['/ContrastName1 MFX-GLM',
                 '/NumWaves\t1',
                 '/NumPoints\t1',
                 '',
                 '/Matrix',
                 '1']
    with open(tcon_file, 'w') as fo:
        fo.write('\n'.join(tcon_data))

    cope_4d_img.to_filename(cope_file)
    varcope_4d_img.to_filename(varcope_file)
    dof_4d_img.to_filename(dof_file)
    mask.to_filename(mask_file)

    design_matrix = ['/NumWaves\t1',
                     '/NumPoints\t{0}'.format(con_maps.shape[0]),
                     '/PPheights\t1',
                     '',
                     '/Matrix']
    design_matrix += ['1'] * con_maps.shape[0]
    with open(design_file, 'w') as fo:
        fo.write('\n'.join(design_matrix))

    flameo = fsl.FLAMEO()
    flameo.inputs.cope_file = cope_file
    flameo.inputs.var_cope_file = varcope_file
    flameo.inputs.cov_split_file = cov_split_file
    flameo.inputs.design_file = design_file
    flameo.inputs.t_con_file = tcon_file
    flameo.inputs.mask_file = mask_file
    flameo.inputs.run_mode = run_mode
    flameo.inputs.dof_var_cope_file = dof_file
    res = flameo.run()

    temp_img = nib.load(res.outputs.zstats)
    temp_img = nib.Nifti1Image(temp_img.get_data() * -1, temp_img.affine)
    temp_img.to_filename(op.join(work_dir, 'temp_zstat2.nii.gz'))

    temp_img2 = nib.load(res.outputs.copes)
    temp_img2 = nib.Nifti1Image(temp_img2.get_data() * -1, temp_img2.affine)
    temp_img2.to_filename(op.join(work_dir, 'temp_copes2.nii.gz'))

    # FWE correction
    # Estimate smoothness
    est = fsl.model.SmoothEstimate()
    est.inputs.dof = con_maps.shape[0] - 1
    est.inputs.mask_file = mask_file
    est.inputs.residual_fit_file = res.outputs.res4d
    est_res = est.run()

    # Positive clusters
    cl = fsl.model.Cluster()
    cl.inputs.threshold = cdt_z
    cl.inputs.pthreshold = q
    cl.inputs.in_file = res.outputs.zstats
    cl.inputs.cope_file = res.outputs.copes
    cl.inputs.use_mm = True
    cl.inputs.find_min = False
    cl.inputs.dlh = est_res.outputs.dlh
    cl.inputs.volume = est_res.outputs.volume
    cl.inputs.out_threshold_file = op.join(work_dir, 'thresh_zstat1.nii.gz')
    cl.inputs.connectivity = 26
    cl.inputs.out_localmax_txt_file = op.join(work_dir, 'lmax_zstat1_tal.txt')
    cl_res = cl.run()

    out_cope_img = nib.load(res.outputs.copes)
    out_t_img = nib.load(res.outputs.tstats)
    out_z_img = nib.load(res.outputs.zstats)
    out_cope_map = apply_mask(out_cope_img, mask)
    out_t_map = apply_mask(out_t_img, mask)
    out_z_map = apply_mask(out_z_img, mask)
    pos_z_map = apply_mask(nib.load(cl_res.outputs.threshold_file), mask)

    if two_sided:
        # Negative clusters
        cl2 = fsl.model.Cluster()
        cl2.inputs.threshold = cdt_z
        cl2.inputs.pthreshold = q
        cl2.inputs.in_file = op.join(work_dir, 'temp_zstat2.nii.gz')
        cl2.inputs.cope_file = op.join(work_dir, 'temp_copes2.nii.gz')
        cl2.inputs.use_mm = True
        cl2.inputs.find_min = False
        cl2.inputs.dlh = est_res.outputs.dlh
        cl2.inputs.volume = est_res.outputs.volume
        cl2.inputs.out_threshold_file = op.join(work_dir,
                                                'thresh_zstat2.nii.gz')
        cl2.inputs.connectivity = 26
        cl2.inputs.out_localmax_txt_file = op.join(work_dir,
                                                   'lmax_zstat2_tal.txt')
        cl2_res = cl2.run()

        neg_z_map = apply_mask(nib.load(cl2_res.outputs.threshold_file), mask)
        thresh_z_map = pos_z_map - neg_z_map
    else:
        thresh_z_map = pos_z_map

    LGR.info('Cleaning up...')
    rmtree(work_dir)
    rmtree(res.outputs.stats_dir)

    # Compile outputs
    out_p_map = stats.norm.sf(abs(out_z_map)) * 2
    log_p_map = -np.log10(out_p_map)
    images = {'cope': out_cope_map,
              'z': out_z_map,
              'thresh_z': thresh_z_map,
              't': out_t_map,
              'p': out_p_map,
              'log_p': log_p_map}
    return images


def ffx_glm(con_maps, se_maps, sample_sizes, mask, cdt=0.01, q=0.05,
            work_dir='mfx_glm', two_sided=True):
    """
    Run a fixed-effects GLM on contrast and standard error images.

    Parameters
    ----------
    con_maps : (n_contrasts, n_voxels) :obj:`numpy.ndarray`
        A 2D array of contrast maps in the same space, after masking.
    var_maps : (n_contrasts, n_voxels) :obj:`numpy.ndarray`
        A 2D array of contrast standard error maps in the same space, after
        masking. Must match shape and order of ``con_maps``.
    sample_sizes : (n_contrasts,) :obj:`numpy.ndarray`
        A 1D array of sample sizes associated with contrasts in ``con_maps``
        and ``var_maps``. Must be in same order as rows in ``con_maps`` and
        ``var_maps``.
    mask : :obj:`nibabel.Nifti1Image`
        Mask image, used to unmask results maps in compiling output.
    cdt : :obj:`float`, optional
        Cluster-defining p-value threshold.
    q : :obj:`float`, optional
        Alpha for multiple comparisons correction.
    work_dir : :obj:`str`, optional
        Working directory for FSL flameo outputs.
    two_sided : :obj:`bool`, optional
        Whether analysis should be two-sided (True) or one-sided (False).

    Returns
    -------
    result : :obj:`dict`
        Dictionary containing maps for test statistics, p-values, and
        negative log(p) values.
    """
    result = fsl_glm(con_maps, se_maps, sample_sizes, mask, inference='ffx',
                     cdt=0.01, q=0.05, work_dir='mfx_glm', two_sided=True)
    return result


class FFX_GLM(IBMAEstimator):
    """
    An image-based meta-analytic test using contrast and standard error images.
    Don't estimate variance, just take from first level

    Requirements:
        - con
        - se
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None
        self.sample_sizes = None
        self.equal_var = None

    def fit(self, ids, sample_sizes=None, equal_var=True, corr='FWE',
            two_sided=True):
        """
        Perform meta-analysis given parameters.
        """
        self.ids = ids
        self.sample_sizes = sample_sizes
        self.equal_var = equal_var
        con_maps = self.dataset.get_images(self.ids, imtype='con')
        var_maps = self.dataset.get_images(self.ids, imtype='con_se')
        if self.sample_sizes is not None:
            sample_sizes = np.repeat(self.sample_sizes, len(ids))
        else:
            sample_sizes = self.dataset.get_metadata(self.ids, 'n')
        images = ffx_glm(con_maps, var_maps, sample_sizes, self.mask,
                         equal_var=self.equal_var, corr=corr,
                         two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=images)


def mfx_glm(con_maps, se_maps, sample_sizes, mask, cdt=0.01, q=0.05,
            work_dir='mfx_glm', two_sided=True):
    """
    Run a mixed-effects GLM on contrast and standard error images.

    Parameters
    ----------
    con_maps : (n_contrasts, n_voxels) :obj:`numpy.ndarray`
        A 2D array of contrast maps in the same space, after masking.
    var_maps : (n_contrasts, n_voxels) :obj:`numpy.ndarray`
        A 2D array of contrast standard error maps in the same space, after
        masking. Must match shape and order of ``con_maps``.
    sample_sizes : (n_contrasts,) :obj:`numpy.ndarray`
        A 1D array of sample sizes associated with contrasts in ``con_maps``
        and ``var_maps``. Must be in same order as rows in ``con_maps`` and
        ``var_maps``.
    mask : :obj:`nibabel.Nifti1Image`
        Mask image, used to unmask results maps in compiling output.
    cdt : :obj:`float`, optional
        Cluster-defining p-value threshold.
    q : :obj:`float`, optional
        Alpha for multiple comparisons correction.
    work_dir : :obj:`str`, optional
        Working directory for FSL flameo outputs.
    two_sided : :obj:`bool`, optional
        Whether analysis should be two-sided (True) or one-sided (False).

    Returns
    -------
    result : :obj:`dict`
        Dictionary containing maps for test statistics, p-values, and
        negative log(p) values.
    """
    result = fsl_glm(con_maps, se_maps, sample_sizes, mask, inference='mfx',
                     cdt=0.01, q=0.05, work_dir='mfx_glm', two_sided=True)
    return result


class MFX_GLM(IBMAEstimator):
    """
    The gold standard image-based meta-analytic test. Uses contrast and
    standard error images.

    Requirements:
        - con
        - se
    """
    def __init__(self, dataset):
        self.dataset = dataset
        self.mask = self.dataset.mask
        self.ids = None

    def fit(self, ids, sample_sizes=None, equal_var=True, corr='FWE',
            two_sided=True):
        """
        Perform meta-analysis given parameters.
        """
        self.ids = ids
        self.sample_sizes = sample_sizes
        self.equal_var = equal_var
        con_maps = self.dataset.get_images(self.ids, imtype='con')
        var_maps = self.dataset.get_images(self.ids, imtype='con_se')
        if self.sample_sizes is not None:
            sample_sizes = np.repeat(self.sample_sizes, len(ids))
        else:
            sample_sizes = self.dataset.get_metadata(self.ids, 'n')
        images = ffx_glm(con_maps, var_maps, sample_sizes, self.mask,
                         equal_var=self.equal_var, corr=corr,
                         two_sided=two_sided)
        self.results = MetaResult(self, self.mask, maps=images)