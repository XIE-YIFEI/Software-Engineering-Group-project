"""1-minute traffic series per lane and per direction, on the camera's own clock, from one or more
results JSON files of ``run_alpr.py`` on a lane camera (e.g. one run per hourly piece of a long recording):
the flow / occupancy / density / speed counterpart of ``python -m alpr.congestion --series`` (far cameras).

    python run_alpr.py --profile malaysia_selayang_lanes --backend openvino --headless --no-plates
    python -m alpr.series --runs outputs/malaysia_selayang_lanes.plates.json --clock0 2024-01-01T07:45:00 \\
        --out data/series/selayang.csv

Clock: second s of a source video is at  clock0 + piece_start + s + osd_offset, where clock0 is the
stream's start (yt-dlp ``release_timestamp``), piece_start the piece's offset in the stream (the
``<video>.json`` written next to the piece by alpr/download_hls.py; 0 without one) and osd_offset the
difference to the on-screen clock, read off frames (TEAM_GUIDE.md section 10.1).

Columns, for every lane and every lane group (direction), ``<name>.<measure>``:
  flow          vehicles crossing the count line in that minute, scaled to veh/min when only part of
                the minute was observed
  occupancy     share of the observed time the lane's stretch of the count line was covered (group:
                mean over its lanes)
  density       mean number of vehicles in the lane zone (group: sum over its lanes); veh/km when the
                camera is calibrated
  speed         mean speed (km/h) of the vehicles counted (calibrated cameras only)
  observed_sec  seconds of video analysed in that minute (0 = gap)
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

MEASURES = ("flow", "occupancy", "density", "speed", "observed_sec")


def piece_start(source: str) -> float:
    """Offset (s) of a piece in its stream, from the downloader's sidecar <video>.json (0 without one)."""
    side = os.path.splitext(source)[0] + ".json"
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            return float(json.load(f).get("start_sec", 0.0))
    return 0.0


def minute_series(reports: Sequence[dict], clock0: datetime, osd_offset: float = 0.0,
                  starts: Optional[Sequence[float]] = None) -> Dict:
    """Per-minute rows from results JSONs (each needs its "lanes" section). starts: stream offset of each
    report's video (default: from its sidecar). Returns {"minutes": [datetime], "lanes": [...],
    "groups": {group: [lanes]}, "rows": {name: {measure: [values]}}}."""
    reps = [r for r in reports if r.get("lanes")]
    if not reps:
        raise ValueError("no results with a 'lanes' section")
    lanes = reps[0]["lanes"]["lanes"]
    groups: Dict[str, List[str]] = defaultdict(list)
    for name in lanes:
        g = reps[0]["lanes"]["per_lane"][name].get("group") or "All"
        groups[g].append(name)
    acc = defaultdict(lambda: defaultdict(float))     # (minute, lane) -> sums
    seen = defaultdict(float)                          # minute -> observed seconds
    for k, rep in enumerate(reps):
        start = (starts[k] if starts is not None else piece_start(rep["source"])) + osd_offset
        base = clock0 + timedelta(seconds=start)
        lanes_rep = rep["lanes"]
        for c in lanes_rep["crossings"]:
            if c["lane"] in lanes:
                m = _minute(base + timedelta(seconds=c["time_sec"]))
                acc[(m, c["lane"])]["count"] += 1
        for lane, bins in lanes_rep["per_10s"].items():
            if lane not in lanes:
                continue
            for b in bins:
                t0 = base + timedelta(seconds=b["t_start"])
                dur = b["t_end"] - b["t_start"]
                obs = b.get("observed_sec", dur) if b.get("observed_sec") is not None else dur
                # split the bin over the minutes it overlaps (bins are 10 s, minutes are wall-clock)
                for m, share in _split(t0, dur):
                    w = obs * share
                    a = acc[(m, lane)]
                    a["w"] += w
                    a["occ"] += w * b.get("occupancy", 0.0)
                    a["dens"] += w * b.get("density_veh_km", b.get("mean_vehicles", 0.0))
                    if b.get("mean_speed_kmh") is not None and b.get("count"):
                        a["spd"] += b["mean_speed_kmh"] * b["count"] * share
                        a["spd_n"] += b["count"] * share
                    if lane == lanes[0]:
                        seen[m] += w
    if not seen:
        raise ValueError("no observed time")
    m0, m1 = min(seen), max(seen)
    minutes = [m0 + timedelta(minutes=i) for i in range(int((m1 - m0).total_seconds() // 60) + 1)]
    rows: Dict[str, Dict[str, List[float]]] = {}
    for lane in lanes:
        r = {k: [] for k in MEASURES}
        for m in minutes:
            a = acc.get((m, lane), {})
            obs = min(60.0, seen.get(m, 0.0))
            r["observed_sec"].append(round(obs, 1))
            if obs < 10.0:                                  # less than 10 s seen: no value
                for k in ("flow", "occupancy", "density", "speed"):
                    r[k].append(float("nan"))
                continue
            r["flow"].append(round(a.get("count", 0.0) * 60.0 / obs, 3))
            w = a.get("w", 0.0)
            r["occupancy"].append(round(a.get("occ", 0.0) / w, 4) if w else float("nan"))
            r["density"].append(round(a.get("dens", 0.0) / w, 3) if w else float("nan"))
            r["speed"].append(round(a["spd"] / a["spd_n"], 1) if a.get("spd_n") else float("nan"))
        rows[lane] = r
    for g, members in groups.items():
        r = {k: [] for k in MEASURES}
        for i in range(len(minutes)):
            vals = {k: [rows[l][k][i] for l in members] for k in MEASURES}
            r["observed_sec"].append(rows[members[0]]["observed_sec"][i])
            r["flow"].append(_nansum(vals["flow"]))
            r["occupancy"].append(_nanmean(vals["occupancy"]))
            r["density"].append(_nansum(vals["density"]))
            fl = [(s, f) for s, f in zip(vals["speed"], vals["flow"]) if s == s and f == f and f > 0]
            r["speed"].append(round(sum(s * f for s, f in fl) / sum(f for _, f in fl), 1) if fl else float("nan"))
        rows[g] = r
    return {"minutes": minutes, "lanes": list(lanes), "groups": dict(groups), "rows": rows}


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _split(t0: datetime, dur: float):
    """[(minute, share of dur)] for an interval starting at t0."""
    out, t, left = [], t0, dur
    while left > 1e-9:
        m = _minute(t)
        room = 60.0 - (t - m).total_seconds()
        d = min(room, left)
        out.append((m, d / dur))
        t += timedelta(seconds=d)
        left -= d
    return out


def _nansum(v):
    v = [x for x in v if x == x]
    return round(sum(v), 3) if v else float("nan")


def _nanmean(v):
    v = [x for x in v if x == x]
    return round(sum(v) / len(v), 4) if v else float("nan")


def write_csv(series: Dict, path: str) -> None:
    names = series["lanes"] + list(series["groups"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "minute_of_day"] + [f"{n}.{m}" for n in names for m in MEASURES])
        for i, m in enumerate(series["minutes"]):
            w.writerow([m.isoformat(timespec="minutes"), m.hour * 60 + m.minute]
                       + ["" if series["rows"][n][k][i] != series["rows"][n][k][i] else series["rows"][n][k][i]
                          for n in names for k in MEASURES])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="+", required=True, help="results JSONs (globs allowed), one camera-day")
    ap.add_argument("--clock0", required=True, help="wall-clock time of stream second 0 (ISO, camera time zone)")
    ap.add_argument("--osd-offset", type=float, default=0.0, help="on-screen clock minus stream clock (s)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    paths = sorted({p for g in args.runs for p in glob.glob(g)})
    reports = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            reports.append(json.load(f))
    s = minute_series(reports, datetime.fromisoformat(args.clock0), args.osd_offset)
    write_csv(s, args.out)
    print(f"{len(paths)} runs -> {len(s['minutes'])} minutes "
          f"({s['minutes'][0]:%H:%M} - {s['minutes'][-1]:%H:%M}), lanes {s['lanes']}, groups {list(s['groups'])} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
