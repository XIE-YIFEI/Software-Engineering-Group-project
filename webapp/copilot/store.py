"""Storage of the copilot: conversations and their API transcript, the cost of every model call, retention.

The transcript is append-only and replayed verbatim (content blocks as the API returned them, thinking blocks
included), which keeps the prompt cache warm and the model's earlier reasoning valid. A question whose loop did not
end cleanly (an error, a refusal, a truncated tool call) is not stored, so the transcript always stays a valid request.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .. import clock, db
from ..security import Forbidden, User


def create_conversation(conn: sqlite3.Connection, user: User, title: str, model: str, prompt_hash: str) -> int:
    now = clock.now_iso()
    return db.insert(conn, "copilot_conversations", {"user_id": user.id, "title": title[:120], "model": model,
                                                     "prompt_hash": prompt_hash, "created_at": now, "updated_at": now})


def conversation(conn: sqlite3.Connection, user: User, conv_id: int) -> Dict[str, Any]:
    c = db.row(conn, "SELECT * FROM copilot_conversations WHERE id = ?", (conv_id,))
    if c is None or c["user_id"] != user.id:                # only the owner reads a conversation
        raise LookupError("no such conversation")
    return c


def transcript(conn: sqlite3.Connection, conv_id: int) -> List[Dict[str, Any]]:
    return [{"role": r["role"], "content": json.loads(r["content"])}
            for r in db.rows(conn, "SELECT role, content FROM copilot_messages WHERE conversation_id = ? ORDER BY seq",
                             (conv_id,))]


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
    return db.rows(conn, "SELECT c.id, c.title, c.model, c.created_at, c.updated_at, (SELECT COUNT(*) FROM "
                         "copilot_messages m WHERE m.conversation_id = c.id AND m.role = 'user' AND m.display IS NOT NULL) "
                         "AS questions FROM copilot_conversations c WHERE c.user_id = ? ORDER BY c.updated_at DESC LIMIT ?",
                   (user.id, limit))


def history(conn: sqlite3.Connection, user: User, conv_id: int) -> Dict[str, Any]:
    """A conversation as the panel shows it: questions and answers (display data), oldest first."""
    c = conversation(conn, user, conv_id)
    items = [{"role": r["role"], **json.loads(r["display"])} for r in db.rows(
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
