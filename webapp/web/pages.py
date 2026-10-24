"""Page routes: login / logout, the role's home, and one shell page per feature."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..deps import client_ip, get_conn, optional_user
from ..security import COOKIE, AuthError, User, check_csrf, login, logout, session_user
from ..settings import PURPOSES, REVEAL_REASONS

HERE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter(include_in_schema=False)

# (key, label, url) per role: the left navigation and, with it, which pages a role may open
NAV = {
    "police": [("overview", "Overview", "/"), ("plates", "Plate search", "/plates"), ("alerts", "Alert centre", "/alerts"),
               ("lists", "Watch list & blacklist", "/lists"), ("traffic", "Traffic analytics", "/traffic"),
               ("forecast", "Congestion forecast", "/forecast"), ("upload", "Upload & analyse", "/upload"),
               ("reports", "Reports", "/reports")],
    "guard": [("gate", "Guardhouse", "/gate"), ("access", "Access log", "/gate/log"),
              ("visitors", "Residents & visitors", "/gate/visitors"), ("alerts", "Gate alerts", "/alerts"),
              ("reports", "Reports", "/reports")],
    "traffic": [("overview", "Overview", "/"), ("traffic", "Traffic analytics", "/traffic"),
                ("forecast", "Congestion forecast", "/forecast"), ("plates", "Plate search", "/plates"),
                ("upload", "Upload & analyse", "/upload"), ("reports", "Reports", "/reports")],
    "admin": [("overview", "Overview", "/"), ("plates", "Plate search", "/plates"), ("alerts", "Alert centre", "/alerts"),
              ("traffic", "Traffic analytics", "/traffic"), ("forecast", "Congestion forecast", "/forecast"),
              ("upload", "Upload & analyse", "/upload"), ("reports", "Reports", "/reports"),
              ("users", "Users", "/admin/users"), ("data", "Data", "/admin/data"),
              ("privacy", "Privacy & audit", "/admin/privacy"), ("settings", "Settings", "/admin/settings")],
}
HOME = {"police": "/", "guard": "/gate", "traffic": "/", "admin": "/"}
ROLE_LABEL = {"police": "Police / JPJ", "guard": "Guard", "traffic": "Traffic manager", "admin": "Administrator"}
EXTRA = {"timeline": ("police",)}                     # pages reached from other pages, not in the navigation


def _page(request: Request, conn: sqlite3.Connection, key: str, title: str, template: str, **ctx) -> HTMLResponse:
    found = session_user(conn, request.cookies.get(COOKIE))
    if found is None:
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    user, csrf = found
    allowed = {k for k, _, _ in NAV[user.role]} | {k for k, roles in EXTRA.items() if user.role in roles}
    if key not in allowed:
        return templates.TemplateResponse(request, "error.html", {"user": user, "nav": NAV[user.role], "active": "",
                                                                  "csrf": csrf, "role_label": ROLE_LABEL[user.role],
                                                                  "purposes": PURPOSES, "reveal_reasons": REVEAL_REASONS,
                                                                  "title": "Not available", "status": 403,
                                                                  "message": f"The {ROLE_LABEL[user.role]} role cannot "
                                                                             f"open this page."}, status_code=403)
    return templates.TemplateResponse(request, template, {
        "user": user, "nav": NAV[user.role], "active": key, "csrf": csrf, "title": title,
        "role_label": ROLE_LABEL[user.role], "purposes": PURPOSES, "reveal_reasons": REVEAL_REASONS, **ctx})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": next if next.startswith("/") else "/",
                                                              "error": error})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
                 conn: sqlite3.Connection = Depends(get_conn)):
    try:
        user, token, _ = login(conn, username, password, client_ip(request))
    except AuthError as e:
        return RedirectResponse(f"/login?error={quote(str(e))}&next={quote(next)}", status_code=303)
    dest = next if next.startswith("/") and next != "/" and not next.startswith("//") else HOME[user.role]
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                    max_age=24 * 3600, path="/")
    return resp


@router.post("/logout")
def logout_submit(request: Request, csrf_token: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    found = session_user(conn, request.cookies.get(COOKIE))
    if found and check_csrf(found[1], csrf_token):
        logout(conn, request.cookies.get(COOKIE), found[0])
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: Optional[User] = Depends(optional_user), conn: sqlite3.Connection = Depends(get_conn)):
    if user is not None and user.role == "guard":
        return RedirectResponse("/gate", status_code=303)
    return _page(request, conn, "overview", "Overview", "overview.html")


def simple(path: str, key: str, title: str, template: str):
    def view(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return _page(request, conn, key, title, template)
    router.add_api_route(path, view, methods=["GET"], response_class=HTMLResponse, name=key)


for _path, _key, _title, _tpl in (
        ("/plates", "plates", "Plate search", "plates.html"),
        ("/alerts", "alerts", "Alert centre", "alerts.html"),
        ("/lists", "lists", "Watch list & blacklist", "lists.html"),
        ("/traffic", "traffic", "Traffic analytics", "traffic.html"),
        ("/forecast", "forecast", "Congestion forecast", "forecast.html"),
        ("/upload", "upload", "Upload & analyse", "upload.html"),
        ("/reports", "reports", "Reports", "reports.html"),
        ("/gate", "gate", "Guardhouse", "gate.html"),
        ("/gate/log", "access", "Access log", "access_log.html"),
        ("/gate/visitors", "visitors", "Residents & visitors", "visitors.html"),
        ("/admin/users", "users", "Users", "admin_users.html"),
        ("/admin/data", "data", "Data", "admin_data.html"),
        ("/admin/privacy", "privacy", "Privacy & audit", "admin_privacy.html"),
        ("/admin/settings", "settings", "Settings", "admin_settings.html")):
    simple(_path, _key, _title, _tpl)


@router.get("/timeline/{plate}", response_class=HTMLResponse)
def timeline_page(plate: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, "timeline", f"Timeline · {plate}", "timeline.html", plate=plate)
