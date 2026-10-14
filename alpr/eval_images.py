"""Static-image ALPR benchmark on a YOLO-format dataset with hand-labelled plate text.

    python -m alpr.eval_images --dataset "../Malaysian Car Plate Dataset" --split val \
        --gt alpr/gt/malaysia_val_plates.csv --save outputs/eval_val

Reports
  * detection: precision / recall / F1 of plate boxes at IoU >= 0.5;
  * recognition (needs --gt): end-to-end exact-match rate over all labelled plates (a missed plate
    counts as wrong), OCR exact match over the plates that were detected, mean CER, and a
    breakdown by GT tag (two_row, special, small ...). Plates tagged ``dealer`` (dealer frames, not
    registrations) are excluded from scoring.
``--oracle`` reads the GT boxes instead of detections, isolating OCR quality from detection.
Without --gt only detection is scored, against the dataset's own labels (any split).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

import cv2

from .config import DEFAULT_PATH, load_config
from .dataset import IMG_EXTS, _split_dirs, load_plate_boxes
from .evaluate import edit_distance
from .image import ImageALPR, PlateResult, annotate

EXCLUDED_TAGS = {"dealer"}


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match(gts: List[Sequence[float]], preds: List[Sequence[float]], thr: float = 0.5) -> Dict[int, int]:
    """Greedy one-to-one GT->prediction matching by IoU (highest IoU pairs first)."""
    pairs = sorted(((iou(g, p), gi, pi) for gi, g in enumerate(gts) for pi, p in enumerate(preds)), reverse=True)
    out, used = {}, set()
    for v, gi, pi in pairs:
        if v < thr:
            break
        if gi not in out and pi not in used:
            out[gi] = pi
            used.add(pi)
    return out


def load_gt_text(path: str) -> Dict[str, List[dict]]:
    """{image: [{box (normalised xyxy), plate, tags}]} from the hand-labelled CSV."""
    gt: Dict[str, List[dict]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gt[r["image"]].append({"box": tuple(float(r[k]) for k in ("x1", "y1", "x2", "y2")),
                                   "plate": r["plate"].strip().upper(),
                                   "tags": [t for t in r.get("tags", "").split(";") if t]})
    return gt


def evaluate_split(alpr: ImageALPR, dataset: str, split: str, gt_csv: Optional[str] = None,
                   oracle: bool = False, save_dir: Optional[str] = None, limit: int = 0) -> dict:
    img_dir, _ = _split_dirs(dataset, split)
    if gt_csv:
        gt = load_gt_text(gt_csv)
        names = sorted(n for n in os.listdir(img_dir) if n.lower().endswith(IMG_EXTS))
    else:
        if oracle:
            raise ValueError("--oracle needs --gt")
        labels = load_plate_boxes(dataset, split)
        gt = {k: [{"box": b, "plate": None, "tags": []} for b in v] for k, v in labels.items()}
        names = sorted(labels)
    if limit:
        names = names[:limit]
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    tp = fp = fn = 0
    rows, t0 = [], time.time()
    for name in names:
        img = cv2.imread(os.path.join(img_dir, name))
        if img is None:
            continue
        h, w = img.shape[:2]
        gts = gt.get(name, [])
        gt_px = [(g["box"][0] * w, g["box"][1] * h, g["box"][2] * w, g["box"][3] * h) for g in gts]
        if oracle:
            preds = []
            for b in gt_px:
                box = tuple(int(round(v)) for v in b)
                r = alpr.read(img, box)
                preds.append({"box": box, "text": r.text if r else "", "valid": bool(r and r.valid),
                              "conf": r.conf if r else 0.0, "raw": r.raw if r else ""})
        else:
            preds = [{"box": p.box, "text": p.compact, "valid": p.valid, "conf": p.ocr_conf, "raw": p.raw}
                     for p in alpr(img)]
        m = match(gt_px, [p["box"] for p in preds])
        tp, fn, fp = tp + len(m), fn + len(gts) - len(m), fp + len(preds) - len(m)
        img_rows = []
        for gi, g in enumerate(gts):
            p = preds[m[gi]] if gi in m else None
            img_rows.append({"image": name, "gt": g["plate"], "tags": g["tags"], "detected": p is not None,
                             "pred": p["text"] if p else "", "raw": p["raw"] if p else "",
                             "valid": p["valid"] if p else False, "conf": round(p["conf"], 4) if p else 0.0})
        matched = set(m.values())
        for pi, p in enumerate(preds):
            if pi not in matched:
                img_rows.append({"image": name, "gt": None, "tags": ["false_positive"], "detected": True,
                                 "pred": p["text"], "raw": p["raw"], "valid": p["valid"],
                                 "conf": round(p["conf"], 4)})
        rows += img_rows
        if save_dir and any(r["gt"] is None or (r["gt"] and r["pred"] != r["gt"]) for r in img_rows):
            shown = [PlateResult(p["box"], 1.0, p["text"], p["text"], p["conf"], p["valid"], "", p["raw"])
                     for p in preds]
            vis = annotate(img, shown)
            for b in gt_px:                                   # GT boxes in magenta
                cv2.rectangle(vis, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (255, 0, 255), 1)
            cv2.imwrite(os.path.join(save_dir, name), vis)

    det = {"tp": tp, "fp": fp, "fn": fn, "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn)}
    det["f1"] = 2 * det["precision"] * det["recall"] / max(1e-9, det["precision"] + det["recall"])
    report = {"dataset": dataset, "split": split, "oracle": oracle, "images": len(names),
              "seconds_per_image": round((time.time() - t0) / max(1, len(names)), 3),
              "detection": None if oracle else det, "rows": rows}
    if gt_csv:
        report["recognition"] = _recognition(rows)
    return report


def _recognition(rows: List[dict]) -> dict:
    scored = [r for r in rows if r["gt"] and not EXCLUDED_TAGS & set(r["tags"])]

    def stats(rs):
        det = [r for r in rs if r["detected"]]
        cer = [edit_distance(r["pred"], r["gt"]) / len(r["gt"]) if r["detected"] else 1.0 for r in rs]
        return {"plates": len(rs), "end_to_end": sum(r["pred"] == r["gt"] for r in rs) / max(1, len(rs)),
                "ocr_on_detected": sum(r["pred"] == r["gt"] for r in det) / max(1, len(det)),
                "mean_cer": sum(cer) / max(1, len(cer))}

    by_tag = defaultdict(list)
    for r in scored:
        for t in r["tags"] or ["standard"]:
            by_tag[t].append(r)
    fps = [r for r in rows if r["gt"] is None]
    return {"all": stats(scored), "by_tag": {t: stats(v) for t, v in sorted(by_tag.items())},
            "false_positive_boxes": len(fps), "false_positive_valid_reads": sum(r["valid"] for r in fps)}


def print_report(rep: dict) -> None:
    print("=" * 72)
    print(f"{rep['dataset']}  split={rep['split']}  images={rep['images']}  "
          f"{'ORACLE boxes' if rep['oracle'] else 'detector boxes'}  {rep['seconds_per_image']}s/img")
    if rep["detection"]:
        d = rep["detection"]
        print(f"detection  P {d['precision']:.3f}  R {d['recall']:.3f}  F1 {d['f1']:.3f}  "
              f"(TP {d['tp']} FP {d['fp']} FN {d['fn']})")
    rec = rep.get("recognition")
    if rec:
        a = rec["all"]
        print(f"recognition  end-to-end exact {a['end_to_end']:.1%}  |  OCR exact on detected "
              f"{a['ocr_on_detected']:.1%}  |  mean CER {a['mean_cer']:.3f}  ({a['plates']} plates)")
        for t, s in rec["by_tag"].items():
            print(f"   {t:<10} n={s['plates']:<3} end-to-end {s['end_to_end']:.1%}  OCR {s['ocr_on_detected']:.1%}")
        print(f"false-positive boxes {rec['false_positive_boxes']} (read as a legal plate: "
              f"{rec['false_positive_valid_reads']})")
        errors = [r for r in rep["rows"] if r["gt"] and r["pred"] != r["gt"] and not EXCLUDED_TAGS & set(r["tags"])]
        if errors:
            print("errors:")
            for r in errors:
                print(f"   GT {r['gt']:<10} -> {(r['pred'] or 'MISSED'):<10} raw {r['raw']:<10} "
                      f"{','.join(r['tags'])}  {r['image'][:40]}")
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--split", default="val")
    ap.add_argument("--gt", help="hand-labelled plate text CSV (enables recognition metrics)")
    ap.add_argument("--oracle", action="store_true", help="read GT boxes instead of detected ones")
    ap.add_argument("--profile", default="image")
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--plate-model", help="override plate_model")
    ap.add_argument("--ocr", choices=["fast_plate_ocr", "easyocr"], help="override ocr_backend")
    ap.add_argument("--no-tta", action="store_true")
    ap.add_argument("--conf", type=float, help="override plate_conf")
    ap.add_argument("--imgsz", type=int, help="override plate_imgsz")
    ap.add_argument("--limit", type=int, default=0, help="only the first N images")
    ap.add_argument("--save", help="write annotated images of every mistake + report.json here")
    args = ap.parse_args()

    cfg = load_config(args.profile, args.config)
    for attr, value in (("plate_model", args.plate_model), ("ocr_backend", args.ocr),
                        ("plate_conf", args.conf), ("plate_imgsz", args.imgsz)):
        if value is not None:
            setattr(cfg, attr, value)
    if args.no_tta:
        cfg.ocr_tta = False
    rep = evaluate_split(ImageALPR(cfg), args.dataset, args.split, args.gt, args.oracle, args.save, args.limit)
    rep["config"] = {"plate_model": cfg.plate_model, "ocr_backend": cfg.ocr_backend, "ocr_model": cfg.ocr_model,
                     "ocr_tta": cfg.ocr_tta, "plate_conf": cfg.plate_conf, "plate_imgsz": cfg.plate_imgsz}
    print_report(rep)
    if args.save:
        with open(os.path.join(args.save, "report.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print(f"report + mistake images -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
