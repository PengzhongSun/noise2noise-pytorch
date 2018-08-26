#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
from torch.optim import Adam

from unet import UNet
from rednet import RedNet
from utils import *
from dataset import *

import os
import json
from argparse import ArgumentParser
import matplotlib.pyplot as plt


class Noise2Noise(object):
    """Implementation of Noise2Noise from Lehtinen et al. (2018)."""

    def __init__(self, params, trainable):
        """Initializes model."""

        self.p = params
        self.trainable = trainable
        self._compile()


    def _compile(self):
        """Compiles model (architecture, loss function, optimizers, etc.)."""

        print('Noise2Noise: Learning Image Restoration without Clean Data (Lethinen et al. 2018)')

        # Model
        self.model = UNet()

        if self.trainable:
            # Optimizer
            self.optim = Adam(self.model.parameters(),
                                lr=self.p.learning_rate,
                                betas=self.p.adam[:2],
                                eps=self.p.adam[2])

            # Loss function
            if self.p.loss == 'rmse':
                raise NotImplementedError('rMSE loss not implemented yet!')
            elif self.p.loss == 'l2':
                self.loss = nn.MSELoss()
            else:
                self.loss = nn.L1Loss()

        # CUDA support
        self.use_cuda = torch.cuda.is_available() and self.p.cuda
        if self.use_cuda:
            self.model = self.model.cuda()
            self.loss = self.loss.cuda()


    def _print_params(self):
        """Formats parameters to print when training."""

        print('Training parameters:')
        self.p.cuda = self.use_cuda
        param_dict = vars(self.p)
        pretty = lambda x: x.replace('_', ' ').replace('ckpt ', 'checkpoint ').capitalize()
        print('\n'.join('  {} = {}'.format(pretty(k), pretty(str(v))) for k, v in param_dict.items()))
        print()


    def save_model(self, epoch, stats, first=False):
        """Saves model to files; can be overwritten at every epoch to save disk space."""

        # Create directory for model checkpoints, if nonexistent
        if first:
            timestamp = f'{datetime.now():{self.p.noise_type}-%y%m%d-%H%M}'
            self.ckpt_dir = os.path.join(self.p.ckpt_save_path, timestamp)
            if not os.path.isdir(self.ckpt_dir):
                os.mkdir(self.ckpt_dir)

        if self.p.ckpt_overwrite:
            fname_unet = '{}/n2n-{}.pt'.format(self.ckpt_dir, self.p.noise_type)
        else:
            valid_loss = stats['valid_loss'][epoch]
            fname_unet = '{}/n2n-epoch{}-{:>1.5f}.pt'.format(self.ckpt_dir, epoch + 1, valid_loss)
        print('Saving checkpoint to: {}\n'.format(fname_unet))
        torch.save(self.model.state_dict(), fname_unet)

        # Save stats to JSON
        fname_dict = '{}/n2n-stats.json'.format(self.ckpt_dir)
        with open(fname_dict, 'w') as fp:
            json.dump(stats, fp, indent=2)


    def load_model(self, ckpt_fname):
        """Loads model from checkpoint file."""

        print('Loading checkpoint from: {}'.format(ckpt_fname))
        if self.use_cuda:
            self.model.load_state_dict(torch.load(ckpt_fname))
        else:
            self.model.load_state_dict(torch.load(ckpt_fname, map_location='cpu'))


    def test(self, test_loader, show):
        """Evaluates denoiser on test set."""

        self.model.train(False)

        noisy_imgs = []
        denoised_imgs = []
        clean_imgs = []

        # Create directory for denoised images
        save_path = os.path.join(self.p.data, 'denoised')
        if not os.path.isdir(save_path):
            os.mkdir(save_path)

        for batch_idx, (source, target) in enumerate(test_loader):
            # Only show first <show> images
            if show == 0 or batch_idx >= show:
                break

            noisy_imgs.append(source)
            clean_imgs.append(target)

            if self.use_cuda:
                source = source.cuda()

            # Denoise
            denoised_img = self.model(source).detach()
            denoised_imgs.append(denoised_img)

        # Squeeze tensors
        noisy_imgs = [t.squeeze(0) for t in noisy_imgs]
        denoised_imgs = [t.squeeze(0) for t in denoised_imgs]
        clean_imgs = [t.squeeze(0) for t in clean_imgs]

        # Create montage and save images
        print('Saving images and montages to: {}'.format(save_path))
        for i in range(len(noisy_imgs)):
            img_name = test_loader.dataset.imgs[i]
            create_montage(img_name, save_path, noisy_imgs[i], denoised_imgs[i], clean_imgs[i], show)


    def eval(self, valid_loader):
        """Evaluates denoiser on validation set."""

        self.model.train(False)

        valid_start = datetime.now()
        loss_meter = AvgMeter()
        psnr_meter = AvgMeter()

        for batch_idx, (source, target) in enumerate(valid_loader):
            if self.use_cuda:
                source = source.cuda()
                target = target.cuda()

            # Denoise
            source_denoised = self.model(source)

            # Update loss
            loss = self.loss(source_denoised, target)
            loss_meter.update(loss.item())
            psnr_meter.update(psnr(source_denoised, target).item())

        valid_loss = loss_meter.avg
        valid_time = time_elapsed_since(valid_start)[0]
        psnr_avg = psnr_meter.avg

        return valid_loss, valid_time, psnr_avg


    def train(self, train_loader, valid_loader):
        """Trains denoiser on training set."""

        self.model.train(True)

        self._print_params()
        num_batches = len(train_loader)
        assert num_batches % self.p.report_interval == 0, 'Report interval must divide total number of batches'

        # Dictionaries of tracked stats
        stats = {'noise_type': self.p.noise_type,
                 'noise_param': self.p.noise_param,
                 'train_loss': [],
                 'valid_loss': [],
                 'valid_psnr': []}

        # Main training loop
        start = datetime.now()
        for epoch in range(self.p.nb_epochs):
            print('EPOCH {:d} / {:d}'.format(epoch + 1, self.p.nb_epochs))

            # Some stats trackers
            epoch_start = datetime.now()
            train_loss_meter = AvgMeter()
            loss_meter = AvgMeter()
            time_meter = AvgMeter()

            # Minibatch SGD
            for batch_idx, (source, target) in enumerate(train_loader):
                batch_start = datetime.now()
                progress_bar(batch_idx, num_batches, self.p.report_interval, loss_meter.val)

                if self.use_cuda:
                    source = source.cuda()
                    target = target.cuda()

                # Denoise image
                source_denoised = self.model(source)
                loss = self.loss(source_denoised, target)
                loss_meter.update(loss.item())

                # Zero gradients, perform a backward pass, and update the weights
                self.optim.zero_grad()
                loss.backward()
                self.optim.step()

                # Report/update statistics
                time_meter.update(time_elapsed_since(batch_start)[1])
                if (batch_idx + 1) % self.p.report_interval == 0 and batch_idx:
                    show_on_report(batch_idx, num_batches, loss_meter.avg, time_meter.avg)
                    train_loss_meter.update(loss_meter.avg)
                    loss_meter.reset()
                    time_meter.reset()

            # Evaluate model on validation set
            print('\rTesting model on validation set... ', end='')
            epoch_time = time_elapsed_since(epoch_start)[0]
            valid_loss, valid_time, valid_psnr = self.eval(valid_loader)
            show_on_epoch_end(epoch_time, valid_time, valid_loss, valid_psnr)

            # Save checkpoint
            stats['train_loss'].append(train_loss_meter.avg)
            train_loss_meter.reset()
            stats['valid_loss'].append(valid_loss)
            stats['valid_psnr'].append(valid_psnr)
            self.save_model(epoch, stats, epoch == 0)

            # Plot stats
            if self.p.plot_stats:
                loss_str = f'{self.p.loss} loss'.capitalize()
                plot_per_epoch(self.ckpt_dir, 'Valid loss', stats['valid_loss'], loss_str)
                plot_per_epoch(self.ckpt_dir, 'Valid PSNR', stats['valid_psnr'], 'PSNR (dB)')

        elapsed = time_elapsed_since(start)[0]
        print('Training done! Total elapsed time: {}\n'.format(elapsed))