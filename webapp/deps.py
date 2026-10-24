"""FastAPI dependencies: a database connection per request, the logged-in user, CSRF protection."""
from __future__ import annotations

import sqlite3
from typing import Iterator, Optional

from fastapi import Depends, Request

from . import db
from .security import COOKIE, AuthError, User, check_csrf, session_user
from .settings import active

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect(active().db_path)
    try:
        yield conn
    finally:
        conn.close()


def _session(request: Request, conn: sqlite3.Connection):
    found = session_user(conn, request.cookies.get(COOKIE))
    if found is None:
        return None
    user, csrf = found
    request.state.csrf = csrf
    return user, csrf


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> User:
    """The logged-in user (401 otherwise). State-changing requests must carry the session's CSRF token in the
    X-CSRF-Token header (the page's sign-out form posts to its own route, web/pages.py)."""
    found = _session(request, conn)
    if found is None:
        raise AuthError("please log in")
    user, csrf = found
    if request.method in UNSAFE:
        if not check_csrf(csrf, request.headers.get("x-csrf-token")):
            raise PermissionError("missing or wrong CSRF token")
    return user


def optional_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Optional[User]:
    found = _session(request, conn)
    return found[0] if found else None


def client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None
