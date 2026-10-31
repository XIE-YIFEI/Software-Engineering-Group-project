"""Every alert is created here - the one hook point for later push notifications.

Re-creating an alert with the same natural key (a re-import) updates its data but keeps its workflow state
(status, handler history), so acknowledged / resolved alerts stay that way.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import clock, db
from .services.common import RANK


def create_alert(conn: sqlite3.Connection, *, natural_key: str, source: str, code: str, severity: str, message: str,
                 seen_at: str, provenance: str, run_id: Optional[int] = None, camera_id: Optional[int] = None,
                 sighting_id: Optional[int] = None, lane_event_id: Optional[int] = None,
                 compact: Optional[str] = None, plate_text: Optional[str] = None) -> int:
    now = clock.now_iso()
    existed = db.scalar(conn, "SELECT id FROM alerts WHERE natural_key = ?", (natural_key,))
    aid = db.upsert(conn, "alerts", {
        "natural_key": natural_key, "run_id": run_id, "camera_id": camera_id, "sighting_id": sighting_id,
        "lane_event_id": lane_event_id, "source": source, "code": code, "severity": severity,
        "rank": RANK[severity], "message": message, "compact": compact, "plate_text": plate_text, "seen_at": seen_at,
        "provenance": provenance, "status": "new", "created_at": now, "updated_at": now, "closed_at": None},
        key=("natural_key",), keep=("status", "created_at", "updated_at", "closed_at"))
    if not existed:
        db.insert(conn, "alert_actions", {"alert_id": aid, "user_id": None, "action": "created", "from_status": None,
                                          "to_status": "new", "note": f"raised by {source} check", "at": now})
    return aid
