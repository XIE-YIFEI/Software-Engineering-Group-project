"""Time-lapse demo of a congestion camera (alpr/congestion.py) with the forecast panel, plus the forecast
series as JSON.

    python -m alpr.forecast_demo --profile jb_ciq_cam02_2023-03-22 \\
        --train data/series/jb_ciq_ipc_2023-03-21.csv --test data/series/jb_ciq_cam02_2023-03-22.csv \\
        --entity "To CIQ" --pool TC1,TC2,TC3,TC4 --from 05:30 --to 09:50 --every 10 \\
        --out demo/jb_ciq_forecast_timelapse.mp4

Each video frame is one keyframe of the test day, one every --every seconds of camera time (10 s of camera
time per frame at 30 FPS = 300x speed): the camera view with the zones tinted by their state (free /
dense / queue by fill) and the measured vehicle edges highlighted; the zone table (calibrated vehicles,
fill); and the forecast card of the direction: the last hour as measured, what the model forecast for
that hour 15 minutes earlier, and its current forecast for +5 / +15 / +30 minutes with an error band.

The model (LightGBM on the fill series, pooled over the zones of the direction) is trained on --train
only. Its error band is its MAE on the last 30 % of the training day, with the model refitted on the
first 70 %, so nothing of the test day is used except its past. <out>.forecast.json holds the test day's
per-minute measured series and the forecasts made at every minute (same model).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Dict, Sequence

import cv2
import numpy as np

from .config import DEFAULT_PATH, load_config
from .congestion import CRITICAL_FILL, make_meter, sources, vehicles
from .lanes import LaneMap
from .forecast import HORIZONS, LightGBMForecaster, Target, future_target, load_series, mae_rmse, target_of
from .hud import FORECAST_RGB, GREEN, LABEL, ORANGE, RED, YELLOW, _bgr, forecast_status, render_dashboard
from .pipeline import VideoOut

LAG = 15                       # the "forecast made earlier" line uses the 15-minute forecasts


def train_with_band(train: Target, pool: Sequence[Target] = (), horizons=HORIZONS) -> Dict:
    """Model fitted on the whole training day (+ pooled series) and its MAE per horizon on the last 30 %
    of that day, from a model refitted on the first 70 % (the error band shown on the card)."""
    n = len(train.y)
    cut = int(0.7 * n)
    head = Target(train.y[:cut], train.minute_of_day[:cut], {})
    head_pool = [Target(p.y[:cut], p.minute_of_day[:cut], {}) for p in pool]
    m = LightGBMForecaster().fit(head, horizons, head_pool)
    fc = m.forecast(train, horizons)
    band = {}
    for h in horizons:
        o = np.arange(cut, n - h)
        band[h] = mae_rmse(future_target(train.y, h)[o], fc[h][o])[0]
    model = LightGBMForecaster().fit(train, horizons, pool)
    return {"model": model, "band": band}


def card(title: str, measure: str, test: Target, fc: Dict[int, np.ndarray], band: Dict[int, float], i: int,
         threshold, note: str) -> Dict:
    """Forecast card data at test minute index i (minutes relative to now)."""
    y = test.y
    hist = [(k - i, float(y[k])) for k in range(max(0, i - 60), i + 1) if y[k] == y[k]]
    past = [(k - i, float(fc[LAG][k - LAG])) for k in range(max(LAG, i - 60 + LAG), i + 1)
            if fc[LAG][k - LAG] == fc[LAG][k - LAG]]
    now = {h: (float(fc[h][i]) if fc[h][i] == fc[h][i] else None) for h in fc}
    return {"title": title, "measure": measure, "model": "LightGBM", "threshold": threshold, "history": hist,
            "past_forecast": past, "lag": LAG, "forecast": now, "error": band, "note": note}


def _status_rgb(status: str):
    return RED if status.startswith("Congested") else ORANGE if status.startswith("Congestion") else GREEN


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="profile of the test camera-day")
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--train", required=True, help="series CSV of the training day")
    ap.add_argument("--test", required=True, help="series CSV of the test day (this profile)")
    ap.add_argument("--entity", default="To CIQ")
    ap.add_argument("--pool", default="", help="zones of the training day pooled into the model's training data")
    ap.add_argument("--from", dest="t_from", default="00:00")
    ap.add_argument("--to", dest="t_to", default="23:59")
    ap.add_argument("--every", type=float, default=10.0, help="camera seconds between frames")
    ap.add_argument("--samples", help="samples JSON of the test day (default outputs/causeway/<profile>.samples.json)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cfg = load_config(args.profile, args.config)
    samples_path = args.samples or f"outputs/causeway/{args.profile}.samples.json"
    with open(samples_path, encoding="utf-8") as f:
        res = json.load(f)
    calib_path = os.path.splitext(samples_path)[0].replace(".samples", "") + ".calibration.json"
    with open(calib_path, encoding="utf-8") as f:
        calib = json.load(f)
    tr, te = load_series(args.train), load_series(args.test)
    pool = [target_of(tr, z, "fill") for z in args.pool.split(",") if z and f"{z}.fill" in tr.columns]
    t_tr, t_te = target_of(tr, args.entity, "fill"), target_of(te, args.entity, "fill")
    trained = train_with_band(t_tr, pool)
    fc = trained["model"].forecast(t_te, HORIZONS)
    minute_index = {t[:16]: k for k, t in enumerate(te.times)}
    # frames
    paths = {os.path.basename(p): p for p in sources(cfg.source)}
    cap0 = cv2.VideoCapture(next(iter(paths.values())))
    w, h = int(cap0.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap0.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap0.release()
    meter = make_meter(cfg, w, h)
    capacity = calib["capacity"]
    group = [l for l in meter.lm.lanes if l.group == args.entity and l.name in calib["zones"]]
    shown = LaneMap(group, meter.lm.solid_lines)                  # only the calibrated zones are drawn
    in_group = np.zeros((h, w), bool)
    for l in group:
        in_group |= meter.masks[l.name]
    g = calib["groups"].get(args.entity, {})
    note = [f"Trained on {tr.name[-10:]} only · tested on {te.name[-10:]}",
            f"Calibrated on {g.get('frames', 0)} hand-counted frames (±{g.get('loo_mae', float('nan')):.0f} veh)",
            "Congested: over half the jam capacity"]
    picks, last = [], None
    for s in res["samples"]:
        t = datetime.fromisoformat(s["time"])
        if not args.t_from <= t.strftime("%H:%M") <= args.t_to:
            continue
        if last is None or (t - last).total_seconds() >= args.every - 1e-6:
            picks.append(s)
            last = t
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    writer = VideoOut(args.out, 30.0, tuple(cfg.dashboard_size))
    caps: Dict[str, cv2.VideoCapture] = {}
    edge_bgr = np.array(_bgr(FORECAST_RGB), np.float32)
    for n, s in enumerate(picks):
        cap = caps.setdefault(s["piece"], cv2.VideoCapture(paths[s["piece"]]))
        cap.set(cv2.CAP_PROP_POS_MSEC, s["sec"] * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        sel = meter.edge_mask(frame) & in_group
        view = frame.copy()
        view[sel] = (0.5 * view[sel] + 0.5 * edge_bgr).astype(np.uint8)
        t = datetime.fromisoformat(s["time"])
        i = minute_index.get(t.strftime("%Y-%m-%dT%H:%M"))
        rows = []
        for lane in group:
            veh = vehicles(calib, lane.name, s["zones"][lane.name], s["lighting"])
            f_ = veh / capacity[lane.name] if veh is not None and capacity.get(lane.name) else 0.0
            rows.append({"name": lane.name, "group": lane.group, "direction": lane.direction, "polygon": lane.polygon,
                         "count": int(round(veh)) if veh is not None else 0, "rate": None, "occupancy": f_,
                         "density": veh or 0.0, "density_unit": "veh", "speed": None})
        cards = [card(args.entity, "fill", t_te, fc, trained["band"], i, CRITICAL_FILL, note)] if i is not None else []
        lanes = {"map": shown, "rows": rows, "events": [], "withheld": {}, "calibrated": False, "simulated": False,
                 "fps": 30.0, "t0": 0.0, "forecast": cards, "state_thresholds": (CRITICAL_FILL, 0.25),
                 "panel_title": "Zones", "panel_caption": "vehicles · share of jam capacity"}
        status, _ = forecast_status(cards[0]) if cards else ("Collecting history", LABEL)
        grp = [r for r in rows if r["group"] == args.entity]
        veh_now = sum(r["density"] for r in grp)
        fill_now = veh_now / max(1e-6, sum(capacity.get(l.name, 0) for l in group))
        q = sum(1 for r in grp if r["occupancy"] >= CRITICAL_FILL)
        stats = {"fps": 0.0, "fps_video": 30.0, "time_sec": 0.0, "count": 0, "by_type": {}, "rate_per_min": 0.0,
                 "plates": 0, "confirmed": 0, "alerts": None, "legend": "congestion",
                 "legend_note": f"{t:%a %d %b %Y} · one keyframe every {args.every:.0f} s of camera time",
                 "kpis": [("Camera time", t.strftime("%H:%M:%S"), LABEL),
                          (f"{args.entity}", f"{veh_now:.0f} veh", LABEL),
                          ("Jam capacity used", f"{fill_now * 100:.0f}%", RED if fill_now >= CRITICAL_FILL else LABEL),
                          ("Queue", f"{q}/{len(grp)} zones", RED if q >= 2 else (YELLOW if q else GREEN)),
                          ("Outlook", status.replace(" · next 30 min", ""), _status_rgb(status))]}
        writer.write(render_dashboard(view, [], [], stats, cfg, lanes=lanes))
        if n % 200 == 0:
            print(f"{n}/{len(picks)} {s['time']} {status}", flush=True)
    writer.close()
    for c in caps.values():
        c.release()
    rep = {"profile": args.profile, "entity": args.entity, "train": tr.name, "test": te.name,
           "model": "LightGBM (fill, pooled over the zones of the training day)" if pool else "LightGBM (fill)",
           "horizons": list(HORIZONS), "congestion_fill": CRITICAL_FILL, "minutes": te.times,
           "measured_fill": [None if v != v else float(v) for v in t_te.y],
           "forecast_fill": {str(h): [None if v != v else round(float(v), 4) for v in fc[h]] for h in fc},
           "error_band_mae": {str(h): trained["band"][h] for h in trained["band"]}}
    with open(os.path.splitext(args.out)[0] + ".forecast.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    print(f"{len(picks)} frames -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
