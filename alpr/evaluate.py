"""Quantitative plate evaluation: exact-match rate and character error rate vs hand-labelled plates.

Ground truth is a text file with one plate per line (spaces ignored, ``#`` comments allowed), the
plates you can read by eye in the video. A line starting with ``?`` is a vehicle whose plate cannot be
verified (occluded): a prediction within 2 edits of it is ignored instead of counted as a false
positive. Predictions are the ``*.plates.json`` written by run_alpr.py; a plate predicted twice (the
same vehicle split into two tracks) counts once as a hit and once as a false positive.

Registry alerts: a line may end with ``! CODE[,CODE]`` (e.g. ``XSA1001 ! EXPIRED,TYPE_MISMATCH``).
If any line of the file does, alerts are scored too: every GT plate without ``!`` must raise no
alert, every plate with ``!`` exactly the listed ones.

    python -m alpr.evaluate --pred outputs/x.plates.json --gt data/gt/x.txt
"""
from __future__ import annotations

import argparse
import json
from typing import Dict, List, Optional, Set

from .plates import normalize


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def evaluate(preds: List[str], gts: List[str], ignore: List[str] = (),
             pred_alerts: Optional[List[Set[str]]] = None, gt_alerts: Optional[List[Set[str]]] = None) -> Dict:
    """One-to-one matching of GT plates to predictions, closest pairs (fewest edits) first.
    With `pred_alerts` / `gt_alerts` (alert codes per prediction / per GT plate) alerts are scored."""
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
    score_alerts = pred_alerts is not None and gt_alerts is not None
    for i, gt in enumerate(gts):
        k, d = match.get(i, (None, len(gt)))
        cers.append(min(1.0, d / max(1, len(gt))))
        row = {"gt": gt, "pred": preds[k] if k is not None else "", "cer": round(cers[-1], 3)}
        if score_alerts:
            row["gt_alerts"] = sorted(gt_alerts[i])
            row["pred_alerts"] = sorted(pred_alerts[k]) if k is not None else []
            row["alerts_ok"] = k is not None and set(row["pred_alerts"]) == set(row["gt_alerts"])
        rows.append(row)
    n = max(1, len(gts))
    rep = {"n_gt": len(gts), "n_pred": len(preds), "exact_match": sum(c == 0 for c in cers) / n,
           "mean_cer": sum(cers) / n, "false_positives": len(unused),
           "false_positive_plates": [preds[k] for k in sorted(unused)], "rows": rows}
    if score_alerts:
        flagged = [r for r in rows if r["gt_alerts"]]
        clean = [r for r in rows if not r["gt_alerts"] and r["pred"]]
        rep["alerts"] = {"violations": len(flagged), "violations_caught": sum(r["alerts_ok"] for r in flagged),
                         "valid_plates": len(clean),
                         "false_alerts": sum(bool(r["pred_alerts"]) for r in clean),
                         "false_alerts_on_unmatched": sum(bool(pred_alerts[k]) for k in unused)}
    return rep


def _load_gt(path: str):
    """-> (plates, ignored plates, expected alerts per plate or None when the file lists none)."""
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln.split("#")[0].strip() for ln in f]
    lines = [ln for ln in lines if ln]
    plates, alerts = [], []
    for ln in lines:
        if ln.startswith("?"):
            continue
        text, _, codes = ln.partition("!")
        plates.append(text.strip())
        alerts.append({c.strip().upper() for c in codes.split(",") if c.strip()})
    ignore = [ln[1:].split("!")[0].strip() for ln in lines if ln.startswith("?")]
    return plates, ignore, (alerts if any("!" in ln for ln in lines) else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--min-status", default="TENTATIVE", choices=["ACQUIRING", "TENTATIVE", "CONFIRMED"])
    args = ap.parse_args()
    order = {"ACQUIRING": 0, "TENTATIVE": 1, "CONFIRMED": 2}
    with open(args.pred, encoding="utf-8") as f:
        plates = json.load(f)["plates"]
    kept = [p for p in plates if order[p["status"]] >= order[args.min_status]]
    preds = [p["compact"] for p in kept]
    gts, ignore, gt_alerts = _load_gt(args.gt)
    pred_alerts = [{a["code"] for a in p.get("alerts", [])} for p in kept] if gt_alerts is not None else None
    rep = evaluate(preds, gts, ignore, pred_alerts, gt_alerts)
    for r in rep["rows"]:
        line = f"  GT {r['gt']:<10} -> pred {r['pred'] or '-':<10} CER {r['cer']}"
        if "alerts_ok" in r:
            want = ",".join(r["gt_alerts"]) or "none"
            got = ",".join(r["pred_alerts"]) or "none"
            line += f"   alerts expected {want:<26} got {got:<26} {'OK' if r['alerts_ok'] else 'WRONG'}"
        print(line)
    print(f"exact match {rep['exact_match']:.0%} | mean CER {rep['mean_cer']:.3f} | "
          f"false positives {rep['false_positives']} {rep['false_positive_plates'] or ''} | "
          f"GT {rep['n_gt']} / pred {rep['n_pred']}")
    if "alerts" in rep:
        a = rep["alerts"]
        print(f"violations caught {a['violations_caught']}/{a['violations']} | valid plates with a false alert "
              f"{a['false_alerts']}/{a['valid_plates']} | alerts on unmatched predictions {a['false_alerts_on_unmatched']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
