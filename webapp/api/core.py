"""API: login / logout, overview, cameras and runs, media files (videos with range requests, crops, scene frames)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db
from ..deps import client_ip, current_user, get_conn
from ..models import CameraOut, Overview, RunOut, UserOut
from ..privacy import audit, blurred_copy, can_see_full
from ..security import COOKIE, Forbidden, User, login, logout
from ..services import overview as overview_svc
from ..services import traffic as traffic_svc
from ..services.common import can_play, visible_camera_ids
from ..settings import ROOT, active

router = APIRouter()
media = APIRouter()


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    user: UserOut
    csrf_token: str


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, display_name=u.display_name, role=u.role, site_id=u.site_id,
                   masked=u.masked)


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                        max_age=int(24 * 3600), path="/")


@router.post("/auth/login", response_model=LoginOut, tags=["auth"])
def api_login(body: LoginIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    user, token, csrf = login(conn, body.username, body.password, client_ip(request))
    set_session_cookie(response, token, request)
    return LoginOut(user=user_out(user), csrf_token=csrf)


@router.post("/auth/logout", tags=["auth"])
def api_logout(request: Request, response: Response, user: User = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
    logout(conn, request.cookies.get(COOKIE), user)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me", response_model=LoginOut, tags=["auth"])
def api_me(request: Request, user: User = Depends(current_user)):
    return LoginOut(user=user_out(user), csrf_token=request.state.csrf)


@router.get("/overview", response_model=Overview, tags=["overview"])
def api_overview(date: Optional[str] = None, user: User = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    return overview_svc.summary(conn, user, date)


@router.get("/cameras", response_model=List[CameraOut], tags=["overview"])
def api_cameras(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return overview_svc.cameras(conn, user)


@router.get("/runs", response_model=List[RunOut], tags=["overview"])
def api_runs(camera_id: Optional[int] = None, user: User = Depends(current_user),
             conn: sqlite3.Connection = Depends(get_conn)):
    cams = visible_camera_ids(conn, user)
    rs = db.rows(conn, "SELECT r.*, c.name AS camera_name, c.kind AS camera_kind FROM runs r JOIN cameras c "
                       "ON c.id = r.camera_id ORDER BY r.id")
    return [traffic_svc.run_out(r, user) for r in rs if r["camera_id"] in cams and (camera_id is None or r["camera_id"] == camera_id)]


# ---------------------------------------------------------------------------- media
def _file(path: Optional[str]) -> Path:
    if not path:
        raise LookupError("no file")
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    roots = [ROOT / "demo", active().data_dir]
    if not p.exists() or not any(_inside(p, r) for r in roots):
        raise LookupError("file not found")
    return p


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _run_for(conn, user: User, run_id: int) -> dict:
    r = db.row(conn, "SELECT r.*, c.kind AS camera_kind FROM runs r JOIN cameras c ON c.id = r.camera_id "
                     "WHERE r.id = ?", (run_id,))
    if r is None or r["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such run")
    return r


@media.get("/runs/{run_id}/video", tags=["media"])
def media_run_video(run_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """The run's video (HTTP range requests work, so the player can seek). Not for masked roles on plate cameras:
    their videos show the full plates."""
    run = _run_for(conn, user, run_id)
    if not can_play(user, run["camera_kind"]):
        raise Forbidden("this video shows full plates; your role sees masked plates")
    return FileResponse(_file(run["video_path"]), media_type="video/mp4")


@media.get("/runs/{run_id}/image", tags=["media"])
def media_run_image(run_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    if user.masked:
        raise Forbidden("the annotated image shows full plates")
    return FileResponse(_file(_run_for(conn, user, run_id)["image_path"]))


@media.get("/series/{day_id}/video", tags=["media"])
def media_series_video(day_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    d = db.row(conn, "SELECT * FROM series_days WHERE id = ?", (day_id,))
    if d is None or d["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such series")
    return FileResponse(_file(d["video_path"]), media_type="video/mp4")


def _sighting(conn, user: User, sid: int) -> dict:
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (sid,))
    if s is None or s["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such sighting")
    return s


@media.get("/sightings/{sid}/crop", tags=["media"])
def media_crop(sid: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """The best plate crop: sharp for police and guards (and revealed records), blurred on the server otherwise."""
    s = _sighting(conn, user, sid)
    src = _file(str(active().media_dir / s["crop_path"]) if s["crop_path"] else None)
    if not can_see_full(conn, user, sid, s["compact"]):
        src = blurred_copy(src, src.with_name(src.stem + ".masked.jpg"))
    return FileResponse(src, headers={"Cache-Control": "private, no-store"})


@media.get("/sightings/{sid}/scene", tags=["media"])
def media_scene(sid: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """The full camera frame of the best crop (police, guards, revealed records only). Audit-logged."""
    s = _sighting(conn, user, sid)
    if not can_see_full(conn, user, sid, s["compact"]):
        raise Forbidden("the scene frame shows the full plate: reveal the record first")
    path = _file(str(active().media_dir / s["scene_path"]) if s["scene_path"] else None)
    audit(conn, user, "view_scene", "sighting", sid)
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})
