"""Overview (home of police, traffic managers and admins): camera status, the day's flow, open alerts and the
current congestion."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional

from .. import db
from ..models import CameraOut, Overview
from ..security import User, require
from . import alerts as alert_svc
from . import forecast as forecast_svc
from .common import in_clause, visible_camera_ids


def cameras(conn: sqlite3.Connection, user: User) -> List[CameraOut]:
    out = []
    for c in db.rows(conn, f"SELECT c.*, s.name AS site FROM cameras c LEFT JOIN sites s ON s.id = c.site_id WHERE "
                           f"c.id IN {in_clause(visible_camera_ids(conn, user))} ORDER BY c.kind = 'congestion', c.id"):
        flow = db.scalar(conn, "SELECT SUM(flow_total) FROM runs WHERE camera_id = ? AND flow_primary = 1", (c["id"],))
        last = db.scalar(conn, "SELECT MAX(last_seen_at) FROM sightings WHERE camera_id = ?", (c["id"],))
        if c["kind"] == "congestion":
            last = db.scalar(conn, "SELECT MAX(z.minute) FROM zone_minutes z JOIN series_days d ON d.id = z.day_id "
                                   "WHERE d.camera_id = ?", (c["id"],))
        out.append(CameraOut(
            id=c["id"], code=c["code"], name=c["name"], kind=c["kind"], site=c["site"], lat=c["lat"], lon=c["lon"],
            location_note=c["location_note"], source_credit=c["source_credit"], status=c["status"],
            notional_clock=bool(c["notional_clock"]),
            runs=db.scalar(conn, "SELECT COUNT(*) FROM runs WHERE camera_id = ?", (c["id"],))
            + db.scalar(conn, "SELECT COUNT(*) FROM series_days WHERE camera_id = ?", (c["id"],)),
            sightings=db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE camera_id = ? AND provenance != 'synthetic'",
                                (c["id"],)),
            synthetic=db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE camera_id = ? AND provenance = 'synthetic'",
                                (c["id"],)),
            open_alerts=db.scalar(conn, "SELECT COUNT(*) FROM alerts WHERE camera_id = ? AND status IN ('new', "
                                        "'acknowledged')", (c["id"],)),
            flow_total=flow, last_seen=last))
    return out


def summary(conn: sqlite3.Connection, user: User, date: Optional[str] = None) -> Overview:
    """The home page data. `date` (YYYY-MM-DD) defaults to the most recent day with camera data (for the demo data
    the notional demo day, 14 Oct 2026)."""
    require(user, "police", "traffic", "admin")
    cams = cameras(conn, user)
    plate_cams = [c.id for c in cams if c.kind != "congestion"]
    dates = [r["d"] for r in db.rows(conn, f"SELECT DISTINCT substr(seen_at, 1, 10) AS d FROM (SELECT seen_at FROM "
                                           f"sightings WHERE camera_id IN {in_clause(plate_cams)} UNION ALL SELECT "
                                           f"x.seen_at FROM crossings x JOIN runs r ON r.id = x.run_id WHERE r.camera_id "
                                           f"IN {in_clause(plate_cams)}) ORDER BY d DESC")]
    date = date or (dates[0] if dates else "")
    demo_day = bool(db.scalar(conn, f"SELECT 1 FROM runs WHERE origin = 'demo' AND substr(clock_origin, 1, 10) = ? AND "
                                    f"camera_id IN {in_clause(plate_cams)}", (date,)))
    by_type: Dict[str, int] = {}
    by_camera: Dict[str, int] = {}
    sparks: Dict[str, List[int]] = {}
    for r in db.rows(conn, f"SELECT r.*, c.name AS camera_name FROM runs r JOIN cameras c ON c.id = r.camera_id WHERE "
                           f"r.flow_primary = 1 AND r.camera_id IN {in_clause(plate_cams)} AND "
                           f"substr(r.clock_origin, 1, 10) = ?", (date,)):
        if r["flow_total"] is None:
            continue
        by_camera[r["camera_name"]] = by_camera.get(r["camera_name"], 0) + r["flow_total"]
        for k, v in json.loads(r["flow_by_type"] or "{}").items():
            by_type[k] = by_type.get(k, 0) + v
        sparks[r["camera_name"]] = [b["total"] for b in db.rows(conn, "SELECT total FROM flow_bins WHERE run_id = ? "
                                                                      "ORDER BY t_start", (r["id"],))]
    counts = alert_svc.counts(conn, user) if user.role != "traffic" else _lane_counts(conn, user)
    top = alert_svc.open_alerts_top(conn, user) if user.role != "traffic" else []
    cong = cong_day = None
    fdays = [d for d in forecast_svc.days(conn, user) if d.calibrated and d.sections]
    if fdays:
        cong_day = next((d for d in fdays if d.video_url), fdays[0])
        cong = forecast_svc.state(conn, user, cong_day.code)
    system = None
    if user.role == "admin":
        from ..settings import active
        st = active()
        system = {"database_mb": round(st.db_path.stat().st_size / 1e6, 2) if st.db_path.exists() else 0,
                  "last_import": db.scalar(conn, "SELECT value FROM meta WHERE key = 'demo_imported_at'"),
                  "jobs_queued": db.scalar(conn, "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"),
                  "audit_rows": db.scalar(conn, "SELECT COUNT(*) FROM audit_log"),
                  "users": db.scalar(conn, "SELECT COUNT(*) FROM users WHERE active = 1")}
    return Overview(date=date, dates=dates, demo_day=demo_day, cameras=cams, flow_total=sum(by_camera.values()), flow_by_type=by_type,
                    flow_by_camera=by_camera, sparklines=sparks, alerts=counts, top_alerts=top, congestion=cong,
                    congestion_day=cong_day, system=system)


def _lane_counts(conn: sqlite3.Connection, user: User):
    """Traffic managers have no alert centre: their overview counts the lane hazards and violations."""
    from ..models import AlertCounts
    cams = visible_camera_ids(conn, user)
    n = {r["rank"]: r["n"] for r in db.rows(conn, f"SELECT rank, COUNT(*) AS n FROM alerts WHERE source = 'lane' AND "
                                                  f"camera_id IN {in_clause(cams)} AND status IN ('new', 'acknowledged') "
                                                  f"GROUP BY rank")}
    sim = db.scalar(conn, f"SELECT COUNT(*) FROM alerts WHERE source = 'lane' AND camera_id IN {in_clause(cams)} AND "
                          f"status IN ('new', 'acknowledged') AND provenance != 'camera'")
    return AlertCounts(by_status={}, by_rank={"High": n.get(1, 0), "Hazard": n.get(2, 0), "Medium": n.get(3, 0)},
                       simulated=sim, total_open=sum(n.values()))
