# Copyright 2021 Dakewe Biotech Corporation. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
print('Importing...')
import logging
import random
import os
import sys
import psutil
import numpy as np
import pandas as pd
from argparse import ArgumentParser
from os.path import join

import warnings
warnings.simplefilter("ignore", UserWarning)

import torch
from torch.backends import cudnn
from torch.nn import BCELoss
from torch.nn import MSELoss
from torch.optim import Adam
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader

from srgan_pytorch.dataset import BaseDataset
from srgan_pytorch.loss import ContentLoss
from srgan_pytorch.model import Discriminator
from srgan_pytorch.model import Generator
from srgan_pytorch.utils import create_folder

from PIL import Image
from skimage.color import rgb2ycbcr
from skimage.io import imread
from skimage.metrics import peak_signal_noise_ratio
from skimage.metrics import structural_similarity
from torchvision.transforms import ToTensor
from torchvision.utils import save_image
print('Parameters...')
# It is a convenient method for simple scripts to configure the log package at one time.
logger = logging.getLogger(__name__)
logging.basicConfig(format="[ %(levelname)s ] %(message)s", level=logging.INFO)

parserTrain = ArgumentParser()
parserTrain.add_argument("--dataroot", default="/home/calexand/datasets/histo_split_4/only_val",
                    help="Path to dataset.")
parserTrain.add_argument("--p-epochs", default=512, type=int,
                    help="Number of total p-oral epochs to run. (Default: 512)")
parserTrain.add_argument("--g-epochs", default=128, type=int,
                    help="Number of total g-oral epochs to run. (Default: 128)")
parserTrain.add_argument("--batch-size", default=16, type=int,
                    help="The batch size of the dataset. (Default: 16)")
parserTrain.add_argument("--p-lr", default=0.0001, type=float,
                    help="Learning rate for psnr-oral. (Default: 0.0001)")
parserTrain.add_argument("--g-lr", default=0.0001, type=float,
                    help="Learning rate for gan-oral. (Default: 0.0001)")
parserTrain.add_argument("--image-size", default=96, type=int,
                    help="Image size of high resolution image. (Default: 96)")
parserTrain.add_argument("--scale", default=4, type=int,
                    help="Low to high resolution scaling factor.")
parserTrain.add_argument("--netD", default="", type=str,
                    help="Path to Discriminator checkpoint.")
parserTrain.add_argument("--netG", default="", type=str,
                    help="Path to Generator checkpoint.")
parserTrain.add_argument("--name", default="DEF", type=str,
                    help="Name for some folders")
parserTrain.add_argument("--seed", default=None, type=int,
                    help="Seed for initializing training.")
parserTrain.add_argument("--pretrained", dest="pretrained", action="store_true",
                    help="Use pre-trained model.")
parserTrain.add_argument("--cuda", dest="cuda", action="store_true",
                    help="Enables cuda.")
args = parserTrain.parse_args()

if ((args.image_size / args.scale)%1) != 0:
    sys.exit('The image size is not correctly divizible by the scale.')
if ((args.image_size / 16)%1) != 0:
    sys.exit('The image size is not correctly divizible by 16.')

# Random seed can ensure that the results of each training are inconsistent.
if args.seed is None:
    args.seed = random.randint(1, 10000)
logger.info(f"Random Seed: {args.seed}")
random.seed(args.seed)
torch.manual_seed(args.seed)

# Because the resolution of each input image is fixed, setting it to `True`
# will make CUDNN automatically find the optimal convolution method.
# If the input image resolution is not fixed, it needs to be set to `False`.
cudnn.benchmark = True

# Set whether to use CUDA.
if torch.cuda.is_available() and not args.cuda:
    logger.warning("You have a CUDA device, so you should probably "
                   "run with --cuda")

device = torch.device("cuda:0" if args.cuda else "cpu")
print('Using device:',device)

print('Loading Dataset...')
# Load dataset.
dataset = BaseDataset(dataroot=args.dataroot,
                      image_size=args.image_size,
                      scale=args.scale)
dataloader = DataLoader(dataset=dataset,
                        batch_size=args.batch_size,
                        shuffle=True,
                        pin_memory=True)

print('Loading model...')
# Load model.
netD = Discriminator(args.image_size).to(device)
netG = Generator(args.scale).to(device)

# Optional: Resume training.
start_p_epoch = 0
start_g_epoch = 0
if args.netD != "" and args.netG != "":
    netD.load_state_dict(torch.load(args.netD))
    start_g_epoch = "".join(list(filter(str.isdigit, args.netD)))
    logger.info(f"You loaded {args.netD} for discriminator."
                f"G-Oral resume epoch from {start_g_epoch}.")
if args.netG != "" and args.netD == "":
    netG.load_state_dict(torch.load(args.netG))
    start_p_epoch = "".join(list(filter(str.isdigit, args.netG)))
    logger.info(f"You loaded {args.netG} for generator."
                f"P-Oral resume epoch from {start_p_epoch}.")

# Define loss function.
pixel_criterion = MSELoss().to(device)
content_criterion = ContentLoss().to(device)
adv_criterion = BCELoss().to(device)

# Define optimizer function.
p_optim = Adam(netG.parameters(), args.p_lr, (0.9, 0.999))
d_optim = Adam(netD.parameters(), args.g_lr, (0.9, 0.999))
g_optim = Adam(netG.parameters(), args.g_lr, (0.9, 0.999))

# Define scheduler function.
d_scheduler = StepLR(d_optim, args.g_epochs // 2, 0.1)
g_scheduler = StepLR(g_optim, args.g_epochs // 2, 0.1)

print('Starting...')

def main():
    # Use PSNR value as the image evaluation index in the process of training PSNR.
    # Use SSIM value as the image evaluation index in the process of training GAN.
    # If an Epoch is higher than the current index, save the model weight under
    # the current Epoch as `XXX-best.pth` and save it to the `weights` folder.
    best_psnr = 0.0
    best_ssim = 0.0

    bestPSNRepoch = 0
    bestGANepoch = 0

    # to save statistics
    # resultsP = {'p_loss': [], 'p_psnr': [], 'p_ssim': []}
    pLoss = ['p_loss']
    pPSNR = ['p_psnr']
    pSSIM = ['p_ssim']

    # Train the PSNR stage of the generative model, and save the model weight
    # after reaching a certain index.
    for epoch in range(int(start_p_epoch), args.p_epochs):
        # Training.
        psnrLoss = train_psnr(epoch)
        # Test.
        lrImg = 'lr_' + str(args.scale) + '.jpg'
        sr(netG, join("assets", lrImg), join("assets",args.name, "sr.jpg"))
        if args.scale > 15:
            psnr, ssim = iqa(join("assets",args.name, "sr.jpg"), join("assets", "hr_extra.jpg"))
        else:
            psnr, ssim = iqa(join("assets",args.name, "sr.jpg"), join("assets", "hr.jpg"))
        logger.info(f"P-Oral epoch {epoch} PSNR: {psnr:.2f}dB SSIM: {ssim:.4f}.")
        # Write result.
        pLoss.append(psnrLoss.cpu().item())
        pPSNR.append(psnr)
        pSSIM.append(ssim)

        # Check whether the PSNR value of the current epoch is the highest value
        # ever in the training PSNR phase.
        is_best = psnr > best_psnr
        best_psnr = max(psnr, best_psnr)
        # Save the model once after each epoch. If the current PSNR value is the
        # highest, save another model ending with `best`.
        #torch.save(netG.state_dict(), join("weights",args.name, f"P_epoch{epoch}.pth"))
        if is_best:
            print('[ BEST ] PSNR at epoch {} with best PSNR {}'.format(str(epoch),best_psnr))
            bestPSNRepoch = int(epoch)
            torch.save(netG.state_dict(), join("weights",args.name, "P-best.pth"))
            

    # Save the model weights of the last iteration of the PSNR stage.
    torch.save(netG.state_dict(), join("weights",args.name, "P-last.pth"))

    # out_path = 'stats/'
    # data_frame_p = pd.DataFrame(
    #     data={'Loss_P': resultsP['p_loss'], 'PSNR': resultsP['p_psnr'], 'SSIM': resultsP['p_ssim']},
    #     index=range(1, args.p_epochs + 1))
    # data_frame_p.to_csv(out_path + str(args.name)+'_' + str(args.scale) + '_P_results.csv', index_label='Epoch')

    out_path = 'stats/' + str(args.name)+'_' + str(args.scale) + '_P_results.csv'
    np.savetxt(out_path, np.dstack((np.arange(0, len(pLoss)),pLoss,pPSNR,pSSIM))[0],"%s,%s,%s,%s")
    # print('[ INFO ] PSNR Statistics saved.')


    # resultsG = {'d_loss': [],'g_loss': [], 'g_psnr': [], 'g_ssim': []}
    dLoss = ['d_loss']
    gLoss = ['g_loss']
    gPSNR = ['g_psnr']
    gSSIM = ['g_ssim']

    # Train the generative model in the GAN stage and save the model weight after
    # reaching a certain index.
    saveEpochs = [10,25,40,50]
    for epoch in range(int(start_g_epoch), args.g_epochs):
        # Training.
        allLossD,allLossG = train_gan(epoch)
        # Test.
        lrImg = 'lr_' + str(args.scale) + '.jpg'
        sr(netG, join("assets", lrImg), join("assets",args.name, "sr.jpg"))
        if args.scale > 15:
            psnr, ssim = iqa(join("assets",args.name, "sr.jpg"), join("assets", "hr_extra.jpg"))
        else:
            psnr, ssim = iqa(join("assets",args.name, "sr.jpg"), join("assets", "hr.jpg"))
        logger.info(f"G-Oral epoch {epoch} PSNR: {psnr:.2f}dB SSIM: {ssim:.4f}.")
        # Write result
        dLoss.append(allLossD.cpu().item())
        gLoss.append(allLossG.cpu().item())
        gPSNR.append(psnr)
        gSSIM.append(ssim)

        # Check whether the PSNR value of the current epoch is the highest value
        # in the history of the training GAN stage.
        is_best = ssim > best_ssim
        best_ssim = max(ssim, best_ssim)
        # Save the model once after each epoch, if the current PSNR value is the
        # highest, save another model ending with `best`.
        #torch.save(netD.state_dict(), join("weights",args.name, f"D_epoch{epoch}.pth"))
        if epoch in saveEpochs:
            torch.save(netG.state_dict(), join("weights",args.name, f"G_epoch{epoch}.pth"))
        if is_best:
            print('[ BEST ] GAN at epoch {} with SSIM {}'.format(str(epoch),best_ssim))
            bestGANepoch = int(epoch)
            torch.save(netD.state_dict(), join("weights",args.name, "D-best.pth"))
            torch.save(netG.state_dict(), join("weights",args.name, "G-best.pth"))

        # Call the scheduler function to adjust the learning rate of the
        # generator model and the discrimination model.
        d_scheduler.step()
        g_scheduler.step()


    print('[ BEST ] PSNR was at epoch {}'.format(bestPSNRepoch))
    print('[ BEST ] GAN was at epoch {}'.format(bestGANepoch))

    # Save the model weights of the last iteration of the GAN stage.
    torch.save(netG.state_dict(), join("weights",args.name, "G-last.pth"))    

    # data_frame_g = pd.DataFrame(
    #     data={'Loss_D': resultsG['d_loss'], 'Loss_G': resultsG['g_loss'], 'PSNR': resultsG['g_psnr'], 'SSIM': resultsG['g_ssim']},
    #     index=range(1, args.g_epochs + 1))
    # data_frame_g.to_csv(out_path + str(args.name)+'_' + str(args.scale) + '_G_train.csv', index_label='Epoch')

    out_path = 'stats/' + str(args.name)+'_' + str(args.scale) + '_G_results.csv'
    np.savetxt(out_path, np.dstack((np.arange(0, len(dLoss)),dLoss,gLoss,gPSNR,gSSIM))[0],"%s,%s,%s,%s,%s")
    # print('[ INFO ] GAN Statistics saved.')


def train_psnr(epoch):
    num_batches = len(dataloader)
    allLoss = []
    for index, data in enumerate(dataloader, 1):
        
        inputs, target = data[0].to(device), data[1].to(device)

        ##############################################
        # (0) Update G network: min MSE(output, target)
        ##############################################
        netG.zero_grad()
        output = netG(inputs)
        # print('Output shape',output.shape)
        # print('Target shape',target.shape)
        loss = pixel_criterion(output, target)
        loss.backward()
        allLoss.append(loss)
        p_optim.step()

        logger.info(f"Epoch[{epoch+1}/{args.p_epochs}]"
                    f"({index}/{num_batches}) P Loss: {loss.item():.4f}.")

        # Write the loss value during PSNR training into Tensorboard.
        batches = index + epoch * num_batches + 1
    return (sum(allLoss) / len(allLoss))


def train_gan(epoch):
    num_batches = len(dataloader)
    allLossD = []
    allLossG = []
    for index, data in enumerate(dataloader, 1):
        # Copy the data to the designated device.
        inputs, target = data[0].to(device), data[1].to(device)
        batch_size = inputs.size(0)

        # Set the real sample label to 1, and the false sample label to 0.
        real_label = torch.full((batch_size, 1), 1, dtype=inputs.dtype).to(
            device)
        fake_label = torch.full((batch_size, 1), 0, dtype=inputs.dtype).to(
            device)

        ##############################################
        # (1) Update D network: E(real)[log(D(real))] + E(fake)[log(1 - D(G(fake))]
        ##############################################
        netD.zero_grad()
        fake = netG(inputs)
        # print('target is:',target.shape)
        # print('fake is:',fake.shape)
        # print('real_label is:',real_label)
        # print('fake_label is:',fake_label)
        d_loss_real = adv_criterion(netD(target), real_label)
        d_loss_fake = adv_criterion(netD(fake.detach()), fake_label)
        # d_loss = torch.tensor([0], dtype=torch.float)
        d_loss = d_loss_real + d_loss_fake
        # print('d_loss_real is:',d_loss_real)
        # print('d_loss_fake is:',d_loss_fake)
        # print('d_loss is:',d_loss)
        d_loss.backward()
        d_optim.step()

        ##############################################
        # (2) Update G network: E(fake)[log(1 - D(G(fake))]
        ##############################################
        netG.zero_grad()
        fake = netG(inputs)
        pixel_loss = 1e+1 * pixel_criterion(fake, target.detach())
        content_loss = 2e-6 * content_criterion(fake, target.detach())
        adv_loss = 1e-3 * adv_criterion(netD(fake), real_label)
        g_loss = pixel_loss + content_loss + adv_loss
        g_loss.backward()
        g_optim.step()

        logger.info(f"Epoch[{epoch+1}/{args.g_epochs}]"
                    f"({index}/{num_batches}) "
                    f"D Loss: {d_loss.item():.4f} "
                    f"G Loss: {g_loss.item():.4f}.")

        # Write the loss value during GAN training into Tensorboard.
        allLossD.append(d_loss)
        allLossG.append(g_loss)

    allLossD = (sum(allLossD) / len(allLossD))
    allLossG = (sum(allLossG) / len(allLossG))
    return allLossD,allLossG


def sr(model, lr_filename, sr_filename):
    r""" Turn low resolution into super resolution.

    Args:
        model (torch.nn.Module): SR model.
        lr_filename (str): Low resolution image address.
        sr_filename (srt): Super resolution image address.
    """
    with torch.no_grad():
        lr = Image.open(lr_filename).convert("RGB")
        lr_tensor = ToTensor()(lr).unsqueeze(0).to(device)
        sr_tensor = model(lr_tensor)
        save_image(sr_tensor.detach(), sr_filename, normalize=True)


def iqa(sr_filename, hr_filename):
    r""" Image quality evaluation function.

    Args:
        sr_filename (str): Super resolution image address.
        hr_filename (srt): High resolution image address.

    Returns:
        PSNR value(float), SSIM value(float).
    """
    sr_image = imread(sr_filename)
    hr_image = imread(hr_filename)

    srSize = sr_image.shape[0]
    hrSize = hr_image.shape[0]

    # Delete 4 pixels around the image to facilitate PSNR calculation.
    if(srSize == hrSize):
        sr_image = sr_image[4:-4, 4:-4, ...]
        hr_image = hr_image[4:-4, 4:-4, ...]
    else:
        raise Exception("Difference between SR and HR sizes. Should be equal.")

    # Calculate the Y channel of the image. Use the Y channel to calculate PSNR
    # and SSIM instead of using RGB three channels.
    sr_image = sr_image / 255.0
    hr_image = hr_image / 255.0
    sr_image = rgb2ycbcr(sr_image)[:, :, 0:1]
    hr_image = rgb2ycbcr(hr_image)[:, :, 0:1]
    # Because rgb2ycbcr() outputs a floating point type and the range is [0, 255],
    # it needs to be renormalized to [0, 1].
    sr_image = sr_image / 255.0
    hr_image = hr_image / 255.0

    psnr = peak_signal_noise_ratio(sr_image, hr_image)
    ssim = structural_similarity(sr_image,
                                 hr_image,
                                 win_size=11,
                                 gaussian_weights=True,
                                 multichannel=True,
                                 data_range=1.0,
                                 K1=0.01,
                                 K2=0.03,
                                 sigma=1.5)

    return psnr, ssim


if __name__ == "__main__":
    # create_folder("weights")
    create_folder(os.path.join("weights", args.name))
    create_folder(os.path.join("assets", args.name))
    # create_folder("stats")
    # create_folder(os.path.join("stats", args.name))

    logger.info("TrainEngine:")
    logger.info("\tAPI version .......... 0.4.0")
    logger.info("\tBuild ................ 2021.07.09")
    logger.info("\tModified by ... Catalin Alexandru")
    logger.info("\tOn ................... 2021.07.21")

    main()

    logger.info("All training has been completed successfully.\n")
