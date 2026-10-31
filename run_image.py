#!/usr/bin/env python3
"""Read Malaysian licence plates in photos.

    python run_image.py --source car.jpg
    python run_image.py --source "../Malaysian Car Plate Dataset/val/images" --out outputs/images_alpr
    python run_image.py --source photos/ --ocr easyocr          # generic OCR fallback

    python run_image.py --source photos/ --check                # + checks against the SIMULATED registry

Writes one annotated copy per image (green = legal Malaysian plate, orange = non-standard text or a
registry alert, red = high-severity alert) and `results.json` with every plate: text, validity,
state/category, OCR + detector confidence and, with --check, registry alerts (UNMATCHED, EXPIRED,
ILLEGAL_FORMAT, BLACKLIST, DUPLICATE; see alpr/registry.py - the demo registry is simulated).
Thresholds live in the `image` profile of alpr/config.yaml. Accuracy on a labelled dataset:
`python -m alpr.eval_images --help`.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

import cv2

from alpr.config import DEFAULT_PATH, load_config
from alpr.dataset import IMG_EXTS
from alpr.image import ImageALPR, annotate
from alpr.registry import ILLEGAL_FORMAT, PlateChecker


def list_images(source: str):
    if os.path.isdir(source):
        return sorted(os.path.join(source, f) for f in os.listdir(source) if f.lower().endswith(IMG_EXTS))
    if any(c in source for c in "*?["):
        return sorted(p for p in glob.glob(source) if p.lower().endswith(IMG_EXTS))
    return [source] if os.path.exists(source) else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="image file, folder or glob (default: profile source)")
    ap.add_argument("--out", help="output folder (default: profile out)")
    ap.add_argument("--profile", default="image")
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--plate-model", help="override plate_model")
    ap.add_argument("--ocr", choices=["fast_plate_ocr", "easyocr"], help="override ocr_backend")
    ap.add_argument("--conf", type=float, help="override plate_conf")
    ap.add_argument("--tta", action="store_true", help="average OCR over several crop margins")
    ap.add_argument("--no-save", action="store_true", help="only print / write results.json")
    ap.add_argument("--check", action="store_true", help="check plates against the registry (registry_path)")
    ap.add_argument("--at", help="observation time for the road-tax check, ISO format (default: now)")
    args = ap.parse_args(argv)

    cfg = load_config(args.profile, args.config)
    for attr, value in (("source", args.source), ("out", args.out), ("plate_model", args.plate_model),
                        ("ocr_backend", args.ocr), ("plate_conf", args.conf)):
        if value is not None:
            setattr(cfg, attr, value)
    cfg.ocr_tta = cfg.ocr_tta or args.tta

    paths = list_images(cfg.source)
    if not paths:
        print(f"Error: no images found at {cfg.source}")
        return 1
    os.makedirs(cfg.out, exist_ok=True)
    checker = PlateChecker.from_config(cfg) if (args.check or cfg.registry_enabled) else None
    when = datetime.fromisoformat(args.at) if args.at else None
    alpr = ImageALPR(cfg, checker=checker)
    print(f"{len(paths)} image(s)  plate_model={cfg.plate_model}  ocr={cfg.ocr_backend}  -> {cfg.out}"
          + (f"  registry={cfg.registry_path} (SIMULATED)" if checker else ""))

    results, t0 = [], time.time()
    for path in paths:
        img = cv2.imread(path)
        if img is None:
            print(f"  skip (unreadable): {path}")
            continue
        plates = alpr(img, when)
        results.append({"image": path, "plates": [p.to_dict() for p in plates]})
        shown = ", ".join(f"{p.text}{'' if p.valid else ' (non-standard)'}"
                          + "".join(f" !{a.code}" for a in p.alerts if a.code != ILLEGAL_FORMAT)
                          for p in plates) or "no plate"
        print(f"  {os.path.basename(path)[:48]:<48} {shown}")
        if not args.no_save:
            cv2.imwrite(os.path.join(cfg.out, os.path.basename(path)), annotate(img, plates))

    json_path = os.path.join(cfg.out, "results.json")
    report = {"plate_model": cfg.plate_model, "ocr_backend": cfg.ocr_backend, "images": results}
    if checker:
        report["registry"] = {"path": cfg.registry_path, "rules": sorted(checker.rules),
                              "disclaimer": checker.registry.get_meta("disclaimer") or ""}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    n_plates = sum(len(r["plates"]) for r in results)
    n_alerts = sum(len(p["alerts"]) for r in results for p in r["plates"])
    print(f"{n_plates} plate(s) in {len(results)} image(s), {(time.time() - t0) / max(1, len(results)):.2f}s/image"
          + (f", {n_alerts} registry alert(s)" if checker else "") + f"  results -> {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
