"""Action cards: write operations the assistant PREPARES and the user confirms.

`prepare` checks everything it can without writing (the role, the record, the plate, the time window, the allowed
alert transition) and stores the card; nothing changes in the system. Only `confirm`, called when the signed-in owner
of the card presses Confirm, runs the ordinary service function - with the same permission checks as the pages - as
that user with via="assistant", so the service's own audit row says who confirmed it and that it came through the
assistant; a `copilot_action` audit row links it to the card. Cards expire after 30 minutes.

Autumn 2026 the copilot is read-only (config.ACTIONS_ENABLED is False, plan Rev2): `prepare` and `confirm` refuse
(HTTP 403 through the API), so no card is created and none runs, not even one prepared earlier; `cancel` still works.
The code stays for the spring plan, where turning it back on is a separate, reviewed change.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .. import clock, db
from ..analysis import fuzzy
from ..privacy import audit, reveal as privacy_reveal
from ..security import Forbidden, User, require
from ..services import alerts as alert_svc
from ..services import gate as gate_svc
from ..services import lists as list_svc
from ..services import plates as plate_svc
from ..services.common import visible_camera_ids
from ..settings import REVEAL_REASONS
from . import config

TITLES = {"visitor_pass": "Issue a visitor pass", "list_entry": "Add to a list", "alert_status": "Change an alert's status",
          "reveal": "Show full plate (reveal with a reason)"}
LIST_LABEL = {"watch": "watch list", "blacklist": "blacklist", "resident": "residents", "banned": "site ban list"}
EDITABLE = {"visitor_pass": ("plate", "unit_no", "visitor_label", "valid_from", "valid_to"),
            "list_entry": ("plate", "reason", "label", "unit_no", "valid_to"),
            "alert_status": ("note", "corrected_plate"), "reveal": ("reason", "note")}


def _plate(text: str) -> str:
    compact = fuzzy.compact(text)
    if not compact:
        raise ValueError("a plate is needed")
    return compact


def _window(conn: sqlite3.Connection, valid_from: str, valid_to: str, hours: Optional[float]) -> tuple:
    start = clock.parse(valid_from) if valid_from else clock.now()
    end = clock.parse(valid_to) if valid_to else start + timedelta(
        hours=float(hours or db.get_setting(conn, "default_pass_hours")))
    if end <= start:
        raise ValueError("the pass must end after it starts")
    return clock.iso(start)[:16], clock.iso(end)[:16]


def _check(conn: sqlite3.Connection, user: User, kind: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Validate (no writes) and normalise the parameters; returns the card's preview lines in p['_lines']."""
    lines: List[str] = []
    if kind == "visitor_pass":
        require(user, "guard")
        compact = _plate(p.get("plate", ""))
        if not str(p.get("unit_no", "")).strip():
            raise ValueError("a visitor pass needs the unit number")
        p["valid_from"], p["valid_to"] = _window(conn, p.get("valid_from") or "", p.get("valid_to") or "", p.pop("hours", None))
        p["plate"] = gate_svc._text(compact)
        ident = gate_svc.identify(conn, user, compact)
        lines = [f"Plate {p['plate']} · unit {p['unit_no']}" + (f" · {p['visitor_label']}" if p.get("visitor_label") else ""),
                 f"Valid {p['valid_from'].replace('T', ' ')} to {p['valid_to'].replace('T', ' ')}",
                 f"Now: {ident.title} ({ident.detail})"]
        if ident.identity == "banned":
            lines.append("Warning: this plate is on the site's ban list.")
    elif kind == "list_entry":
        lst = p.get("list", "")
        list_svc._check(user, lst)                                     # the role may manage this list
        compact = _plate(p.get("plate", ""))
        p["plate"] = gate_svc._text(compact)
        site = user.site_id if lst in list_svc.GUARD_LISTS else None
        if db.scalar(conn, "SELECT 1 FROM list_entries WHERE list = ? AND compact = ? AND active = 1 AND "
                           "COALESCE(site_id, 0) = COALESCE(?, 0)", (lst, compact, site)):
            raise ValueError(f"{p['plate']} is already on the {LIST_LABEL[lst]}")
        lines = [f"{p['plate']} -> {LIST_LABEL[lst]}"] + [f"{k.replace('_', ' ').capitalize()}: {p[k]}" for k in
                                                           ("reason", "label", "unit_no", "valid_to") if p.get(k)]
    elif kind == "alert_status":
        require(user, "police", "guard")
        a = alert_svc.get_alert(conn, user, int(p["alert_id"]))
        if p.get("to") not in a.allowed_transitions:
            raise ValueError(f"alert {a.id} is {a.status}; it can go to: {', '.join(a.allowed_transitions) or 'nothing'}")
        if p.get("corrected_plate") and p["to"] != "false_alarm":
            raise ValueError("a corrected plate only goes with a false alarm")
        lines = [f"Alert #{a.id} {a.title} · {a.plate or 'no plate'}", f"{a.status} -> {p['to']}"]
        if a.provenance != "camera":
            lines.append(a.provenance_label)
    elif kind == "reveal":
        if not user.masked:
            raise Forbidden("this role already sees full plates")
        if p.get("sighting_id"):
            s = db.row(conn, "SELECT id, plate_text, camera_id FROM sightings WHERE id = ?", (int(p["sighting_id"]),))
            if s is None or s["camera_id"] not in visible_camera_ids(conn, user):
                raise LookupError("no such sighting")
            p["sighting_id"] = s["id"]
            p.pop("plate", None)
            lines =[f"Record S:{s['id']} (plate masked until you confirm with a reason)"]
        else:
            p["plate"] = _plate(p.get("plate", ""))
            lines = [f"Plate number {p['plate']}: searching by it shows its records in full"]
        lines.append(f"Lasts {int(db.get_setting(conn, 'reveal_minutes'))} minutes and is recorded in the audit log "
                     f"with your reason.")
    else:
        raise ValueError(f"unknown action {kind}")
    p["_lines"] = lines
    return p


def _fields(kind: str, p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The card's form: what the user can check and complete before confirming."""
    if kind == "visitor_pass":
        return [{"name": "plate", "label": "Plate", "value": p.get("plate", ""), "required": True},
                {"name": "unit_no", "label": "Unit", "value": p.get("unit_no", ""), "required": True},
                {"name": "visitor_label", "label": "Label (no names or IC numbers)", "value": p.get("visitor_label", "")},
                {"name": "valid_from", "label": "Valid from", "type": "datetime-local", "value": p.get("valid_from", "")},
                {"name": "valid_to", "label": "Valid to", "type": "datetime-local", "value": p.get("valid_to", ""),
                 "required": True}]
    if kind == "list_entry":
        lst = p.get("list")
        return [{"name": "plate", "label": "Plate", "value": p.get("plate", ""), "required": True},
                {"name": "reason", "label": "Reason (recorded in the audit log)", "type": "textarea",
                 "value": p.get("reason", ""), "required": lst in ("watch", "blacklist", "banned")},
                {"name": "unit_no", "label": "Unit", "value": p.get("unit_no", ""), "required": lst == "resident"}
                if lst == "resident" else {"name": "label", "label": "Label", "value": p.get("label", "")}]
    if kind == "alert_status":
        closing = p.get("to") in ("resolved", "false_alarm")
        f = [{"name": "note", "label": "Note: who checked what" + (" (required to close)" if closing else ""),
              "type": "textarea", "value": p.get("note", ""), "required": closing}]
        if p.get("to") == "false_alarm":
            f.append({"name": "corrected_plate", "label": "Correct plate, if the OCR misread it (optional)",
                      "value": p.get("corrected_plate") or ""})
        return f
    return [{"name": "reason", "label": "Reason (recorded in the audit log)", "type": "select", "options": REVEAL_REASONS,
             "value": "", "required": True},
            {"name": "note", "label": "Note (required for 'Other')", "type": "textarea", "value": p.get("note", "")}]


def card(row: Dict[str, Any]) -> Dict[str, Any]:
    p = json.loads(row["params"])
    status = row["status"]
    if status == "pending" and row["expires_at"] < clock.now_iso():
        status = "expired"
    return {"id": row["id"], "kind": row["kind"], "title": TITLES[row["kind"]], "lines": p.get("_lines", []),
            "fields": _fields(row["kind"], p), "status": status, "expires_at": row["expires_at"],
            "result": json.loads(row["result"]) if row["result"] else None}


def prepare(conn: sqlite3.Connection, user: User, kind: str, params: Dict[str, Any],
            conversation_id: Optional[int] = None) -> Dict[str, Any]:
    if not config.ACTIONS_ENABLED:
        raise Forbidden(config.READ_ONLY)
    p = _check(conn, user, kind, {k: v for k, v in params.items() if v not in (None, "")})
    now = clock.now()
    aid = db.insert(conn, "copilot_actions", {
        "conversation_id": conversation_id, "user_id": user.id, "kind": kind, "params": json.dumps(p, ensure_ascii=False),
        "status": "pending", "result": None, "created_at": clock.iso(now),
        "expires_at": clock.iso(now + timedelta(minutes=config.ACTION_MINUTES))})
    return card(db.row(conn, "SELECT * FROM copilot_actions WHERE id = ?", (aid,)))


def _own(conn: sqlite3.Connection, user: User, action_id: int) -> Dict[str, Any]:
    row = db.row(conn, "SELECT * FROM copilot_actions WHERE id = ?", (action_id,))
    if row is None or row["user_id"] != user.id:                 # only the person it was prepared for confirms it
        raise LookupError("no such action")
    if row["status"] != "pending":
        raise ValueError(f"this action is already {row['status']}")
    if row["expires_at"] < clock.now_iso():
        conn.execute("UPDATE copilot_actions SET status = 'expired', decided_at = ? WHERE id = ?",
                     (clock.now_iso(), action_id))
        raise ValueError("this card has expired: ask the assistant to prepare it again")
    return row


def confirm(conn: sqlite3.Connection, user: User, action_id: int, edits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run the prepared operation as `user` (via the assistant). A missing or invalid field leaves the card pending
    (HTTP 422) so the user can correct it; a refusal by the service marks it failed. Read-only: refused, nothing runs."""
    if not config.ACTIONS_ENABLED:
        raise Forbidden(config.READ_ONLY)
    row = _own(conn, user, action_id)
    kind = row["kind"]
    p = json.loads(row["params"])
    for k, v in (edits or {}).items():
        if k in EDITABLE[kind]:
            p[k] = v.strip() if isinstance(v, str) else v
    via = replace(user, via="assistant")
    if kind == "reveal" and not p.get("reason"):
        raise ValueError("choose the reason for showing the full plate")
    p = _check(conn, user, kind, {k: v for k, v in p.items() if not k.startswith("_")})
    try:
        if kind == "visitor_pass":
            out = gate_svc.issue_pass(conn, via, p["plate"], p["unit_no"], p.get("visitor_label", ""), p["valid_from"],
                                      p["valid_to"])
            result = {"ref": f"P:{out.id}", "text": f"Pass #{out.id} issued: {out.plate}, unit {out.unit_no}, until "
                                                    f"{out.valid_to[:16].replace('T', ' ')}"}
        elif kind == "list_entry":
            out = list_svc.add(conn, via, p["list"], p["plate"], p.get("reason", ""), p.get("label", ""),
                               p.get("unit_no", ""), p.get("valid_to") or None)
            result = {"text": f"{out.plate} added to the {LIST_LABEL[out.list]} (entry #{out.id})"}
        elif kind == "alert_status":
            out = alert_svc.transition(conn, via, int(p["alert_id"]), p["to"], p.get("note", ""),
                                       p.get("corrected_plate") or None)
            result = {"ref": f"A:{out.id}", "text": f"Alert #{out.id} is now {out.status}"}
        else:
            if p.get("sighting_id"):
                out = plate_svc.reveal_sighting(conn, via, int(p["sighting_id"]), p["reason"], p.get("note", ""))
                result = {"ref": f"S:{out.sighting_id}", "plate": out.plate, "expires_at": out.expires_at,
                          "text": f"Record S:{out.sighting_id}: {out.plate}, shown until {out.expires_at[11:16]}"}
            else:
                exp = privacy_reveal(conn, via, p["reason"], p.get("note", "") or "search by plate number (assistant)",
                                     compact=p["plate"])
                result = {"plate": p["plate"], "expires_at": exp,
                          "text": f"{p['plate']} can be searched in full until {exp[11:16]}: ask again"}
    except (Forbidden, LookupError) as e:
        _close(conn, via, row, "failed", {"error": str(e).strip("'\"")})
        raise
    _close(conn, via, row, "confirmed", result, p)
    return card(db.row(conn, "SELECT * FROM copilot_actions WHERE id = ?", (action_id,)))


def cancel(conn: sqlite3.Connection, user: User, action_id: int) -> Dict[str, Any]:
    row = _own(conn, user, action_id)
    _close(conn, replace(user, via="assistant"), row, "cancelled", None)
    return card(db.row(conn, "SELECT * FROM copilot_actions WHERE id = ?", (action_id,)))


def _close(conn: sqlite3.Connection, user: User, row: Dict[str, Any], status: str, result: Optional[Dict[str, Any]],
           params: Optional[Dict[str, Any]] = None) -> None:
    sets, vals = ["status = ?", "result = ?", "decided_at = ?"], [status, json.dumps(result, ensure_ascii=False)
                                                                   if result else None, clock.now_iso()]
    if params is not None:
        sets.append("params = ?")
        vals.append(json.dumps(params, ensure_ascii=False))
    conn.execute(f"UPDATE copilot_actions SET {', '.join(sets)} WHERE id = ?", (*vals, row["id"]))
    audit(conn, user, "copilot_action", "copilot_action", row["id"],
          {"kind": row["kind"], "status": status, "conversation": row["conversation_id"],
           "result": (result or {}).get("text") or (result or {}).get("error")})
