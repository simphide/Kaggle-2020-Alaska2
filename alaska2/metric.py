import itertools
from typing import Callable

import torch
from catalyst.dl import Callback, RunnerState, CallbackOrder
from pytorch_toolbelt.utils import plot_confusion_matrix, render_figure_to_tensor
from pytorch_toolbelt.utils.catalyst import get_tensorboard_logger
from pytorch_toolbelt.utils.distributed import all_gather
from pytorch_toolbelt.utils.torch_utils import to_numpy
from sklearn import metrics
import numpy as np
import torch.nn.functional as F
from sklearn.metrics import roc_curve

from .dataset import INPUT_IMAGE_QF_KEY

__all__ = [
    "CompetitionMetricCallback",
    "alaska_weighted_auc",
    "shaky_wauc",
    "OutputDistributionCallback",
    "binary_logits_to_probas",
    "classifier_logits_to_probas",
    "embedding_to_probas",
    "CompetitionMetricCallbackFromMask",
]


#
# def anokas_alaska_weighted_auc(y_true, y_pred, **kwargs):
#     try:
#         tpr_thresholds = [0.0, 0.4, 1.0]
#         weights = [2, 1]
#
#         fpr, tpr, thresholds = metrics.roc_curve(y_true, y_pred, pos_label=1)
#
#         # size of subsets
#         areas = np.array(tpr_thresholds[1:]) - np.array(tpr_thresholds[:-1])
#
#         # The total area is normalized by the sum of weights such that the final weighted AUC is between 0 and 1.
#         normalization = np.dot(areas, weights)
#
#         competition_metric = 0
#         for idx, weight in enumerate(weights):
#             y_min = tpr_thresholds[idx]
#             y_max = tpr_thresholds[idx + 1]
#             mask = (y_min < tpr) & (tpr <= y_max)
#
#             if mask.sum() == 0:
#                 continue
#
#             x_padding = np.linspace(fpr[mask][-1], 1, 100)
#             x = np.concatenate([fpr[mask], x_padding])
#             y = np.concatenate([tpr[mask], [y_max] * len(x_padding)])
#
#             y = y - y_min  # normalize such that curve starts at y=0
#             score = metrics.auc(x, y)
#             submetric = score * weight
#             best_subscore = (y_max - y_min) * weight
#             competition_metric += submetric
#
#         return competition_metric / normalization
#     except Exception as e:
#         print(e)
#         print("Returning 0 from anokas_alaska_weighted_auc")
#         return 0

#
# def weighted_roc_auc_score(ytrue, ypred, **kwargs):
#     fpr, tpr, _ = roc_curve(ytrue, ypred)
#
#     # the curve
#     y = (tpr[1:] + tpr[:-1]) / 2
#
#     # tpr threshold
#     # a = (y < 0.4).astype(np.float32)  # inclusive or exclusive ?
#     a = (y <= 0.4).astype(np.float32)  # inclusive or exclusive ?
#
#     # curve under tpr_threshold
#     y1 = y * a
#     y1 = y1 + y1.max() * (1 - a)
#
#     # curve above tpr_threshold
#     y2 = y - y1
#
#     # weighted sum
#     yy = 2 * y1 + y2
#
#     # make roc curve great again.
#     # bugged: yy = (yy - yy.min()) / (yy.max() - yy.min())
#     yy = yy / yy.max()
#
#     # sum to area
#     return ((fpr[1:] - fpr[:-1]) * yy).sum()


def wauc(y_true, y_pred):
    y_true = np.array(y_true)
    fpr, tpr, thresholds = roc_curve((y_true > 0).astype(int), y_pred, drop_intermediate=False)
    tpr_thresholds = [0.0, 0.4, 1.0]
    weights = [2.0, 1.0]
    auc_x = 0.0
    for idx in range(len(tpr_thresholds) - 1):
        mask = tpr >= tpr_thresholds[idx]
        x = fpr[mask]
        y = tpr[mask]
        mask = y > tpr_thresholds[idx + 1]
        y[mask] = tpr_thresholds[idx + 1]
        y = y - tpr_thresholds[idx]
        auc_x = auc_x + weights[idx] * np.trapz(y, x)
    areas = np.array(tpr_thresholds[1:]) - np.array(tpr_thresholds[:-1])
    normalization = np.dot(areas, np.array(weights))
    return auc_x / normalization


# EXPECTED_TEST_DISTRIBUTION = [0.25, 0.25, 0.25, 0.25]
EXPECTED_TEST_DISTRIBUTION = [0.5, 0.5 / 3, 0.5 / 3, 0.5 / 3]


def shaky_wauc(
    y_true, y_pred, n: int = 1000, k=5000, j=5000, distribution=EXPECTED_TEST_DISTRIBUTION, return_scores=False
):
    samples = (np.array(distribution) * k).astype(int)

    y_true = np.array(y_true, dtype=int)
    assert len(np.unique(y_true)) > 2
    y_pred = np.array(y_pred, dtype=np.float32)
    scores = []
    for _ in range(n):
        sample_y_true = []
        sample_y_pred = []
        for class_index, num_samples in enumerate(samples):
            y_true_i = y_true[y_true == class_index]
            y_pred_i = y_pred[y_true == class_index]
            indexes = np.random.choice(np.arange(len(y_true_i)), num_samples, replace=False)
            sample_y_true.extend(y_true_i[indexes])
            sample_y_pred.extend(y_pred_i[indexes])

        sample_y_true = np.array(sample_y_true)
        sample_y_pred = np.array(sample_y_pred)
        if j != k:
            public_lb_indexes = np.random.choice(np.arange(len(sample_y_true)), 1000, replace=False)
            sample_y_true = sample_y_true[public_lb_indexes]
            sample_y_pred = sample_y_pred[public_lb_indexes]

        wauc_score = alaska_weighted_auc(sample_y_true, sample_y_pred)
        scores.append(wauc_score)

    if return_scores:
        return scores

    return np.mean(scores)


def shaky_wauc_public(
    y_true, y_pred, n: int = 1000, k=5000, distribution=EXPECTED_TEST_DISTRIBUTION, return_scores=False
):
    """
    Compute the "shaky" wAUC metric by bootstraping 5k samples from the (y_true, y_pred) N times, using expected
    distribution of targets in test.
    Additionaly it picks 1000 random elements from bootstraped 5k samples and compute wAUC on it.
    This approach tries to estimate public LB
    """
    return shaky_wauc(y_true, y_pred, n, k, 1000, distribution, return_scores)


alaska_weighted_auc = wauc


def binary_logits_to_probas(x):
    return x.sigmoid().squeeze(1)


def classifier_logits_to_probas(x):
    return x.softmax(dim=1)[:, 1:].sum(dim=1)


def log_plus_one(x):
    return torch.log(x + 1)


def embedding_to_probas(x: torch.Tensor):
    background = torch.zeros(x.size(1), device=x.device, dtype=x.dtype)
    background[0] = 1

    predicted = 1 - F.cosine_similarity(x, background.unsqueeze(0), dim=1).pow_(2)
    return predicted


class CompetitionMetricCallback(Callback):
    def __init__(self, input_key: str, output_key: str, output_activation: Callable, prefix="auc", class_names=None):
        super().__init__(CallbackOrder.Metric)
        self.prefix = prefix
        self.input_key = input_key
        self.output_key = output_key
        self.true_labels = []
        self.pred_labels = []
        self.quality_factors = []
        self.output_activation = output_activation
        if class_names is None:
            class_names = ["Cover", "JMiPOD", "JUNIWARD", "UERD"]

        self.class_names = class_names

    def on_loader_start(self, state: RunnerState):
        self.true_labels = []
        self.pred_labels = []
        self.quality_factors = []

    @torch.no_grad()
    def on_batch_end(self, state: RunnerState):
        output = self.output_activation(state.output[self.output_key].detach().cpu())
        if INPUT_IMAGE_QF_KEY in state.input:
            self.quality_factors.extend(to_numpy(state.input[INPUT_IMAGE_QF_KEY]).flatten())
        self.true_labels.extend(to_numpy(state.input[self.input_key]).flatten())
        self.pred_labels.extend(to_numpy(output).flatten())

    def on_loader_end(self, state: RunnerState):
        true_labels = np.array(self.true_labels)
        pred_labels = np.array(self.pred_labels)
        quality_factors = np.array(self.quality_factors)

        true_labels = all_gather(true_labels)
        true_labels = np.concatenate(true_labels)

        pred_labels = all_gather(pred_labels)
        pred_labels = np.concatenate(pred_labels)

        quality_factors = all_gather(quality_factors)
        quality_factors = np.concatenate(quality_factors)

        true_labels_b = (true_labels > 0).astype(int)
        # Just ensure true_labels are 0,1
        score = alaska_weighted_auc(true_labels_b, pred_labels)
        state.metrics.epoch_values[state.loader_name][self.prefix] = float(score)

        logger = get_tensorboard_logger(state)
        logger.add_pr_curve(self.prefix, true_labels_b, pred_labels)

        # Compute
        if len(quality_factors) > 0:
            score_75 = alaska_weighted_auc(true_labels_b[quality_factors == 0], pred_labels[quality_factors == 0])
            score_90 = alaska_weighted_auc(true_labels_b[quality_factors == 1], pred_labels[quality_factors == 1])
            score_95 = alaska_weighted_auc(true_labels_b[quality_factors == 2], pred_labels[quality_factors == 2])

            state.metrics.epoch_values[state.loader_name][self.prefix + "/qf_75"] = float(score_75)
            state.metrics.epoch_values[state.loader_name][self.prefix + "/qf_90"] = float(score_90)
            state.metrics.epoch_values[state.loader_name][self.prefix + "/qf_95"] = float(score_95)

            score_mask = np.zeros((3, 3))
            for qf in [0, 1, 2]:
                for target in range(len(self.class_names) - 1):
                    mask = (quality_factors == qf) & ((true_labels == 0) | (true_labels == target + 1))
                    score = alaska_weighted_auc(true_labels_b[mask], pred_labels[mask])
                    score_mask[qf, target] = score

            fig = self.plot_matrix(
                score_mask,
                figsize=(8, 8),
                x_names=self.class_names[1:],
                y_names=["75", "90", "95"],
                normalize=False,
                noshow=True,
            )
            fig = render_figure_to_tensor(fig)
            logger.add_image(f"{self.prefix}/matrix", fig, global_step=state.step)

    @staticmethod
    def plot_matrix(
        cm, x_names, y_names, figsize=(16, 16), normalize=False, title="AUC matrix", fname=None, noshow=False
    ):
        """Render the confusion matrix and return matplotlib's figure with it.
        Normalization can be applied by setting `normalize=True`.
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        cmap = plt.cm.Oranges

        f = plt.figure(figsize=figsize)
        plt.title(title)
        plt.imshow(cm, interpolation="nearest", cmap=cmap)

        plt.xticks(np.arange(len(x_names)), x_names, rotation=45, ha="right")
        plt.yticks(np.arange(len(y_names)), y_names)

        fmt = ".2f"
        thresh = (cm.max() + cm.min()) / 2
        for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
            plt.text(
                j,
                i,
                format(cm[i, j], fmt),
                horizontalalignment="center",
                color="white" if cm[i, j] > thresh else "black",
            )

        plt.tight_layout()
        plt.ylabel("True label")
        plt.xlabel("Predicted label")

        if fname is not None:
            plt.savefig(fname=fname)

        if not noshow:
            plt.show()

        return f


def target_from_mask(x):
    return (x > 0).sum(dim=(2, 3))


def probas_from_mask(x):
    return x.sigmoid().sum(dim=(2, 3))


class CompetitionMetricCallbackFromMask(CompetitionMetricCallback):
    def __init__(
        self,
        input_key: str,
        output_key: str,
        prefix: str,
        input_activation=target_from_mask,
        output_activation=probas_from_mask,
    ):
        super().__init__(input_key, output_key, output_activation, prefix)
        self.input_activation = input_activation

    def on_loader_start(self, state: RunnerState):
        self.true_labels = []
        self.pred_labels = []

    @torch.no_grad()
    def on_batch_end(self, state: RunnerState):
        target = self.input_activation(state.input[self.input_key].detach().cpu())
        output = self.output_activation(state.output[self.output_key].detach().cpu())

        self.true_labels.extend(to_numpy(target).flatten())
        self.pred_labels.extend(to_numpy(output).flatten())

    def on_loader_end(self, state: RunnerState):
        true_labels = np.array(self.true_labels)
        pred_labels = np.array(self.pred_labels)

        true_labels = all_gather(true_labels)
        true_labels = np.concatenate(true_labels)

        pred_labels = all_gather(pred_labels)
        pred_labels = np.concatenate(pred_labels)

        true_labels_b = (true_labels > 0).astype(int)
        # Just ensure true_labels are 0,1
        score = alaska_weighted_auc(true_labels_b, pred_labels)
        state.metrics.epoch_values[state.loader_name][self.prefix] = float(score)

        logger = get_tensorboard_logger(state)
        logger.add_pr_curve(self.prefix, true_labels_b, pred_labels)


class OutputDistributionCallback(Callback):
    def __init__(self, input_key: str, output_key: str, output_activation: Callable, prefix="distribution"):
        super().__init__(CallbackOrder.Metric)
        self.prefix = prefix
        self.input_key = input_key
        self.output_key = output_key
        self.true_labels = []
        self.pred_labels = []
        self.output_activation = output_activation

    def on_loader_start(self, state: RunnerState):
        self.true_labels = []
        self.pred_labels = []

    @torch.no_grad()
    def on_batch_end(self, state: RunnerState):
        output = state.output[self.output_key].detach()

        self.true_labels.extend(to_numpy(state.input[self.input_key]).flatten())
        self.pred_labels.extend(to_numpy(self.output_activation(output)).flatten())

    def on_loader_end(self, state: RunnerState):
        true_labels = np.array(self.true_labels)
        pred_probas = np.array(self.pred_labels)

        if len(np.unique(true_labels) > 2):
            true_labels = true_labels > 0.5
        else:
            true_labels = true_labels.astype(np.bool)

        logger = get_tensorboard_logger(state)
        logger.add_histogram(self.prefix + "/neg", pred_probas[true_labels == False], state.epoch)
        logger.add_histogram(self.prefix + "/pos", pred_probas[true_labels == True], state.epoch)
