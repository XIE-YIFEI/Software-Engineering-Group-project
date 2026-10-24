"""Congestion forecast of the JB CIQ approach: the state of each section at a replay clock, the +5 / +15 / +30 min
forecasts with their error band and a warning when congestion is coming, forecast vs actual (model accuracy), and
the what-if "open more lanes" queue model. The data are the 1-minute series of alpr/congestion.py and the LightGBM
forecasts of alpr/forecast.py (committed JSON, plus the sections computed at import)."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional, Tuple

import numpy as np

from .. import db
from ..analysis import whatif as wf
from ..models import Accuracy, ForecastDay, ForecastState, SectionState, WhatIfOut
from ..privacy import audit
from ..security import User, require
from ..settings import active
from .common import in_clause, visible_camera_ids

ROLES = ("police", "traffic", "admin")
HORIZONS = (5, 15, 30)
DENSE = 0.25                              # free < 25 % of jam capacity <= dense < 50 % <= queue (TEAM_GUIDE 10.2)
_COLOUR = {(255, 69, 58): "red", (255, 159, 10): "orange", (48, 209, 88): "green", (10, 132, 255): "blue",
           (142, 142, 147): "gray"}
_WARNINGS: Dict[tuple, Tuple[List[str], Optional[str]]] = {}


def _day(conn: sqlite3.Connection, user: User, code: str) -> dict:
    require(user, *ROLES)
    d = db.row(conn, f"SELECT d.*, c.name AS camera FROM series_days d JOIN cameras c ON c.id = d.camera_id "
                     f"WHERE d.code = ? AND d.camera_id IN {in_clause(visible_camera_ids(conn, user))}", (code,))
    if d is None:
        raise LookupError("no such series day")
    return d


def _points(conn, day_id: int, section: str) -> Tuple[Optional[dict], List[dict]]:
    run = db.row(conn, "SELECT * FROM forecast_runs WHERE day_id = ? AND entity = ?", (day_id, section))
    if run is None:
        return None, []
    return run, db.rows(conn, "SELECT * FROM forecast_points WHERE run_id = ? ORDER BY minute", (run["id"],))


def _arr(pts: List[dict], key: str) -> np.ndarray:
    return np.array([np.nan if p[key] is None else p[key] for p in pts], float)


def _onsets_and_warning(conn: sqlite3.Connection, run: dict, pts: List[dict]) -> Tuple[List[str], Optional[str]]:
    """Congestion onsets of the measured series and the first warning before the first onset (alpr.forecast),
    recomputed only when the run's forecast points change (the smoothing takes ~50 ms per series)."""
    version = (str(active().db_path), run["id"], run["congestion_fill"]) + tuple(conn.execute(
        "SELECT COUNT(*), TOTAL(measured), TOTAL(f5) + TOTAL(f15) + TOTAL(f30) FROM forecast_points WHERE run_id = ?",
        (run["id"],)).fetchone())
    if version not in _WARNINGS:
        _WARNINGS[version] = _compute_onsets_and_warning(pts, run["congestion_fill"])
    return _WARNINGS[version]


def _compute_onsets_and_warning(pts: List[dict], thr: float) -> Tuple[List[str], Optional[str]]:
    from alpr.forecast import congestion_scores, congestion_state, onsets
    y = _arr(pts, "measured")
    state = congestion_state(y, thr)
    ons = onsets(state)
    first = None
    if ons:
        leads = []
        for h in HORIZONS:
            sc = congestion_scores(y, _arr(pts, f"f{h}"), h, thr)
            w = sc["onsets"][0]["warning_min"] if sc["onsets"] else None
            if w:
                leads.append(w)
        if leads:
            first = pts[ons[0] - max(leads)]["minute"]
    return [pts[i]["minute"] for i in ons], first


def sections_of(conn, day_id: int) -> List[str]:
    secs = [r["entity"] for r in db.rows(conn, "SELECT entity FROM forecast_runs WHERE day_id = ?", (day_id,))]
    return sorted(secs, key=lambda s: (not s.startswith("To"), s))


def days(conn: sqlite3.Connection, user: User) -> List[ForecastDay]:
    """The camera-days with congestion series, their sections, first warning and congestion onsets."""
    require(user, *ROLES)
    out = []
    for d in db.rows(conn, f"SELECT d.*, c.name AS camera FROM series_days d JOIN cameras c ON c.id = d.camera_id "
                           f"WHERE d.camera_id IN {in_clause(visible_camera_ids(conn, user))} ORDER BY d.calibrated DESC, d.date DESC"):
        secs = sections_of(conn, d["id"])
        run, pts = _points(conn, d["id"], "To CIQ") if "To CIQ" in secs else (None, [])
        ons, first = _onsets_and_warning(conn, run, pts) if run else ([], None)
        g = db.row(conn, "SELECT * FROM calibrations WHERE day_id = ? AND kind = 'group'", (d["id"],))
        mm = db.row(conn, "SELECT MIN(minute) AS a, MAX(minute) AS b FROM zone_minutes WHERE day_id = ?", (d["id"],))
        out.append(ForecastDay(code=d["code"], label=d["label"], camera=d["camera"], date=d["date"],
                               first_minute=mm["a"], last_minute=mm["b"], calibrated=bool(d["calibrated"]),
                               sections=secs, train_day=run["train_day"] if run else None,
                               model=run["model"] if run else None, first_warning=first, onsets=ons,
                               video_url=f"/media/series/{d['id']}/video" if d["video_path"] else None,
                               video_from_minute=d["video_from_minute"], video_to_minute=d["video_to_minute"],
                               video_duration_sec=d["video_duration_sec"], loo_mae_vehicles=g["loo_mae"] if g else None,
                               relative_mae=g["relative_mae"] if g else None))
    return out


def _status(history: List[float], fc: Dict[int, Optional[float]], thr: float) -> Tuple[str, str]:
    from alpr.hud import forecast_status
    text, rgb = forecast_status({"threshold": thr, "history": [(0, v) for v in history],
                                 "forecast": {h: v for h, v in fc.items()}})
    return text, _COLOUR.get(tuple(rgb), "gray")


def state(conn: sqlite3.Connection, user: User, day: str, at: Optional[str] = None) -> ForecastState:
    """Every section at clock minute `at` (HH:MM; default the first warning of the day): current fill, 5-minute mean,
    vehicles in view, state (free / dense / queue), the forecasts made at that minute for +5 / +15 / +30 min with
    their error band, and the status line of the dashboard ('Congestion in ~15 min', ...)."""
    d = _day(conn, user, day)
    secs = sections_of(conn, d["id"])
    fdays = {x.code: x for x in days(conn, user)}
    if not at:
        at = (fdays[day].first_warning or fdays[day].first_minute)[11:16]
    minute = f"{d['date']}T{at}"
    out, thr = [], 0.5
    for sec in secs:
        run, pts = _points(conn, d["id"], sec)
        thr = run["congestion_fill"]
        idx = next((i for i, p in enumerate(pts) if p["minute"] == minute), None)
        cal = db.row(conn, "SELECT capacity FROM calibrations WHERE day_id = ? AND entity = ?", (d["id"], sec))
        cap = cal["capacity"] if cal else None
        band = {k: round(v, 4) for k, v in json.loads(run["error_band"]).items()}
        if idx is None:
            out.append(SectionState(section=sec, label=_label(sec), state="no data", status="No data at this time",
                                    status_colour="gray", warning=False, band=band, capacity=cap, model=run["model"],
                                    origin=run["origin"]))
            continue
        hist = [p["measured"] for p in pts[max(0, idx - 4):idx + 1] if p["measured"] is not None]
        mean5 = float(np.mean(hist)) if hist else None
        fc = {h: pts[idx][f"f{h}"] for h in HORIZONS}
        text, colour = _status(hist, fc, thr)
        st = "no data" if mean5 is None else "queue" if mean5 >= thr else "dense" if mean5 >= DENSE else "free"
        now = pts[idx]["measured"]
        out.append(SectionState(section=sec, label=_label(sec), now=now, mean5=None if mean5 is None else round(mean5, 4),
                                vehicles=None if now is None or cap is None else round(now * cap, 1), capacity=cap,
                                state=st, forecast={str(h): v for h, v in fc.items()}, band=band, status=text,
                                status_colour=colour, warning=text.startswith("Congestion in"), model=run["model"],
                                origin=run["origin"]))
    return ForecastState(day=day, at=at, sections=out, threshold=thr)


def _label(sec: str) -> str:
    return {"To CIQ": "To CIQ (whole approach)", "TC1": "TC1 · queue head at the CIQ"}.get(sec, sec)


def accuracy(conn: sqlite3.Connection, user: User, day: str, section: str = "To CIQ", horizon: int = 30) -> Accuracy:
    """Measured fill vs the forecast made `horizon` minutes earlier, minute by minute, with MAE / RMSE over the day,
    the expected error band, the congestion onsets and the warning time before each (alpr.forecast metrics)."""
    from alpr.forecast import congestion_scores, mae_rmse
    if horizon not in HORIZONS:
        raise ValueError("horizon must be 5, 15 or 30")
    d = _day(conn, user, day)
    run, pts = _points(conn, d["id"], section)
    if run is None:
        raise LookupError("no forecast for this section")
    y = _arr(pts, "measured")
    f_origin = _arr(pts, f"f{horizon}")                      # made at t for t + h
    shifted = np.r_[np.full(horizon, np.nan), f_origin[:-horizon]] if len(f_origin) > horizon else np.full(len(y), np.nan)
    mae, rmse, n = mae_rmse(y, shifted)
    thr = run["congestion_fill"]
    sc = congestion_scores(y, f_origin, horizon, thr)
    ons, _ = _onsets_and_warning(conn, run, pts)
    band = json.loads(run["error_band"]).get(str(horizon))

    def opt(v):
        return None if v != v else round(float(v), 4)
    audit(conn, user, "view", "forecast_accuracy", f"{day}/{section}", {"horizon": horizon})
    return Accuracy(day=day, section=section, horizon=horizon, minutes=[p["minute"] for p in pts],
                    measured=[opt(v) for v in y], forecast=[opt(v) for v in shifted], mae=opt(mae), rmse=opt(rmse), n=n,
                    band=band, threshold=thr, onsets=ons, warning_min=[o["warning_min"] for o in sc["onsets"]],
                    precision=opt(sc["precision"]), recall=opt(sc["recall"]))


def whatif(conn: sqlite3.Connection, user: User, day: str, section: str = "To CIQ", capacity_pct: float = 20.0,
           mu: Optional[float] = None, start: Optional[str] = None) -> WhatIfOut:
    """Projected queue of a section if its discharge capacity were raised by `capacity_pct` % from `start` (HH:MM;
    default the first warning), with the deterministic point-queue model of webapp/analysis/whatif.py. `mu`: an
    assumed discharge capacity in vehicles per minute (default: estimated from the fastest clearing).
    An estimate under stated assumptions (listed in the result)."""
    if not 0 <= capacity_pct <= 100:
        raise ValueError("capacity_pct must be between 0 and 100")
    d = _day(conn, user, day)
    run, pts = _points(conn, d["id"], section)
    cal = db.row(conn, "SELECT capacity FROM calibrations WHERE day_id = ? AND entity = ?", (d["id"], section))
    if run is None or not cal or not cal["capacity"]:
        raise LookupError("no calibrated series for this section")
    cap = float(cal["capacity"])
    queue = [None if p["measured"] is None else p["measured"] * cap for p in pts]
    if start is None:
        _, first = _onsets_and_warning(conn, run, pts)
        start = (first or pts[0]["minute"])[11:16]
    minute = f"{d['date']}T{start}"
    i0 = next((i for i, p in enumerate(pts) if p["minute"] >= minute), 0)
    res = wf.run(queue, i0, capacity_pct, run["congestion_fill"] * cap, mu)
    audit(conn, user, "whatif", "forecast", f"{day}/{section}", {"capacity_pct": capacity_pct, "mu": mu, "start": start})
    return WhatIfOut(day=day, section=section, capacity_pct=capacity_pct, start_minute=pts[i0]["minute"], mu=res.mu,
                     mu_source=res.mu_source, mu_estimate=res.mu_estimate,
                     mu_estimated_at=pts[res.mu_estimated_at]["minute"] if res.mu_estimated_at is not None else None,
                     minutes=[p["minute"] for p in pts], baseline=res.baseline, projected=res.projected,
                     threshold_vehicles=round(res.threshold, 2), capacity_vehicles=cap,
                     congested_minutes=list(res.congested_minutes), peak=[round(v, 1) for v in res.peak],
                     assumptions=res.assumptions)
