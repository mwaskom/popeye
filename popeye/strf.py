#!/usr/bin/python

""" Classes and functions for fitting population encoding models """

from __future__ import division
import time
import warnings
warnings.simplefilter("ignore")

import numpy as np
from scipy.optimize import brute, fmin_powell
from scipy.special import gamma
from scipy.stats import linregress
import scipy.signal as ss

from dipy.core.onetime import auto_attr

import popeye.utilities as utils
from popeye.base import PopulationModel, PopulationFit
from popeye.spinach import MakeFastGaussian2D, MakeFastAudioPrediction

np.set_printoptions(suppress=True)

def recast_estimation_results(output, grid_parent, write=True):
    """
    Recasts the output of the pRF estimation into two nifti_gz volumes.
    
    Takes `output`, a list of multiprocessing.Queue objects containing the
    output of the pRF estimation for each voxel.  The pRF estimates are
    expressed in both polar and Cartesian coordinates.  If the default value
    for the `write` parameter is set to False, then the function returns the
    arrays without writing the nifti files to disk.  Otherwise, if `write` is
    True, then the two nifti files are written to disk.
    
    Each voxel contains the following metrics: 
    
        0 x / polar angle
        1 y / eccentricity
        2 sigma
        3 HRF delay
        4 RSS error of the model fit
        5 correlation of the model fit
        
    Parameters
    ----------
    output : list
        A list of PopulationFit objects.
    grid_parent : nibabel object
        A nibabel object to use as the geometric basis for the statmap.  
        The grid_parent (x,y,z) dim and pixdim will be used.
        
    Returns
    ------ 
    cartes_filename : string
        The absolute path of the recasted pRF estimation output in Cartesian
        coordinates. 
    plar_filename : string
        The absolute path of the recasted pRF estimation output in polar
        coordinates. 
        
    """
    
    
    # load the gridParent
    dims = list(grid_parent.shape)
    dims = dims[0:3]
    dims.append(5)
    
    # initialize the statmaps
    nii_out = np.zeros(dims)
    
    # extract the pRF model estimates from the results queue output
    for fit in output:
        
        if fit.__dict__.has_key('rss'):
        
            nii_out[fit.voxel_index] = (fit.freq_center, 
                                        fit.freq_sigma,
                                        fit.hrf_delay,
                                        fit.rss,
                                        fit.fit_stats[2])
                                 
    # get header information from the gridParent and update for the pRF volume
    aff = grid_parent.get_affine()
    hdr = grid_parent.get_header()
    hdr.set_data_shape(dims)
    
    # recast as nifti
    nif = nibabel.Nifti1Image(nii_out,aff,header=hdr)
    nif.set_data_dtype('float32')
    
    return nif

# this is actually not used, but it serves as the model for the cython function ...
def gaussian_2D(X, Y, x0, y0, sigma_x, sigma_y, degrees, amplitude=1):
    
    theta = degrees*np.pi/180
        
    a = np.cos(theta)**2/2/sigma_x**2 + np.sin(theta)**2/2/sigma_y**2
    b = -np.sin(2*theta)/4/sigma_x**2 + np.sin(2*theta)/4/sigma_y**2
    c = np.sin(theta)**2/2/sigma_x**2 + np.cos(theta)**2/2/sigma_y**2
    
    Z = amplitude*np.exp( - (a*(X-x0)**2 + 2*b*(X-x0)*(Y-y0) + c*(Y-y0)**2))
    
    return Z


def compute_model_ts(freq_center, freq_sigma, hrf_delay, 
                     time_coord, freq_coord, spectrogram,
                     tr_length, num_timepoints,
                     norm_func=utils.zscore):
    
    # compute the STRF
    gaussian = MakeFastGaussian2D(time_coord, freq_coord, time_coord.max()*0.5, freq_center, time_coord.max()*0.5, freq_sigma, 0)
    
    # compute the stim
    stim = MakeFastAudioPrediction(spectrogram, gaussian, time_coord, freq_coord, freq_center, freq_sigma, hrf_delay, num_timepoints)
    stim_norm = norm_func(stim)
    
    # convolve it with the HRF
    hrf = utils.double_gamma_hrf(hrf_delay, tr_length)
    model = ss.fftconvolve(stim_norm, hrf, 'same')
    model_norm = norm_func(model)
    
    return model_norm

def compute_model_ts_slow(freq_center, freq_sigma, hrf_delay, 
                          time_coord, freq_coord, spectrogram,
                          tr_length, num_timepoints,
                          norm_func=utils.zscore):
    
    
    time_center = freq_coord.max()*0.5
    time_sigma = freq_coord.max()*0.5
                     
    # create the STRF
    g = MakeFastGaussian2D(time_coord, freq_coord, time_center, freq_center, time_sigma, freq_sigma, 0)
    
    s_factor_3 = (3.0*freq_sigma)**2
    
    # initialize the inter-TR stimulus model
    stim = np.zeros(num_timepoints)
    
    
    # loop over each TR
    for tr in np.arange(0, spectrogram.shape[-1],spectrogram.shape[-1]/num_timepoints):
        
        tr_num = tr/(spectrogram.shape[1]/num_timepoints)
        
        # initialize the intra-TR stimulus model
        tr_model = np.zeros(spectrogram.shape[0])        
        
        from_slice = tr
        to_slice = tr + spectrogram.shape[1]/num_timepoints
        
        # grab the sound frame an normalize it to 1
        sound_frame = spectrogram[:,from_slice:to_slice]
        
        conv_sum = 0.0
        
        # loop over each frequency and convolve
        for f in range(spectrogram.shape[0]):
            
            f_vector = sound_frame[f,:]
            g_vector = g[f,:]
            
            # something is wrong here?????
            for conv_i in np.arange(1,len(f_vector)):
                d = (time_coord[f,conv_i]-time_coord[f,conv_i])**2 + (freq_coord[f,conv_i]-freq_center)**2
                if d <= s_factor_3:
                    for conv_j in np.arange(1,len(f_vector)):
                        conv_sum += f_vector[conv_i] * g_vector[conv_i-conv_j+1]
            
        stim[tr_num] = np.mean(conv_sum)
        
    stim_norm = norm_func(stim)
    
    # convolve it with the HRF
    hrf = double_gamma_hrf(hrf_delay, tr_length)
    model = norm_func(ss.fftconvolve(stim_norm, hrf, 'same'))
    model_norm = norm_func(model)
    
    return model

def error_function(parameters, response, 
                   time_coord, freq_coord, spectrogram,
                   tr_length, num_timepoints):
    
    # unpack the tuple
    freq_center, freq_sigma, hrf_delay = parameters[:]
    
    # if the frequency is out of range, abort with an inf
    if freq_center > np.floor(np.max(freq_coord))-1:
        return np.inf
    if freq_center <= 0:
        return np.inf
    if freq_sigma > np.floor(np.max(freq_coord))-1:
        return np.inf
        
    # if the sigma is <= 0, abort with an inf
    if freq_sigma <= 0:
        return np.inf
        
    # if the HRF delay parameter is greater than 5 seconds, abort with an inf
    if np.abs(hrf_delay) > 5:
        return np.inf
            
    # otherwise generate a prediction
    model_ts = compute_model_ts(freq_center, freq_sigma, hrf_delay,
                                time_coord, freq_coord, spectrogram,
                                tr_length, num_timepoints, norm_func=utils.zscore)
    
    # compute the RSS
    error = np.sum((model_ts-response)**2)
    
    # print("%.03f,%.03f,%.03f,%.03f" %(freq_center,freq_sigma,hrf_delay,error))
    
    # catch NaN
    if np.isnan(np.sum(error)):
        return np.inf
        
    return error

def brute_force_search(bounds, response,
                       time_coord, freq_coord, spectrogram, 
                       tr_length, num_timepoints):
    
    [freq_center, freq_sigma, hrf_delay], err,  _, _ =\
        brute(error_function,
              args=(response, time_coord, freq_coord, spectrogram, 
                    tr_length, num_timepoints),
              ranges=bounds,
              Ns=5,
              finish=None,
              full_output=True,
              disp=None)
    
    # return the estimates
    return freq_center, freq_sigma, hrf_delay


def gradient_descent_search(freq_center_0, freq_sigma_0, hrf_delay_0,
                            error_function, response,
                            time_coord, freq_coord, spectrogram,
                            tr_length, num_timepoints):
                            
                            
    [freq_center, freq_sigma, hrf_delay], err,  _, _, _, warnflag =\
        fmin_powell(error_function,(freq_center_0, freq_sigma_0, hrf_delay_0),
                    args=(response, time_coord, freq_coord, spectrogram,
                          tr_length, num_timepoints),
                    full_output=True,
                    disp=False)
                    
    return freq_center, freq_sigma, hrf_delay


# this method is used to simply multiprocessing.Pool interactions
def parallel_fit(args):
    
    # unpackage the arguments
    response = args[0]
    model = args[1]
    bounds = args[2]
    tr_length = args[3]
    voxel_index = args[4]
    uncorrected_rval = args[5]
    verbose = args[6]
    
    # fit the data
    fit = SpectrotemporalFit(response,
                             model,
                             bounds,
                             tr_length,
                             voxel_index,
                             uncorrected_rval,
                             verbose)
    return fit


class SpectrotemporalModel(PopulationModel):
    
    """
    Gaussian population receptive field model.
    """
    
    def __init__(self, stim_arr):
        
        # this is a weird notation
        PopulationModel.__init__(self, stim_arr)


class SpectrotemporalFit(PopulationFit):
    
    def __init__(self, data, model, bounds, tr_length, voxel_index, uncorrected_rval, verbose=True):
        
        self.data = utils.zscore(data)
        self.model = model
        self.bounds = bounds
        self.tr_length = tr_length
        self.voxel_index = voxel_index
        self.uncorrected_rval = uncorrected_rval
        self.verbose = verbose
        
        # just make sure that all data is inside the mask
        tic = time.clock()
        self.fit_stats;
        toc = time.clock()
        
        # print to screen if verbose
        if self.verbose:
            print("VOXEL=(%.03d,%.03d,%.03d)  TIME=%.03d  ERROR=%.03d  RVAL=%.02f" 
                  %(self.voxel_index[0],
                    self.voxel_index[1],
                    self.voxel_index[2],
                    toc-tic,
                    self.rss,
                    self.fit_stats[2]))

    @auto_attr
    def ballpark_estimate(self):
        return brute_force_search(self.bounds, self.data,
                                  self.model.stimulus.time_coord,
                                  self.model.stimulus.freq_coord,
                                  self.model.stimulus.spectrogram,
                                  self.model.stimulus.tr_length,
                                  self.model.stimulus.num_timepoints)
    @auto_attr
    def strf_estimate(self):
        return gradient_descent_search(self.f0, self.fs0, self.hrf0,
                                       error_function, self.data, 
                                       self.model.stimulus.time_coord,
                                       self.model.stimulus.freq_coord,
                                       self.model.stimulus.spectrogram,
                                       self.model.stimulus.tr_length,
                                       self.model.stimulus.num_timepoints)
    @auto_attr
    def f0(self):
        return self.ballpark_estimate[0]
    
    @auto_attr
    def fs0(self):
        return self.ballpark_estimate[1]
    
    @auto_attr
    def hrf0(self):
        return self.ballpark_estimate[2]
    
    @auto_attr
    def freq_center(self):
        return self.strf_estimate[0]
        
    @auto_attr
    def freq_sigma(self):
        return self.strf_estimate[1]
        
    @auto_attr
    def hrf_delay(self):
        return self.strf_estimate[2]
    
    @auto_attr
    def model_ts(self):
        return compute_model_ts(self.freq_center, self.freq_sigma, self.hrf_delay,
                                self.model.stimulus.time_coord,
                                self.model.stimulus.freq_coord,
                                self.model.stimulus.spectrogram,
                                self.model.stimulus.tr_length,
                                self.model.stimulus.num_timepoints)                                
    @auto_attr
    def fit_stats(self):
        return linregress(self.data, self.model_ts)
        
    @auto_attr
    def rss(self):
        return np.sum((self.data - self.model_ts)**2)
                                    