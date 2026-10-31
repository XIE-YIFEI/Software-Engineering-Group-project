"""Plate search, one record, "show full plate", the cross-camera timeline and CSV export."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Dict, List, Optional, Tuple

from .. import clock, db
from ..analysis import fuzzy, timeline
from ..models import (AlertBrief, Page, RegistryRecord, RevealOut, SightingDetail, SightingOut, SightingQuery,
                      Timeline, TimelineLeg, TimelinePoint)
from ..privacy import audit, can_see_full, mask_plate, reveal
from ..registry_provider import get_provider
from ..security import Forbidden, User, require
from ..settings import active
from .common import (UNMATCHED_NOTE, in_clause, provenance_label, title, video_ref, visible_camera_ids)

SEARCH_ROLES = ("police", "guard", "traffic", "admin")
MAX_ROWS = 5000
_INDEX: Dict[str, tuple] = {}


def plate_index(conn: sqlite3.Connection) -> fuzzy.PlateIndex:
    """The encoded distinct plates for the fuzzy search, rebuilt only when the plates table changes."""
    version = (str(active().db_path),) + tuple(conn.execute("SELECT COUNT(*), MAX(rowid) FROM plates").fetchone())
    cached = _INDEX.get("plates")
    if cached is None or cached[0] != version:
        cached = (version, fuzzy.PlateIndex([r["compact"] for r in db.rows(conn, "SELECT compact FROM plates")]))
        _INDEX["plates"] = cached
    return cached[1]


def _runs(conn: sqlite3.Connection) -> Dict[int, dict]:
    return {r["id"]: r for r in db.rows(conn, "SELECT r.*, c.name AS camera_name, c.kind AS camera_kind FROM runs r "
                                              "JOIN cameras c ON c.id = r.camera_id")}


def _alerts_of(conn: sqlite3.Connection, sighting_ids: List[int]) -> Dict[int, List[AlertBrief]]:
    out: Dict[int, List[AlertBrief]] = {}
    if not sighting_ids:
        return out
    for a in db.rows(conn, f"SELECT * FROM alerts WHERE sighting_id IN {in_clause(sighting_ids)} ORDER BY rank, code"):
        out.setdefault(a["sighting_id"], []).append(AlertBrief(id=a["id"], code=a["code"], title=title(a["code"]),
                                                               severity=a["severity"], rank=a["rank"],
                                                               message=a["message"], status=a["status"]))
    return out


def crop_url(r: dict, full: bool) -> Optional[str]:
    if not r.get("crop_path"):
        return None
    return f"/media/sightings/{r['id']}/crop" + ("?v=full" if full else "")


def to_out(conn: sqlite3.Connection, user: User, r: dict, runs: Dict[int, dict], alerts: Dict[int, List[AlertBrief]],
           distance: Optional[float] = None) -> SightingOut:
    full = can_see_full(conn, user, r["id"], r["compact"])
    run = runs[r["run_id"]]
    return SightingOut(
        id=r["id"], run_id=r["run_id"], run_label=run["label"], camera_id=r["camera_id"], camera_name=run["camera_name"],
        plate=r["plate_text"] if full else mask_plate(r["plate_text"]), masked=not full, distance=distance,
        confidence=r["confidence"], votes=r["votes"], status=r["status"], valid_format=bool(r["valid_format"]),
        vtype=r["vtype"], region=r["region"], first_sec=r["first_sec"], last_sec=r["last_sec"], seen_at=r["seen_at"],
        last_seen_at=r["last_seen_at"], provenance=r["provenance"], provenance_label=provenance_label(r["provenance"]),
        crop_url=crop_url(r, full), alerts=alerts.get(r["id"], []), video=video_ref(run, r["first_sec"], user))


def _where(conn: sqlite3.Connection, user: User, q: SightingQuery) -> Tuple[str, list, Optional[Dict[str, float]]]:
    cams = visible_camera_ids(conn, user)
    where, params = [f"s.camera_id IN {in_clause(cams)}"], []
    matches: Optional[Dict[str, float]] = None
    plate = fuzzy.compact(q.plate)
    if plate:
        index = plate_index(conn)
        if "*" in q.plate or "?" in q.plate:
            if user.masked:
                raise Forbidden("masked roles search by one exact plate number (with a reason), not by pattern")
            rx = fuzzy.wildcard_regex(q.plate)
            matches = {c: 0.0 for c in index.plates if rx.match(c)}
        else:
            matches = index.matches(plate, 0.0 if user.masked else q.tolerance)
        where.append(f"s.compact IN ({','.join('?' * len(matches)) or 'NULL'})")
        params += list(matches)
    for col, val, op in (("s.seen_at", q.start, ">="), ("s.seen_at", q.end, "<=")):
        if val:
            where.append(f"{col} {op} ?")
            params.append(clock.iso(clock.parse(val)) if len(val) > 10 else val + ("T23:59:59.999" if op == "<=" else ""))
    for col, val in (("s.camera_id", q.camera_id), ("s.run_id", q.run_id), ("s.vtype", q.vtype), ("s.status", q.status)):
        if val not in (None, ""):
            where.append(f"{col} = ?")
            params.append(val)
    if q.provenance == "simulated":
        where.append("s.provenance != 'camera'")
    elif q.provenance:
        where.append("s.provenance = ?")
        params.append(q.provenance)
    if q.has_alerts is not None:
        where.append(("" if q.has_alerts else "NOT ") + "EXISTS (SELECT 1 FROM alerts a WHERE a.sighting_id = s.id)")
    return " AND ".join(where), params, matches


def _authorise_search(conn: sqlite3.Connection, user: User, q: SightingQuery) -> None:
    require(user, *SEARCH_ROLES)
    if not q.purpose.strip():
        raise ValueError("choose the purpose of this search (it is recorded in the audit log)")
    plate = fuzzy.compact(q.plate)
    if user.masked and plate and "*" not in q.plate and "?" not in q.plate:
        if not q.reveal_reason:
            raise Forbidden("searching by a plate number reveals that plate: choose a reason first")
        reveal(conn, user, q.reveal_reason, note=f"search by plate number ({q.purpose})", compact=plate)


def search(conn: sqlite3.Connection, user: User, q: SightingQuery) -> Page[SightingOut]:
    """Fuzzy plate search (OCR-aware, tolerance 0-2 characters; * and ? wildcards) with time, camera, vehicle type,
    status and provenance filters, newest first (best match first when searching by plate). Results are masked for
    traffic managers and admins. Every search is audit-logged with its filters and purpose."""
    _authorise_search(conn, user, q)
    where, params, matches = _where(conn, user, q)
    total = db.scalar(conn, f"SELECT COUNT(*) FROM sightings s WHERE {where}", params)
    order = "s.seen_at DESC, s.id DESC"
    if matches:
        rs = db.rows(conn, f"SELECT s.* FROM sightings s WHERE {where} LIMIT {MAX_ROWS}", params)
        rs.sort(key=lambda r: (matches.get(r["compact"], 9), _neg(r["seen_at"])))
        page_rows = rs[(q.page - 1) * q.page_size: q.page * q.page_size]
    else:
        page_rows = db.rows(conn, f"SELECT s.* FROM sightings s WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                            params + [q.page_size, (q.page - 1) * q.page_size])
    runs, alerts = _runs(conn), _alerts_of(conn, [r["id"] for r in page_rows])
    items = [to_out(conn, user, r, runs, alerts, matches.get(r["compact"]) if matches else None) for r in page_rows]
    audit(conn, user, "search", "sightings", None,
          {"filters": q.model_dump(exclude={"purpose", "reference", "reveal_reason"}, exclude_none=True),
           "results": total, "reference": q.reference}, reason=q.purpose)
    return Page[SightingOut](items=items, total=total, page=q.page, page_size=q.page_size)


def _neg(text: str) -> str:
    """Key that sorts ISO times newest first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


def get(conn: sqlite3.Connection, user: User, sighting_id: int, purpose: str = "") -> SightingDetail:
    """One sighting with its alerts, video position, scene frame and (police only) the SIMULATED registry record."""
    require(user, *SEARCH_ROLES)
    r = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (sighting_id,))
    if r is None or r["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such sighting")
    runs = _runs(conn)
    base = to_out(conn, user, r, runs, _alerts_of(conn, [r["id"]]))
    full = not base.masked
    rec, name, note = None, "", ""
    if user.role == "police":
        prov = get_provider(str(active().registry_path))
        d = prov.lookup(r["compact"])
        rec = RegistryRecord(**d) if d else None
        name = prov.name
        note = "" if d else UNMATCHED_NOTE
    others = db.scalar(conn, f"SELECT COUNT(*) FROM sightings WHERE compact = ? AND id != ? AND camera_id IN "
                             f"{in_clause(visible_camera_ids(conn, user))}", (r["compact"], r["id"]))
    audit(conn, user, "view", "sighting", sighting_id, {"masked": not full}, reason=purpose)
    return SightingDetail(**base.model_dump(), track_ids=json.loads(r["track_ids"] or "[]"), first_frame=r["first_frame"],
                          last_frame=r["last_frame"],
                          scene_url=f"/media/sightings/{r['id']}/scene" if (r["scene_path"] and full) else None,
                          registry=rec, registry_name=name, registry_note=note, other_sightings=others,
                          can_reveal=user.masked and not full)


def reveal_sighting(conn: sqlite3.Connection, user: User, sighting_id: int, reason: str, note: str = "") -> RevealOut:
    """'Show full plate' for one record (traffic managers, admins): needs a reason, is audit-logged, lasts 15 min."""
    r = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (sighting_id,))
    if r is None or r["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such sighting")
    expires = reveal(conn, user, reason, note, sighting_id=sighting_id)
    return RevealOut(sighting_id=sighting_id, plate=r["plate_text"], crop_url=crop_url(r, True), expires_at=expires)


def timeline_of(conn: sqlite3.Connection, user: User, plate: str) -> Timeline:
    """Every sighting of one plate across cameras in time order, with distance, time gap and implied speed between
    consecutive sightings; impossible travel flagged by alpr.registry.check_travel. Includes the registry side's
    external sightings (SIMULATED). Police only."""
    require(user, "police")
    compact = fuzzy.compact(plate)
    if not compact:
        raise ValueError("enter a plate")
    cams = {c["id"]: c for c in db.rows(conn, f"SELECT * FROM cameras WHERE id IN "
                                              f"{in_clause(visible_camera_ids(conn, user))}")}
    runs = _runs(conn)
    pts = []
    for s in db.rows(conn, f"SELECT * FROM sightings WHERE compact = ? AND camera_id IN {in_clause(list(cams))} "
                           f"ORDER BY seen_at", (compact,)):
        c = cams[s["camera_id"]]
        pts.append(timeline.Point(clock.parse(s["seen_at"]), c["name"], c["lat"], c["lon"], s["provenance"], "camera",
                                  s["id"], runs[s["run_id"]]["label"]))
    for x in get_provider(str(active().registry_path)).sightings(compact):
        pts.append(timeline.Point(clock.parse(x["seen_at"]), x["location"], x["lat"], x["lon"], "synthetic",
                                  "registry", None, "external sighting (SIMULATED registry)"))
    chain = timeline.build(pts, float(db.get_setting(conn, "max_speed_kmh")))
    near = sorted(c for c in plate_index(conn).matches(compact, 1.0) if c != compact)
    points = []
    for p in chain.points:
        s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (p.sighting_id,)) if p.sighting_id else None
        points.append(TimelinePoint(seen_at=clock.iso(p.seen_at), place=p.place, lat=p.lat, lon=p.lon,
                                    provenance=p.provenance, provenance_label=provenance_label(p.provenance),
                                    source=p.source, sighting_id=p.sighting_id, run_label=p.run_label,
                                    video=video_ref(runs[s["run_id"]], s["first_sec"], user) if s else None))
    text = db.scalar(conn, "SELECT plate_text FROM plates WHERE compact = ?", (compact,)) or plate
    audit(conn, user, "timeline", "plate", compact, {"points": len(points), "impossible": chain.impossible})
    return Timeline(plate=text, points=points,
                    legs=[TimelineLeg(km=l.km, seconds=l.seconds, speed_kmh=l.speed_kmh, impossible=l.impossible,
                                      message=l.message) for l in chain.legs],
                    impossible=chain.impossible,
                    rule=f"alpr.registry.check_travel: more than {db.get_setting(conn, 'max_speed_kmh')} km/h between "
                         f"two sightings is impossible; legs under 2 km (the same site) are ignored",
                    simulated=any(p.provenance != "camera" for p in points), near_plates=near)


def export_csv(conn: sqlite3.Connection, user: User, q: SightingQuery) -> str:
    """The search result as CSV (all pages, masked for masked roles, with a provenance column). Audit-logged."""
    _authorise_search(conn, user, q)
    where, params, matches = _where(conn, user, q)
    rs = db.rows(conn, f"SELECT s.* FROM sightings s WHERE {where} ORDER BY s.seen_at DESC LIMIT {MAX_ROWS}", params)
    runs, alerts = _runs(conn), _alerts_of(conn, [r["id"] for r in rs])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seen_at", "last_seen_at", "plate", "status", "valid_format", "vehicle_type", "region", "camera", "run",
                "confidence", "readings", "alerts", "provenance"])
    for r in rs:
        o = to_out(conn, user, r, runs, alerts)
        w.writerow([o.seen_at, o.last_seen_at, o.plate, "provisional" if o.status == "TENTATIVE" else o.status.lower(),
                    o.valid_format, o.vtype, o.region, o.camera_name, o.run_label,
                    "" if o.confidence is None else round(o.confidence, 4), o.votes,
                    ";".join(a.code for a in o.alerts), o.provenance])
    audit(conn, user, "export", "sightings", None, {"format": "csv", "rows": len(rs),
                                                    "filters": q.model_dump(exclude_none=True, exclude={"purpose"})},
          reason=q.purpose)
    return buf.getvalue()


def plate_sightings_count(conn: sqlite3.Connection, compact: str) -> int:
    return db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE compact = ?", (compact,))
