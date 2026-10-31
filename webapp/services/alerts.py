"""Alert centre: the six registry alerts, watch / ban list hits and lane violations, sorted by severity; the
workflow new -> acknowledged -> resolved / false alarm with handler and notes; corrected plates of false alarms go
to the OCR correction pool."""
from __future__ import annotations

import shutil
import sqlite3
from typing import Dict, List, Optional

from .. import clock, db
from ..analysis import fuzzy
from ..models import AlertAction, AlertCounts, AlertDetail, AlertOut, ComparisonRow, Page, RegistryRecord
from ..privacy import audit, can_see_full, mask_plate
from ..registry_provider import get_provider
from ..security import Forbidden, User, require
from ..settings import active
from .common import RANK_NAME, UNMATCHED_NOTE, in_clause, provenance_label, title, video_ref, visible_camera_ids

STATUSES = ("new", "acknowledged", "resolved", "false_alarm")
TRANSITIONS = {"new": ("acknowledged", "resolved", "false_alarm"), "acknowledged": ("resolved", "false_alarm"),
               "resolved": ("acknowledged",), "false_alarm": ("acknowledged",)}
ACTION_OF = {"acknowledged": "acknowledge", "resolved": "resolve", "false_alarm": "false_alarm"}
OPEN = ("new", "acknowledged")


def _scope(conn: sqlite3.Connection, user: User) -> str:
    """SQL condition for the alerts this user may see: police road / lane cameras, a guard their gate, admins all."""
    require(user, "police", "guard", "admin")
    return f"a.camera_id IN {in_clause(visible_camera_ids(conn, user))}"


def _runs(conn) -> Dict[int, dict]:
    return {r["id"]: r for r in db.rows(conn, "SELECT r.*, c.name AS camera_name, c.kind AS camera_kind FROM runs r "
                                              "JOIN cameras c ON c.id = r.camera_id")}


def _out(conn: sqlite3.Connection, user: User, a: dict, runs: Dict[int, dict]) -> AlertOut:
    run = runs.get(a["run_id"]) or {}
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (a["sighting_id"],)) if a["sighting_id"] else None
    ev = db.row(conn, "SELECT * FROM lane_events WHERE id = ?", (a["lane_event_id"],)) if a["lane_event_id"] else None
    full = can_see_full(conn, user, a["sighting_id"], a["compact"]) if a["compact"] else True
    at = s["first_sec"] if s else (ev["time_sec"] if ev else None)
    crop = None
    if s and s["crop_path"]:
        crop = f"/media/sightings/{s['id']}/crop" + ("?v=full" if full else "")
    return AlertOut(id=a["id"], code=a["code"], title=title(a["code"]), severity=a["severity"], rank=a["rank"],
                    message=a["message"], status=a["status"], source=a["source"], provenance=a["provenance"],
                    provenance_label=provenance_label(a["provenance"]), seen_at=a["seen_at"], camera_id=a["camera_id"],
                    camera_name=run.get("camera_name", ""), run_id=a["run_id"], run_label=run.get("label", ""),
                    plate=(a["plate_text"] or "") if full else mask_plate(a["plate_text"]), masked=not full,
                    sighting_id=a["sighting_id"], lane_event_id=a["lane_event_id"], lane=ev["lane"] if ev else None,
                    crop_url=crop, video=video_ref(run, at, user) if run else None, updated_at=a["updated_at"])


def list_alerts(conn: sqlite3.Connection, user: User, status: Optional[str] = None, severity: Optional[str] = None,
                code: Optional[str] = None, camera_id: Optional[int] = None, include_simulated: bool = True,
                plate: str = "", page: int = 1, page_size: int = 50) -> Page[AlertOut]:
    """Alerts sorted by severity (high and lane violations first, then hazards, then medium), newest first."""
    where, params = [_scope(conn, user)], []
    if status == "open":
        where.append("a.status IN ('new', 'acknowledged')")
    elif status:
        where.append("a.status = ?")
        params.append(status)
    if severity:
        where.append("a.rank = ?" if severity.isdigit() else "a.severity = ?")
        params.append(int(severity) if severity.isdigit() else severity)
    for col, val in (("a.code", code), ("a.camera_id", camera_id)):
        if val not in (None, ""):
            where.append(f"{col} = ?")
            params.append(val)
    if not include_simulated:
        where.append("a.provenance = 'camera'")
    if plate and not user.masked:
        where.append("a.compact = ?")
        params.append(fuzzy.compact(plate))
    w = " AND ".join(where)
    total = db.scalar(conn, f"SELECT COUNT(*) FROM alerts a WHERE {w}", params)
    rs = db.rows(conn, f"SELECT a.* FROM alerts a WHERE {w} ORDER BY a.rank, a.seen_at DESC, a.id DESC LIMIT ? OFFSET ?",
                 params + [page_size, (page - 1) * page_size])
    runs = _runs(conn)
    audit(conn, user, "list_alerts", "alerts", None, {"status": status, "code": code, "results": total})
    return Page[AlertOut](items=[_out(conn, user, a, runs) for a in rs], total=total, page=page, page_size=page_size)


def counts(conn: sqlite3.Connection, user: User) -> AlertCounts:
    w = _scope(conn, user)
    by_status = {s: 0 for s in STATUSES}
    by_status.update({r["status"]: r["n"] for r in db.rows(conn, f"SELECT status, COUNT(*) AS n FROM alerts a WHERE {w} "
                                                                  f"GROUP BY status")})
    by_rank = {RANK_NAME[r["rank"]]: r["n"] for r in db.rows(
        conn, f"SELECT rank, COUNT(*) AS n FROM alerts a WHERE {w} AND status IN ('new', 'acknowledged') GROUP BY rank")}
    sim = db.scalar(conn, f"SELECT COUNT(*) FROM alerts a WHERE {w} AND status IN ('new', 'acknowledged') AND "
                          f"provenance != 'camera'")
    return AlertCounts(by_status=by_status, by_rank={k: by_rank.get(k, 0) for k in ("High", "Hazard", "Medium")},
                       simulated=sim, total_open=by_status["new"] + by_status["acknowledged"])


def _comparison(rec: Optional[dict], s: Optional[dict]) -> List[ComparisonRow]:
    """Registered (SIMULATED record) vs observed (what the camera saw)."""
    if s is None:
        return []
    from alpr.registry import BODY_TYPES
    rows = [ComparisonRow(field="Plate", registered=rec["plate"] if rec else "(no record)", observed=s["compact"],
                          match=bool(rec))]
    if rec:
        body = rec.get("body_type")
        fits = s["vtype"] in BODY_TYPES.get(body, (body,)) if body and s["vtype"] else None
        rows.append(ComparisonRow(field="Vehicle type", registered=body or "not recorded",
                                  observed=s["vtype"] or "unknown", match=fits))
        rows.append(ComparisonRow(field="Make / model", registered=rec.get("make_model") or "not recorded",
                                  observed="not measured"))
        rows.append(ComparisonRow(field="Colour", registered=rec.get("colour") or "not recorded", observed="not measured"))
        exp = rec.get("road_tax_expiry")
        rows.append(ComparisonRow(field="Road tax", registered=f"valid to {exp}" if exp else "not recorded",
                                  observed=f"seen {s['seen_at'][:10]}",
                                  match=None if not exp else s["seen_at"][:10] <= exp))
        rows.append(ComparisonRow(field="Status", registered=rec.get("status", "ok"), observed="-",
                                  match=rec.get("status") != "blacklisted"))
    return rows


def get_alert(conn: sqlite3.Connection, user: User, alert_id: int) -> AlertDetail:
    """One alert with its history, the registry comparison (police) and speed +- error for lane events."""
    a = db.row(conn, f"SELECT a.* FROM alerts a WHERE a.id = ? AND {_scope(conn, user)}", (alert_id,))
    if a is None:
        raise LookupError("no such alert")
    base = _out(conn, user, a, _runs(conn))
    hist = [AlertAction(at=h["at"], user=h["username"] or "system", action=h["action"], from_status=h["from_status"],
                        to_status=h["to_status"], note=h["note"], corrected_plate=h["corrected_plate"])
            for h in db.rows(conn, "SELECT x.*, u.username FROM alert_actions x LEFT JOIN users u ON u.id = x.user_id "
                                   "WHERE alert_id = ? ORDER BY x.id", (alert_id,))]
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (a["sighting_id"],)) if a["sighting_id"] else None
    ev = db.row(conn, "SELECT * FROM lane_events WHERE id = ?", (a["lane_event_id"],)) if a["lane_event_id"] else None
    rec = None
    if user.role == "police" and a["compact"]:
        rec = get_provider(str(active().registry_path)).lookup(a["compact"])
    speed = None
    if ev and ev["value"] is not None and ev["code"] == "SPEEDING":
        speed = f"{ev['value']:.0f} ± {2 * (ev['error_1sigma'] or 0):.0f} km/h (95 %, 2σ); 1σ = {ev['error_1sigma']:.1f} km/h"
    elif ev and ev["value"] is not None and ev["code"] == "TAILGATING":
        speed = f"time headway {ev['value']:.2f} s"
    note = UNMATCHED_NOTE if a["code"] == "UNMATCHED" else ""
    full = not base.masked
    audit(conn, user, "view", "alert", alert_id, {"code": a["code"]})
    return AlertDetail(**base.model_dump(), history=hist, registry=RegistryRecord(**rec) if rec else None,
                       comparison=_comparison(rec, s) if user.role == "police" else [], speed_text=speed,
                       value=ev["value"] if ev else None, error_1sigma=ev["error_1sigma"] if ev else None, note=note,
                       scene_url=f"/media/sightings/{s['id']}/scene" if (s and s["scene_path"] and full) else None,
                       allowed_transitions=list(TRANSITIONS[a["status"]]) if user.role in ("police", "guard") else [],
                       can_export=user.role == "police")


def transition(conn: sqlite3.Connection, user: User, alert_id: int, to: str, note: str = "",
               corrected_plate: Optional[str] = None) -> AlertDetail:
    """Move an alert along the workflow (police; guards for their gate). A false alarm can carry the correct plate:
    it goes to the correction pool (OCR fine-tuning later; subject to the retention period)."""
    require(user, "police", "guard")
    a = db.row(conn, f"SELECT a.* FROM alerts a WHERE a.id = ? AND {_scope(conn, user)}", (alert_id,))
    if a is None:
        raise LookupError("no such alert")
    if to not in TRANSITIONS[a["status"]]:
        raise ValueError(f"cannot go from {a['status']} to {to}")
    if to in ("resolved", "false_alarm") and not note.strip():
        raise ValueError("add a note when closing an alert")
    corrected = fuzzy.compact(corrected_plate or "")
    if corrected and to != "false_alarm":
        raise ValueError("a corrected plate only goes with a false alarm")
    now = clock.now_iso()
    with db.transaction(conn):
        conn.execute("UPDATE alerts SET status = ?, updated_at = ?, closed_at = ? WHERE id = ?",
                     (to, now, now if to in ("resolved", "false_alarm") else None, alert_id))
        db.insert(conn, "alert_actions", {"alert_id": alert_id, "user_id": user.id,
                                          "action": "reopen" if a["status"] in ("resolved", "false_alarm") else ACTION_OF[to],
                                          "from_status": a["status"], "to_status": to, "note": note.strip()[:2000],
                                          "corrected_plate": corrected or None, "at": now})
        if corrected:
            _to_correction_pool(conn, user, a, corrected, now)
        audit(conn, user, "alert_" + to, "alert", alert_id, {"from": a["status"], "corrected_plate": corrected or None},
              reason=note.strip()[:200])
    return get_alert(conn, user, alert_id)


def _to_correction_pool(conn, user: User, a: dict, corrected: str, now: str) -> None:
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (a["sighting_id"],)) if a["sighting_id"] else None
    crop_rel = None
    st = active()
    if s and s["crop_path"] and (st.media_dir / s["crop_path"]).exists():
        crop_rel = f"{a['id']}_{corrected}{(st.media_dir / s['crop_path']).suffix}"
        st.corrections_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(st.media_dir / s["crop_path"], st.corrections_dir / crop_rel)
    db.insert(conn, "corrections", {"alert_id": a["id"], "sighting_id": a["sighting_id"],
                                    "ocr_compact": a["compact"] or "", "corrected_compact": corrected,
                                    "crop_path": crop_rel, "created_by": user.id, "created_at": now})


def open_alerts_top(conn: sqlite3.Connection, user: User, n: int = 5) -> List[AlertOut]:
    if user.role not in ("police", "guard", "admin"):
        return []
    rs = db.rows(conn, f"SELECT a.* FROM alerts a WHERE {_scope(conn, user)} AND a.status IN ('new', 'acknowledged') "
                       f"ORDER BY a.rank, a.seen_at DESC LIMIT ?", (n,))
    runs = _runs(conn)
    return [_out(conn, user, a, runs) for a in rs]


def require_police(user: User) -> None:
    if user.role != "police":
        raise Forbidden("only police officers export evidence packs")
