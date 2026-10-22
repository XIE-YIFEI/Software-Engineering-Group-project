"""Congestion measurement for far, high cameras: how full each stretch of road is, every few seconds, over
hours of video (the Johor Bahru CIQ / Causeway cameras).

Why not count vehicles one by one? On these cameras cars are 15-40 px wide, half hidden by the parapets of
the elevated roads, and stand bumper to bumper in the morning queue: the COCO detector, even on 3x
enlarged tiles at confidence 0.05, finds 1 of the 38 cars queueing in camera C's four zones at 07:30 and
5 of 53 at 09:30 (TEAM_GUIDE.md section 10). A traffic engineer's video detector measures presence instead, and so does this module.

How: only the keyframes are decoded (one every ~5 s, very fast). In each, the share of every zone's pixels
that lie on an edge (Sobel magnitude >= `edge_thresh` on a slightly blurred grey image) is that zone's
*texture occupancy*: an empty carriageway is smooth asphalt with a few lane markings, a queue is a mosaic
of roofs, windows and gaps. Nothing is learnt from the same day, so a queue that lasts all morning is
still measured (an earlier version masked edges present in most frames of the day as "static"; on
camera C the all-morning queue was masked with them). The markings, kerbs and parapets that every frame
has are a constant per zone and lighting: the calibration to hand counts (vehicles = slope x occupancy +
intercept, with its own intercept at night) takes them out, and its leave-one-out error is reported.

    python -m alpr.congestion --profile jb_ciq_ipc_2023-03-21 --measure      # keyframes -> samples JSON
    python -m alpr.congestion --profile jb_ciq_ipc_2023-03-21 --gt-sheets    # images to hand-count
    python -m alpr.congestion --profile jb_ciq_ipc_2023-03-21 --calibrate    # fit + error vs hand counts
    python -m alpr.congestion --profile jb_ciq_ipc_2023-03-21 --series data/series/jb_ciq_ipc_2023-03-21.csv

The profile (alpr/config.yaml) gives the video pieces (``source`` may be a glob), the zones (``lanes``:
polygons with a group = direction; within a group the first zone is the head of the queue), the clock
(``video_start_time`` = camera clock at stream second 0; ``start_sec`` = first stream second used) and the
``congestion`` settings.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .config import DEFAULT_PATH, load_config
from .lanes import LaneMap

DEFAULTS = {"edge_thresh": 40.0,      # Sobel magnitude (on a slightly blurred grey image) that counts as an edge
            "night_sky": 100.0,       # mean grey level of `sky_box` below this = night lighting
            "sky_box": [0.0, 0.05, 1.0, 0.17],   # frame fractions measured for the lighting (the sky, if in view)
            "jam_occupancy": 0.45,    # (dashboard colours)
            "baseline_window": ["03:00", "05:00"],   # night hours whose median occupancy is each zone's baseline
            "jam_delta": 0.15,        # a zone this far above its baseline is jammed (queue = jammed zones)
            "every_keyframe": 1}      # use every Nth keyframe


def sources(pattern: str) -> List[str]:
    """The video pieces of a profile, in order (``source`` may be a glob)."""
    paths = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
    return [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".avi", ".mov"))]


def piece_start(path: str) -> float:
    side = os.path.splitext(path)[0] + ".json"
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            return float(json.load(f).get("start_sec", 0.0))
    return 0.0


def keyframes(path: str, every: int = 1) -> Iterator[Tuple[float, np.ndarray]]:
    """(seconds into the file, BGR image) of every `every`th keyframe; only keyframes are decoded."""
    import av
    with av.open(path) as c:
        s = c.streams.video[0]
        s.codec_context.skip_frame = "NONKEY"
        s.thread_type = "AUTO"
        for i, fr in enumerate(c.decode(s)):
            if i % every == 0:
                yield float(fr.time), fr.to_ndarray(format="bgr24")


class ZoneMeter:
    """Texture occupancy of each zone from one frame (pure image processing, no model)."""

    def __init__(self, lane_map: LaneMap, w: int, h: int, params: Dict):
        self.p = {**DEFAULTS, **(params or {})}
        self.lm = lane_map
        self.masks = {}
        for lane in lane_map.lanes:
            m = np.zeros((h, w), np.uint8)
            cv2.fillPoly(m, [np.int32(lane.polygon)], 1)
            self.masks[lane.name] = m.astype(bool)
        allm = np.zeros((h, w), bool)
        for m in self.masks.values():
            allm |= m
        ys, xs = np.nonzero(allm)
        pad = 12
        self.roi = (max(0, xs.min() - pad), max(0, ys.min() - pad), min(w, xs.max() + pad), min(h, ys.max() + pad))
        x1, y1, x2, y2 = self.roi
        self.zone_roi = {k: m[y1:y2, x1:x2] for k, m in self.masks.items()}

    def edges(self, frame: np.ndarray) -> np.ndarray:
        """Edge pixels inside the zones' bounding region (boolean, ROI coordinates)."""
        x1, y1, x2, y2 = self.roi
        g = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (3, 3), 0.8).astype(np.float32)
        mag = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
        return mag >= self.p["edge_thresh"]

    def lighting(self, frame: np.ndarray) -> str:
        """'night' or 'day' from the brightness of `sky_box` (lit roads look bright at night, the sky does not)."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.p["sky_box"]
        g = cv2.cvtColor(frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)], cv2.COLOR_BGR2GRAY)
        return "night" if float(g.mean()) < self.p["night_sky"] else "day"

    def measure(self, frame: np.ndarray) -> Tuple[str, Dict[str, float]]:
        """(lighting, {zone: texture occupancy 0..1})."""
        e = self.edges(frame)
        return self.lighting(frame), {name: float(e[m].mean()) for name, m in self.zone_roi.items()}

    def edge_mask(self, frame: np.ndarray) -> np.ndarray:
        """Full-frame boolean mask of the edge pixels inside the zones (what is measured; for the dashboard)."""
        out = np.zeros(frame.shape[:2], bool)
        x1, y1, x2, y2 = self.roi
        inzone = np.zeros(out[y1:y2, x1:x2].shape, bool)
        for m in self.zone_roi.values():
            inzone |= m
        out[y1:y2, x1:x2] = self.edges(frame) & inzone
        return out


def make_meter(cfg, w: int, h: int) -> ZoneMeter:
    lm = LaneMap.from_config(cfg.lanes, cfg.solid_lines, w, h)
    return ZoneMeter(lm, w, h, cfg.congestion)


def clock_of(cfg, path: str, sec: float) -> datetime:
    """Camera-clock time of second `sec` of piece `path`."""
    return datetime.fromisoformat(cfg.video_start_time) + timedelta(seconds=piece_start(path) + sec)


def measure_profile(cfg, out_path: str, log=print) -> Dict:
    """Every keyframe of every piece -> {time, lighting, zones: {name: texture occupancy}}."""
    paths = sources(cfg.source)
    if not paths:
        raise FileNotFoundError(cfg.source)
    cap = cv2.VideoCapture(paths[0])
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    meter = make_meter(cfg, w, h)
    gate = None
    if cfg.view_gate:
        from .pipeline import ViewGate
        gate = ViewGate.from_config(cfg.view_gate, w, h)
    every = int(meter.p["every_keyframe"])
    rows = []
    for p in paths:
        n0 = len(rows)
        p0 = piece_start(p)
        for sec, f in keyframes(p, every):
            if p0 + sec < cfg.start_sec or (gate is not None and not gate(f)):
                continue                                    # before the profile's start, or another view
            light, occ = meter.measure(f)
            rows.append({"time": clock_of(cfg, p, sec).isoformat(timespec="seconds"), "piece": os.path.basename(p),
                         "sec": round(sec, 3), "lighting": light, "zones": {k: round(v, 4) for k, v in occ.items()}})
        log(f"{os.path.basename(p)}: {len(rows) - n0} keyframes")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    res = {"profile": cfg.profile, "zones": [l.name for l in meter.lm.lanes],
           "groups": meter.lm.groups(), "params": meter.p, "samples": rows}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f)
    return res


# ---------------------------------------------------------------------------- calibration to hand counts
def load_counts(path: str) -> List[Tuple[str, str, int]]:
    """Hand counts: lines 'HH:MM:SS zone vehicles' (time = the keyframe's camera clock, as on the GT sheet)."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.split("#")[0].strip()
            if ln:
                t, z, n = ln.split()[:3]
                out.append((t, z, int(n)))
    return out


def _design(o: np.ndarray, night: np.ndarray) -> np.ndarray:
    cols = [o, np.ones_like(o)]
    if night.any() and (~night).any():
        cols.append(night.astype(float))                # its own intercept at night (lamps light the kerbs)
    return np.column_stack(cols)


def _fit(o: np.ndarray, night: np.ndarray, n: np.ndarray) -> np.ndarray:
    return np.linalg.lstsq(_design(o, night), n, rcond=None)[0]


def _predict(coef: np.ndarray, o: float, night: bool) -> float:
    v = coef[0] * o + coef[1] + (coef[2] if len(coef) > 2 and night else 0.0)
    return max(0.0, float(v))


def fit_calibration(pairs: Sequence[Tuple[str, str, str, float, int]], groups: Optional[Dict[str, List[str]]] = None) -> Dict:
    """Texture occupancy -> vehicles, one line per zone: vehicles = slope x occupancy + intercept (+ a night
    offset when both lightings were counted). Zones differ in length and distance, so they are not pooled.
    pairs: (zone, frame time, lighting, occupancy, hand count). Leave-one-frame-out errors per zone and for
    each group's total (all zones of a frame refitted without that frame)."""
    zones = sorted({p[0] for p in pairs})
    frames = sorted({p[1] for p in pairs})
    out: Dict = {"zones": {}, "groups": {}}
    loo_pred: Dict[Tuple[str, str], float] = {}
    for z in zones:
        P = [(t, l, o, n) for zz, t, l, o, n in pairs if zz == z]
        if len(P) < 4:
            continue
        o = np.array([p[2] for p in P]); night = np.array([p[1] == "night" for p in P]); n = np.array([p[3] for p in P], float)
        coef = _fit(o, night, n)
        errs = []
        for i, (t, l, oi, ni) in enumerate(P):
            m = np.arange(len(P)) != i
            ci = _fit(o[m], night[m], n[m])
            loo_pred[(z, t)] = _predict(ci, oi, l == "night")
            errs.append(loo_pred[(z, t)] - ni)
        errs = np.array(errs)
        out["zones"][z] = {"slope": float(coef[0]), "intercept": float(coef[1]),
                           "night_offset": float(coef[2]) if len(coef) > 2 else 0.0, "n": len(P),
                           "loo_mae": float(np.abs(errs).mean()), "loo_bias": float(errs.mean()),
                           "mean_count": float(n.mean()),
                           "corr": float(np.corrcoef(o, n)[0, 1]) if o.std() > 0 and n.std() > 0 else float("nan")}
    for g, members in (groups or {}).items():
        mem = [z for z in members if z in out["zones"]]
        if not mem:
            continue
        tot_true, tot_pred = [], []
        for t in frames:
            truth = [n for zz, tt, l, o, n in pairs if tt == t and zz in mem]
            if len(truth) == len(mem):
                tot_true.append(sum(truth))
                tot_pred.append(sum(loo_pred[(z, t)] for z in mem))
        if tot_true:
            e = np.array(tot_pred) - np.array(tot_true)
            out["groups"][g] = {"frames": len(tot_true), "loo_mae": float(np.abs(e).mean()), "loo_bias": float(e.mean()),
                                "mean_count": float(np.mean(tot_true)),
                                "relative_mae": float(np.abs(e).sum() / max(1e-9, np.sum(tot_true))),
                                "corr": float(np.corrcoef(tot_pred, tot_true)[0, 1]) if len(tot_true) > 2 else float("nan"),
                                "truth": tot_true, "loo_pred": [round(v, 1) for v in tot_pred]}
    return out


def vehicles(calib: Optional[Dict], zone: str, occupancy: float, lighting: str = "day") -> Optional[float]:
    """Calibrated vehicles in a zone (None when the zone has no calibration)."""
    c = (calib or {}).get("zones", {}).get(zone)
    if not c:
        return None
    return max(0.0, c["slope"] * occupancy + c["intercept"] + (c.get("night_offset", 0.0) if lighting == "night" else 0.0))


# ---------------------------------------------------------------------------- per-minute series
def baselines(res: Dict, window: Sequence[str]) -> Dict[str, float]:
    """Each zone's night baseline: median occupancy of the samples in the clock window (e.g. 03:00-05:00).
    Static texture the edge mask missed sits in it, so `occupancy - baseline` compares cameras."""
    lo, hi = window
    out = {}
    for z in res["zones"]:
        v = [s["zones"][z] for s in res["samples"] if lo <= s["time"][11:16] < hi]
        out[z] = float(np.median(v)) if v else 0.0
    return out


def minute_series(res: Dict, calib: Optional[Dict] = None, params: Optional[Dict] = None) -> Dict:
    """Per camera-clock minute, per zone and per group (direction):
      occupancy  texture occupancy, mean of the keyframes in that minute (group: mean of its zones)
      excess     occupancy above the zone's night baseline (no calibration needed)
      density    vehicles in the zone (calibrated to hand counts; group: sum)
      fill       density / jam density: the zone's standstill capacity, taken as its largest hand count in a
                 queue (group: sums). Traffic is congested above fill 0.5, the critical density of the
                 Greenshields model (TEAM_GUIDE.md section 10)
      queue      group only: how many of its zones are congested (fill >= 0.5; excess >= jam_delta without
                 a calibration)"""
    p = {**DEFAULTS, **res.get("params", {}), **(params or {})}
    base = baselines(res, p["baseline_window"])
    cap = (calib or {}).get("capacity", {})
    by_min: Dict[datetime, List[dict]] = {}
    for s in res["samples"]:
        m = datetime.fromisoformat(s["time"]).replace(second=0)
        by_min.setdefault(m, []).append(s)
    if not by_min:
        raise ValueError("no samples")
    m0, m1 = min(by_min), max(by_min)
    minutes = [m0 + timedelta(minutes=i) for i in range(int((m1 - m0).total_seconds() // 60) + 1)]
    zones, groups = res["zones"], res["groups"]
    nan = float("nan")
    rows = {k: {m: [] for m in SERIES_MEASURES} for k in zones + list(groups)}
    for m in minutes:
        ss = by_min.get(m, [])
        for z in zones:
            occ = [s["zones"][z] for s in ss]
            dens = [vehicles(calib, z, s["zones"][z], s["lighting"]) for s in ss]
            dens = [d for d in dens if d is not None]
            o = round(float(np.mean(occ)), 4) if occ else nan
            d = round(float(np.mean(dens)), 2) if dens else nan
            r = rows[z]
            r["occupancy"].append(o)
            r["excess"].append(round(o - base[z], 4) if occ else nan)
            r["density"].append(d)
            r["fill"].append(round(d / cap[z], 3) if d == d and cap.get(z) else nan)
            r["queue"].append(nan)
            r["observed_sec"].append(len(ss) * 5.0)
        for g, members in groups.items():
            col = {k: [rows[z][k][-1] for z in members] for k in ("occupancy", "excess", "density", "fill")}
            ok = {k: [v for v in vs if v == v] for k, vs in col.items()}
            r = rows[g]
            r["occupancy"].append(round(float(np.mean(ok["occupancy"])), 4) if ok["occupancy"] else nan)
            r["excess"].append(round(float(np.mean(ok["excess"])), 4) if ok["excess"] else nan)
            full = len(ok["density"]) == len(members) and ok["density"]
            dsum = float(np.sum(ok["density"])) if full else nan
            r["density"].append(round(dsum, 2) if full else nan)
            csum = sum(cap.get(z, 0) for z in members)
            r["fill"].append(round(dsum / csum, 3) if full and csum and all(cap.get(z) for z in members) else nan)
            if ok["fill"] and len(ok["fill"]) == len(members):
                r["queue"].append(float(sum(1 for f in ok["fill"] if f >= CRITICAL_FILL)))
            elif ok["excess"]:
                r["queue"].append(float(sum(1 for e in ok["excess"] if e >= p["jam_delta"])))
            else:
                r["queue"].append(nan)
            r["observed_sec"].append(len(ss) * 5.0)
    return {"minutes": minutes, "zones": zones, "groups": groups, "rows": rows, "baselines": base}


SERIES_MEASURES = ("occupancy", "excess", "density", "fill", "queue", "observed_sec")
CRITICAL_FILL = 0.5           # Greenshields: flow peaks at half the jam density; above it traffic is congested


def write_series_csv(series: Dict, path: str) -> None:
    import csv
    names = series["zones"] + list(series["groups"])
    measures = list(SERIES_MEASURES)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "minute_of_day"] + [f"{n}.{m}" for n in names for m in measures])
        for i, m in enumerate(series["minutes"]):
            vals = []
            for n in names:
                for k in measures:
                    v = series["rows"][n][k][i]
                    vals.append("" if v != v else v)
            w.writerow([m.isoformat(timespec="minutes"), m.hour * 60 + m.minute] + vals)


# ---------------------------------------------------------------------------- GT sheets
def gt_sheet(frame: np.ndarray, meter: ZoneMeter, group: str, scale: float = 2.6,
             occupancy: Optional[Dict[str, float]] = None) -> np.ndarray:
    """Hand-counting sheet of one direction: each of its zones cut out of the keyframe, enlarged, the
    surroundings dimmed and the zone outlined, stacked top to bottom (one zone = one count)."""
    H, W = frame.shape[:2]
    tiles = []
    for lane in meter.lm.lanes:
        if lane.group != group:
            continue
        P = np.asarray(lane.polygon)
        x1, y1 = np.floor(P.min(0)).astype(int) - 6
        x2, y2 = np.ceil(P.max(0)).astype(int) + 6
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        crop = frame[y1:y2, x1:x2].copy()
        m = np.zeros(crop.shape[:2], np.uint8)
        cv2.fillPoly(m, [np.int32(P - [x1, y1])], 255)
        crop[m == 0] = (crop[m == 0] * 0.3).astype(np.uint8)
        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        cv2.polylines(big, [np.int32((P - [x1, y1]) * scale)], True, (0, 255, 255), 1, cv2.LINE_AA)
        label = lane.name + (f"  occ {occupancy[lane.name]:.2f}" if occupancy and lane.name in occupancy else "")
        cv2.rectangle(big, (0, 0), (len(label) * 12 + 8, 24), (0, 0, 0), -1)
        cv2.putText(big, label, (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(big)
    if not tiles:
        raise ValueError(f"no zones in group {group!r}")
    w = max(t.shape[1] for t in tiles)
    rows = []
    for t in tiles:
        rows += [np.hstack([t, np.zeros((t.shape[0], w - t.shape[1], 3), np.uint8)]), np.full((6, w, 3), 60, np.uint8)]
    return np.vstack(rows[:-1])


def _secs(hms: str) -> int:
    h, m, s = (int(v) for v in hms.split(":"))
    return h * 3600 + m * 60 + s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--samples", help="samples JSON (default outputs/causeway/<profile>.samples.json)")
    ap.add_argument("--measure", action="store_true", help="decode the keyframes and measure every zone")
    ap.add_argument("--gt-sheets", nargs="*", metavar="HH:MM:SS", help="write hand-counting sheets (enlarged zones, "
                    "one image per direction) of the keyframes nearest these times (default: 3 random keyframes per "
                    "lighting + the busiest)")
    ap.add_argument("--counts", help="hand counts 'HH:MM:SS zone vehicles' (default alpr/gt/causeway/<profile>.txt)")
    ap.add_argument("--calibrate", action="store_true", help="fit occupancy -> vehicles and report the error")
    ap.add_argument("--series", help="write the per-minute series CSV here")
    args = ap.parse_args(argv)
    cfg = load_config(args.profile, args.config)
    samples = args.samples or f"outputs/causeway/{args.profile}.samples.json"
    counts_path = args.counts or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt", "causeway",
                                              f"{args.profile}.txt")
    if args.measure:
        res = measure_profile(cfg, samples)
        print(f"{len(res['samples'])} keyframes -> {samples}")
    with open(samples, encoding="utf-8") as f:
        res = json.load(f)
    if args.gt_sheets is not None:
        out_dir = os.path.join(os.path.dirname(samples), f"{args.profile}_gt")
        os.makedirs(out_dir, exist_ok=True)
        rng = np.random.default_rng(0)
        picks = []
        if args.gt_sheets:                              # the keyframe nearest to each requested clock time
            secs = [_secs(s["time"][11:19]) for s in res["samples"]]
            for want in args.gt_sheets:
                w = _secs(want if want.count(":") == 2 else want + ":00")
                picks.append(res["samples"][int(np.argmin([abs(x - w) for x in secs]))])
        else:
            for light in ("night", "day"):
                pool = [s for s in res["samples"] if s["lighting"] == light]
                picks += [pool[i] for i in rng.choice(len(pool), min(3, len(pool)), replace=False)] if pool else []
            busiest = max(res["samples"], key=lambda s: sum(s["zones"].values()))
            picks.append(busiest)
        paths = {os.path.basename(p): p for p in sources(cfg.source)}
        cap0 = cv2.VideoCapture(next(iter(paths.values())))
        w, h = int(cap0.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap0.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap0.release()
        meter = make_meter(cfg, w, h)
        for s in picks:
            cap = cv2.VideoCapture(paths[s["piece"]])
            cap.set(cv2.CAP_PROP_POS_MSEC, s["sec"] * 1000.0)
            ok, f = cap.read()
            cap.release()
            if ok:
                for g in res["groups"]:
                    p = os.path.join(out_dir, f"{s['time'][11:19].replace(':', '')}_{s['lighting']}_{g.replace(' ', '')}.png")
                    cv2.imwrite(p, gt_sheet(f, meter, g, occupancy=s["zones"]))
                    print(p)
    calib = None
    calib_path = os.path.splitext(samples)[0].replace(".samples", "") + ".calibration.json"
    if args.calibrate:
        if not os.path.exists(counts_path):
            print(f"no hand counts at {counts_path}")
        else:
            gt = load_counts(counts_path)
            by_t = {s["time"][11:19]: s for s in res["samples"]}
            pairs = [(z, t, by_t[t]["lighting"], by_t[t]["zones"][z], n) for t, z, n in gt
                     if t in by_t and z in by_t[t]["zones"]]
            calib = fit_calibration(pairs, res["groups"])
            calib["counts"] = os.path.relpath(counts_path)
            calib["capacity"] = {z: max(n for zz, _, _, _, n in pairs if zz == z) for z in calib["zones"]}
            calib["capacity_note"] = "jam density per zone = its largest hand count (a standstill queue)"
            with open(calib_path, "w", encoding="utf-8") as f:
                json.dump(calib, f, indent=1)
            for z, c in calib["zones"].items():
                print(f"  {z}: vehicles = {c['slope']:.1f} x occupancy {c['intercept']:+.1f} (night {c['night_offset']:+.1f})  (n {c['n']}, r {c['corr']:.2f}, "
                      f"leave-one-out MAE {c['loo_mae']:.1f} veh, mean {c['mean_count']:.1f})")
            for g, c in calib["groups"].items():
                print(f"{g}: total of its zones, leave-one-frame-out MAE {c['loo_mae']:.1f} veh on a mean of "
                      f"{c['mean_count']:.1f} ({100 * c['relative_mae']:.0f} %), r {c['corr']:.2f}, bias {c['loo_bias']:+.1f}")
            print(f"calibration -> {calib_path}")
    elif os.path.exists(calib_path):
        with open(calib_path, encoding="utf-8") as f:
            calib = json.load(f)
    if args.series:
        s = minute_series(res, calib, cfg.congestion)
        write_series_csv(s, args.series)
        print(f"{len(s['minutes'])} minutes ({s['minutes'][0]:%H:%M} - {s['minutes'][-1]:%H:%M}) -> {args.series}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
