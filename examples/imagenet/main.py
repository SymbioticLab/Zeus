"""
This script trains imagenet dataset. Currently only support single node.
    - Enable multiprocessing training with `--multiprocessing-distributed`.
    - Enable Zeus with `--zeus`.

Commands:
    - Using `torch.multiprocessing`
        [WARNING: This is NOT recommended. No process management is provided for this launching method. Please consider using the others.]
        $ python train.py [DATA_DIR] --multiprocessing-distributed --zeus [OTHER_OPTIONS]
    - Using `torch.distributed.launch` utility:
        $ python -m torch.distributed.launch --nnodes=1 --nproc_per_node=[NUM_OF_GPUS] train.py [DATA_DIR] --zeus [OTHER_OPTIONS]
    - Using `torchrun`:
        $ torchrun --nnodes 1 --nproc_per_node [NUM_OF_GPUS] train.py [DATA_DIR] --zeus --torchrun [OTHER_OPTIONS]
    - Using Slurm:
        - Example script.sh:
            ```sh
            #!/bin/bash
            #SBATCH --partition=gpu
            #SBATCH --nodes=1                           # number of nodes (single node)
            #SBATCH --ntasks-per-node=[NUM_OF_GPUS]     # number of tasks per node (1 task per GPU)
            #SBATCH --gres=gpu:[NUM_OF_GPUS]            # number of GPUs reserved per node (here all the GPUs)
            #SBATCH --mem=64GB
            ```
        - On terminal:
            $ sbatch script.sh
"""

import argparse
import os
import random
import time
import warnings
import subprocess
from enum import Enum

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
from torch.optim.lr_scheduler import StepLR
import torch.multiprocessing as mp
import torch.utils.data
from torch.utils.data import DataLoader
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
from torch.utils.data import Subset

# ZEUS
from zeus.run import ZeusDataLoader
from zeus.profile.torch import ProfileDataLoader

from torch.distributed.elastic.multiprocessing.errors import (
    record,
)  # NOTE: for debugging purpose, will be removed later


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    # List choices of models
    model_names = sorted(
        name
        for name in models.__dict__
        if name.islower() and not name.startswith("__") and callable(models.__dict__[name])
    )

    parser = argparse.ArgumentParser(description="PyTorch ImageNet Training")

    # ImageNet
    parser.add_argument(
        "data",
        metavar="DIR",
        nargs="?",
        default="/data/imagenet",
        help="path to dataset (default: /data/imagenet)",
    )
    parser.add_argument(
        "-a",
        "--arch",
        metavar="ARCH",
        default="resnet18",
        choices=model_names,
        help="model architecture: " + " | ".join(model_names) + " (default: resnet18)",
    )
    parser.add_argument(
        "-j",
        "--workers",
        default=4,
        type=int,
        metavar="N",
        help="number of data loading workers (default: 4)",
    )
    parser.add_argument(
        "--epochs", default=90, type=int, metavar="N", help="number of total epochs to run"
    )
    parser.add_argument(
        "--start-epoch",
        default=0,
        type=int,
        metavar="N",
        help="manual epoch number (useful on restarts)",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        default=256,
        type=int,
        metavar="N",
        help="mini-batch size (default: 256), this is the total "
        "batch size of all GPUs on the current node when "
        "using Data Parallel or Distributed Data Parallel",
    )
    parser.add_argument(
        "--lr",
        "--learning-rate",
        default=0.1,
        type=float,
        metavar="LR",
        help="initial learning rate",
        dest="lr",
    )
    parser.add_argument("--momentum", default=0.9, type=float, metavar="M", help="momentum")
    parser.add_argument(
        "--wd",
        "--weight-decay",
        default=1e-4,
        type=float,
        metavar="W",
        help="weight decay (default: 1e-4)",
        dest="weight_decay",
    )
    parser.add_argument(
        "-p",
        "--print-freq",
        default=10,
        type=int,
        metavar="N",
        help="print frequency (default: 10)",
    )
    parser.add_argument(
        "-e",
        "--evaluate",
        dest="evaluate",
        action="store_true",
        help="evaluate model on validation set",
    )
    parser.add_argument(
        "--pretrained", dest="pretrained", action="store_true", help="use pre-trained model"
    )
    parser.add_argument(
        "--dist-url",
        default="tcp://127.0.0.1:12306",
        type=str,
        help="url used to set up distributed training",
    )
    parser.add_argument(
        "--dist-backend", default="nccl", type=str, help="distributed backend"
    )
    parser.add_argument(
        "--seed", default=None, type=int, help="seed for initializing training. "
    )
    parser.add_argument("--gpu", default=None, type=int, help="GPU id to use.")
    parser.add_argument(
        "--multiprocessing-distributed",
        action="store_true",
        help="Use `torch.multiprocessing` to launch N processes on this node, "
        "which has N GPUs. PLEASE DO NOT use this argument if you are using "
        "`torchrun` or ``torch.distributed.launch`.",
    )
    parser.add_argument("--dummy", action="store_true", help="use fake data to benchmark")
    
    # ZEUS
    parser.add_argument("--zeus", action="store_true", help="Whether to run Zeus.")
    parser.add_argument("--profile", action="store_true", help="Whether to just profile power.")
    parser.add_argument(
        "--target_metric",
        default=None,
        type=float,
        help=(
            "Stop training when the target metric is reached. This is ignored when running in Zeus mode because"
            " ZeusDataLoader will receive the target metric via environment variable and stop training by itself."
        ),
    )

    # DATA PARALLEL
    parser.add_argument(
        "--local_rank",
        default=-1,
        type=int,
        help="Local rank for data parallel training. This is necessary for using the `torch.distributed.launch` utility.",
    )
    parser.add_argument("--torchrun", action="store_true", help="Use torchrun. This means we will read local_rank from environment variable set by `torchrun`.")

    args = parser.parse_args()

    # Sanity check
    # QA: necessary or not?
    if args.multipocessing_distributed and (args.torchrun or args.local_rank >= 0):
        raise ValueError(
            "Can not set --multiprocessing-distributed when using `torch.distributed.launch` or `torchrun`. "
            "Please refer to the docstring for more info about launching methods."
        )

    return args

best_acc1 = 0


def main():
    args = parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn(
            "You have chosen to seed training. "
            "This will turn on the CUDNN deterministic setting, "
            "which can slow down your training considerably! "
            "You may see unexpected behavior when restarting "
            "from checkpoints."
        )

    if args.gpu is not None:
        warnings.warn(
            "You have chosen a specific GPU. This will completely "
            "disable data parallelism."
        )

    # Preparation for SLURM
    if "SLURM_PROCID" in os.environ:
        # Retrieve local_rank.
        # NOTE: We only consider Single GPU for now. So `local_rank == rank`.
        args.local_rank = int(os.environ["SLURM_PROCID"])

        # QA: I find using MASTER_ADDR and MASTER_PORT more clear (to me).
        #     Let's just tell user they need to set these envs, otherwise,
        #     we pick a port on the node for them.
        # Source: https://gist.github.com/andrewssobral/4bf5700ad258b807e7b9d10106e467ea#file-pytorch-distributed-slurm-example-py-L129
        # jobid = os.environ["SLURM_JOBID"]
        # hostfile = "dist_url." + jobid + ".txt"
        # if args.dist_file is not None:
        #     args.dist_url = "file://{}.{}".format(
        #         os.path.realpath(args.dist_file), jobid
        #     )
        # elif args.rank == 0:
        #     ip = socket.gethostbyname(socket.gethostname())
        #     port = random.randint(49152, 65535)
        #     args.dist_url = "tcp://{}:{}".format(ip, port)
        #     with open(hostfile, "w") as f:
        #         f.write(args.dist_url)
        # else:
        #     while not os.path.exists(hostfile):
        #         time.sleep(1)
        #     with open(hostfile, "r") as f:
        #         args.dist_url = f.read()
        # print(
        #     "dist-url:{} at PROCID {} / {}".format(
        #         args.dist_url, args.rank, args.world_size
        #     )
        # )

        # Source: https://github.com/BIGBALLON/distribuuuu/blob/master/tutorial/mnmc_ddp_slurm.py#L28
        # Retrieve the node address
        node_list = os.environ["SLURM_NODELIST"]
        addr = subprocess.getoutput(f"scontrol show hostname {node_list} | head -n1")
        # Specify master address and master port
        if "MASTER_ADDR" not in os.environ:
            os.environ["MASTER_ADDR"] = addr
        if "MASTER_PORT" not in os.environ:
            os.environ["MASTER_PORT"] = "29500"

    # Preparation for torchrun.
    # Integrate launching by `torchrun` and `torch.distributed.launch`
    # by retrieving local rank from environment variable LOCAL_RANK.
    if args.torchrun:
        args.local_rank = int(os.environ["LOCAL_RANK"])

    # Manual specify MASTER_ADDR and MASTER_PORT if using `torch.multiprocessing`.
    # if args.multiprocessing_distributed:
    #     os.environ["MASTER_ADDR"] = args.master_addr
    #     os.environ["MASTER_PORT"] = str(args.master_port)

    args.distributed = args.multiprocessing_distributed or args.local_rank >= 0
    ngpus_per_node = torch.cuda.device_count()

    if args.multiprocessing_distributed:
        # Use `torch.multiprocessing.spawn` to launch distributed processes: the
        # main_worker process function.
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    elif args.local_rank >= 0:
        # Use `torch.distributed.launch` or `turchrun` or `slurm`.
        # Simply call `main_worker` at `local_rank`.
        main_worker(args.local_rank, ngpus_per_node, args)
    else:
        # Use a specific GPU.
        # Simply call main_worker function.
        main_worker(args.gpu, ngpus_per_node, args)


@record
def main_worker(gpu, ngpus_per_node, args):
    global best_acc1
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.multiprocessing_distributed:
            # Use `torch.multiprocessing`
            # Spawn N processes, one for each GPU
            dist.init_process_group(
                backend=args.dist_backend,
                init_method=args.dist_url,
                world_size=ngpus_per_node,
                rank=args.gpu,
            )
        else:
            # Use `torchrun`, `torch.distributed.launch` or SLURM
            # `MASTER_ADDR` and `MASTER_PORT` are already set as environment variables,
            # so no need to pass to `init_process_group``.
            dist.init_process_group(backend=args.dist_backend)

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()
    if not torch.cuda.is_available():
        print("using CPU, this will be slow")
    elif args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            # When using a single GPU per process and per
            # DistributedDataParallel, we need to divide the batch size
            # ourselves based on the total number of GPUs of the current node.
            args.batch_size = int(args.batch_size / ngpus_per_node)
            args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
            model = torch.nn.parallel.DistributedDataParallel(
                model, device_ids=[args.gpu], output_device=args.gpu,
            )
            # QA: a serious bug here!!! "nccl" as backend will make GPU_1 hanging forever!!
            # https://discuss.pytorch.org/t/dataparallel-and-distributeddataparallel-stuck-at-100-gpu-usage/125490
            # https://github.com/ultralytics/yolov5/issues/2375
            # https://github.com/pytorch/pytorch/issues/52916
            #  - Same here: "gloo" is find, "nccl" got stuck
            #  - DataParallel on either GPU works
        else:
            model.cuda()
            # DistributedDataParallel will divide and allocate batch_size to all
            # available GPUs if device_ids are not set
            model = torch.nn.parallel.DistributedDataParallel(model)
    elif args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith("alexnet") or args.arch.startswith("vgg"):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()

    # define loss function (criterion), optimizer, and learning rate scheduler
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)

    optimizer = torch.optim.SGD(
        model.parameters(),
        args.lr,
        momentum=args.momentum,
        weight_decay=args.weight_decay,
    )

    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    scheduler = StepLR(optimizer, step_size=30, gamma=0.1)

    cudnn.benchmark = True

    # Data loading code
    if args.dummy:
        print("=> Dummy data is used!")
        train_dataset = datasets.FakeData(
            1281167, (3, 224, 224), 1000, transforms.ToTensor()
        )
        val_dataset = datasets.FakeData(
            50000, (3, 224, 224), 1000, transforms.ToTensor()
        )
    else:
        traindir = os.path.join(args.data, "train")
        valdir = os.path.join(args.data, "val")
        normalize = transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
        )

        train_dataset = datasets.ImageFolder(
            traindir,
            transforms.Compose(
                [
                    transforms.RandomResizedCrop(224),
                    transforms.RandomHorizontalFlip(),
                    transforms.ToTensor(),
                    normalize,
                ]
            ),
        )

        val_dataset = datasets.ImageFolder(
            valdir,
            transforms.Compose(
                [
                    transforms.Resize(256),
                    transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    normalize,
                ]
            ),
        )

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(
            val_dataset, shuffle=False, drop_last=True
        )
    else:
        train_sampler = None
        val_sampler = None

    # ZEUS
    # Prepare dataloaders.
    if args.zeus:
        # Zeus
        # Take either of the launching approaches of the data parallel training on
        # single-node multi-GPU will activate the data parallel ("dp") mode in zeus.
        os.environ["ZEUS_TARGET_METRIC"] = str(0.7)
        zeus_distributed = "dp" if args.distributed else None
        train_loader = ZeusDataLoader(
            train_dataset,
            batch_size=args.batch_size,
            distributed=zeus_distributed,
            max_epochs=args.epochs,
            shuffle=(train_sampler is None),
            num_workers=args.workers,
            pin_memory=True,
            sampler=train_sampler,
        )
        val_loader = ZeusDataLoader(
            val_dataset,
            batch_size=args.batch_size,
            distributed=zeus_distributed,
            shuffle=False,
            num_workers=args.workers,
            pin_memory=True,
            sampler=val_sampler,
        )
        # TODO: add profile dataloader?
    else:
        train_loader = DataLoader(
            train_dataset,
            batch_size=args.batch_size,
            shuffle=(train_sampler is None),
            num_workers=args.workers,
            pin_memory=True,
            sampler=train_sampler,
        )
        val_loader = DataLoader(
            val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.workers,
            pin_memory=True,
            sampler=val_sampler,
        )

    if args.evaluate:
        validate(val_loader, model, criterion, args)
        return

    if args.zeus:
        assert isinstance(train_loader, ZeusDataLoader)
        epoch_iter = train_loader.epochs()
    else:
        epoch_iter = range(args.start_epoch, args.epochs)

    for epoch in epoch_iter:
        if args.distributed:
            train_sampler.set_epoch(epoch)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, args)

        # evaluate on validation set
        acc1 = validate(val_loader, model, criterion, args)

        scheduler.step()

        if args.zeus:
            assert isinstance(train_loader, ZeusDataLoader)
            # Scale the accuracy and report to `train_loader`.`
            train_loader.report_metric(acc1 / 100, higher_is_better=True)
        elif args.target_metric is not None:
            if acc1 / 100 >= args.target_metric:
                print(
                    "Reached target metric %s in %s epochs.",
                    args.target_metric,
                    epoch + 1,
                )
                break

        # remember best acc@1
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)


def train(train_loader, model, criterion, optimizer, epoch, args):
    batch_time = AverageMeter("Time", ":6.3f")
    data_time = AverageMeter("Data", ":6.3f")
    losses = AverageMeter("Loss", ":.4e")
    top1 = AverageMeter("Acc@1", ":6.2f")
    top5 = AverageMeter("Acc@5", ":6.2f")

    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch),
    )

    # switch to train mode
    model.train()

    end = time.time()
    for i, (images, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if args.gpu is not None:
            images = images.cuda(args.gpu, non_blocking=True)
        if torch.cuda.is_available():
            target = target.cuda(args.gpu, non_blocking=True)

        # compute output
        output = model(images)
        loss = criterion(output, target)

        # measure accuracy and record loss
        acc1, acc5 = accuracy(output, target, topk=(1, 5))
        losses.update(loss.item(), images.size(0))
        top1.update(acc1[0], images.size(0))
        top5.update(acc5[0], images.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i + 1)


def validate(val_loader, model, criterion, args):
    def run_validate(loader, base_progress=0):
        with torch.no_grad():
            end = time.time()
            for i, (images, target) in enumerate(loader):
                i = base_progress + i
                if args.gpu is not None:
                    images = images.cuda(args.gpu, non_blocking=True)
                if torch.cuda.is_available():
                    target = target.cuda(args.gpu, non_blocking=True)

                # compute output
                output = model(images)
                loss = criterion(output, target)

                # measure accuracy and record loss
                acc1, acc5 = accuracy(output, target, topk=(1, 5))
                losses.update(loss.item(), images.size(0))
                top1.update(acc1[0], images.size(0))
                top5.update(acc5[0], images.size(0))

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                if i % args.print_freq == 0:
                    progress.display(i + 1)

    batch_time = AverageMeter("Time", ":6.3f", Summary.NONE)
    losses = AverageMeter("Loss", ":.4e", Summary.NONE)
    top1 = AverageMeter("Acc@1", ":6.2f", Summary.AVERAGE)
    top5 = AverageMeter("Acc@5", ":6.2f", Summary.AVERAGE)
    progress = ProgressMeter(
        len(val_loader)
        + (
            args.distributed
            and (len(val_loader.sampler) < len(val_loader.dataset))
        ),
        [batch_time, losses, top1, top5],
        prefix="Test: ",
    )

    # switch to evaluate mode
    model.eval()

    run_validate(val_loader)
    if args.distributed:
        top1.all_reduce()
        top5.all_reduce()

    if args.distributed and (
        len(val_loader.sampler) < len(val_loader.dataset)
    ):
        aux_val_dataset = Subset(
            val_loader.dataset,
            range(len(val_loader.sampler), len(val_loader.dataset)),
        )
        aux_val_loader = torch.utils.data.DataLoader(
            aux_val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.workers,
            pin_memory=True,
        )
        run_validate(aux_val_loader, len(val_loader))

    progress.display_summary()

    return top1.avg


class Summary(Enum):
    NONE = 0
    AVERAGE = 1
    SUM = 2
    COUNT = 3


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, name, fmt=":f", summary_type=Summary.AVERAGE):
        self.name = name
        self.fmt = fmt
        self.summary_type = summary_type
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def all_reduce(self):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        total = torch.tensor([self.sum, self.count], dtype=torch.float32, device=device)
        dist.all_reduce(total, dist.ReduceOp.SUM, async_op=False)
        self.sum, self.count = total.tolist()
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = "{name} {val" + self.fmt + "} ({avg" + self.fmt + "})"
        return fmtstr.format(**self.__dict__)

    def summary(self):
        fmtstr = ""
        if self.summary_type is Summary.NONE:
            fmtstr = ""
        elif self.summary_type is Summary.AVERAGE:
            fmtstr = "{name} {avg:.3f}"
        elif self.summary_type is Summary.SUM:
            fmtstr = "{name} {sum:.3f}"
        elif self.summary_type is Summary.COUNT:
            fmtstr = "{name} {count:.3f}"
        else:
            raise ValueError("invalid summary type %r" % self.summary_type)

        return fmtstr.format(**self.__dict__)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print("\t".join(entries))

    def display_summary(self):
        entries = [" *"]
        entries += [meter.summary() for meter in self.meters]
        print(" ".join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = "{:" + str(num_digits) + "d}"
        return "[" + fmt + "/" + fmt.format(num_batches) + "]"


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == "__main__":
    main()
