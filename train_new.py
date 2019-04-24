import torch
import torch.autograd as autograd
import torch.multiprocessing as mp
#import torch.nn as nn
#import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils.data import DataLoader
#from torch.nn.modules.distance import CosineSimilarity
#import torchvision
from torchvision.utils import save_image
#from PIL import Image

import logging
import os
#import sys
import numpy as np
import time

import discriminator as D
import generator as G

from ImageVoice import ImageVoice


# main routine for the project
def train(gen, dis, dataloader, opt):    
    # hyperparameters
    warm_id = opt['warm_model_id']
    epochs = opt['epochs']
    lambda_gp = opt['lambda_gp']
    n_critic = opt['n_critic']  # how many iterations to train discriminator before training generator
    annealing_schedule = opt['annealing_schedule']
    latent_dim = opt['latent_dim'] if 'latent_dim' in opt else 100
    do_penalty = opt['do_gradient_penalty']
    feature_match_ratio = opt['feature_match_ratio']
    do_label_smoothing = opt['label_smoothing']
    sample_interval = 300  # save 25 images every 100 batches
    save_interval = sample_interval * 2  # save model every 1000 batches
    
#    device = torch.device('cpu')
    device = torch.device('cuda')
    print('using {}'.format(device))
    # tuning parameter
#    clip_value = 0.01  # weight clipping, used in WGAN, not in WGAN-GP
    # optimizer parameter
    betas = (0.5, 0.999)
    lr = 0.0001  # from WGAN-GP paper
    
    # optimizers & losses
    optimizer_G = torch.optim.Adam(gen.parameters(), lr=lr*10, betas=betas)
#    optimizer_G = torch.optim.SGD(gen.parameters(), lr=0.01, momentum=0.9)
    optimizer_D = torch.optim.Adam(dis.parameters(), lr=lr, betas=betas)
#    optimizer_D = torch.optim.SGD(dis.parameters(), lr=0.0001)
#    adversarial_loss = torch.nn.BCELoss()
#    metric_loss = torch.nn.CosineSimilarity()
    adversarial_loss = torch.nn.MSELoss()
    # warm start from disk
    if warm_id:
        try:
            dis_save_file = 'discriminator_{:d}'.format(warm_id)
            dis_save_file = os.path.join('experiments', str(opt['warm_run_id']), dis_save_file)
            gen_save_file = 'generator_{:d}'.format(warm_id)
            gen_save_file = os.path.join('experiments', str(opt['warm_run_id']), gen_save_file)
            gen = warm_start_model(gen, gen_save_file)
            dis = warm_start_model(dis, dis_save_file)
            logger.info('model warm started from experiment folder {}, model id {}'.format(opt['warm_run_id'], warm_id))
        except Exception as e:
            print(e)
            print('warm start failed, start from scratch')
        
    batches_done = 0  # record training process in terms of batches, not epochs
    
    dis.to(device)
    gen.to(device)
    
    dis.train()
    gen.train()
    for epoch in range(epochs):
        logger.info('# ---- Epoch {}/{} ---- #'.format(epoch+1, epochs))
        
        if (epoch+1) in annealing_schedule:
            optimizer_G.param_groups[0]['lr'] *= 10
            optimizer_D.param_groups[0]['lr'] *= 10
            logger.info(' learning rate decreased by factor of 10 ')
            
        for i, (imgs, sounds) in enumerate(dataloader):
            # Configure input
#            real_imgs = Variable(imgs.type(Tensor))
            real_imgs, sounds = imgs.to(device), sounds.to(device)
            
            # Sample noise as generator input
            z = Variable(Tensor(np.random.normal(0, 1, size=(imgs.shape[0], latent_dim, 1, 1))))
            z = z.to(device)
            # Generate real and fake labels for loss calculation
            if do_label_smoothing:
                valid = 1 - torch.rand((imgs.shape[0], 1)) * 0.1
                valid = Variable(valid, requires_grad=False)
                fake = torch.rand((imgs.shape[0], 1)) * 0.1
                fake = Variable(fake, requires_grad=False)
                valid_gen = torch.ones_like(imgs)
                valid_gen = Variable(valid_gen, requires_grad=False)
            else:
                valid = Variable(torch.FloatTensor(imgs.shape[0], 1).fill_(1.0), requires_grad=False)
                valid_gen = Variable(torch.FloatTensor(imgs.shape[0], 1).fill_(1.0), requires_grad=False)
                fake = Variable(torch.FloatTensor(imgs.shape[0], 1).fill_(0.0), requires_grad=False)
                
            valid, fake, valid_gen = valid.to(device), fake.to(device), valid_gen.to(device)

            # Real images
            real_validity, real_latent = dis(real_imgs, sounds)
            # Generate a batch of images
            fake_imgs = gen(z, sounds)
            
            # ---------------------
            #  Train Discriminator
            # ---------------------
    
            # Fake images
            fake_validity, fake_latent = dis(fake_imgs.detach(), sounds)
            # Gradient penalty
            if do_penalty:
                gradient_penalty = compute_gradient_penalty(dis, sounds, real_imgs.data, fake_imgs.data)
            else:
                gradient_penalty = 0
            # Adversarial loss
#                d_loss = -torch.mean(real_validity) + torch.mean(fake_validity) + lambda_gp * gradient_penalty
            d_loss = (adversarial_loss(real_validity, valid) + adversarial_loss(fake_validity, fake) )/2 + lambda_gp * gradient_penalty
            
            optimizer_D.zero_grad()
            d_loss.backward()
            optimizer_D.step()
    
            # Train the dis every n_critic steps
            if (i+1) % n_critic == 0:

                # Loss measures generator's ability to fool the dis
                # Train on fake images
                fake_validity, fake_latent = dis(fake_imgs, sounds)
                
                # -----------------
                #  Train Generator
                # -----------------
                
                g_loss = adversarial_loss(fake_validity, valid_gen)
    #            g_loss = -torch.mean(fake_validity)
                g_feature_loss = adversarial_loss(fake_latent, real_latent.detach())
                g_loss += feature_match_ratio * g_feature_loss
    
                
                optimizer_G.zero_grad()
                g_loss.backward()
                optimizer_G.step()  
    
                logger.info(
                    "[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f] [G feature loss: %f]\n"
                    % (epoch+1, epochs, i+1, len(real_loader), d_loss.item(), g_loss.item(), g_feature_loss.item())
                )
                logger.info(
                    "\t[D first grad: %f] [D final grad: %f] [G first grad: %f] [G final grad: %f] \n"
                    % (
                            torch.mean(torch.abs(dis.firstblock[0].weight.grad)).item(),
                            torch.mean(torch.abs(dis.fc_layers.weight.grad)).item(),
                            torch.mean(torch.abs(gen.layers[0].weight.grad)).item(), 
                            torch.mean(torch.abs(gen.finallayer.weight.grad)).item()
                            )
                )
                if batches_done % sample_interval == 0:
                    save_image(real_imgs.data[:25], os.path.join('..', 'reals', "{:d}.png".format(batches_done)),
                               nrow=5, normalize=True)
                    save_image(fake_imgs.data[:25], os.path.join('..', 'fakes', "{:d}.png".format(batches_done)),
                               nrow=5, normalize=True)
                    logger.info('generated sample images saved to disk as {:d}.png\n'.format(batches_done))
                if batches_done % save_interval == 0:
                    dis_save_file = 'discriminator_{:d}\n'.format(batches_done)
                    gen_save_file = 'generator_{:d}\n'.format(batches_done)
                    torch.save(gen.state_dict(), os.path.join('experiments', opt['run_id'], gen_save_file))
                    logger.info('generator model saved at {:d}'.format(batches_done))
                    torch.save(dis.state_dict(), os.path.join('experiments', opt['run_id'], dis_save_file))
                    logger.info('discriminator model saved at {:d}\n'.format(batches_done))
                batches_done += n_critic
#    return gen
                

# helper function for warm starting model
def warm_start_model(model, model_save_file):
    new_state = model.state_dict()
    warm_state = torch.load(model_save_file)['network']
    for (k, v) in warm_state.items():
        if k in new_state:
            shape_old = warm_state[k].size()
            shape_new = new_state[k].size()
            if shape_new == shape_old:
                new_state.update({k: v})
    model.load_state_dict(new_state)
    return model


# helper function for calculating GP in WGAN-GP
Tensor = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor
#Tensor = torch.FloatTensor
def compute_gradient_penalty(D, sounds, real_samples, fake_samples):
    """Calculates the gradient penalty loss for WGAN GP"""
    # Random weight term for interpolation between real and fake samples
    alpha = Tensor(np.random.random((real_samples.size(0), 1, 1, 1)))
    # Get random interpolation between real and fake samples
    interpolates = (alpha * real_samples + ((1 - alpha) * fake_samples)).requires_grad_(True)
    d_interpolates, _ = D(interpolates, sounds)
    fake = Variable(Tensor(real_samples.shape[0], 1).fill_(1.0), requires_grad=False)
    # Get gradient w.r.t. interpolates
    gradients = autograd.grad(
        outputs=d_interpolates,
        inputs=interpolates,
        grad_outputs=fake,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]
    gradients = gradients.view(gradients.size(0), -1)
    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
    return gradient_penalty

logger = logging.getLogger(__name__)
#--- run main routine and prediction ---#
if __name__ == '__main__':
    # generate folders to save model
    run_id = str(int(time.time()))
    if not os.path.exists('./experiments'):
        os.mkdir('./experiments')
    os.mkdir('./experiments/%s' % run_id)
    print("Saving models and logs to ./experiments/%s" % run_id)
    
    logger.setLevel(logging.DEBUG)
    # create console handler and set level to debug
    logfile = os.path.join('experiments', run_id, 'train.log')
    ch = logging.FileHandler(logfile, mode='w')
    ch.setLevel(logging.DEBUG)
    # create formatter
    formatter = logging.Formatter('%(levelname)s - %(message)s')
    # add formatter to ch
    ch.setFormatter(formatter)
    # add ch to logger
    logger.addHandler(ch)
    
    # create console handler and set level to debug
    sysout = logging.StreamHandler()
    sysout.setLevel(logging.DEBUG)
    # add ch to logger
    logger.addHandler(sysout)    
    
    # generate data loader
    bsize = 32  # batch size
    nworkers = mp.cpu_count() # number of workers
    shuffle = True
    train_path = ''  # real training set
    train_ds = ImageVoice(train_path)
    print(len(train_ds), bsize, nworkers)
    real_loader = DataLoader(train_ds, batch_size=bsize, shuffle=shuffle, num_workers=nworkers)
    
    # generate model
    opt = {'in_feat': 1, 
           'out_feat': 3, 
           'latent_dim': 100, 
           'epochs': 20,
           'annealing_schedule':[5, 10, 15, 18, 20], 
           'do_gradient_penalty': False,
           'n_critic':3, 
           'lambda_gp': 10, 
           'feature_match_ratio':10,
           'label_smoothing': True,
           'run_id': run_id, 
           'warm_run_id': None, 
           'warm_model_id':None
           }
    logger.info(opt)
    real_feats = 3  # color channel count for real imgs
    num_classes = 1  # discriminator output size, only outputs a validity score
    soundnet = G.SoundCNN(opt)
    gen = G.CGen(opt, SoundNet=soundnet)
    dis = D.DPNmini(real_feats, num_classes, kernel=5, SoundNet=soundnet)
    
    # train model
    generator = train(gen, dis, real_loader, opt)