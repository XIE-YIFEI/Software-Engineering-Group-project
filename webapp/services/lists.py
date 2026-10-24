"""Lists on top of the SIMULATED registry: the police watch list and blacklist (all road cameras) and each site's
resident list and ban list (its guards). New imports and uploads are matched against the active lists
(webapp/importer.py match_lists). The registry itself stays behind webapp/registry_provider.py, so an authorised
registry can replace the simulated one without touching the lists."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from .. import clock, db
from ..analysis import fuzzy
from ..models import ListEntryOut
from ..privacy import audit
from ..registry_provider import get_provider
from ..security import Forbidden, User
from ..settings import active

POLICE_LISTS = ("watch", "blacklist")
GUARD_LISTS = ("resident", "banned")


def entry_out(conn: sqlite3.Connection, e: dict, registry_status: Optional[str] = None) -> ListEntryOut:
    return ListEntryOut(id=e["id"], list=e["list"], plate=e["plate_text"], label=e["label"], unit_no=e["unit_no"],
                        reason=e["reason"], valid_to=e["valid_to"], simulated=bool(e["simulated"]),
                        created_by=e.get("username") or ("seed (simulated)" if e["simulated"] else ""),
                        created_at=e["created_at"], active=bool(e["active"]),
                        past_sightings=db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE compact = ?", (e["compact"],)),
                        registry_status=registry_status)


def _check(user: User, lst: str) -> Optional[int]:
    """The site of a guard list, None for a police list; raises when the role may not touch that list."""
    if lst in POLICE_LISTS:
        if user.role != "police":
            raise Forbidden("only police officers manage the watch list and blacklist")
        return None
    if lst in GUARD_LISTS:
        if user.role != "guard" or user.site_id is None:
            raise Forbidden("residents and the ban list belong to a site's guards")
        return user.site_id
    raise ValueError(f"unknown list {lst}")


def entries(conn: sqlite3.Connection, user: User, lst: str) -> List[ListEntryOut]:
    site = _check(user, lst)
    rs = db.rows(conn, "SELECT e.*, u.username FROM list_entries e LEFT JOIN users u ON u.id = e.created_by WHERE "
                       "e.list = ? AND COALESCE(e.site_id, 0) = COALESCE(?, 0) ORDER BY e.active DESC, e.created_at DESC",
                 (lst, site))
    prov = get_provider(str(active().registry_path)) if lst in POLICE_LISTS else None
    out = []
    for e in rs:
        rec = prov.lookup(e["compact"]) if prov else None
        out.append(entry_out(conn, e, (rec or {}).get("status", "not in the simulated registry") if prov else None))
    return out


def add(conn: sqlite3.Connection, user: User, lst: str, plate: str, reason: str = "", label: str = "",
        unit_no: str = "", valid_to: Optional[str] = None) -> ListEntryOut:
    """Add a plate to a list. Police lists need a reason; residents need a unit number."""
    site = _check(user, lst)
    compact = fuzzy.compact(plate)
    if not compact:
        raise ValueError("enter a plate")
    if lst in POLICE_LISTS + ("banned",) and not reason.strip():
        raise ValueError("a reason is required")
    if lst == "resident" and not unit_no.strip():
        raise ValueError("a resident needs a unit number")
    if db.scalar(conn, "SELECT 1 FROM list_entries WHERE list = ? AND compact = ? AND active = 1 AND "
                       "COALESCE(site_id, 0) = COALESCE(?, 0)", (lst, compact, site)):
        raise ValueError("this plate is already on the list")
    from .gate import _text
    with db.transaction(conn):
        eid = db.insert(conn, "list_entries", {"list": lst, "site_id": site, "compact": compact, "plate_text": _text(compact),
                                               "label": label.strip()[:60], "unit_no": unit_no.strip()[:20],
                                               "reason": reason.strip()[:300], "valid_to": valid_to, "simulated": 0,
                                               "created_by": user.id, "created_at": clock.now_iso(), "active": 1})
        audit(conn, user, "list_add", "list_entry", eid, {"list": lst, "plate": compact}, reason=reason.strip()[:200])
    return entry_out(conn, db.row(conn, "SELECT e.*, u.username FROM list_entries e LEFT JOIN users u ON u.id = "
                                        "e.created_by WHERE e.id = ?", (eid,)))


def deactivate(conn: sqlite3.Connection, user: User, entry_id: int, reason: str = "") -> ListEntryOut:
    e = db.row(conn, "SELECT * FROM list_entries WHERE id = ?", (entry_id,))
    if e is None:
        raise LookupError("no such entry")
    site = _check(user, e["list"])
    if (e["site_id"] or None) != site:
        raise Forbidden("this entry belongs to another site")
    conn.execute("UPDATE list_entries SET active = 0 WHERE id = ?", (entry_id,))
    audit(conn, user, "list_remove", "list_entry", entry_id, {"list": e["list"], "plate": e["compact"]}, reason=reason)
    return entry_out(conn, {**db.row(conn, "SELECT * FROM list_entries WHERE id = ?", (entry_id,)), "username": None})
