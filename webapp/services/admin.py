"""Administration: accounts, settings, the audit log (and its hash-chain check), retention purge, the correction
pool and the status of the imported data."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional

from .. import clock, db
from ..models import AuditRow, UserOut
from ..privacy import PurgeReport, audit, purge, verify_audit
from ..security import ROLES, User, create_user, hash_password, require
from ..settings import DEFAULTS, active

EDITABLE = tuple(DEFAULTS)


def _user_out(r: dict) -> UserOut:
    return UserOut(id=r["id"], username=r["username"], display_name=r["display_name"], role=r["role"],
                   site_id=r["site_id"], masked=r["role"] in ("traffic", "admin"))


def users(conn: sqlite3.Connection, user: User) -> List[dict]:
    require(user, "admin")
    return [{**_user_out(r).model_dump(), "active": bool(r["active"]), "demo": bool(r["demo"]),
             "last_login_at": r["last_login_at"], "site": r["site"]}
            for r in db.rows(conn, "SELECT u.*, s.name AS site FROM users u LEFT JOIN sites s ON s.id = u.site_id "
                                   "ORDER BY u.role, u.username")]


def add_user(conn: sqlite3.Connection, user: User, username: str, display_name: str, role: str, password: str,
             site_code: Optional[str] = None) -> UserOut:
    require(user, "admin")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    site = db.scalar(conn, "SELECT id FROM sites WHERE code = ?", (site_code,)) if site_code else None
    if role == "guard" and site is None:
        raise ValueError("a guard needs a site")
    if db.scalar(conn, "SELECT 1 FROM users WHERE username = ?", (username,)):
        raise ValueError("this username exists")
    uid = create_user(conn, username.strip(), display_name.strip() or username, role, password, site)
    audit(conn, user, "user_add", "user", uid, {"username": username, "role": role})
    return _user_out(db.row(conn, "SELECT * FROM users WHERE id = ?", (uid,)))


def update_user(conn: sqlite3.Connection, user: User, user_id: int, active_flag: Optional[bool] = None,
                password: Optional[str] = None, role: Optional[str] = None) -> UserOut:
    require(user, "admin")
    r = db.row(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
    if r is None:
        raise LookupError("no such user")
    changes: Dict[str, object] = {}
    if active_flag is not None:
        if user_id == user.id and not active_flag:
            raise ValueError("you cannot deactivate your own account")
        changes["active"] = int(active_flag)
    if role:
        if role not in ROLES:
            raise ValueError("unknown role")
        changes["role"] = role
    if password:
        if len(password) < 8:
            raise ValueError("password must have at least 8 characters")
        changes["password_hash"] = hash_password(password)
        changes["failed_logins"], changes["locked_until"] = 0, None
    if changes:
        conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                     [*changes.values(), user_id])
        if "active" in changes or "password_hash" in changes or "role" in changes:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))     # sign them out everywhere
        audit(conn, user, "user_update", "user", user_id, {k: ("***" if k == "password_hash" else v)
                                                            for k, v in changes.items() if k != "locked_until"})
    return _user_out(db.row(conn, "SELECT * FROM users WHERE id = ?", (user_id,)))


def get_settings(conn: sqlite3.Connection, user: User) -> Dict[str, object]:
    require(user, "admin")
    return {k: db.get_setting(conn, k) for k in EDITABLE}


def put_settings(conn: sqlite3.Connection, user: User, values: Dict[str, object]) -> Dict[str, object]:
    require(user, "admin")
    unknown = set(values) - set(EDITABLE)
    if unknown:
        raise ValueError(f"unknown settings {sorted(unknown)}")
    for k, v in values.items():
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number")
        if num <= 0 or num > 3650:
            raise ValueError(f"{k} must be between 0 and 3650")
        db.set_setting(conn, k, int(num) if num.is_integer() else num)
    audit(conn, user, "settings", "settings", None, {k: values[k] for k in values})
    return get_settings(conn, user)


def audit_log(conn: sqlite3.Connection, user: User, username: str = "", action: str = "", start: str = "",
              end: str = "", limit: int = 300) -> List[AuditRow]:
    require(user, "admin")
    where, params = ["1 = 1"], []
    for col, val in (("username", username), ("action", action)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if start:
        where.append("at >= ?")
        params.append(start)
    if end:
        where.append("at <= ?")
        params.append(end if len(end) > 10 else end + "T23:59:59.999")
    rs = db.rows(conn, f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params + [limit])
    return [AuditRow(id=r["id"], at=r["at"], username=r["username"], role=r["role"], action=r["action"],
                     target_type=r["target_type"], target_id=r["target_id"], details=json.loads(r["details"] or "{}"),
                     reason=r["reason"], ip=r["ip"]) for r in rs]


def verify(conn: sqlite3.Connection, user: User) -> dict:
    require(user, "admin")
    return verify_audit(conn)


def run_purge(conn: sqlite3.Connection, user: User, dry_run: bool = True) -> PurgeReport:
    require(user, "admin")
    st = active()
    return purge(conn, st.media_dir, st.corrections_dir, user, dry_run)


def corrections(conn: sqlite3.Connection, user: User) -> List[dict]:
    require(user, "admin")
    return db.rows(conn, "SELECT c.id, c.ocr_compact, c.corrected_compact, c.created_at, c.crop_path IS NOT NULL AS has_crop, "
                         "u.username AS created_by FROM corrections c LEFT JOIN users u ON u.id = c.created_by "
                         "ORDER BY c.created_at DESC")


def data_status(conn: sqlite3.Connection, user: User) -> dict:
    require(user, "admin")
    runs = db.rows(conn, "SELECT r.id, r.code, r.label, r.origin, r.results_path, r.results_sha256, r.imported_at, "
                         "c.name AS camera, (SELECT COUNT(*) FROM sightings s WHERE s.run_id = r.id) AS sightings, "
                         "(SELECT COUNT(*) FROM sightings s WHERE s.run_id = r.id AND s.crop_path IS NOT NULL) AS crops, "
                         "(SELECT COUNT(*) FROM alerts a WHERE a.run_id = r.id) AS alerts FROM runs r JOIN cameras c "
                         "ON c.id = r.camera_id ORDER BY r.id")
    days = db.rows(conn, "SELECT d.code, d.label, d.csv_path, d.csv_sha256, d.calibrated, d.imported_at, (SELECT COUNT(*) "
                         "FROM forecast_runs f WHERE f.day_id = d.id) AS forecast_sections FROM series_days d ORDER BY d.id")
    fruns = db.rows(conn, "SELECT d.code AS day, f.entity, f.model, f.train_day, f.origin, f.source_path FROM forecast_runs "
                          "f JOIN series_days d ON d.id = f.day_id ORDER BY d.code, f.entity")
    st = active()
    return {"runs": runs, "series_days": days, "forecast_runs": fruns, "registry": str(st.registry_path),
            "registry_simulated": True, "data_dir": str(st.data_dir),
            "last_import": db.scalar(conn, "SELECT value FROM meta WHERE key = 'demo_imported_at'"),
            "now": clock.now_iso()}
