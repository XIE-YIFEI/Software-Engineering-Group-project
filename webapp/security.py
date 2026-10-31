"""Accounts and sessions: scrypt password hashes, session cookies, CSRF tokens, login lockout, role checks.

The cookie holds a random token; the database stores only its SHA-256, so a copy of the database cannot be used to
log in. Every state-changing request must send the session's CSRF token in the X-CSRF-Token header (the pages'
JavaScript does this), so another site cannot make the browser act for the user.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from . import clock, db

COOKIE = "secs_session"
ROLES = ("police", "guard", "traffic", "admin")
MAX_FAILED = 5
LOCK_MINUTES = 5
_N, _R, _P = 2 ** 14, 8, 1                       # scrypt cost (about 50 ms per check, 16 MB)


class AuthError(Exception):
    """Not logged in, wrong password, or locked (HTTP 401)."""


class Forbidden(Exception):
    """Logged in, but this role may not do that (HTTP 403)."""


@dataclass(frozen=True)
class User:
    """The logged-in person, passed to every service function (permissions, masking and audit follow it)."""
    id: int
    username: str
    display_name: str
    role: str
    site_id: Optional[int] = None
    via: str = "web"                              # "assistant" when an AI tool call acts for the user

    @property
    def masked(self) -> bool:
        """Traffic managers and admins see masked plates and blurred crops unless they reveal one record."""
        return self.role in ("traffic", "admin")


SYSTEM = User(0, "system", "System (importer / worker)", "admin")


def require(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise Forbidden(f"the {user.role} role cannot do this")


# ---------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _user(r: dict) -> User:
    return User(r["id"], r["username"], r["display_name"], r["role"], r["site_id"])


# ---------------------------------------------------------------------------- login / sessions
def create_user(conn: sqlite3.Connection, username: str, display_name: str, role: str, password: str,
                site_id: Optional[int] = None, demo: bool = False) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if len(password) < 8:
        raise ValueError("password must have at least 8 characters")
    return db.insert(conn, "users", {"username": username, "display_name": display_name, "role": role,
                                     "site_id": site_id, "password_hash": hash_password(password), "active": 1,
                                     "demo": int(demo), "created_at": clock.now_iso()})


def login(conn: sqlite3.Connection, username: str, password: str, ip: Optional[str] = None):
    """Check the password; on success create a session. Returns (user, cookie_token, csrf_token).
    Five wrong passwords in a row lock the account for 5 minutes. Every attempt is audit-logged."""
    from .privacy import audit
    now = clock.now()
    r = db.row(conn, "SELECT * FROM users WHERE username = ?", (username.strip(),))
    if r is None or not r["active"]:
        audit(conn, None, "login_failed", details={"username": username[:64]}, ip=ip)
        raise AuthError("wrong username or password")
    if r["locked_until"] and r["locked_until"] > clock.iso(now):
        audit(conn, _user(r), "login_locked", ip=ip)
        raise AuthError("too many failed attempts; try again in a few minutes")
    if not verify_password(password, r["password_hash"]):
        failed = r["failed_logins"] + 1
        locked = clock.iso(now + timedelta(minutes=LOCK_MINUTES)) if failed >= MAX_FAILED else None
        conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                     (0 if locked else failed, locked, r["id"]))
        audit(conn, _user(r), "login_failed", details={"locked": bool(locked)}, ip=ip)
        raise AuthError("wrong username or password")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    hours = int(db.get_setting(conn, "session_hours"))
    with db.transaction(conn):
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                     (clock.iso(now), r["id"]))
        db.insert(conn, "sessions", {"token_hash": _token_hash(token), "user_id": r["id"], "csrf_token": csrf,
                                     "created_at": clock.iso(now), "last_seen_at": clock.iso(now),
                                     "expires_at": clock.iso(now + timedelta(hours=hours)), "ip": ip})
        user = _user(r)
        audit(conn, user, "login", ip=ip)
    return user, token, csrf


def session_user(conn: sqlite3.Connection, token: Optional[str]):
    """(user, csrf_token) of a valid session cookie, else None."""
    if not token:
        return None
    r = db.row(conn, "SELECT s.id AS sid, s.csrf_token, s.expires_at, u.* FROM sessions s JOIN users u "
                     "ON u.id = s.user_id WHERE s.token_hash = ?", (_token_hash(token),))
    if r is None or not r["active"] or r["expires_at"] < clock.now_iso():
        return None
    return _user(r), r["csrf_token"]


def logout(conn: sqlite3.Connection, token: Optional[str], user: Optional[User] = None) -> None:
    from .privacy import audit
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    if user is not None:
        audit(conn, user, "logout")


def check_csrf(expected: str, sent: Optional[str]) -> bool:
    return bool(sent) and hmac.compare_digest(expected, sent)
