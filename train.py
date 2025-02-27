from __future__ import absolute_import

import argparse
import collections
import gc
import json
import os
from datetime import datetime

import numpy as np
from catalyst.dl import SupervisedRunner, OptimizerCallback, SchedulerCallback
from catalyst.utils import load_checkpoint, unpack_checkpoint
from pytorch_toolbelt.optimization.functional import get_lr_decay_parameters, get_optimizable_parameters
from pytorch_toolbelt.utils import fs, torch_utils
from pytorch_toolbelt.utils.catalyst import (
    ShowPolarBatchesCallback,
    report_checkpoint,
    clean_checkpoint,
    HyperParametersCallback,
)
from pytorch_toolbelt.utils.random import set_manual_seed
from pytorch_toolbelt.utils.torch_utils import count_parameters, transfer_weights
from torch import nn
from torch.utils.data import DataLoader

from alaska2 import *


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-acc", "--accumulation-steps", type=int, default=1, help="Number of batches to process")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--obliterate", type=float, default=0, help="Change of obliteration")
    parser.add_argument("-nid", "--negative-image-dir", type=str, default=None, help="Change of obliteration")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("-dd", "--data-dir", type=str, default=os.environ.get("KAGGLE_2020_ALASKA2"))
    parser.add_argument("-m", "--model", type=str, default="resnet34", help="")
    parser.add_argument("-b", "--batch-size", type=int, default=16, help="Batch Size during training, e.g. -b 64")
    parser.add_argument(
        "-wbs", "--warmup-batch-size", type=int, default=None, help="Batch Size during training, e.g. -b 64"
    )
    parser.add_argument("-e", "--epochs", type=int, default=100, help="Epoch to run")
    parser.add_argument(
        "-es", "--early-stopping", type=int, default=None, help="Maximum number of epochs without improvement"
    )
    parser.add_argument("-fe", "--freeze-encoder", action="store_true", help="Freeze encoder parameters for N epochs")
    parser.add_argument("-lr", "--learning-rate", type=float, default=1e-3, help="Initial learning rate")

    parser.add_argument(
        "-l", "--modification-flag-loss", type=str, default=None, action="append", nargs="+"  # [["ce", 1.0]],
    )
    parser.add_argument(
        "--modification-type-loss", type=str, default=None, action="append", nargs="+"  # [["ce", 1.0]],
    )
    parser.add_argument("--embedding-loss", type=str, default=None, action="append", nargs="+")  # [["ce", 1.0]],
    parser.add_argument("--feature-maps-loss", type=str, default=None, action="append", nargs="+")  # [["ce", 1.0]],
    parser.add_argument("--mask-loss", type=str, default=None, action="append", nargs="+")  # [["ce", 1.0]],
    parser.add_argument("--bits-loss", type=str, default=None, action="append", nargs="+")  # [["ce", 1.0]],

    parser.add_argument("-o", "--optimizer", default="RAdam", help="Name of the optimizer")
    parser.add_argument(
        "-c", "--checkpoint", type=str, default=None, help="Checkpoint filename to use as initial model weights"
    )
    parser.add_argument("-w", "--workers", default=8, type=int, help="Num workers")
    parser.add_argument("-a", "--augmentations", default="safe", type=str, help="Level of image augmentations")
    parser.add_argument("--transfer", default=None, type=str, help="")
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--mixup", action="store_true")
    parser.add_argument("--cutmix", action="store_true")
    parser.add_argument("--tsa", action="store_true")
    parser.add_argument("--fold", default=None, type=int)
    parser.add_argument("-s", "--scheduler", default=None, type=str, help="")
    parser.add_argument("-x", "--experiment", default=None, type=str, help="")
    parser.add_argument("-d", "--dropout", default=None, type=float, help="Dropout before head layer")
    parser.add_argument(
        "--warmup", default=0, type=int, help="Number of warmup epochs with reduced LR on encoder parameters"
    )
    parser.add_argument(
        "--fine-tune", default=0, type=int, help="Number of warmup epochs with reduced LR on encoder parameters"
    )
    parser.add_argument("-wd", "--weight-decay", default=0, type=float, help="L2 weight decay")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--freeze-bn", action="store_true")

    args = parser.parse_args()
    set_manual_seed(args.seed)

    assert (
        args.modification_flag_loss or args.modification_type_loss or args.embedding_loss
    ), "At least one of losses must be set"

    modification_flag_loss = args.modification_flag_loss
    modification_type_loss = args.modification_type_loss
    embedding_loss = args.embedding_loss
    feature_maps_loss = args.feature_maps_loss
    mask_loss = args.mask_loss
    bits_loss = args.bits_loss

    freeze_encoder = args.freeze_encoder
    data_dir = args.data_dir
    cache = args.cache
    num_workers = args.workers
    num_epochs = args.epochs
    learning_rate = args.learning_rate
    model_name: str = args.model
    optimizer_name = args.optimizer
    image_size = (512, 512)
    fast = args.fast
    augmentations = args.augmentations
    fp16 = args.fp16
    scheduler_name = args.scheduler
    experiment = args.experiment
    dropout = args.dropout
    verbose = args.verbose
    warmup = args.warmup
    show = args.show
    accumulation_steps = args.accumulation_steps
    weight_decay = args.weight_decay
    fold = args.fold
    balance = args.balance
    freeze_bn = args.freeze_bn
    train_batch_size = args.batch_size
    mixup = args.mixup
    cutmix = args.cutmix
    tsa = args.tsa
    fine_tune = args.fine_tune
    obliterate_p = args.obliterate
    negative_image_dir = args.negative_image_dir
    warmup_batch_size = args.warmup_batch_size or args.batch_size

    # Compute batch size for validation
    valid_batch_size = train_batch_size
    run_train = num_epochs > 0

    custom_model_kwargs = {}
    if dropout is not None:
        custom_model_kwargs["dropout"] = float(dropout)

    if embedding_loss is not None:
        custom_model_kwargs["need_embedding"] = True

    model: nn.Module = get_model(model_name, **custom_model_kwargs).cuda()
    required_features = model.required_features

    if mask_loss is not None:
        required_features.append(INPUT_TRUE_MODIFICATION_MASK)

    if args.transfer:
        transfer_checkpoint = fs.auto_file(args.transfer)
        print("Transferring weights from model checkpoint", transfer_checkpoint)
        checkpoint = load_checkpoint(transfer_checkpoint)
        pretrained_dict = checkpoint["model_state_dict"]

        transfer_weights(model, pretrained_dict)

    if args.checkpoint:
        checkpoint = load_checkpoint(fs.auto_file(args.checkpoint))
        unpack_checkpoint(checkpoint, model=model)

        print("Loaded model weights from:", args.checkpoint)
        report_checkpoint(checkpoint)

    if freeze_bn:
        from pytorch_toolbelt.optimization.functional import freeze_model

        freeze_model(model, freeze_bn=True)
        print("Freezing bn params")

    main_metric = "loss"
    main_metric_minimize = True

    current_time = datetime.now().strftime("%b%d_%H_%M")
    checkpoint_prefix = f"{current_time}_{args.model}_fold{fold}"

    if fp16:
        checkpoint_prefix += "_fp16"

    if fast:
        checkpoint_prefix += "_fast"

    if mixup:
        checkpoint_prefix += "_mixup"

    if cutmix:
        checkpoint_prefix += "_cutmix"

    if experiment is not None:
        checkpoint_prefix = experiment

    log_dir = os.path.join("runs", checkpoint_prefix)
    os.makedirs(log_dir, exist_ok=False)

    config_fname = os.path.join(log_dir, f"{checkpoint_prefix}.json")
    with open(config_fname, "w") as f:
        train_session_args = vars(args)
        f.write(json.dumps(train_session_args, indent=2))

    default_callbacks = []

    if show:
        default_callbacks += [ShowPolarBatchesCallback(draw_predictions, metric="loss", minimize=True)]

    # Pretrain/warmup
    if warmup:
        train_ds, valid_ds, train_sampler = get_datasets(
            data_dir=data_dir,
            augmentation=augmentations,
            balance=balance,
            fast=fast,
            fold=fold,
            features=required_features,
            obliterate_p=0,
        )

        criterions_dict, loss_callbacks = get_criterions(
            modification_flag=modification_flag_loss,
            modification_type=modification_type_loss,
            embedding_loss=embedding_loss,
            mask_loss=mask_loss,
            bits_loss=bits_loss,
            feature_maps_loss=feature_maps_loss,
            num_epochs=warmup,
            mixup=mixup,
            cutmix=cutmix,
            tsa=tsa,
        )

        callbacks = (
            default_callbacks
            + loss_callbacks
            + [
                OptimizerCallback(accumulation_steps=accumulation_steps, decouple_weight_decay=False),
                HyperParametersCallback(
                    hparam_dict={
                        "model": model_name,
                        "scheduler": scheduler_name,
                        "optimizer": optimizer_name,
                        "augmentations": augmentations,
                        "size": image_size[0],
                        "weight_decay": weight_decay,
                    }
                ),
            ]
        )

        loaders = collections.OrderedDict()
        loaders["train"] = DataLoader(
            train_ds,
            batch_size=warmup_batch_size,
            num_workers=num_workers,
            pin_memory=True,
            drop_last=True,
            shuffle=train_sampler is None,
            sampler=train_sampler,
        )

        loaders["valid"] = DataLoader(valid_ds, batch_size=warmup_batch_size, num_workers=num_workers, pin_memory=True)

        if freeze_encoder:
            from pytorch_toolbelt.optimization.functional import freeze_model

            freeze_model(model.encoder, freeze_parameters=True, freeze_bn=None)

        optimizer = get_optimizer(
            "Ranger", get_optimizable_parameters(model), weight_decay=weight_decay, learning_rate=3e-4
        )
        scheduler = None

        print("Train session    :", checkpoint_prefix)
        print("  FP16 mode      :", fp16)
        print("  Fast mode      :", args.fast)
        print("  Epochs         :", num_epochs)
        print("  Workers        :", num_workers)
        print("  Data dir       :", data_dir)
        print("  Log dir        :", log_dir)
        print("  Cache          :", cache)
        print("Data              ")
        print("  Augmentations  :", augmentations)
        print("  Negative images:", negative_image_dir)
        print("  Train size     :", len(loaders["train"]), "batches", len(train_ds), "samples")
        print("  Valid size     :", len(loaders["valid"]), "batches", len(valid_ds), "samples")
        print("  Image size     :", image_size)
        print("  Balance        :", balance)
        print("  Mixup          :", mixup)
        print("  CutMix         :", cutmix)
        print("  TSA            :", tsa)
        print("Model            :", model_name)
        print("  Parameters     :", count_parameters(model))
        print("  Dropout        :", dropout, "(Non-default)" if dropout is not None else "")
        print("Optimizer        :", optimizer_name)
        print("  Learning rate  :", learning_rate)
        print("  Weight decay   :", weight_decay)
        print("  Scheduler      :", scheduler_name)
        print("  Batch sizes    :", train_batch_size, valid_batch_size)
        print("Losses            ")
        print("  Flag           :", modification_flag_loss)
        print("  Type           :", modification_type_loss)
        print("  Embedding      :", embedding_loss)
        print("  Feature maps   :", feature_maps_loss)
        print("  Mask           :", mask_loss)
        print("  Bits           :", bits_loss)

        runner = SupervisedRunner(input_key=required_features, output_key=None)
        runner.train(
            fp16=fp16,
            model=model,
            criterion=criterions_dict,
            optimizer=optimizer,
            scheduler=scheduler,
            callbacks=callbacks,
            loaders=loaders,
            logdir=os.path.join(log_dir, "warmup"),
            num_epochs=warmup,
            verbose=verbose,
            main_metric=main_metric,
            minimize_metric=main_metric_minimize,
            checkpoint_data={"cmd_args": vars(args)},
        )

        del optimizer, loaders, runner, callbacks

        best_checkpoint = os.path.join(log_dir, "warmup", "checkpoints", "best.pth")
        model_checkpoint = os.path.join(log_dir, f"{checkpoint_prefix}_warmup.pth")
        clean_checkpoint(best_checkpoint, model_checkpoint)

        # Restore state of best model
        # unpack_checkpoint(load_checkpoint(model_checkpoint), model=model)

        torch.cuda.empty_cache()
        gc.collect()

    if run_train:
        train_ds, valid_ds, train_sampler = get_datasets(
            data_dir=data_dir,
            augmentation=augmentations,
            balance=balance,
            fast=fast,
            fold=fold,
            features=required_features,
            obliterate_p=obliterate_p,
        )

        if negative_image_dir:
            negatives_ds = get_negatives_ds(
                negative_image_dir, fold=fold, features=required_features, max_images=16536
            )
            train_ds = train_ds + negatives_ds
            train_sampler = None  # TODO: Add proper support of sampler
            print("Adding", len(negatives_ds), "negative samples to training set")

        criterions_dict, loss_callbacks = get_criterions(
            modification_flag=modification_flag_loss,
            modification_type=modification_type_loss,
            embedding_loss=embedding_loss,
            feature_maps_loss=feature_maps_loss,
            mask_loss=mask_loss,
            bits_loss=bits_loss,
            num_epochs=num_epochs,
            mixup=mixup,
            cutmix=cutmix,
            tsa=tsa,
        )

        callbacks = (
            default_callbacks
            + loss_callbacks
            + [
                OptimizerCallback(accumulation_steps=accumulation_steps, decouple_weight_decay=False),
                HyperParametersCallback(
                    hparam_dict={
                        "model": model_name,
                        "scheduler": scheduler_name,
                        "optimizer": optimizer_name,
                        "augmentations": augmentations,
                        "size": image_size[0],
                        "weight_decay": weight_decay,
                    }
                ),
            ]
        )

        loaders = collections.OrderedDict()
        loaders["train"] = DataLoader(
            train_ds,
            batch_size=train_batch_size,
            num_workers=num_workers,
            pin_memory=True,
            drop_last=True,
            shuffle=train_sampler is None,
            sampler=train_sampler,
        )

        loaders["valid"] = DataLoader(valid_ds, batch_size=valid_batch_size, num_workers=num_workers, pin_memory=True)

        print("Train session    :", checkpoint_prefix)
        print("  FP16 mode      :", fp16)
        print("  Fast mode      :", args.fast)
        print("  Epochs         :", num_epochs)
        print("  Workers        :", num_workers)
        print("  Data dir       :", data_dir)
        print("  Log dir        :", log_dir)
        print("  Cache          :", cache)
        print("Data              ")
        print("  Augmentations  :", augmentations)
        print("  Obliterate (%) :", obliterate_p)
        print("  Negative images:", negative_image_dir)
        print("  Train size     :", len(loaders["train"]), "batches", len(train_ds), "samples")
        print("  Valid size     :", len(loaders["valid"]), "batches", len(valid_ds), "samples")
        print("  Image size     :", image_size)
        print("  Balance        :", balance)
        print("  Mixup          :", mixup)
        print("  CutMix         :", cutmix)
        print("  TSA            :", tsa)
        print("Model            :", model_name)
        print("  Parameters     :", count_parameters(model))
        print("  Dropout        :", dropout)
        print("Optimizer        :", optimizer_name)
        print("  Learning rate  :", learning_rate)
        print("  Weight decay   :", weight_decay)
        print("  Scheduler      :", scheduler_name)
        print("  Batch sizes    :", train_batch_size, valid_batch_size)
        print("Losses            ")
        print("  Flag           :", modification_flag_loss)
        print("  Type           :", modification_type_loss)
        print("  Embedding      :", embedding_loss)
        print("  Feature maps   :", feature_maps_loss)
        print("  Mask           :", mask_loss)
        print("  Bits           :", bits_loss)

        optimizer = get_optimizer(
            optimizer_name, get_optimizable_parameters(model), learning_rate=learning_rate, weight_decay=weight_decay
        )
        scheduler = get_scheduler(
            scheduler_name, optimizer, lr=learning_rate, num_epochs=num_epochs, batches_in_epoch=len(loaders["train"])
        )
        if isinstance(scheduler, CyclicLR):
            callbacks += [SchedulerCallback(mode="batch")]

        # model training
        runner = SupervisedRunner(input_key=required_features, output_key=None)
        runner.train(
            fp16=fp16,
            model=model,
            criterion=criterions_dict,
            optimizer=optimizer,
            scheduler=scheduler,
            callbacks=callbacks,
            loaders=loaders,
            logdir=os.path.join(log_dir, "main"),
            num_epochs=num_epochs,
            verbose=verbose,
            main_metric=main_metric,
            minimize_metric=main_metric_minimize,
            checkpoint_data={"cmd_args": vars(args)},
        )

        del optimizer, loaders, runner, callbacks

        best_checkpoint = os.path.join(log_dir, "main", "checkpoints", "best.pth")
        model_checkpoint = os.path.join(log_dir, f"{checkpoint_prefix}.pth")

        # Restore state of best model
        clean_checkpoint(best_checkpoint, model_checkpoint)
        # unpack_checkpoint(load_checkpoint(model_checkpoint), model=model)

        torch.cuda.empty_cache()
        gc.collect()

    if fine_tune:
        train_ds, valid_ds, train_sampler = get_datasets(
            data_dir=data_dir,
            augmentation="light",
            balance=balance,
            fast=fast,
            fold=fold,
            features=required_features,
            obliterate_p=obliterate_p,
        )

        criterions_dict, loss_callbacks = get_criterions(
            modification_flag=modification_flag_loss,
            modification_type=modification_type_loss,
            embedding_loss=embedding_loss,
            feature_maps_loss=feature_maps_loss,
            mask_loss=mask_loss,
            bits_loss=bits_loss,
            num_epochs=fine_tune,
            mixup=False,
            cutmix=False,
            tsa=False,
        )

        callbacks = (
            default_callbacks
            + loss_callbacks
            + [
                OptimizerCallback(accumulation_steps=accumulation_steps, decouple_weight_decay=False),
                HyperParametersCallback(
                    hparam_dict={
                        "model": model_name,
                        "scheduler": scheduler_name,
                        "optimizer": optimizer_name,
                        "augmentations": augmentations,
                        "size": image_size[0],
                        "weight_decay": weight_decay,
                    }
                ),
            ]
        )

        loaders = collections.OrderedDict()
        loaders["train"] = DataLoader(
            train_ds,
            batch_size=train_batch_size,
            num_workers=num_workers,
            pin_memory=True,
            drop_last=True,
            shuffle=train_sampler is None,
            sampler=train_sampler,
        )

        loaders["valid"] = DataLoader(valid_ds, batch_size=valid_batch_size, num_workers=num_workers, pin_memory=True)

        print("Train session    :", checkpoint_prefix)
        print("  FP16 mode      :", fp16)
        print("  Fast mode      :", args.fast)
        print("  Epochs         :", num_epochs)
        print("  Workers        :", num_workers)
        print("  Data dir       :", data_dir)
        print("  Log dir        :", log_dir)
        print("  Cache          :", cache)
        print("Data              ")
        print("  Augmentations  :", augmentations)
        print("  Obliterate (%) :", obliterate_p)
        print("  Negative images:", negative_image_dir)
        print("  Train size     :", len(loaders["train"]), "batches", len(train_ds), "samples")
        print("  Valid size     :", len(loaders["valid"]), "batches", len(valid_ds), "samples")
        print("  Image size     :", image_size)
        print("  Balance        :", balance)
        print("  Mixup          :", mixup)
        print("  CutMix         :", cutmix)
        print("  TSA            :", tsa)
        print("Model            :", model_name)
        print("  Parameters     :", count_parameters(model))
        print("  Dropout        :", dropout)
        print("Optimizer        :", optimizer_name)
        print("  Learning rate  :", learning_rate)
        print("  Weight decay   :", weight_decay)
        print("  Scheduler      :", scheduler_name)
        print("  Batch sizes    :", train_batch_size, valid_batch_size)
        print("Losses            ")
        print("  Flag           :", modification_flag_loss)
        print("  Type           :", modification_type_loss)
        print("  Embedding      :", embedding_loss)
        print("  Feature maps   :", feature_maps_loss)
        print("  Mask           :", mask_loss)
        print("  Bits           :", bits_loss)

        optimizer = get_optimizer(
            "SGD", get_optimizable_parameters(model), learning_rate=learning_rate, weight_decay=weight_decay
        )
        scheduler = get_scheduler(
            "cos", optimizer, lr=learning_rate, num_epochs=fine_tune, batches_in_epoch=len(loaders["train"])
        )
        if isinstance(scheduler, CyclicLR):
            callbacks += [SchedulerCallback(mode="batch")]

        # model training
        runner = SupervisedRunner(input_key=required_features, output_key=None)
        runner.train(
            fp16=fp16,
            model=model,
            criterion=criterions_dict,
            optimizer=optimizer,
            scheduler=scheduler,
            callbacks=callbacks,
            loaders=loaders,
            logdir=os.path.join(log_dir, "finetune"),
            num_epochs=fine_tune,
            verbose=verbose,
            main_metric=main_metric,
            minimize_metric=main_metric_minimize,
            checkpoint_data={"cmd_args": vars(args)},
        )

        best_checkpoint = os.path.join(log_dir, "finetune", "checkpoints", "best.pth")
        model_checkpoint = os.path.join(log_dir, f"{checkpoint_prefix}_finetune.pth")

        clean_checkpoint(best_checkpoint, model_checkpoint)
        unpack_checkpoint(load_checkpoint(model_checkpoint), model=model)

        del optimizer, loaders, runner, callbacks


if __name__ == "__main__":
    main()
