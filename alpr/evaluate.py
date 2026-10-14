"""Quantitative plate evaluation: exact-match rate and character error rate vs hand-labelled plates.

Ground truth is a text file with one plate per line (spaces ignored, ``#`` comments allowed), the
plates you can read by eye in the video. A line starting with ``?`` is a vehicle whose plate cannot be
verified (occluded): a prediction within 2 edits of it is ignored instead of counted as a false
positive. Predictions are the ``*.plates.json`` written by run_alpr.py; a plate predicted twice (the
same vehicle split into two tracks) counts once as a hit and once as a false positive.

    python -m alpr.evaluate --pred outputs/x.plates.json --gt data/gt/x.txt
"""
from __future__ import annotations

import argparse
import json
from typing import Dict, List

from .plates import normalize


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def evaluate(preds: List[str], gts: List[str], ignore: List[str] = ()) -> Dict:
    """One-to-one matching of GT plates to predictions, closest pairs (fewest edits) first."""
    preds = [normalize(p) for p in preds]
    gts = [normalize(g) for g in gts]
    unused = set(range(len(preds)))
    for ig in (normalize(i) for i in ignore):                 # unverifiable vehicles: not a hit, not a FP
        near = sorted((edit_distance(preds[k], ig), k) for k in unused)
        if near and near[0][0] <= 2:
            unused.discard(near[0][1])
    pairs = sorted((edit_distance(preds[k], g), i, k) for i, g in enumerate(gts) for k in unused)
    match = {}
    for d, i, k in pairs:
        if i not in match and k in unused:
            match[i] = (k, d)
            unused.discard(k)
    rows, cers = [], []
    for i, gt in enumerate(gts):
        k, d = match.get(i, (None, len(gt)))
        cers.append(min(1.0, d / max(1, len(gt))))
        rows.append({"gt": gt, "pred": preds[k] if k is not None else "", "cer": round(cers[-1], 3)})
    n = max(1, len(gts))
    return {"n_gt": len(gts), "n_pred": len(preds), "exact_match": sum(c == 0 for c in cers) / n,
            "mean_cer": sum(cers) / n, "false_positives": len(unused),
            "false_positive_plates": [preds[k] for k in sorted(unused)], "rows": rows}


def _load_gt(path: str):
    """-> (plates, ignored plates)."""
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln.split("#")[0].strip() for ln in f]
    lines = [ln for ln in lines if ln]
    return [ln for ln in lines if not ln.startswith("?")], [ln[1:].strip() for ln in lines if ln.startswith("?")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--min-status", default="TENTATIVE", choices=["ACQUIRING", "TENTATIVE", "CONFIRMED"])
    args = ap.parse_args()
    order = {"ACQUIRING": 0, "TENTATIVE": 1, "CONFIRMED": 2}
    with open(args.pred, encoding="utf-8") as f:
        plates = json.load(f)["plates"]
    preds = [p["compact"] for p in plates if order[p["status"]] >= order[args.min_status]]
    gts, ignore = _load_gt(args.gt)
    rep = evaluate(preds, gts, ignore)
    for r in rep["rows"]:
        print(f"  GT {r['gt']:<10} -> pred {r['pred'] or '-':<10} CER {r['cer']}")
    print(f"exact match {rep['exact_match']:.0%} | mean CER {rep['mean_cer']:.3f} | "
          f"false positives {rep['false_positives']} {rep['false_positive_plates'] or ''} | "
          f"GT {rep['n_gt']} / pred {rep['n_pred']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
