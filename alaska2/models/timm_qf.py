import numpy as np
import torch
import torch.nn.functional as F
from pytorch_toolbelt.modules import Normalize, GlobalAvgPool2d
from timm.models import efficientnet, resnet
from torch import nn

from alaska2.dataset import (
    OUTPUT_PRED_MODIFICATION_FLAG,
    OUTPUT_PRED_MODIFICATION_TYPE,
    INPUT_IMAGE_KEY,
    INPUT_IMAGE_QF_KEY,
)

__all__ = [
    "ImageAndQFModel",
    "rgb_qf_tf_efficientnet_b2_ns",
    "rgb_qf_tf_efficientnet_b6_ns",
    "rgb_qf_swsl_resnext101_32x8d",
]


class ImageAndQFModel(nn.Module):
    def __init__(
        self,
        encoder,
        num_classes,
        dropout=0,
        mean=[0.3914976, 0.44266784, 0.46043398],
        std=[0.17819773, 0.17319807, 0.18128773],
    ):
        super().__init__()
        self.encoder = encoder
        max_pixel_value = 255
        self.rgb_bn = Normalize(np.array(mean) * max_pixel_value, np.array(std) * max_pixel_value)
        self.pool = GlobalAvgPool2d(flatten=True)
        self.drop = nn.Dropout(dropout)

        # Recombination of embedding and quality factor
        self.fc1 = nn.Sequential(nn.Linear(encoder.num_features + 3, encoder.num_features), nn.ReLU())

        self.type_classifier = nn.Linear(encoder.num_features, num_classes)
        self.flag_classifier = nn.Linear(encoder.num_features, 1)

    def forward(self, **kwargs):
        x = kwargs[INPUT_IMAGE_KEY]
        qf = F.one_hot(kwargs[INPUT_IMAGE_QF_KEY], 3)

        x = self.rgb_bn(x.float())
        x = self.encoder.forward_features(x)
        x = self.pool(x)

        x = torch.cat([x, qf.type_as(x)], dim=1)
        x = self.fc1(x)

        return {
            OUTPUT_PRED_MODIFICATION_FLAG: self.flag_classifier(self.drop(x)),
            OUTPUT_PRED_MODIFICATION_TYPE: self.type_classifier(self.drop(x)),
        }

    @property
    def required_features(self):
        return [INPUT_IMAGE_KEY, INPUT_IMAGE_QF_KEY]


# Model zoo


def rgb_qf_tf_efficientnet_b2_ns(num_classes=4, pretrained=True, dropout=0):
    encoder = efficientnet.tf_efficientnet_b2_ns(pretrained=pretrained)
    del encoder.classifier

    return ImageAndQFModel(encoder, num_classes=num_classes, dropout=dropout)


def rgb_qf_tf_efficientnet_b6_ns(num_classes=4, pretrained=True, dropout=0):
    encoder = efficientnet.tf_efficientnet_b6_ns(pretrained=pretrained)
    del encoder.classifier

    return ImageAndQFModel(encoder, num_classes=num_classes, dropout=dropout)


def rgb_qf_swsl_resnext101_32x8d(num_classes=4, pretrained=True, dropout=0):
    encoder = resnet.swsl_resnext101_32x8d(pretrained=pretrained)
    del encoder.fc

    return ImageAndQFModel(
        encoder,
        num_classes=num_classes,
        dropout=dropout,
        mean=encoder.default_cfg["mean"],
        std=encoder.default_cfg["std"],
    )
