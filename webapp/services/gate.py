"""Guardhouse: identity of a plate at the gate (banned / resident / visitor with a valid pass / unregistered
visitor), the replay cards of the demo video, the guard's decisions (Allow, Stop & note, Exited) written to the
access log, the visitors inside now with overstays first, visitor passes, residents and the site's ban list.

The gate camera reads plates on the entry lane only (exit-lane plates are too small and oblique), so exits come
from the guard's "Exited" button (or simulated exits in the demo seed)."""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import timedelta
from typing import List, Optional

from .. import clock, db
from ..analysis import fuzzy
from ..models import AccessEventOut, GateCard, Identity, InsideOut, ListEntryOut, PassOut
from ..privacy import audit
from ..security import Forbidden, User, require
from .common import in_clause, provenance_label, visible_camera_ids

DECISIONS = ("allowed", "stopped", "exited")
_TITLE = {"banned": "Banned at this site", "resident": "Resident", "visitor_pre": "Pre-registered visitor",
          "visitor_unregistered": "Unregistered visitor"}
_COLOUR = {"banned": "red", "resident": "green", "visitor_pre": "teal", "visitor_unregistered": "yellow"}


def _site(user: User) -> int:
    require(user, "guard")
    if user.site_id is None:
        raise Forbidden("this guard account is not assigned to a site")
    return user.site_id


def _text(compact: str) -> str:
    from alpr.plates import parse_plate
    p = parse_plate(compact, 0)
    return p.text if p and p.compact == compact else compact


def identify(conn: sqlite3.Connection, user: User, plate: str) -> Identity:
    """Identity colour of a plate at this guard's gate, now: red = on the site's ban list, green = resident,
    teal = visitor with a pass valid now, yellow = unregistered visitor (also a pass outside its window)."""
    site = _site(user)
    compact = fuzzy.compact(plate)
    if not compact:
        raise ValueError("enter a plate")
    now = clock.now_iso()
    text = _text(compact)

    def ident(kind: str, detail: str, **kw) -> Identity:
        return Identity(plate=text, identity=kind, colour=_COLOUR[kind], title=_TITLE[kind], detail=detail, **kw)
    ban = db.row(conn, "SELECT * FROM list_entries WHERE list = 'banned' AND active = 1 AND site_id = ? AND compact = ?",
                 (site, compact))
    if ban:
        return ident("banned", ban["reason"] or "on the site's ban list: do not allow entry",
                     simulated_list=bool(ban["simulated"]))
    res = db.row(conn, "SELECT * FROM list_entries WHERE list = 'resident' AND active = 1 AND site_id = ? AND compact = ?",
                 (site, compact))
    if res:
        return ident("resident", f"Unit {res['unit_no']}" + (f" · {res['label']}" if res["label"] else ""),
                     unit_no=res["unit_no"], simulated_list=bool(res["simulated"]))
    p = db.row(conn, "SELECT * FROM visitor_passes WHERE site_id = ? AND compact = ? AND status = 'active' AND "
                     "valid_from <= ? AND valid_to >= ? ORDER BY valid_to DESC LIMIT 1", (site, compact, now, now))
    if p:
        return ident("visitor_pre", f"Unit {p['unit_no']} · pass valid until {p['valid_to'][11:16]}"
                     + (f" · {p['visitor_label']}" if p["visitor_label"] else ""),
                     unit_no=p["unit_no"], pass_id=p["id"], pass_valid_to=p["valid_to"], simulated_list=bool(p["simulated"]))
    other = db.row(conn, "SELECT * FROM visitor_passes WHERE site_id = ? AND compact = ? AND status = 'active' "
                         "ORDER BY valid_to DESC LIMIT 1", (site, compact))
    if other:
        when = "expired" if other["valid_to"] < now else "not valid yet"
        return ident("visitor_unregistered", f"Pass for unit {other['unit_no']} {when} "
                     f"({other['valid_from'][11:16]}-{other['valid_to'][11:16]}): check with the unit",
                     unit_no=other["unit_no"], pass_id=other["id"], pass_valid_to=other["valid_to"])
    return ident("visitor_unregistered", "Not a resident and no valid visitor pass: ask for the unit, issue a pass")


def replay(conn: sqlite3.Connection, user: User, run_id: Optional[int] = None) -> dict:
    """The gate demo: the replay video of this guard's gate and one card per plate, shown when the video reaches the
    plate's first_sec (minus the video's start offset)."""
    site = _site(user)
    cams = visible_camera_ids(conn, user)
    run = db.row(conn, f"SELECT * FROM runs WHERE camera_id IN {in_clause(cams)} "
                       + ("AND id = ? " if run_id else "") + "ORDER BY origin = 'demo' DESC, id DESC LIMIT 1",
                 (run_id,) if run_id else ())
    if run is None:
        raise LookupError("no recorded run for this gate")
    cards = []
    for s in db.rows(conn, "SELECT * FROM sightings WHERE run_id = ? AND status = 'CONFIRMED' ORDER BY first_sec",
                     (run["id"],)):
        cards.append(GateCard(sighting_id=s["id"], plate=s["plate_text"], vtype=s["vtype"], first_sec=s["first_sec"],
                              video_time=round(max(0.0, s["first_sec"] - (run["video_offset_sec"] or 0)), 2),
                              seen_at=s["seen_at"], provenance=s["provenance"],
                              provenance_label=provenance_label(s["provenance"]), identity=identify(conn, user, s["compact"])))
    audit(conn, user, "view", "gate_replay", run["id"], {"cards": len(cards)})
    return {"run_id": run["id"], "label": run["label"], "video_url": f"/media/runs/{run['id']}/video",
            "camera_id": run["camera_id"], "site_id": site, "cards": [c.model_dump() for c in cards],
            "note": run["provenance_note"]}


def decide(conn: sqlite3.Connection, user: User, plate: str, decision: str, note: str = "",
           sighting_id: Optional[int] = None, unit_no: str = "") -> AccessEventOut:
    """Log the guard's decision: allowed (entry), stopped (entry refused, with a note) or exited (the visitor left)."""
    site = _site(user)
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision == "stopped" and not note.strip():
        raise ValueError("add a note when stopping a vehicle")
    compact = fuzzy.compact(plate)
    ident = identify(conn, user, compact)
    cam = db.scalar(conn, f"SELECT id FROM cameras WHERE id IN {in_clause(visible_camera_ids(conn, user))} LIMIT 1")
    simulated = 0
    if sighting_id is not None:
        s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (sighting_id,))
        if s is None or s["camera_id"] not in visible_camera_ids(conn, user):
            raise LookupError("no such sighting at this gate")
        simulated = int(s["provenance"] != "camera")
    identity = ident.identity
    if decision == "exited":
        last = db.row(conn, "SELECT identity, unit_no, simulated FROM access_events WHERE site_id = ? AND compact = ? "
                            "AND direction = 'entry' ORDER BY at DESC LIMIT 1", (site, compact))
        if last:
            identity, unit_no, simulated = last["identity"], unit_no or last["unit_no"], max(simulated, last["simulated"])
    with db.transaction(conn):
        eid = db.insert(conn, "access_events", {
            "site_id": site, "camera_id": cam, "sighting_id": sighting_id, "compact": compact, "plate_text": _text(compact),
            "direction": "exit" if decision == "exited" else "entry", "decision": decision, "identity": identity,
            "pass_id": ident.pass_id, "unit_no": unit_no or ident.unit_no, "guard_id": user.id,
            "note": note.strip()[:500], "at": clock.now_iso(), "simulated": simulated})
        audit(conn, user, "gate_" + decision, "access_event", eid, {"plate": compact, "identity": identity})
    return _event(db.row(conn, "SELECT e.*, u.username FROM access_events e LEFT JOIN users u ON u.id = e.guard_id "
                               "WHERE e.id = ?", (eid,)))


def _event(e: dict) -> AccessEventOut:
    return AccessEventOut(id=e["id"], at=e["at"], plate=e["plate_text"], direction=e["direction"], decision=e["decision"],
                          identity=e["identity"], unit_no=e["unit_no"], pass_id=e["pass_id"],
                          guard=e.get("username") or ("seed (simulated)" if e["simulated"] else ""), note=e["note"],
                          simulated=bool(e["simulated"]), sighting_id=e["sighting_id"])


def inside(conn: sqlite3.Connection, user: User) -> List[InsideOut]:
    """Visitors whose last access event is an entry that was allowed: still inside. Overstays first: inside longer
    than `overstay_hours`, or past the end of their pass."""
    site = _site(user)
    hours = float(db.get_setting(conn, "overstay_hours"))
    now = clock.now()
    rows = db.rows(conn, "SELECT e.*, p.valid_to AS pass_to FROM access_events e LEFT JOIN visitor_passes p ON "
                         "p.id = e.pass_id WHERE e.site_id = ? AND e.id IN (SELECT MAX(id) FROM access_events WHERE "
                         "site_id = ? GROUP BY compact) AND e.direction = 'entry' AND e.decision = 'allowed' AND "
                         "e.identity IN ('visitor_pre', 'visitor_unregistered')", (site, site))
    out = []
    for e in rows:
        mins = int((now - clock.parse(e["at"])).total_seconds() // 60)
        expired = bool(e["pass_to"] and e["pass_to"] < clock.iso(now))
        out.append(InsideOut(plate=e["plate_text"], identity=e["identity"], unit_no=e["unit_no"], entered_at=e["at"],
                             minutes_inside=mins, pass_valid_to=e["pass_to"], pass_expired=expired,
                             overstay=mins >= hours * 60 or expired, simulated=bool(e["simulated"]), note=e["note"]))
    out.sort(key=lambda x: (not x.overstay, -x.minutes_inside))
    return out


def access_log(conn: sqlite3.Connection, user: User, plate: str = "", start: str = "", end: str = "",
               decision: str = "", identity: str = "", limit: int = 500) -> List[AccessEventOut]:
    """The site's access log, newest first; plate search is fuzzy (tolerance 1). Audit-logged."""
    site = _site(user)
    where, params = ["e.site_id = ?"], [site]
    if plate.strip():
        cands = [r["compact"] for r in db.rows(conn, "SELECT DISTINCT compact FROM access_events WHERE site_id = ?",
                                               (site,))]
        m = fuzzy.best_matches(plate, cands, 1.0)
        where.append(f"e.compact IN ({','.join('?' * len(m)) or 'NULL'})")
        params += list(m)
    for col, val, op in (("e.at", start, ">="), ("e.at", end, "<=")):
        if val:
            where.append(f"{col} {op} ?")
            params.append(val if len(val) > 10 else val + ("T23:59:59.999" if op == "<=" else ""))
    for col, val in (("e.decision", decision), ("e.identity", identity)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    rs = db.rows(conn, f"SELECT e.*, u.username FROM access_events e LEFT JOIN users u ON u.id = e.guard_id WHERE "
                       f"{' AND '.join(where)} ORDER BY e.at DESC LIMIT ?", params + [limit])
    audit(conn, user, "search", "access_log", None, {"plate": plate, "results": len(rs)}, reason="Gate operations")
    return [_event(e) for e in rs]


def access_log_csv(conn: sqlite3.Connection, user: User, **filters) -> str:
    evs = access_log(conn, user, **filters)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "plate", "direction", "decision", "identity", "unit", "guard", "note", "simulated"])
    for e in evs:
        w.writerow([e.at, e.plate, e.direction, e.decision, e.identity, e.unit_no, e.guard, e.note, e.simulated])
    audit(conn, user, "export", "access_log", None, {"format": "csv", "rows": len(evs)})
    return buf.getvalue()


# ---------------------------------------------------------------------------- passes and site lists
def _pass(p: dict, now: str) -> PassOut:
    return PassOut(id=p["id"], plate=p["plate_text"], unit_no=p["unit_no"], visitor_label=p["visitor_label"],
                   valid_from=p["valid_from"], valid_to=p["valid_to"], status=p["status"],
                   valid_now=p["status"] == "active" and p["valid_from"] <= now <= p["valid_to"],
                   simulated=bool(p["simulated"]), created_by=p.get("username") or ("seed" if p["simulated"] else ""))


def passes(conn: sqlite3.Connection, user: User) -> List[PassOut]:
    site = _site(user)
    now = clock.now_iso()
    return [_pass(p, now) for p in db.rows(conn, "SELECT p.*, u.username FROM visitor_passes p LEFT JOIN users u ON "
                                                 "u.id = p.created_by WHERE p.site_id = ? ORDER BY p.valid_to DESC", (site,))]


def issue_pass(conn: sqlite3.Connection, user: User, plate: str, unit_no: str, visitor_label: str = "",
               valid_from: Optional[str] = None, valid_to: Optional[str] = None, hours: Optional[float] = None) -> PassOut:
    """A time-bounded visitor pass: plate, unit number, an optional label (no names or IC numbers) and a validity
    window (default now -> now + default_pass_hours)."""
    site = _site(user)
    compact = fuzzy.compact(plate)
    if not compact or not unit_no.strip():
        raise ValueError("a pass needs a plate and a unit number")
    start = clock.parse(valid_from) if valid_from else clock.now()
    end = clock.parse(valid_to) if valid_to else start + timedelta(
        hours=float(hours or db.get_setting(conn, "default_pass_hours")))
    if end <= start:
        raise ValueError("the pass must end after it starts")
    with db.transaction(conn):
        pid = db.insert(conn, "visitor_passes", {"site_id": site, "compact": compact, "plate_text": _text(compact),
                                                 "unit_no": unit_no.strip()[:20], "visitor_label": visitor_label.strip()[:40],
                                                 "valid_from": clock.iso(start), "valid_to": clock.iso(end),
                                                 "status": "active", "simulated": 0, "seed_key": None,
                                                 "created_by": user.id, "created_at": clock.now_iso()})
        audit(conn, user, "pass_issue", "visitor_pass", pid, {"plate": compact, "unit": unit_no, "to": clock.iso(end)})
    return _pass(db.row(conn, "SELECT p.*, u.username FROM visitor_passes p LEFT JOIN users u ON u.id = p.created_by "
                              "WHERE p.id = ?", (pid,)), clock.now_iso())


def cancel_pass(conn: sqlite3.Connection, user: User, pass_id: int) -> PassOut:
    site = _site(user)
    if not db.scalar(conn, "SELECT 1 FROM visitor_passes WHERE id = ? AND site_id = ?", (pass_id, site)):
        raise LookupError("no such pass")
    conn.execute("UPDATE visitor_passes SET status = 'cancelled' WHERE id = ?", (pass_id,))
    audit(conn, user, "pass_cancel", "visitor_pass", pass_id)
    return _pass(db.row(conn, "SELECT p.*, NULL AS username FROM visitor_passes p WHERE p.id = ?", (pass_id,)),
                 clock.now_iso())


def site_entries(conn: sqlite3.Connection, user: User, lst: str) -> List[ListEntryOut]:
    site = _site(user)
    if lst not in ("resident", "banned"):
        raise Forbidden("guards manage the resident and ban lists of their site")
    from .lists import entry_out
    return [entry_out(conn, e) for e in db.rows(conn, "SELECT e.*, u.username FROM list_entries e LEFT JOIN users u ON "
                                                      "u.id = e.created_by WHERE e.list = ? AND e.site_id = ? ORDER BY "
                                                      "e.active DESC, e.created_at DESC", (lst, site))]
