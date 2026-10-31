"""Traffic analytics: flow per vehicle type, lane and time; per-lane occupancy, density and speed; lane events;
CSV export. Flow comes from the counting line, independent of the plates (vehicles are counted even when their plate
cannot be read), and synthetic vehicles are never counted."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional

from .. import db
from ..models import LaneEventOut, LaneRow, RunOut, Series, TrafficSummary
from ..privacy import audit, can_see_full, mask_plate
from ..security import User, require
from .common import can_play, in_clause, provenance_label, run_video_url, title, video_ref, visible_camera_ids

ROLES = ("police", "traffic", "admin")
SPEED_REL_ERROR = 0.065      # 1-sigma relative calibration error of speed (200 Monte Carlo runs, TEAM_GUIDE.md 5.3)
METRICS = {"occupancy": "occupancy", "density": "density_veh_km", "speed": "mean_speed_kmh",
           "flow": "veh_per_min", "count": "count"}


def run_out(r: dict, user: Optional[User] = None) -> RunOut:
    playable = r["video_path"] and (user is None or can_play(user, r["camera_kind"]))
    return RunOut(id=r["id"], code=r["code"], label=r["label"], camera_id=r["camera_id"], camera_name=r["camera_name"],
                  camera_kind=r["camera_kind"], origin=r["origin"], media=r["media"], profile=r["profile"], fps=r["fps"],
                  frames=r["frames"], window_start_sec=r["window_start_sec"], window_end_sec=r["window_end_sec"],
                  clock_origin=r["clock_origin"], video_url=run_video_url(r["id"]) if playable else None,
                  video_kind=r["video_kind"], video_offset_sec=r["video_offset_sec"] or 0.0,
                  image_url=f"/media/runs/{r['id']}/image" if r.get("image_path") else None,
                  provenance_note=r["provenance_note"], flow_total=r["flow_total"],
                  flow_by_type=json.loads(r["flow_by_type"] or "{}"),
                  registry_rules=json.loads(r["registry_rules"]) if r["registry_rules"] else None,
                  registry_disclaimer=r["registry_disclaimer"], results_path=r["results_path"],
                  results_sha256=r["results_sha256"], imported_at=r["imported_at"])


def _run(conn: sqlite3.Connection, user: User, run_id: int) -> dict:
    r = db.row(conn, "SELECT r.*, c.name AS camera_name, c.kind AS camera_kind FROM runs r JOIN cameras c "
                     "ON c.id = r.camera_id WHERE r.id = ?", (run_id,))
    if r is None or r["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such run")
    return r


def runs(conn: sqlite3.Connection, user: User) -> List[RunOut]:
    """Runs with traffic data (a counting line or lanes) that this role may see, lane cameras first."""
    require(user, *ROLES)
    rs = db.rows(conn, f"SELECT r.*, c.name AS camera_name, c.kind AS camera_kind FROM runs r JOIN cameras c ON "
                       f"c.id = r.camera_id WHERE r.camera_id IN {in_clause(visible_camera_ids(conn, user))} AND "
                       f"(r.flow_total IS NOT NULL OR EXISTS (SELECT 1 FROM lanes l WHERE l.run_id = r.id)) "
                       f"ORDER BY c.kind = 'lane' DESC, r.id")
    return [run_out(r, user) for r in rs]


def summary(conn: sqlite3.Connection, user: User, run_id: int) -> TrafficSummary:
    """KPIs and the per-lane table of one run."""
    require(user, *ROLES)
    r = _run(conn, user, run_id)
    dur = float((r["window_end_sec"] or 0) - (r["window_start_sec"] or 0)) or 1.0
    lanes = [LaneRow(name=l["name"], group=l["grp"], direction=l["direction"], count=l["count"],
                     by_type=json.loads(l["by_type"]), veh_per_min=l["veh_per_min"], occupancy=l["occupancy"],
                     density=l["density"], density_unit=l["density_unit"], zone_length_m=l["zone_length_m"],
                     mean_speed_kmh=l["mean_speed_kmh"],
                     speed_error_kmh=round(SPEED_REL_ERROR * l["mean_speed_kmh"], 1) if l["mean_speed_kmh"] else None)
             for l in db.rows(conn, "SELECT * FROM lanes WHERE run_id = ? ORDER BY position", (run_id,))]
    groups: Dict[str, Dict[str, Optional[float]]] = {}
    for g in dict.fromkeys(l.group for l in lanes):
        ls = [l for l in lanes if l.group == g]
        n = sum(l.count for l in ls)
        sp = [(l.mean_speed_kmh, l.count) for l in ls if l.mean_speed_kmh is not None]
        groups[g] = {"count": n, "occupancy": round(sum(l.occupancy or 0 for l in ls) / len(ls), 3),
                     "speed_kmh": round(sum(v * c for v, c in sp) / max(1, sum(c for _, c in sp)), 1) if sp else None}
    by_type = json.loads(r["flow_by_type"] or "{}")
    total = r["flow_total"] or sum(l.count for l in lanes)
    outside = db.scalar(conn, "SELECT COUNT(*) FROM crossings WHERE run_id = ? AND COALESCE(lane, '') = ''", (run_id,))
    withheld = {w["code"]: w["count"] for w in db.rows(conn, "SELECT * FROM withheld WHERE run_id = ?", (run_id,))}
    note = db.scalar(conn, "SELECT note FROM withheld WHERE run_id = ? LIMIT 1", (run_id,)) or ""
    audit(conn, user, "view", "traffic", run_id)
    return TrafficSummary(run=run_out(r, user), total=total, duration_sec=dur, veh_per_min=round(total * 60.0 / dur, 1),
                          by_type=by_type, lanes=lanes, outside_lanes=outside if lanes else 0, groups=groups,
                          withheld=withheld, withheld_note=note,
                          speed_note="Speeds only on calibrated cameras, ±6.5 % (1σ) calibration error"
                          if any(l.mean_speed_kmh for l in lanes) else "")


def flow_series(conn: sqlite3.Connection, user: User, run_id: int, by: str = "type", bin_sec: float = 10.0) -> Series:
    """Vehicles per time bin, split by vehicle type or lane (lane cameras), or the total."""
    require(user, *ROLES)
    r = _run(conn, user, run_id)
    t0, t1 = r["window_start_sec"] or 0.0, r["window_end_sec"] or 0.0
    crossings = db.rows(conn, "SELECT time_sec, lane, vtype FROM crossings WHERE run_id = ?", (run_id,))
    if not crossings:                                  # plate cameras: the 10 s bins of the counting line
        bins = db.rows(conn, "SELECT * FROM flow_bins WHERE run_id = ? ORDER BY t_start", (run_id,))
        x = [b["t_start"] for b in bins]
        if by == "time":
            return Series(x=x, x_label="seconds", series={"Vehicles": [b["total"] for b in bins]}, unit="vehicles")
        types = sorted({k for b in bins for k in json.loads(b["by_type"])})
        return Series(x=x, x_label="seconds", unit="vehicles",
                      series={t: [json.loads(b["by_type"]).get(t, 0) for b in bins] for t in types})
    n_bins = max(1, int((t1 - t0 + bin_sec - 1e-9) // bin_sec))
    x = [round(t0 + i * bin_sec, 2) for i in range(n_bins)]
    key = {"type": "vtype", "lane": "lane"}.get(by)
    acc: Dict[str, List[int]] = defaultdict(lambda: [0] * n_bins)
    for c in crossings:
        i = min(n_bins - 1, max(0, int((c["time_sec"] - t0) // bin_sec)))
        acc[(c[key] or "?") if key else "Vehicles"][i] += 1
    order = [l["name"] for l in db.rows(conn, "SELECT name FROM lanes WHERE run_id = ? ORDER BY position", (run_id,))]
    names = sorted(acc, key=lambda k: (order.index(k) if k in order else 99, k))
    return Series(x=x, x_label="seconds", series={k: acc[k] for k in names}, unit="vehicles")


def lane_series(conn: sqlite3.Connection, user: User, run_id: int, metric: str = "occupancy") -> Series:
    """One line per lane of a lane-camera run: occupancy, density (veh/km), speed (km/h), flow (veh/min) or count."""
    require(user, *ROLES)
    run = _run(conn, user, run_id)
    col = METRICS.get(metric)
    if col is None:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}")
    end = run["window_end_sec"] if run["window_end_sec"] is not None else 1e12   # bins after the clip ended are empty
    bins = db.rows(conn, f"SELECT lane, t_start, {col} AS v FROM lane_bins WHERE run_id = ? AND t_start < ? "
                         f"ORDER BY t_start", (run_id, end))
    order = [l["name"] for l in db.rows(conn, "SELECT name FROM lanes WHERE run_id = ? ORDER BY position", (run_id,))]
    x = sorted({b["t_start"] for b in bins})
    idx = {t: i for i, t in enumerate(x)}
    series = {lane: [None] * len(x) for lane in order}
    for b in bins:
        series.setdefault(b["lane"], [None] * len(x))[idx[b["t_start"]]] = b["v"]
    unit = {"occupancy": "share of time", "density": "veh/km", "speed": "km/h", "flow": "veh/min", "count": "vehicles"}
    return Series(x=x, x_label="seconds", series=series, unit=unit[metric])


def events(conn: sqlite3.Connection, user: User, run_id: int) -> List[LaneEventOut]:
    """Lane events (lane changes, hazards, violations) in time order. Violations of real vehicles are withheld by
    the pipeline, so every violation here is a synthetic vehicle with a fictitious plate."""
    require(user, *ROLES)
    run = _run(conn, user, run_id)
    out = []
    for e in db.rows(conn, "SELECT * FROM lane_events WHERE run_id = ? ORDER BY time_sec", (run_id,)):
        full = can_see_full(conn, user, None, e["compact"]) if e["compact"] else True
        aid = db.scalar(conn, "SELECT id FROM alerts WHERE lane_event_id = ?", (e["id"],))
        out.append(LaneEventOut(id=e["id"], code=e["code"], title=title(e["code"]), severity=e["severity"],
                                time_sec=e["time_sec"], seen_at=e["seen_at"], lane=e["lane"], from_lane=e["from_lane"],
                                message=e["message"], vtype=e["vtype"],
                                plate=(e["plate_text"] or "") if full else mask_plate(e["plate_text"]),
                                provenance=e["provenance"], provenance_label=provenance_label(e["provenance"]),
                                value=e["value"], error_1sigma=e["error_1sigma"], video=video_ref(run, e["time_sec"], user),
                                alert_id=aid))
    return out


EXPORTS = ("crossings", "lane_bins", "flow_bins", "lanes", "events")


def export_csv(conn: sqlite3.Connection, user: User, run_id: int, what: str = "crossings") -> str:
    require(user, *ROLES)
    run = _run(conn, user, run_id)
    if what not in EXPORTS:
        raise ValueError(f"export one of {', '.join(EXPORTS)}")
    buf = io.StringIO()
    w = csv.writer(buf)
    if what == "events":
        evs = events(conn, user, run_id)
        w.writerow(["seen_at", "time_sec", "code", "severity", "lane", "from_lane", "message", "vehicle_type", "plate",
                    "value", "error_1sigma", "provenance"])
        for e in evs:
            w.writerow([e.seen_at, e.time_sec, e.code, e.severity, e.lane, e.from_lane, e.message, e.vtype, e.plate,
                        e.value, e.error_1sigma, e.provenance])
    else:
        cols = {"crossings": "seen_at, time_sec, lane, vtype, direction",
                "lane_bins": "lane, t_start, t_end, count, veh_per_min, occupancy, mean_vehicles, density_veh_km, "
                             "mean_speed_kmh",
                "flow_bins": "t_start, t_end, total, by_type",
                "lanes": "name, grp, direction, count, by_type, veh_per_min, occupancy, density, density_unit, "
                         "zone_length_m, mean_speed_kmh"}[what]
        rs = db.rows(conn, f"SELECT {cols} FROM {what} WHERE run_id = ?", (run_id,))
        w.writerow([c.strip() for c in cols.split(",")])
        for r in rs:
            w.writerow(list(r.values()))
    audit(conn, user, "export", "traffic", run_id, {"format": "csv", "what": what, "run": run["code"]})
    return buf.getvalue()
