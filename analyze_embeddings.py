import argparse
import os
from multiprocessing import Pool

import cv2
import pandas as pd
from collections import defaultdict

from pytorch_toolbelt.utils import fs
from tqdm import tqdm
import numpy as np

from alaska2.dataset import decode_bgr_from_dct


def read_pil(image_fname):
    from PIL import Image

    return np.asarray(Image.open(image_fname))


def read_cv2(image_fname):
    return cv2.imread(image_fname)


def read_jpeg4py(image_fname):
    import jpeg4py as jpeg

    return jpeg.JPEG(image_fname).decode()


def read_from_dct(image_fname):
    return decode_bgr_from_dct(fs.change_extension(image_fname, ".npz"))


def compute_mask(cover, stego):
    eps = 1e-6
    m = cv2.absdiff(cover, stego)
    m2 = (m > eps).any(axis=2)
    return m2


def count_pixel_difference(cover, stego):
    return np.count_nonzero(compute_mask(cover, stego))


def count_dct_difference(cover_dct, stego_dct):
    dct_y = np.count_nonzero(cover_dct["dct_y"] != stego_dct["dct_y"])
    dct_cr = np.count_nonzero(cover_dct["dct_cr"] != stego_dct["dct_cr"])
    dct_cb = np.count_nonzero(cover_dct["dct_cb"] != stego_dct["dct_cb"])
    return dct_y, dct_cr, dct_cb


def count_dct_difference_bits(cover_dct, stego_dct):
    total_bits = []
    for x, y in [
        (cover_dct["dct_y"], stego_dct["dct_y"]),
        (cover_dct["dct_cr"], stego_dct["dct_cr"]),
        (cover_dct["dct_cb"], stego_dct["dct_cb"]),
    ]:
        mask = np.unpackbits(x.view(np.uint8)) != np.unpackbits(y.view(np.uint8))
        diff = np.count_nonzero(mask)
        total_bits.append(diff)
    return total_bits


def compute_statistics(cover_fname):
    results_df = defaultdict(list)
    # cover_dct = np.load(fs.change_extension(cover_fname, ".npz"))

    cover = read_from_dct(cover_fname)

    for method_name in ["JMiPOD", "JUNIWARD", "UERD"]:
        stego_fname = cover_fname.replace("Cover", method_name)
        stego = read_from_dct(stego_fname)

        mask_fname = fs.change_extension(stego_fname, ".png")
        mask = compute_mask(cover, stego)

        results_df["image"].append(os.path.basename(cover_fname))
        results_df["method"].append(os.path.basename(method_name))
        results_df["pd"].append(count_pixel_difference(cover, stego))

        cv2.imwrite(mask_fname, ((mask > 0) * 255).astype(np.uint8))
        # stego_dct = np.load(fs.change_extension(stego_fname, ".npz"))

        # dct_y, dct_cr, dct_cb = count_dct_difference(cover_dct, stego_dct)
        # results_df["dct_total"].append(dct_y + dct_cr + dct_cb)
        # results_df["dct_y"].append(dct_y)
        # results_df["dct_cr"].append(dct_cr)
        # results_df["dct_cb"].append(dct_cb)
        #
        # dct_y, dct_cr, dct_cb = count_dct_difference_bits(cover_dct, stego_dct)
        # results_df["dct_bits_total"].append(dct_y + dct_cr + dct_cb)
        # results_df["dct_bits_y"].append(dct_y)
        # results_df["dct_bits_cr"].append(dct_cr)
        # results_df["dct_bits_cb"].append(dct_cb)

    return results_df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-dd", "--data-dir", type=str, default=os.environ.get("KAGGLE_2020_ALASKA2"))

    args = parser.parse_args()

    data_dir = args.data_dir
    cover_images = [x for x in fs.find_images_in_dir(os.path.join(data_dir, "Cover")) if str.endswith(x, ".jpg")]

    # cover_images = cover_images[:100]

    pool = Pool(6)
    results_df = []

    for df in tqdm(pool.imap(compute_statistics, cover_images), total=len(cover_images)):
        results_df.append(df)

    results_df = pd.concat([pd.DataFrame.from_dict(x) for x in results_df])
    results_df.to_csv("analyze_embeddings.csv", index=False)


if __name__ == "__main__":
    main()
