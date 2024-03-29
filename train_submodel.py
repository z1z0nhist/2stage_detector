import argparse
import glob
import shutil
import warnings

import albumentations as A
import pandas as pd
from albumentations.pytorch.transforms import ToTensorV2
from ultralytics.yolo.utils import checks
import numpy as np
import random
import logging
import logging.config
import platform
import os
import copy
import json
from pathlib import Path
from collections import defaultdict
import timm
from torchvision.datasets import ImageFolder
import torch
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim import lr_scheduler
import gc
import yaml
import pickle
import pathlib
import inspect
from trainng import *
from detector import Detector
from datautils.prepare_StanfordDog import prepare_dataset
from ultralytics.yolo.utils import LOGGER, colorstr, emojis
device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]

def seed_everything(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

def fetch_scheduler(optimizer):
    scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=500,eta_min=1e-6)
    return scheduler
def data_transforms_img(img_size):
    data_transforms = {
        'train': A.Compose([
            A.Resize(img_size, img_size),
            A.OneOf([A.Rotate(limit=10),
                     A.RandomBrightness(),
                     A.CoarseDropout(always_apply=False, p=0.5, max_holes=20,
                                     max_height=15, max_width=15, min_holes=1,
                                     min_height=8, min_width=8),
                     A.Cutout(num_holes=8, max_h_size=1, max_w_size=1, fill_value=1),
                     ], p=1.0),
            A.GaussNoise(p=0.5),
            A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
            ToTensorV2()]),
        'valid': A.Compose([A.Resize(img_size, img_size),
                            A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                            ToTensorV2()]),
    }
    return data_transforms

class Transforms:
    def __init__(self, transforms: A.Compose):
        self.transforms = transforms
    def __call__(self, img, *args, **kwargs):
        return self.transforms(image=np.array(img))


def run_training(model, m_p, train_loader, valid_loader, optimizer, scheduler, device, num_epochs, test_path, det, target_labels):
    if torch.cuda.is_available():
        LOGGER.info("[INFO] Using GPU: {}\n".format(torch.cuda.get_device_name()))

    best_model_wts = copy.deepcopy(model.state_dict())
    best_epoch_acc = 0
    history = defaultdict(list)

    # bar = tqdm(range(1, num_epochs + 1), total=num_epochs)
    for epoch in range(1, num_epochs + 1):
        gc.collect()
        train_epoch_loss = training_epoch(model, optimizer, scheduler,
                                    dataloader=train_loader,
                                    device=device, epoch=epoch)
        val_epoch_loss = val_epoch(model,dataloader=valid_loader,
                                   device=device, epoch=epoch)
        if epoch > 1: #
            det_epoch_acc = det.infer_detect(path=test_path, target_labels=target_labels)

            if det_epoch_acc > best_epoch_acc:
                LOGGER.info(f"Detection Acc Improved ({best_epoch_acc} ---> {det_epoch_acc})")
                best_epoch_acc = det_epoch_acc
                if not os.path.isdir('{0}/'.format('sub_models')):
                    os.mkdir('{0}/'.format('sub_models'))
                best_epoch = epoch
                best_model_wts = copy.deepcopy(model.state_dict())
                torch.save(model.state_dict(), f"{m_p}")

            LOGGER.info("\nBest Acc: {:.4f}".format(best_epoch_acc))

            LOGGER.info(f"Epoch={epoch}, Train_Loss={train_epoch_loss:.4f}, Valid_Loss = {val_epoch_loss:.4f}, Acc={det_epoch_acc:.4f},"
                  f"LR={optimizer.param_groups[0]['lr']}")
        if epoch == 1:
            torch.save(model.state_dict(), f"{m_p}")
        else:
            if epoch%100==0:
                torch.save(model.state_dict(),
                           "{}/Acc{:.4f}_epoch{:.0f}.pt".format('sub_models', best_epoch_acc, best_epoch))
            model.load_state_dict(best_model_wts)

    return model, history

def print_args(args= None, show_file=True, show_func=False):
    # Print function arguments (optional args dict)
    x = inspect.currentframe().f_back  # previous frame
    file, _, func, _, _ = inspect.getframeinfo(x)
    if args is None:  # get args automatically
        args, _, _, frm = inspect.getargvalues(x)
        args = {k: v for k, v in frm.items() if k in args}
    try:
        file = Path(file).resolve().relative_to(ROOT).with_suffix('')
    except ValueError:
        file = Path(file).stem
    s = (f'{file}: ' if show_file else '') + (f'{func}: ' if show_func else '')
    LOGGER.info(colorstr(s) + ', '.join(f'{k}={v}' for k, v in args.items()))

def parse_opt(known=False):
    parser = argparse.ArgumentParser()

    parser.add_argument('--data', type=str,
                        default='ultralytics/yolo/cfg/stanford_dogs.yaml',
                        help='*.yaml path') #
    parser.add_argument('--test_path', type=str,
                        default='datasets/stanford_dogs/Images',
                        help='test sub_dataset path for detector(!None cropped images! just original detection sub_dataset path)')
    parser.add_argument('--det_w', type=str,
                        default="yolov8m.pt",
                        help='trained detector weight path.')
    parser.add_argument('--epoch', type=int, default=100, help='train epochs')
    parser.add_argument('--name', type=str, default='tf_efficientnet_b0', help='timm model name')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=224, help='inference size h,w')
    parser.add_argument('--lr', type=float, default=0.0001, help='maximum detections per image')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--batch_size', type=int, default=32, help='batch_size')

    return parser.parse_args()

def main(opt):
    seed_everything(1004)
    print_args(vars(opt))
    with open(opt.data, encoding='UTF-8') as f:
        _cfg = yaml.load(f, Loader=yaml.FullLoader)
    category = _cfg['target_labels']
    model_path = _cfg['sub_model']
    names = _cfg['sub_names']
    dataset_p = _cfg['sub_train']
    save_dir = '/'.join(_cfg['sub_model'][0].split('/')[:-1])
    LOGGER.info(colorstr(f'save_dir = ') + save_dir)

    if not os.path.isdir(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    trans = data_transforms_img(opt.imgsz)

    if len(names) != len(model_path) != len(dataset_p) != len(category):
        LOGGER.info(colorstr('red', 'bold', f"check {opt.data} plz. Length of data must be same."))
        exit()
    det = Detector(weight=opt.det_w, cfg=opt.data)
    for idx, cid, d_p, m_p, name in zip(range(len(category)), category, dataset_p, model_path, names):
        train_path = f'{d_p}/train/'
        valid_path = f'{d_p}/valid/'
        if not os.path.isdir(train_path) or not os.path.isdir(valid_path):
            if not prepare_dataset(d_p):
                LOGGER.info(colorstr('red', 'bold', f"check dataset {train_path} or {valid_path} plz.") )

        train_dataset = ImageFolder(train_path, transform=Transforms(transforms=trans['train']))
        valid_dataset = ImageFolder(valid_path, transform=Transforms(transforms=trans['valid']))
        train_loader = DataLoader(train_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=4)
        valid_loader = DataLoader(valid_dataset, batch_size=opt.batch_size, shuffle=False, num_workers=4)
        # check label json.
        jp = _cfg['sub_data'][idx]
        l_p = save_dir + f'/label_data_{cid}.pkl'  # default pkl path
        if not checks.check_pkl(jp, len(train_dataset.classes)) or not os.path.isfile(l_p):
            _cfg['sub_data'].append(f"{l_p}")
            _labels = {_: f"{_}_{v}" for _, v in enumerate(train_dataset.classes)} # v == class name
            with open(l_p, 'wb') as f:
                pickle.dump(_labels, f, pickle.HIGHEST_PROTOCOL)

        # define model
        model = timm.create_model(name,num_classes=len(train_dataset.classes))
        model.to(device)

        optimizer = optim.Adam(model.parameters(), lr=1e-03)
        scheduler = fetch_scheduler(optimizer)

        model, history = run_training(model, m_p, train_loader, valid_loader,
                                        optimizer, scheduler, num_epochs=opt.epoch, test_path=opt.test_path,
                                      det= det, target_labels=[cid], device=device)

    #test model
    det_acc = det.infer_detect(path=opt.test_path, target_labels=category, save=True)
    LOGGER.info(colorstr("final Acc : ")+det_acc)

def run(**kwargs):
    opt = parse_opt(True)
    for k, v in kwargs.items():
        setattr(opt, k, v)
        print(k,v)
    main(opt)
    return opt

if __name__ == '__main__':
    opt = parse_opt()
    main(opt)
