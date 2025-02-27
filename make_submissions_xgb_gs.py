import os

# For reading, visualizing, and preprocessing data
from multiprocessing import Pool
from typing import List

import argparse
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from pytorch_toolbelt.utils import fs
from scipy.stats import entropy
from skimage.morphology import square
from sklearn.metrics import make_scorer
from sklearn.model_selection import GroupKFold, RandomizedSearchCV
from sklearn.preprocessing import StandardScaler
from tqdm import tqdm
from xgboost import XGBClassifier

from alaska2 import get_holdout, INPUT_IMAGE_KEY, get_test_dataset
from alaska2.dataset import decode_bgr_from_dct, INDEX_TO_METHOD
from alaska2.metric import alaska_weighted_auc
from alaska2.submissions import get_x_y_for_stacking
from submissions.eval_tta import get_predictions_csv
from submissions.make_submissions_averaging import compute_checksum_v2


def compute_features_proc(image_fname):
    dct_file = fs.change_extension(image_fname, ".npz")
    image = 2 * (decode_bgr_from_dct(dct_file) / 140 - 0.5)

    entropy_per_channel = [
        entropy(image[..., 0].flatten()),
        entropy(image[..., 1].flatten()),
        entropy(image[..., 2].flatten()),
    ]

    f = [
        image[..., 0].mean(),
        image[..., 1].mean(),
        image[..., 2].mean(),
        image[..., 0].std(),
        image[..., 1].std(),
        image[..., 2].std(),
        entropy_per_channel[0],
        entropy_per_channel[1],
        entropy_per_channel[2],
    ]
    return f


def compute_image_features(image_fnames: List[str]):
    features = []
    with Pool(4) as wp:
        for y in tqdm(wp.imap(compute_features_proc, image_fnames), total=len(image_fnames)):
            features.append(y)

    features = np.array(features, dtype=np.float32)
    return features


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("experiments", nargs="+", type=str)
    parser.add_argument("-o", "--output", type=str, required=False)
    parser.add_argument("-dd", "--data-dir", type=str, default=os.environ.get("KAGGLE_2020_ALASKA2"))
    args = parser.parse_args()

    output_dir = os.path.dirname(__file__)
    data_dir = args.data_dir
    experiments = args.experiments
    output_file = args.output

    holdout_predictions = get_predictions_csv(experiments, "cauc", "holdout", "d4")
    test_predictions = get_predictions_csv(experiments, "cauc", "test", "d4")
    checksum = compute_checksum_v2(experiments)

    holdout_ds = get_holdout("", features=[INPUT_IMAGE_KEY])
    image_ids_h = [fs.id_from_fname(x) for x in holdout_ds.images]
    quality_h = F.one_hot(torch.tensor(holdout_ds.quality).long(), 3).numpy().astype(np.float32)

    test_ds = get_test_dataset("", features=[INPUT_IMAGE_KEY])
    quality_t = F.one_hot(torch.tensor(test_ds.quality).long(), 3).numpy().astype(np.float32)

    with_logits = True
    x, y = get_x_y_for_stacking(holdout_predictions, with_logits=with_logits, tta_logits=with_logits)
    # Force target to be binary
    y = (y > 0).astype(int)
    print(x.shape, y.shape)

    x_test, _ = get_x_y_for_stacking(test_predictions, with_logits=with_logits, tta_logits=with_logits)
    print(x_test.shape)

    if False:
        image_fnames_h = [
            os.path.join(data_dir, INDEX_TO_METHOD[method], f"{image_id}.jpg")
            for (image_id, method) in zip(image_ids_h, y)
        ]
        test_image_ids = pd.read_csv(test_predictions[0]).image_id.tolist()
        image_fnames_t = [os.path.join(data_dir, "Test", image_id) for image_id in test_image_ids]

        entropy_t = compute_image_features(image_fnames_t)
        x_test = np.column_stack([x_test, entropy_t])

        # entropy_h = entropy_t.copy()
        # x = x_test.copy()

        entropy_h = compute_image_features(image_fnames_h)
        x = np.column_stack([x, entropy_h])
        print("Added image features", entropy_h.shape, entropy_t.shape)

    if True:
        sc = StandardScaler()
        x = sc.fit_transform(x)
        x_test = sc.transform(x_test)

    if False:
        sc = PCA(n_components=16)
        x = sc.fit_transform(x)
        x_test = sc.transform(x_test)

    if True:
        x = np.column_stack([x, quality_h])
        x_test = np.column_stack([x_test, quality_t])

    group_kfold = GroupKFold(n_splits=5)

    params = {
        "min_child_weight": [1, 5, 10],
        "gamma": [1e-3, 1e-2, 1e-2, 0.5, 2],
        "subsample": [0.6, 0.8, 1.0],
        "colsample_bytree": [0.6, 0.8, 1.0],
        "max_depth": [2, 3, 4, 5, 6],
        "n_estimators": [16, 32, 64, 128, 256, 1000],
        "learning_rate": [0.001, 0.01, 0.05, 0.2, 1],
    }

    xgb = XGBClassifier(objective="binary:logistic", nthread=1)

    random_search = RandomizedSearchCV(
        xgb,
        param_distributions=params,
        scoring=make_scorer(alaska_weighted_auc, greater_is_better=True, needs_proba=True),
        n_jobs=4,
        n_iter=25,
        cv=group_kfold.split(x, y, groups=image_ids_h),
        verbose=3,
        random_state=42,
    )

    # Here we go
    random_search.fit(x, y)

    print("\n All results:")
    print(random_search.cv_results_)
    print("\n Best estimator:")
    print(random_search.best_estimator_)
    print(random_search.best_score_)
    print("\n Best hyperparameters:")
    print(random_search.best_params_)
    results = pd.DataFrame(random_search.cv_results_)
    results.to_csv("xgb-random-grid-search-results-01.csv", index=False)

    test_pred = random_search.predict_proba(x_test)[:, 1]

    if output_file is None:
        with_logits_sfx = "_with_logits" if with_logits else ""
        submit_fname = os.path.join(
            output_dir, f"xgb_cls_gs_{random_search.best_score_:.4f}_{checksum}{with_logits_sfx}.csv"
        )
    else:
        submit_fname = output_file

    df = pd.read_csv(test_predictions[0]).rename(columns={"image_id": "Id"})
    df["Label"] = test_pred
    df[["Id", "Label"]].to_csv(submit_fname, index=False)
    print("Saved submission to ", submit_fname)

    import json

    with open(fs.change_extension(submit_fname, ".json"), "w") as f:
        json.dump(random_search.best_params_, f, indent=2)


if __name__ == "__main__":
    main()
