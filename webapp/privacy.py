"""Privacy controls in one place: plate masking, "show full plate" (reveal), the audit log and retention.

They follow the data-minimisation principle of Malaysia's PDPA 2010: each role sees only what its task needs,
ordinary sightings are kept only for a set period, and every access is recorded (who, when, what, why). The PDPA
binds private bodies such as a property manager running a guardhouse; it does not apply to the Federal and State
Governments, so nothing here claims that police or JPJ use is bound by it - the same controls are good practice.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import clock, db
from .security import Forbidden, User

GENESIS = "GENESIS"


# ---------------------------------------------------------------------------- masking
def mask_plate(text: Optional[str]) -> str:
    """'VJF 1234' -> 'VJF ****'. The letter prefix (at most 3 letters) stays, the rest is always four stars, so
    neither the digits nor their number are shown. Plates without a letter prefix become '****'."""
    if not text:
        return ""
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    m = re.match(r"[A-Z]{1,3}", compact)
    return f"{m.group(0)} ****" if m else "****"


def can_see_full(conn: sqlite3.Connection, user: User, sighting_id: Optional[int] = None,
                 compact: Optional[str] = None) -> bool:
    """Full plate for this user and record: always for police and guards; for masked roles only while a reveal
    of that record (or that plate, after a search by plate number with a reason) is active."""
    if not user.masked:
        return True
    now = clock.now_iso()
    if sighting_id is not None and db.scalar(
            conn, "SELECT 1 FROM reveals WHERE user_id = ? AND sighting_id = ? AND expires_at > ?",
            (user.id, sighting_id, now)):
        return True
    return compact is not None and bool(db.scalar(
        conn, "SELECT 1 FROM reveals WHERE user_id = ? AND compact = ? AND sighting_id IS NULL AND expires_at > ?",
        (user.id, compact, now)))


def shown_plate(conn: sqlite3.Connection, user: User, plate_text: Optional[str], sighting_id: Optional[int] = None,
                compact: Optional[str] = None) -> str:
    if plate_text is None:
        return ""
    return plate_text if can_see_full(conn, user, sighting_id, compact) else mask_plate(plate_text)


def reveal(conn: sqlite3.Connection, user: User, reason_code: str, note: str = "", sighting_id: Optional[int] = None,
           compact: Optional[str] = None) -> str:
    """Record a "Show full plate" for one sighting (or for one plate number, when a masked role searches by it).
    A reason is required; the reveal is audit-logged and lasts `reveal_minutes`. Returns its expiry time."""
    from .settings import REVEAL_REASONS
    if not user.masked:
        raise Forbidden("this role already sees full plates")
    if reason_code not in REVEAL_REASONS:
        raise ValueError(f"reason must be one of: {', '.join(REVEAL_REASONS)}")
    if reason_code == "Other" and not note.strip():
        raise ValueError("describe the reason when choosing 'Other'")
    minutes = int(db.get_setting(conn, "reveal_minutes"))
    now = clock.now()
    expires = clock.iso(now + timedelta(minutes=minutes))
    with db.transaction(conn):
        db.insert(conn, "reveals", {"user_id": user.id, "sighting_id": sighting_id, "compact": compact,
                                    "reason_code": reason_code, "reason_note": note.strip()[:500],
                                    "at": clock.iso(now), "expires_at": expires})
        audit(conn, user, "reveal", "sighting" if sighting_id else "plate", sighting_id or compact,
              {"minutes": minutes, "note": note.strip()[:500]}, reason=reason_code)
    return expires


# ---------------------------------------------------------------------------- audit log
def _row_hash(prev: str, fields: Dict[str, Any]) -> str:
    payload = json.dumps([prev] + [fields[k] for k in sorted(fields)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit(conn: sqlite3.Connection, user: Optional[User], action: str, target_type: Optional[str] = None,
          target_id: Any = None, details: Optional[Dict[str, Any]] = None, reason: str = "",
          ip: Optional[str] = None) -> int:
    """Append one row (who, when, what, why) to the audit log, chained to the previous row by its hash."""
    d = dict(details or {})
    if user is not None and user.via != "web":
        d["via"] = user.via
    fields = {"at": clock.now_iso(), "user_id": user.id if user else None,
              "username": user.username if user else str(d.get("username", "anonymous")),
              "role": user.role if user else "-", "action": action, "target_type": target_type,
              "target_id": None if target_id is None else str(target_id), "details": db.js(d),
              "reason": reason or "", "ip": ip}
    with db.transaction(conn):
        last = db.scalar(conn, "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        prev = last or db.scalar(conn, "SELECT value FROM meta WHERE key = 'audit_anchor'") or GENESIS
        return db.insert(conn, "audit_log", {**fields, "prev_hash": prev, "row_hash": _row_hash(prev, fields)})


def verify_audit(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Recompute the hash chain. ok=False names the first row that was changed, removed or inserted out of turn."""
    prev = db.scalar(conn, "SELECT value FROM meta WHERE key = 'audit_anchor'") or GENESIS
    n = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        fields = {k: r[k] for k in ("at", "user_id", "username", "role", "action", "target_type", "target_id",
                                    "details", "reason", "ip")}
        if r["prev_hash"] != prev or r["row_hash"] != _row_hash(prev, fields):
            return {"ok": False, "rows_checked": n, "first_bad_id": r["id"]}
        prev, n = r["row_hash"], n + 1
    return {"ok": True, "rows_checked": n, "first_bad_id": None}


# ---------------------------------------------------------------------------- blurred crops
def blurred_copy(src: Path, dst: Path) -> Path:
    """A heavily pixelated + blurred copy of a crop for masked roles (made on the server: a CSS blur would still
    send the sharp image to the browser)."""
    import cv2
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    img = cv2.imread(str(src))
    if img is None:
        raise FileNotFoundError(src)
    h, w = img.shape[:2]
    small = cv2.resize(img, (max(1, w // 12), max(1, h // 12)), interpolation=cv2.INTER_AREA)
    out = cv2.GaussianBlur(cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST), (0, 0), max(2.0, w / 40))
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst), out)
    return dst


# ---------------------------------------------------------------------------- retention
@dataclass
class PurgeReport:
    dry_run: bool
    sightings: int = 0
    alerts: int = 0
    access_events: int = 0
    corrections: int = 0
    audit_rows: int = 0
    reveals: int = 0
    sessions: int = 0
    files: int = 0
    cutoffs: Dict[str, str] = field(default_factory=dict)


def purge(conn: sqlite3.Connection, media_dir: Path, corrections_dir: Path, user: User,
          dry_run: bool = True) -> PurgeReport:
    """Delete what the retention settings no longer allow (or, with dry_run, only count it):
    ordinary sightings `retention_days` after they were recorded; sightings with alerts `retention_alert_days`
    after their last alert was closed (never while one is open); access-log entries, the correction pool and
    the audit log after their own periods; expired reveals and sessions. Files (crops, frames) go with their rows."""
    now = clock.now()
    cut = {k: clock.iso(now - timedelta(days=int(db.get_setting(conn, k)))) for k in
           ("retention_days", "retention_alert_days", "retention_access_days", "retention_corrections_days",
            "retention_audit_days")}
    rep = PurgeReport(dry_run=dry_run, cutoffs=cut)
    ordinary = db.rows(conn, "SELECT id, crop_path, scene_path FROM sightings s WHERE recorded_at < ? AND NOT EXISTS "
                             "(SELECT 1 FROM alerts a WHERE a.sighting_id = s.id)", (cut["retention_days"],))
    closed = db.rows(conn, "SELECT s.id, s.crop_path, s.scene_path FROM sightings s WHERE EXISTS (SELECT 1 FROM alerts a "
                           "WHERE a.sighting_id = s.id) AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.sighting_id = "
                           "s.id AND (a.closed_at IS NULL OR a.closed_at >= ?))", (cut["retention_alert_days"],))
    victims = ordinary + closed
    rep.sightings = len(victims)
    ids = [v["id"] for v in victims]
    rep.alerts = sum(db.scalar(conn, "SELECT COUNT(*) FROM alerts WHERE sighting_id = ?", (i,)) for i in ids)
    rep.access_events = db.scalar(conn, "SELECT COUNT(*) FROM access_events WHERE at < ?", (cut["retention_access_days"],))
    old_corr = db.rows(conn, "SELECT id, crop_path FROM corrections WHERE created_at < ?",
                       (cut["retention_corrections_days"],))
    rep.corrections = len(old_corr)
    rep.audit_rows = db.scalar(conn, "SELECT COUNT(*) FROM audit_log WHERE at < ?", (cut["retention_audit_days"],))
    rep.reveals = db.scalar(conn, "SELECT COUNT(*) FROM reveals WHERE expires_at < ?", (clock.iso(now),))
    rep.sessions = db.scalar(conn, "SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (clock.iso(now),))
    files = [media_dir / p for v in victims for p in (v["crop_path"], v["scene_path"]) if p]
    files += [corrections_dir / c["crop_path"] for c in old_corr if c["crop_path"]]
    rep.files = sum(1 for f in files if f.exists())
    if dry_run:
        return rep
    with db.transaction(conn):
        conn.executemany("DELETE FROM sightings WHERE id = ?", [(i,) for i in ids])   # alerts cascade
        conn.execute("DELETE FROM access_events WHERE at < ?", (cut["retention_access_days"],))
        conn.execute("DELETE FROM corrections WHERE created_at < ?", (cut["retention_corrections_days"],))
        conn.execute("DELETE FROM reveals WHERE expires_at < ?", (clock.iso(now),))
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (clock.iso(now),))
        conn.execute("DELETE FROM plates WHERE compact NOT IN (SELECT compact FROM sightings)")
        if rep.audit_rows:
            anchor = db.scalar(conn, "SELECT row_hash FROM audit_log WHERE at < ? ORDER BY id DESC LIMIT 1",
                               (cut["retention_audit_days"],))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('audit_purge', 'on')")
            conn.execute("DELETE FROM audit_log WHERE at < ?", (cut["retention_audit_days"],))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('audit_purge', 'off')")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('audit_anchor', ?)", (anchor,))
        audit(conn, user, "purge", details={k: v for k, v in rep.__dict__.items() if k not in ("cutoffs", "dry_run")})
    for f in files:
        f.unlink(missing_ok=True)
        for blurred in f.parent.glob(f.stem + ".masked*"):
            blurred.unlink(missing_ok=True)
    return rep


def purge_summary(rep: PurgeReport) -> List[str]:
    verb = "would delete" if rep.dry_run else "deleted"
    return [f"{verb} {rep.sightings} sightings ({rep.alerts} closed alerts with them), {rep.access_events} access-log "
            f"entries, {rep.corrections} correction-pool items, {rep.audit_rows} audit rows, {rep.files} files"]
