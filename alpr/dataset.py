"""YOLO-format plate datasets: read plate boxes (bbox or polygon labels) and build a 1-class training set.

The Malaysian Car Plate Dataset is labelled with polygons and two classes (``car``, ``car plate``),
while the plate detector is a 1-class box model. ``load_plate_boxes`` reads either label style, and
``python -m alpr.dataset`` writes a plate-only bbox copy that ultralytics can train on:

    python -m alpr.dataset --src "../Malaysian Car Plate Dataset" --dst data/malaysia_plates_yolo
"""
from __future__ import annotations

import argparse
import os
import shutil
from typing import Dict, List, Optional, Tuple

import yaml

Box = Tuple[float, float, float, float]          # normalised x1, y1, x2, y2
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def plate_class_id(dataset_dir: str) -> int:
    """Index of the class whose name contains 'plate' in the dataset's data.yaml (0 if there is none)."""
    path = os.path.join(dataset_dir, "data.yaml")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        names = (yaml.safe_load(f) or {}).get("names", [])
    if isinstance(names, dict):
        names = [names[k] for k in sorted(names)]
    return next((i for i, n in enumerate(names) if "plate" in str(n).lower()), 0)


def parse_label_line(line: str, cls_id: int) -> Optional[Box]:
    """One YOLO label line -> normalised xyxy box, or None if it is another class / not a label.
    Accepts ``cls cx cy w h`` and polygon ``cls x1 y1 x2 y2 ...`` lines."""
    parts = line.split()
    if len(parts) < 5 or parts[0] != str(cls_id):
        return None
    try:
        v = [float(p) for p in parts[1:]]
    except ValueError:
        return None
    if len(v) == 4:
        cx, cy, w, h = v
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    else:
        xs, ys = v[0::2], v[1::2]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    x1, y1, x2, y2 = (min(max(c, 0.0), 1.0) for c in (x1, y1, x2, y2))
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def load_plate_boxes(dataset_dir: str, split: str) -> Dict[str, List[Box]]:
    """{image filename: [plate boxes]} for every image of `split` (images without plates map to [])."""
    cls_id = plate_class_id(dataset_dir)
    img_dir, lbl_dir = _split_dirs(dataset_dir, split)
    out: Dict[str, List[Box]] = {}
    for name in sorted(os.listdir(img_dir)):
        if not name.lower().endswith(IMG_EXTS):
            continue
        boxes: List[Box] = []
        lbl = os.path.join(lbl_dir, os.path.splitext(name)[0] + ".txt")
        if os.path.exists(lbl):
            with open(lbl, encoding="utf-8", errors="ignore") as f:
                boxes = [b for b in (parse_label_line(ln, cls_id) for ln in f) if b is not None]
        out[name] = sorted(boxes)
    return out


def _split_dirs(dataset_dir: str, split: str) -> Tuple[str, str]:
    for img_dir, lbl_dir in ((os.path.join(dataset_dir, split, "images"), os.path.join(dataset_dir, split, "labels")),
                             (os.path.join(dataset_dir, "images", split), os.path.join(dataset_dir, "labels", split))):
        if os.path.isdir(img_dir):
            return img_dir, lbl_dir
    raise FileNotFoundError(f"no images for split '{split}' under {dataset_dir}")


def build_plate_dataset(src: str, dst: str, splits=("train", "val")) -> str:
    """Write a 1-class (license_plate) bbox copy of `src` to `dst`; returns the data.yaml path."""
    for split in splits:
        img_src, _ = _split_dirs(src, split)
        img_dst, lbl_dst = os.path.join(dst, split, "images"), os.path.join(dst, split, "labels")
        os.makedirs(img_dst, exist_ok=True)
        os.makedirs(lbl_dst, exist_ok=True)
        n_img = n_box = 0
        for name, boxes in load_plate_boxes(src, split).items():
            shutil.copy2(os.path.join(img_src, name), os.path.join(img_dst, name))
            with open(os.path.join(lbl_dst, os.path.splitext(name)[0] + ".txt"), "w", encoding="utf-8") as f:
                for x1, y1, x2, y2 in boxes:
                    f.write(f"0 {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} {x2 - x1:.6f} {y2 - y1:.6f}\n")
            n_img, n_box = n_img + 1, n_box + len(boxes)
        print(f"{split}: {n_img} images, {n_box} plates -> {img_dst}")
    data_yaml = os.path.join(dst, "data.yaml")
    with open(data_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump({"path": os.path.abspath(dst), "train": "train/images", "val": "val/images",
                        "nc": 1, "names": ["license_plate"]}, f, sort_keys=False)
    return data_yaml


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="YOLO dataset with a class named '*plate*'")
    ap.add_argument("--dst", required=True)
    ap.add_argument("--splits", nargs="+", default=["train", "val"])
    args = ap.parse_args()
    print(f"data.yaml -> {build_plate_dataset(args.src, args.dst, args.splits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
