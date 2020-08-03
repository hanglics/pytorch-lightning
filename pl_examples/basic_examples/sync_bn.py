"""
Sync-bn with DDP (GPU)
"""
import os
import math
from argparse import ArgumentParser

import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl

import torchvision.transforms as transforms
from torchvision.datasets import MNIST
from torch.utils.data import random_split
from torch.utils.data import DataLoader


pl.seed_everything(234)
EPSILON = 1e-12


class MNISTDataModule(pl.LightningDataModule):
    def __init__(self, data_dir: str = './', batch_size=32):
        super().__init__()

        self.data_dir = data_dir
        self.batch_size = batch_size
        self.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,))
        ])

        # self.dims is returned when you call dm.size()
        # Setting default dims here because we know them.
        # Could optionally be assigned dynamically in dm.setup() 
        self.dims = (1, 28, 28)

    def prepare_data(self):
        # download only
        MNIST(os.getcwd(), train=True, download=True, transform=transforms.ToTensor())
        MNIST(os.getcwd(), train=False, download=True, transform=transforms.ToTensor())

    def setup(self, stage=None):

        # Assign train/val datasets for use in dataloaders
        if stage == 'fit' or stage is None:
            mnist_full = MNIST(self.data_dir, train=True, transform=self.transform)
            self.mnist_train, self.mnist_val = random_split(mnist_full, [55000, 5000])

        # Assign test dataset for use in dataloader(s)
        if stage == 'test' or stage is None:
            self.mnist_test = MNIST(self.data_dir, train=False, transform=self.transform)

    def train_dataloader(self):
        return DataLoader(self.mnist_train, batch_size=self.batch_size, shuffle=False)

    def val_dataloader(self):
        return DataLoader(self.mnist_val, batch_size=self.batch_size, shuffle=False)

    def test_dataloader(self):
        return DataLoader(self.mnist_test, batch_size=self.batch_size, shuffle=False)


class SyncBNModule(pl.LightningModule):
    def __init__(self, **kwargs):
        super().__init__()
        
        self.bn_targets = None
        if 'bn_targets' in kwargs:
            self.bn_targets = kwargs['bn_targets']

        self.linear = nn.Linear(28 * 28, 10)
        self.bn_layer = nn.BatchNorm1d(28 * 28)

    def forward(self, x, batch_idx):
        with torch.no_grad():
            out_bn = self.bn_layer(x.view(x.size(0), -1))
            
            if self.bn_targets:
                print('#######')
                print(self.trainer.local_rank)
                print(out_bn.shape)
                print('#######')

                assert 1 == 0
        out = self.linear(out_bn)

        return out, out_bn

    def training_step(self, batch, batch_idx):
        x, y = batch

        y_hat = self(x, batch_idx)
        loss = F.cross_entropy(y_hat, y)

        return pl.TrainResult(loss)

    def configure_optimizers(self):
        return torch.optim.Adam(self.linear.parameters(), lr=0.02)

    @staticmethod
    def add_model_specific_argument(parent_parser, root_dir):
        """
        Define parameters that only apply to this model
        """
        parser = ArgumentParser(parents=[parent_parser])
        
        parser.add_argument('--nodes', default=1, type=int)
        parser.add_argument('--gpu', default=2, type=int)
        
        parser.add_argument('--epochs', default=1, type=int)
        parser.add_argument('--steps', default=3, type=int)
        
        parser.add_argument('--sync_bn', default='torch', type=str)
        
        return parser


def main(args, datamodule, bn_outputs):
    """Main training routine specific for this project."""
    # ------------------------
    # 1 INIT LIGHTNING MODEL
    # ------------------------
    model = SyncBNModule(bn_targets=bn_outputs)

    # ------------------------
    # 2 INIT TRAINER
    # ------------------------
    trainer = pl.Trainer(
        gpus=args.gpu,
        num_nodes=args.nodes,
        distributed_backend='ddp',
        max_epochs=args.epochs,
        max_steps=args.steps,
        sync_bn_backend=args.sync_bn,
        num_sanity_val_steps=0,
    )

    # ------------------------
    # 3 START TRAINING
    # ------------------------
    trainer.fit(model, datamodule)


def run_cli():
    root_dir = os.path.dirname(os.path.realpath(__file__))
    parent_parser = ArgumentParser(add_help=False)

    # define datamodule and dataloader
    dm = MNISTDataModule()
    dm.prepare_data()
    dm.setup(stage=None)

    train_dataloader = dm.train_dataloader()
    model = SyncBNModule()

    bn_outputs = []
    
    # shuffle is false by default
    for idx, batch in enumerate(train_dataloader):
        x, y = batch

        out, out_bn = model.forward(x, idx)
        bn_outputs.append(out_bn)

        # get 3 steps
        if idx == 2:
            break

    bn_outputs = [x.cuda() for x in bn_outputs]

    # reset datamodule
    # batch-size = 16 because 2 GPUs in DDP
    dm = MNISTDataModule(batch_size=16)
    dm.prepare_data()
    dm.setup(stage=None)

    # each LightningModule defines arguments relevant to it
    parser = SyncBNModule.add_model_specific_argument(parent_parser, root_dir=root_dir)
    parser = pl.Trainer.add_argparse_args(parser)
    args = parser.parse_args()

    # ---------------------
    # RUN TRAINING
    # ---------------------
    main(args, dm, bn_outputs)


if __name__ == '__main__':
    run_cli()
