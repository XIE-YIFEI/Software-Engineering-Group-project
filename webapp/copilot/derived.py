"""Figures derived from the service layer's results, so that the model never has to do arithmetic: shares, congestion
episodes with their durations, the longest-queueing zone, forecast accuracy per horizon with the warning lead times,
and the reductions a what-if projects. Every function calls the ordinary service functions (permissions, scoping and
audit apply) and only adds the derived numbers.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import numpy as np

from .. import db
from ..security import User
from ..services import forecast as forecast_svc
from ..services import overview as overview_svc
from ..services import traffic as traffic_svc


def share(part: float, total: float) -> Optional[float]:
    return round(100.0 * part / total, 1) if total else None


def pct(fill: Optional[float]) -> Optional[float]:
    """Fill (vehicles / jam capacity) as a percentage."""
    return None if fill is None else round(100.0 * fill, 1)


def flows(conn: sqlite3.Connection, user: User, date: Optional[str] = None) -> Dict[str, Any]:
    """Vehicles counted on a day: total, by type (with shares), by camera, and the lane-camera runs of that day."""
    ov = overview_svc.summary(conn, user, date)
    total = ov.flow_total
    lanes = []
    for r in traffic_svc.runs(conn, user):
        if r.clock_origin[:10] == ov.date and r.camera_kind == "lane":
            s = traffic_svc.summary(conn, user, r.id)
            lanes.append({"run_id": r.id, "run": r.label, "camera": r.camera_name, "vehicles": s.total,
                          "minutes": round(s.duration_sec / 60.0, 1), "veh_per_min": s.veh_per_min,
                          "by_type": s.by_type, "lanes": len(s.lanes),
                          "groups": {g: {"vehicles": v["count"], "mean_occupancy_pct": pct(v["occupancy"]),
                                         "speed_kmh": v["speed_kmh"]} for g, v in s.groups.items()},
                          "busiest_lane": max(s.lanes, key=lambda l: l.count).name if s.lanes else None,
                          "withheld_real_violations": sum(s.withheld.values()), "withheld_note": s.withheld_note})
    return {"date": ov.date, "other_dates": [d for d in ov.dates if d != ov.date], "demo_day": ov.demo_day,
            "vehicles": total, "by_type": ov.flow_by_type,
            "by_type_share_pct": {k: share(v, total) for k, v in ov.flow_by_type.items()},
            "by_camera": ov.flow_by_camera, "lane_runs": lanes,
            "note": "Counted at the counting line (or the lanes), independent of the plates; synthetic vehicles are "
                    "not counted."}


def episodes(conn: sqlite3.Connection, user: User, day: str) -> Dict[str, Any]:
    """Congested episodes of every zone and direction of a congestion day (the forecast's definition: the 5-minute
    mean fill >= the threshold, short dips merged, at least 10 minutes), the minutes congested, the peak, and the
    longest-queueing zone."""
    from alpr.forecast import ONSET_MIN_LEN, congestion_state
    d = forecast_svc._day(conn, user, day)
    thr = db.scalar(conn, "SELECT congestion_fill FROM forecast_runs WHERE day_id = ? LIMIT 1", (d["id"],)) or 0.5
    kinds = {r["entity"]: r["kind"] for r in db.rows(conn, "SELECT entity, kind FROM calibrations WHERE day_id = ?",
                                                     (d["id"],))}
    out: Dict[str, Any] = {}
    for ent in [r["entity"] for r in db.rows(conn, "SELECT DISTINCT entity FROM zone_minutes WHERE day_id = ? "
                                                   "ORDER BY entity", (d["id"],))]:
        rows = db.rows(conn, "SELECT minute, fill FROM zone_minutes WHERE day_id = ? AND entity = ? ORDER BY minute",
                       (d["id"], ent))
        y = np.array([np.nan if r["fill"] is None else r["fill"] for r in rows], float)
        if not np.isfinite(y).any():
            out[ent] = {"kind": kinds.get(ent, "zone"), "calibrated": False, "episodes": []}
            continue
        st = congestion_state(y, thr)
        eps, i = [], 0
        while i < len(st):
            if st[i]:
                j = i
                while j < len(st) and st[j]:
                    j += 1
                if j - i >= ONSET_MIN_LEN:
                    eps.append({"start": rows[i]["minute"][11:16], "end": rows[j - 1]["minute"][11:16], "minutes": j - i})
                i = j
            else:
                i += 1
        k = int(np.nanargmax(y))
        out[ent] = {"kind": "direction" if kinds.get(ent) == "group" else "zone", "calibrated": True, "episodes": eps,
                    "congested_minutes": sum(e["minutes"] for e in eps), "peak_fill_pct": pct(float(y[k])),
                    "peak_at": rows[k]["minute"][11:16]}
    zones = {e: v for e, v in out.items() if v["kind"] == "zone" and v["calibrated"]}
    longest = max(zones, key=lambda e: (zones[e]["congested_minutes"], zones[e]["peak_fill_pct"] or 0)) if zones else None
    return {"day": day, "date": d["date"], "camera": d["camera"], "threshold_fill_pct": pct(thr),
            "definition": f"congested = 5-minute mean fill >= {pct(thr)} % of the jam capacity for {ONSET_MIN_LEN} "
                          f"minutes or more (short dips merged)",
            "entities": out, "longest_queueing_zone": longest,
            "longest_queueing_zone_minutes": zones[longest]["congested_minutes"] if longest else None}


def accuracy(conn: sqlite3.Connection, user: User, day: str, section: str = "To CIQ",
             horizons: tuple = forecast_svc.HORIZONS) -> Dict[str, Any]:
    """Forecast accuracy per horizon (MAE / RMSE as percentage points of fill, precision, recall) and, per onset, the
    warning lead time: how many minutes before the onset the forecast first called the congestion."""
    rows, onsets = [], []
    for h in horizons:
        a = forecast_svc.accuracy(conn, user, day, section, h)
        onsets = [o[11:16] for o in a.onsets]
        rows.append({"horizon_min": h, "mae_pct_points": pct(a.mae), "rmse_pct_points": pct(a.rmse), "minutes_scored": a.n,
                     "expected_error_band_pct_points": pct(a.band), "precision_pct": pct(a.precision),
                     "recall_pct": pct(a.recall),
                     "warning_lead_min": {o: w for o, w in zip(onsets, a.warning_min)}})
    best = {o: max((r["warning_lead_min"].get(o) or 0 for r in rows), default=0) for o in onsets}
    day_info = next((d for d in forecast_svc.days(conn, user) if d.code == day), None)
    return {"day": day, "section": section, "model": day_info.model if day_info else None,
            "trained_on": day_info.train_day if day_info else None, "onsets": onsets,
            "first_warning": day_info.first_warning[11:16] if day_info and day_info.first_warning else None,
            "by_horizon": rows, "best_warning_lead_min": best,
            "note": "Warning lead time = minutes between the first forecast that called the congestion (while the road "
                    "was still free) and the onset; 0 = noticed only when it happened, null = never before it."}


def whatif(conn: sqlite3.Connection, user: User, day: str, section: str = "To CIQ", capacity_pct: float = 20.0,
           mu: Optional[float] = None, start: Optional[str] = None) -> Dict[str, Any]:
    w = forecast_svc.whatif(conn, user, day, section, capacity_pct, mu, start)
    base, proj = w.congested_minutes
    return {"day": w.day, "section": w.section, "capacity_increase_pct": w.capacity_pct, "from": w.start_minute[11:16],
            "label": w.label, "discharge_capacity_veh_per_min": w.mu, "capacity_source": w.mu_source,
            "minutes_over_threshold_baseline": base, "minutes_over_threshold_projected": proj,
            "minutes_over_threshold_saved": base - proj, "minutes_over_threshold_saved_pct": share(base - proj, base),
            "minutes_definition": "1-minute values of the queue at or above the congestion threshold, counted from the "
                                  "start minute (not the congestion-episode definition, which uses the 5-minute mean)",
            "peak_queue_baseline_veh": w.peak[0], "peak_queue_projected_veh": w.peak[1],
            "peak_queue_reduction_veh": round(w.peak[0] - w.peak[1], 1),
            "congestion_threshold_veh": w.threshold_vehicles, "jam_capacity_veh": w.capacity_vehicles,
            "assumptions": w.assumptions}


def lead_times(conn: sqlite3.Connection, user: User) -> List[Dict[str, Any]]:
    """Per congestion day: the first warning and the onsets with the minutes between them."""
    from datetime import datetime
    out = []
    for d in forecast_svc.days(conn, user):
        lead = None
        if d.first_warning and d.onsets:
            lead = int((datetime.fromisoformat(d.onsets[0]) - datetime.fromisoformat(d.first_warning)).total_seconds() // 60)
        out.append({"day": d.code, "label": d.label, "camera": d.camera, "date": d.date, "calibrated": d.calibrated,
                    "first_warning": d.first_warning[11:16] if d.first_warning else None,
                    "onsets": [o[11:16] for o in d.onsets], "first_warning_lead_min": lead, "model": d.model,
                    "trained_on": d.train_day, "sections": d.sections,
                    "count_error_vehicles": d.loo_mae_vehicles, "relative_count_error_pct": pct(d.relative_mae)})
    return out
