"""Storage of the copilot: conversations and their API transcript, the cost of every model call, retention.

The transcript is append-only and replayed verbatim (content blocks as the API returned them, thinking blocks
included), which keeps the prompt cache warm and the model's earlier reasoning valid. A question whose loop did not
end cleanly (an error, a refusal, a truncated tool call) is not stored, so the transcript always stays a valid request.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import clock, db, settings
from ..privacy import can_see_full, mask_plate
from ..security import Forbidden, User
from ..services.common import can_play, visible_camera_ids
from . import numbers

_NON_PLATE_PREFIX = {
    "ISO", "USD", "MYR", "RM", "MAE", "RMSE", "MAPE", "ACT", "SOP", "PDF", "CSV", "ZIP",
    "API", "LLM", "VLM", "OCR", "FPS", "KMH", "SEC", "MIN", "MAX", "AVG", "SHA", "UTC",
    "MYT", "SIM", "REF", "RUN", "CAM", "TC", "FC", "TS", "FS", "IN", "OUT",
}
_ABBREV_START = re.compile(rf"^(?:{'|'.join(sorted(_NON_PLATE_PREFIX, key=len, reverse=True))})\d")
_PLATE_COLUMNS = ("compact", "ocr_compact", "corrected_compact")


def _add_abbrev_compact(val: Any, out: set[str]) -> None:
    if not val:
        return
    v = re.sub(r"[^A-Z0-9]", "", str(val).upper())
    if _ABBREV_START.match(v):
        out.add(v)


def _add_json_plates(raw: Optional[str], out: set[str]) -> None:
    if not raw or "plate" not in raw:
        return
    try:
        obj = json.loads(raw)
    except Exception:                                                   # noqa: BLE001
        return
    stack: List[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in ("plate", "corrected_plate") and isinstance(v, str):
                    _add_abbrev_compact(v, out)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
                elif isinstance(v, str) and "plate" in v and v.lstrip()[:1] in "{[":
                    _add_json_plates(v, out)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))


def _add_registry_plates(out: set[str]) -> None:
    reg_path = Path(settings.active().registry_path)
    if not reg_path.is_file():
        return
    try:
        rc = sqlite3.connect(f"{reg_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        tables = {r[0] for r in rc.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        for tbl in ("vehicles", "sightings"):
            if tbl in tables:
                for (val,) in rc.execute(f'SELECT DISTINCT plate FROM "{tbl}" WHERE plate IS NOT NULL'):
                    _add_abbrev_compact(val, out)
    except sqlite3.Error:
        return
    finally:
        rc.close()


def _abbrev_plates(conn: sqlite3.Connection) -> set[str]:
    """Plate values held by the system whose prefix collides with an abbreviation (RM 1234, FC 12, TC 1 ...).

    The abbreviation exception only keeps text such as "MAE 3", "RM 50" or the zone "TC1" readable; it must never
    expose an actual plate, so any compact stored in a plate column (sightings, alerts, lane events, lists, passes,
    access events, reveals, corrections ...), recorded in audit/copilot JSON, or held in the vehicle registry is
    masked like every other plate."""
    out: set[str] = set()
    tables = {t["name"] for t in db.rows(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name in tables:
        cols = {c["name"] for c in db.rows(conn, f'PRAGMA table_info("{name}")')}
        for col in _PLATE_COLUMNS:
            if col in cols:
                for r in db.rows(conn, f'SELECT DISTINCT "{col}" AS v FROM "{name}" WHERE "{col}" IS NOT NULL'):
                    _add_abbrev_compact(r["v"], out)
    if "audit_log" in tables:
        for r in db.rows(conn, "SELECT DISTINCT target_id FROM audit_log WHERE target_type = 'plate' AND target_id IS NOT NULL"):
            _add_abbrev_compact(r["target_id"], out)
    if "copilot_messages" in tables:
        for r in db.rows(conn, "SELECT content, display FROM copilot_messages WHERE content IS NOT NULL OR display IS NOT NULL"):
            _add_json_plates(r["content"], out)
            _add_json_plates(r["display"], out)
    if "copilot_actions" in tables:
        for r in db.rows(conn, "SELECT params, result FROM copilot_actions WHERE params IS NOT NULL OR result IS NOT NULL"):
            _add_json_plates(r["params"], out)
            _add_json_plates(r["result"], out)
    _add_registry_plates(out)
    return out


def _current_user(conn: sqlite3.Connection, user: Optional[User], conv_id: Optional[int] = None) -> Optional[User]:
    uid = user.id if user is not None else (
        db.scalar(conn, "SELECT user_id FROM copilot_conversations WHERE id = ?", (conv_id,)) if conv_id else None)
    if uid is None:
        return user
    r = db.row(conn, "SELECT id, username, display_name, role, site_id FROM users WHERE id = ?", (uid,))
    if r is None:
        return user
    return User(id=r["id"], username=r["username"], display_name=r["display_name"], role=r["role"],
                site_id=r["site_id"], via=user.via if user else None)


def _allowed_compacts(conn: sqlite3.Connection, user: User) -> set[str]:
    if not user.masked:
        return set()
    now = clock.now_iso()
    out = {r["compact"] for r in db.rows(
        conn, "SELECT compact FROM reveals WHERE user_id = ? AND compact IS NOT NULL AND sighting_id IS NULL AND expires_at > ?",
        (user.id, now)) if r["compact"]}
    cids = set(visible_camera_ids(conn, user))
    for r in db.rows(conn, "SELECT s.compact, s.camera_id FROM reveals r JOIN sightings s ON s.id = r.sighting_id "
                           "WHERE r.user_id = ? AND r.sighting_id IS NOT NULL AND r.expires_at > ?", (user.id, now)):
        if r["compact"] and r["camera_id"] in cids:
            out.add(r["compact"])
    return out


def _mask_text(text: str, allowed: set[str], plates: frozenset[str] | set[str] = frozenset()) -> str:
    """Mask every plate in `text` that is not in `allowed`. `plates` are actual plate values whose prefix is also an
    abbreviation (see _abbrev_plates): those are never exempted."""
    if not text:
        return text

    def repl(m: re.Match[str]) -> str:
        prefix, digits, suffix = m.group(1), m.group(2), m.group(3) or ""
        compact = f"{prefix}{digits}{suffix}"
        if prefix in _NON_PLATE_PREFIX and compact not in plates:
            return m.group(0)
        return m.group(0) if compact in allowed else mask_plate(compact)

    return numbers.PLATE.sub(repl, text)


def _mask_value(val: Any, allowed: set[str], role: str = "traffic",
                plates: frozenset[str] | set[str] = frozenset()) -> Any:
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                parsed = json.loads(val)
            except Exception:                                           # noqa: BLE001
                return _mask_text(val, allowed, plates)
            return json.dumps(_mask_value(parsed, allowed, role, plates), ensure_ascii=False, separators=(",", ":"))
        return _mask_text(val, allowed, plates)
    if isinstance(val, list):
        return [_mask_value(x, allowed, role, plates) for x in val]
    if isinstance(val, dict):
        if val.get("type") == "thinking":
            return {**val, "thinking": _mask_text(val.get("thinking") or "", allowed, plates)} if val.get("thinking") else dict(val)
        out = {k: (v if k in ("signature", "prompt_hash") else _mask_value(v, allowed, role, plates))
               for k, v in val.items() if role == "police" or k not in ("registry", "registry_vs_observed")}
        if "plate" in out and isinstance(out["plate"], str) and out["plate"].endswith("****"):
            if "masked" in out:
                out["masked"] = True
            if isinstance(out.get("crop_url"), str):
                out["crop_url"] = out["crop_url"].split("?")[0]
        if "plates_masked" in out:
            out["plates_masked"] = True
        return out
    return val


def sanitize_citation(conn: sqlite3.Connection, user: User, cit: Dict[str, Any]) -> Dict[str, Any]:
    user = _current_user(conn, user) or user
    d = dict(cit)
    kind = d.get("kind")
    cid = d.get("id")
    cids = set(visible_camera_ids(conn, user))
    if kind == "sighting" and cid is not None:
        s = db.row(conn, "SELECT s.id, s.plate_text, s.compact, s.camera_id, s.crop_path, c.kind AS camera_kind "
                         "FROM sightings s JOIN cameras c ON c.id = s.camera_id WHERE s.id = ?", (cid,))
        full = bool(s and s["camera_id"] in cids and can_see_full(conn, user, s["id"], s["compact"]))
        plate_src = s["plate_text"] if s else d.get("plate")
        d["plate"] = plate_src if full else mask_plate(plate_src)
        d["masked"] = not full
        if d.get("crop_url"):
            base = str(d["crop_url"]).split("?")[0]
            d["crop_url"] = f"{base}?v=full" if full else base
        if not s or s["camera_id"] not in cids or not can_play(user, s["camera_kind"]):
            d["video"] = None
        return d
    if kind == "alert" and cid is not None:
        a = db.row(conn, "SELECT a.id, a.plate_text, a.compact, a.sighting_id, a.camera_id, c.kind AS camera_kind "
                         "FROM alerts a LEFT JOIN cameras c ON c.id = a.camera_id WHERE a.id = ?", (cid,))
        vis = bool(a and (a["camera_id"] is None or a["camera_id"] in cids))
        full = bool(vis and a and can_see_full(conn, user, a["sighting_id"], a["compact"]))
        plate_src = a["plate_text"] if a else d.get("plate")
        d["plate"] = plate_src if full else mask_plate(plate_src)
        d["masked"] = not full
        if d.get("crop_url"):
            base = str(d["crop_url"]).split("?")[0]
            d["crop_url"] = f"{base}?v=full" if full else base
        if not vis or (a and not can_play(user, a["camera_kind"])):
            d["video"] = None
        if user.role not in ("police", "guard", "admin"):
            d["link"] = None
        return d
    if kind == "event" and cid is not None:
        e = db.row(conn, "SELECT e.id, e.plate_text, e.compact, e.camera_id FROM lane_events e WHERE e.id = ?", (cid,))
        vis = bool(e and e["camera_id"] in cids)
        full = bool(vis and e and can_see_full(conn, user, None, e["compact"]))
        plate_src = e["plate_text"] if e else d.get("plate")
        d["plate"] = (plate_src if full else mask_plate(plate_src)) if plate_src else None
        d["masked"] = not full
        if user.role not in ("police", "guard", "admin"):
            d["link"] = None
        return d
    if kind in ("access", "pass"):
        if user.role != "guard" or user.masked:
            if d.get("plate"):
                d["plate"] = mask_plate(d["plate"])
            d["masked"] = True
            d["link"] = None
        return d
    if user.masked and d.get("plate"):
        allowed = _allowed_compacts(conn, user)
        compact = re.sub(r"[^A-Z0-9]", "", str(d["plate"]).upper())
        if compact not in allowed:
            d["plate"] = mask_plate(d["plate"])
            d["masked"] = True
            if d.get("crop_url"):
                d["crop_url"] = str(d["crop_url"]).split("?")[0]
    return d


def sanitize_citations(conn: sqlite3.Connection, user: User,
                       cites: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    user = _current_user(conn, user) or user
    return {k: sanitize_citation(conn, user, v) for k, v in (cites or {}).items()}


def sanitize_value(conn: sqlite3.Connection, user: User, val: Any) -> Any:
    user = _current_user(conn, user) or user
    if not user.masked:
        return val
    return _mask_value(val, _allowed_compacts(conn, user), user.role, _abbrev_plates(conn))


def sanitize_display(conn: sqlite3.Connection, user: User, d: Dict[str, Any]) -> Dict[str, Any]:
    user = _current_user(conn, user) or user
    out = dict(d)
    if "citations" in out:
        out["citations"] = sanitize_citations(conn, user, out.get("citations"))
    if "record_data" in out:
        out["record_data"] = sanitize_citations(conn, user, out.get("record_data"))
    if user.masked:
        allowed, plates = _allowed_compacts(conn, user), _abbrev_plates(conn)
        for k, v in list(out.items()):
            if k not in ("citations", "record_data"):
                out[k] = _mask_value(v, allowed, user.role, plates)
    return out


def create_conversation(conn: sqlite3.Connection, user: User, title: str, model: str, prompt_hash: str) -> int:
    now = clock.now_iso()
    return db.insert(conn, "copilot_conversations", {"user_id": user.id, "title": title[:120], "model": model,
                                                     "prompt_hash": prompt_hash, "created_at": now, "updated_at": now})


def conversation(conn: sqlite3.Connection, user: User, conv_id: int) -> Dict[str, Any]:
    user = _current_user(conn, user) or user
    c = db.row(conn, "SELECT * FROM copilot_conversations WHERE id = ?", (conv_id,))
    if c is None or c["user_id"] != user.id:                # only the owner reads a conversation
        raise LookupError("no such conversation")
    if user.masked and c.get("title"):
        c = {**c, "title": _mask_text(c["title"], _allowed_compacts(conn, user), _abbrev_plates(conn))}
    return c


def transcript(conn: sqlite3.Connection, conv_id: int, user: Optional[User] = None) -> List[Dict[str, Any]]:
    u = _current_user(conn, user, conv_id)
    rows = [{"role": r["role"], "content": json.loads(r["content"])}
            for r in db.rows(conn, "SELECT role, content FROM copilot_messages WHERE conversation_id = ? ORDER BY seq",
                             (conv_id,))]
    if u is not None and u.masked:
        allowed, plates = _allowed_compacts(conn, u), _abbrev_plates(conn)
        return [{"role": m["role"], "content": _mask_value(m["content"], allowed, u.role, plates)} for m in rows]
    return rows


def questions_in(conn: sqlite3.Connection, conv_id: int) -> int:
    return db.scalar(conn, "SELECT COUNT(*) FROM copilot_messages WHERE conversation_id = ? AND role = 'user' AND "
                           "display IS NOT NULL", (conv_id,))


def append(conn: sqlite3.Connection, conv_id: int, messages: List[Dict[str, Any]],
           displays: Dict[int, Dict[str, Any]]) -> List[int]:
    """Append API messages (one transaction); `displays` maps an index of `messages` to what the panel shows."""
    ids = []
    now = clock.now_iso()
    with db.transaction(conn):
        seq = db.scalar(conn, "SELECT COALESCE(MAX(seq), 0) FROM copilot_messages WHERE conversation_id = ?", (conv_id,))
        for i, m in enumerate(messages):
            ids.append(db.insert(conn, "copilot_messages", {
                "conversation_id": conv_id, "seq": seq + i + 1, "role": m["role"],
                "content": json.dumps(m["content"], ensure_ascii=False),
                "display": json.dumps(displays[i], ensure_ascii=False) if i in displays else None, "created_at": now}))
        conn.execute("UPDATE copilot_conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
    return ids


def conversations(conn: sqlite3.Connection, user: User, limit: int = 30) -> List[Dict[str, Any]]:
    user = _current_user(conn, user) or user
    rows = db.rows(conn, "SELECT c.id, c.title, c.model, c.created_at, c.updated_at, (SELECT COUNT(*) FROM "
                         "copilot_messages m WHERE m.conversation_id = c.id AND m.role = 'user' AND m.display IS NOT NULL) "
                         "AS questions FROM copilot_conversations c WHERE c.user_id = ? ORDER BY c.updated_at DESC LIMIT ?",
                   (user.id, limit))
    if user.masked:
        allowed, plates = _allowed_compacts(conn, user), _abbrev_plates(conn)
        return [{**r, "title": _mask_text(r["title"], allowed, plates)} for r in rows]
    return rows


def history(conn: sqlite3.Connection, user: User, conv_id: int) -> Dict[str, Any]:
    """A conversation as the panel shows it: questions and answers (display data), oldest first."""
    user = _current_user(conn, user) or user
    c = conversation(conn, user, conv_id)
    items = [{"role": r["role"], **sanitize_display(conn, user, json.loads(r["display"]))} for r in db.rows(
        conn, "SELECT role, display FROM copilot_messages WHERE conversation_id = ? AND display IS NOT NULL ORDER BY seq",
        (conv_id,))]
    return {"id": c["id"], "title": c["title"], "model": c["model"], "created_at": c["created_at"], "items": items}


def delete_conversation(conn: sqlite3.Connection, user: User, conv_id: int) -> None:
    conversation(conn, user, conv_id)
    conn.execute("DELETE FROM copilot_conversations WHERE id = ?", (conv_id,))


# ---------------------------------------------------------------------------- usage, limits
def log_usage(conn: sqlite3.Connection, user: Optional[User], feature: str, model: str, usage: Dict[str, int],
              cost: float, stop_reason: Optional[str] = None, request_id: Optional[str] = None,
              conversation_id: Optional[int] = None, question: Optional[int] = None) -> int:
    """One model call. `question` ties the calls of one chat question together: the first call's row id, negated,
    until the answer is stored (then `link_question` sets the answer's message id)."""
    rid = db.insert(conn, "copilot_usage", {
        "at": clock.now_iso(), "user_id": user.id if user else None, "conversation_id": conversation_id,
        "message_id": question, "feature": feature, "model": model, "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0), "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0), "cost_usd": cost, "stop_reason": stop_reason,
        "request_id": request_id})
    if feature == "chat" and question is None:
        conn.execute("UPDATE copilot_usage SET message_id = ? WHERE id = ?", (-rid, rid))
    return rid


def link_question(conn: sqlite3.Connection, question: int, message_id: int, conversation_id: int) -> None:
    conn.execute("UPDATE copilot_usage SET message_id = ?, conversation_id = ? WHERE message_id = ?",
                 (message_id, conversation_id, question))


def requests_last_hour(conn: sqlite3.Connection, user: User) -> int:
    """AI requests of this user in the last hour: questions (counted once, however many tool rounds) + documents."""
    since = clock.iso(clock.now() - timedelta(hours=1))
    q = db.scalar(conn, "SELECT COUNT(DISTINCT message_id) FROM copilot_usage WHERE user_id = ? AND at >= ? "
                        "AND feature = 'chat'", (user.id, since))
    other = db.scalar(conn, "SELECT COUNT(*) FROM copilot_usage WHERE user_id = ? AND at >= ? AND feature != 'chat'",
                      (user.id, since))
    return int(q or 0) + int(other or 0)


def check_rate(conn: sqlite3.Connection, user: User, per_hour: int) -> None:
    if requests_last_hour(conn, user) >= per_hour:
        raise Forbidden(f"you reached the limit of {per_hour} AI requests per hour; the quick questions (templates) "
                        f"still work")


def usage_summary(conn: sqlite3.Connection, days: int = 30) -> Dict[str, Any]:
    """Cost per feature and per question over the last `days` days (admin)."""
    since = clock.iso(clock.now() - timedelta(days=days))
    feats = db.rows(conn, "SELECT feature, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                          "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
                          "SUM(cache_write_tokens) AS cache_write_tokens, ROUND(SUM(cost_usd), 4) AS cost_usd "
                          "FROM copilot_usage WHERE at >= ? GROUP BY feature, model ORDER BY cost_usd DESC", (since,))
    per_q = [r["c"] for r in db.rows(conn, "SELECT SUM(cost_usd) AS c FROM copilot_usage WHERE at >= ? AND feature = "
                                           "'chat' AND message_id IS NOT NULL GROUP BY message_id", (since,))]
    per_q.sort()
    by_user = db.rows(conn, "SELECT COALESCE(u.username, '-') AS username, COUNT(*) AS calls, ROUND(SUM(x.cost_usd), 4) "
                            "AS cost_usd FROM copilot_usage x LEFT JOIN users u ON u.id = x.user_id WHERE x.at >= ? "
                            "GROUP BY x.user_id ORDER BY cost_usd DESC", (since,))
    return {"days": days, "features": feats, "by_user": by_user, "questions": len(per_q),
            "cost_per_question": {"mean": round(sum(per_q) / len(per_q), 4) if per_q else None,
                                  "median": round(per_q[len(per_q) // 2], 4) if per_q else None,
                                  "max": round(per_q[-1], 4) if per_q else None},
            "total_usd": round(sum(r["cost_usd"] or 0 for r in feats), 4)}


# ---------------------------------------------------------------------------- retention
def purge(conn: sqlite3.Connection, dry_run: bool = True) -> Dict[str, int]:
    """Conversations, finished or expired action cards, daily reviews and VLM proposals older than
    copilot_retention_days (case summaries and explanations go with their alert); usage rows (no personal data)
    after the audit period."""
    days = int(db.get_setting(conn, "copilot_retention_days"))
    cut = clock.iso(clock.now() - timedelta(days=days))
    cut_usage = clock.iso(clock.now() - timedelta(days=int(db.get_setting(conn, "retention_audit_days"))))
    where = {"copilot_conversations": ("updated_at < ?", cut),
             "copilot_actions": ("created_at < ? AND conversation_id IS NULL", cut),
             "copilot_documents": ("created_at < ? AND alert_id IS NULL", cut),
             "copilot_vlm": ("created_at < ?", cut), "copilot_usage": ("at < ?", cut_usage)}
    counts = {t: int(db.scalar(conn, f"SELECT COUNT(*) FROM {t} WHERE {w}", (p,)) or 0) for t, (w, p) in where.items()}
    if not dry_run:
        for t, (w, p) in where.items():
            conn.execute(f"DELETE FROM {t} WHERE {w}", (p,))
    return counts
