"""Per-lane counting accuracy against hand-counted ground truth, and the slit-scan images used to
count it.

Ground truth is counted by eye, without the detector: a *slit-scan* stacks the pixel row of the count
line from every frame, so time runs down the image and every vehicle that crosses the line leaves
one block in its lane's column (lane boundaries are drawn in, a tick every second). Each block is
one line of the GT file:

    # time_sec  lane  [type]      (type: car, moto, bus, truck, van; optional)
    12.4  In2  car
    13.1  In4  moto

    python -m alpr.lane_eval --slitscan --profile malaysia_selayang_lanes --group Inbound
    python -m alpr.lane_eval --pred outputs/malaysia_selayang_lanes.plates.json --gt alpr/gt/x_lanes.txt

Scoring: predicted crossings (`lanes.crossings` in the results JSON) are matched one-to-one to GT
crossings of the same lane within ``--tol`` seconds (closest first). A prediction that matches a GT
crossing in time but in the neighbouring lane is a lane-assignment error (shown separately).
Per lane: GT count, predicted count, count accuracy 1 - |pred - gt| / gt (Paper 4's measure),
precision / recall / F1 of the time-matched crossings.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------- ground truth
def load_gt(path: str) -> List[Tuple[float, str, str]]:
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.split("#")[0].strip()
            if not ln:
                continue
            parts = ln.split()
            out.append((float(parts[0]), parts[1], parts[2].lower() if len(parts) > 2 else ""))
    return out


def evaluate_lanes(pred: Sequence[Tuple[float, str]], gt: Sequence[Tuple[float, str]], lanes: Sequence[str],
                   tol: float = 1.0, window: Optional[Tuple[float, float]] = None) -> Dict:
    """pred / gt: (time_sec, lane). Returns per-lane and overall scores (see module docstring)."""
    if window:
        pred = [p for p in pred if window[0] <= p[0] < window[1]]
        gt = [g for g in gt if window[0] <= g[0] < window[1]]
    pred = [p for p in pred if p[1] in lanes]
    gt = [g for g in gt if g[1] in lanes]
    # 1) same lane, closest in time first
    pairs = sorted((abs(p[0] - g[0]), i, j) for i, g in enumerate(gt) for j, p in enumerate(pred)
                   if p[1] == g[1] and abs(p[0] - g[0]) <= tol)
    gm, pm = {}, {}
    for d, i, j in pairs:
        if i not in gm and j not in pm:
            gm[i], pm[j] = j, i
    # 2) left-overs matched in time, any lane: the vehicle was counted, in the wrong lane
    pairs = sorted((abs(p[0] - g[0]), i, j) for i, g in enumerate(gt) for j, p in enumerate(pred)
                   if i not in gm and j not in pm and abs(p[0] - g[0]) <= tol)
    wrong = {}
    for d, i, j in pairs:
        if i not in gm and j not in pm and i not in wrong and j not in wrong.values():
            wrong[i] = j
    per = {}
    for lane in lanes:
        n_gt = sum(g[1] == lane for g in gt)
        n_pr = sum(p[1] == lane for p in pred)
        tp = sum(1 for i in gm if gt[i][1] == lane)
        prec = tp / n_pr if n_pr else (1.0 if n_gt == 0 else 0.0)
        rec = tp / n_gt if n_gt else (1.0 if n_pr == 0 else 0.0)
        per[lane] = {"gt": n_gt, "pred": n_pr, "matched": tp,
                     "count_accuracy": (1.0 - abs(n_pr - n_gt) / n_gt) if n_gt else (1.0 if n_pr == 0 else 0.0),
                     "precision": prec, "recall": rec, "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
                     "wrong_lane_from_here": sum(1 for i in wrong if gt[i][1] == lane)}
    n_gt, n_pr, tp = len(gt), len(pred), len(gm)
    lanes_with_gt = [l for l in lanes if per[l]["gt"]]
    return {"per_lane": per, "n_gt": n_gt, "n_pred": n_pr, "matched": tp, "wrong_lane": len(wrong),
            "missed": n_gt - tp - len(wrong), "extra": n_pr - tp - len(wrong),
            "mean_lane_count_accuracy": float(np.mean([per[l]["count_accuracy"] for l in lanes_with_gt]))
            if lanes_with_gt else 0.0,
            "weighted_count_accuracy": (sum(per[l]["gt"] * max(0.0, per[l]["count_accuracy"]) for l in lanes_with_gt)
                                        / max(1, n_gt)),
            "precision": tp / n_pr if n_pr else 0.0, "recall": tp / n_gt if n_gt else 0.0,
            "total_count_accuracy": 1.0 - abs(n_pr - n_gt) / n_gt if n_gt else 0.0}


# ---------------------------------------------------------------------------- slit-scan
def slitscan(source: str, y_frac: float, x_range: Tuple[int, int], step: int = 2, start_sec: float = 0.0,
             duration: float = 0.0, band: int = 1) -> Tuple[np.ndarray, float]:
    """(rows, fps of the rows): the count-line row (mean of 2*band+1 rows) of every `step`th frame."""
    cap = cv2.VideoCapture(source)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start_sec * fps))
    n = int(duration * fps) if duration > 0 else int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - int(start_sec * fps)
    rows, i = [], 0
    while i < n:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            y = int(y_frac * f.shape[0])
            rows.append(f[y - band:y + band + 1, x_range[0]:x_range[1]].mean(axis=0))
        i += 1
    cap.release()
    return np.asarray(rows, np.uint8), fps / step


def slitscan_sheets(rows: np.ndarray, rate: float, bounds: Dict[str, Tuple[float, float]], x0: int,
                    t0: float, out_prefix: str, chunk_sec: float = 10.0, per_sheet: int = 4,
                    stretch: int = 2) -> List[str]:
    """Write sheets of `per_sheet` columns, each `chunk_sec` of slit-scan, lane boundaries and labels
    drawn in, a tick + label every second. Returns the file names."""
    h_chunk = int(round(chunk_sec * rate))
    n_chunks = int(np.ceil(len(rows) / h_chunk))
    w = rows.shape[1]
    paths = []
    for s0 in range(0, n_chunks, per_sheet):
        cols = []
        for c in range(s0, min(n_chunks, s0 + per_sheet)):
            seg = rows[c * h_chunk:(c + 1) * h_chunk]
            seg = np.repeat(seg, stretch, axis=0)
            pad = np.zeros((h_chunk * stretch - len(seg), w, 3), np.uint8)
            seg = np.vstack([seg, pad]) if len(pad) else seg
            img = np.zeros((seg.shape[0] + 40, w + 52, 3), np.uint8)
            img[40:, 52:] = seg
            for name, (a, b) in bounds.items():
                for xb in (a, b):
                    cv2.line(img, (int(xb - x0) + 52, 40), (int(xb - x0) + 52, img.shape[0]), (0, 255, 255), 1)
                cv2.putText(img, name, (int((a + b) / 2 - x0) + 52 - 16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (0, 255, 255), 2)
            for k in range(int(chunk_sec) + 1):
                y = 40 + int(k * rate * stretch)
                t = t0 + c * chunk_sec + k
                cv2.line(img, (40, y), (52, y), (255, 255, 255), 1)
                if y < img.shape[0] - 4:
                    cv2.putText(img, f"{t:.0f}", (2, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
            cols.append(img)
        hmax = max(c.shape[0] for c in cols)
        cols = [np.vstack([c, np.zeros((hmax - c.shape[0], c.shape[1], 3), np.uint8)]) for c in cols]
        sep = np.full((hmax, 12, 3), 40, np.uint8)
        sheet = cols[0]
        for c in cols[1:]:
            sheet = np.hstack([sheet, sep, c])
        p = f"{out_prefix}_{t0 + s0 * chunk_sec:05.0f}s.png"
        cv2.imwrite(p, sheet)
        paths.append(p)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", help="results JSON written by run_alpr.py (lanes.crossings)")
    ap.add_argument("--gt", help="hand-counted crossings: time_sec lane [type]")
    ap.add_argument("--tol", type=float, default=1.0, help="time tolerance (s) for matching a crossing")
    ap.add_argument("--window", type=float, nargs=2, metavar=("FROM", "TO"), help="score only this time window (s)")
    ap.add_argument("--no-moto", action="store_true", help="leave motorcycles out of GT and predictions")
    ap.add_argument("--lanes", help="comma-separated lanes to score (default: the lanes in the GT file)")
    ap.add_argument("--slitscan", action="store_true", help="write slit-scan sheets for hand counting")
    ap.add_argument("--profile")
    ap.add_argument("--group", help="carriageway (lane group) to slit-scan, e.g. Inbound")
    ap.add_argument("--out", default="outputs/lanes/slitscan")
    args = ap.parse_args(argv)

    if args.slitscan:
        from .config import load_config
        from .lanes import LaneMap
        cfg = load_config(args.profile)
        cap = cv2.VideoCapture(cfg.source)
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        lm = LaneMap.from_config(cfg.lanes, cfg.solid_lines, W, H)
        y = cfg.count_line_y * H
        bounds = {l.name: l.span_at(y) for l in lm.lanes if (not args.group or l.group == args.group) and l.span_at(y)}
        x0 = int(max(0, min(a for a, _ in bounds.values()) - 20))
        x1 = int(min(W, max(b for _, b in bounds.values()) + 20))
        rows, rate = slitscan(cfg.source, cfg.count_line_y, (x0, x1), step=max(1, cfg.frame_step),
                              start_sec=cfg.start_sec, duration=cfg.duration)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        for p in slitscan_sheets(rows, rate, bounds, x0, cfg.start_sec, f"{args.out}_{args.group or 'all'}"):
            print(p)
        return 0

    with open(args.pred, encoding="utf-8") as f:
        rep = json.load(f)["lanes"]
    gt = load_gt(args.gt)
    if args.no_moto:
        gt = [g for g in gt if g[2] != "moto"]
    gt_lanes = {l for _, l, _ in gt}
    lanes = args.lanes.split(",") if args.lanes else [l for l in rep["lanes"] if l in gt_lanes]
    pred = [(c["time_sec"], c["lane"]) for c in rep["crossings"]
            if not (args.no_moto and c.get("vtype") == "Motorcycle")]
    res = evaluate_lanes(pred, [(t, l) for t, l, _ in gt], lanes, args.tol,
                         window=tuple(args.window) if args.window else None)
    print(f"lanes {','.join(lanes)}" + (f" | window {args.window[0]:g}-{args.window[1]:g} s" if args.window else "")
          + (" | motorcycles excluded" if args.no_moto else ""))
    print(f"{'lane':<6} {'GT':>4} {'pred':>5} {'match':>6} {'count acc':>10} {'prec':>6} {'recall':>7} {'F1':>6}")
    for lane in lanes:
        r = res["per_lane"][lane]
        if r["gt"] == 0 and r["pred"] == 0:
            continue
        print(f"{lane:<6} {r['gt']:>4} {r['pred']:>5} {r['matched']:>6} {r['count_accuracy']:>10.1%} "
              f"{r['precision']:>6.1%} {r['recall']:>7.1%} {r['f1']:>6.1%}")
    print(f"total GT {res['n_gt']}, predicted {res['n_pred']}: matched {res['matched']}, wrong lane {res['wrong_lane']}, "
          f"missed {res['missed']}, extra {res['extra']}")
    print(f"mean per-lane count accuracy {res['mean_lane_count_accuracy']:.1%} | weighted {res['weighted_count_accuracy']:.1%}"
          f" | crossing precision {res['precision']:.1%} recall {res['recall']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
