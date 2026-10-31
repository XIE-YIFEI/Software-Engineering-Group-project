"""API: upload & analyse (jobs), reports, administration (users, settings, audit log, retention, data)."""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..deps import current_user, get_conn
from ..models import AuditRow, JobOut, UserOut
from ..security import User
from ..services import admin as admin_svc
from ..services import jobs as job_svc
from ..services import reports as report_svc

router = APIRouter()


@router.get("/jobs/scenes", tags=["jobs"])
def api_scenes(user: User = Depends(current_user)):
    return job_svc.scenes()


@router.post("/jobs", response_model=JobOut, tags=["jobs"])
def api_create_job(file: UploadFile = File(...), camera_id: int = Form(...), scene: str = Form("roadside"),
                   render_video: bool = Form(False), recorded_at: Optional[str] = Form(None),
                   user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Upload an image or video; the background worker analyses it with the existing pipeline."""
    return job_svc.create(conn, user, file.file, file.filename or "upload", camera_id, scene, render_video, recorded_at)


@router.get("/jobs", response_model=List[JobOut], tags=["jobs"])
def api_jobs(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return job_svc.list_jobs(conn, user)


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def api_job(job_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return job_svc.get(conn, user, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"])
def api_cancel_job(job_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return job_svc.cancel(conn, user, job_id)


@router.get("/reports/{span}", tags=["reports"])
def api_report(span: str, date: str, kind: str = "traffic", format: str = Query("json", pattern="^(json|csv|pdf)$"),
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Daily or weekly report (`span` = daily | weekly) as JSON, CSV or PDF."""
    if span not in ("daily", "weekly"):
        raise LookupError("no such report")
    rep = report_svc.build(conn, user, kind, date, span == "weekly")
    name = f"{span}_{kind}_{date}"
    if format == "csv":
        return PlainTextResponse(report_svc.to_csv(rep), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    if format == "pdf":
        return Response(report_svc.to_pdf(rep), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    return rep


@router.get("/users", tags=["admin"])
def api_users(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.users(conn, user)


class UserIn(BaseModel):
    username: str
    display_name: str = ""
    role: str
    password: str
    site_code: Optional[str] = None


@router.post("/users", response_model=UserOut, tags=["admin"])
def api_add_user(body: UserIn, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.add_user(conn, user, body.username, body.display_name, body.role, body.password, body.site_code)


class UserPatch(BaseModel):
    active: Optional[bool] = None
    password: Optional[str] = None
    role: Optional[str] = None


@router.patch("/users/{user_id}", response_model=UserOut, tags=["admin"])
def api_update_user(user_id: int, body: UserPatch, user: User = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.update_user(conn, user, user_id, body.active, body.password, body.role)


@router.get("/settings", tags=["admin"])
def api_get_settings(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.get_settings(conn, user)


@router.put("/settings", tags=["admin"])
def api_put_settings(values: Dict[str, float], user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.put_settings(conn, user, values)


@router.get("/audit", response_model=List[AuditRow], tags=["admin"])
def api_audit(username: str = "", action: str = "", start: str = "", end: str = "", limit: int = Query(300, le=5000),
              user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.audit_log(conn, user, username, action, start, end, limit)


@router.get("/audit/verify", tags=["admin"])
def api_audit_verify(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.verify(conn, user)


@router.post("/retention/purge", tags=["admin"])
def api_purge(dry_run: bool = True, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    rep = admin_svc.run_purge(conn, user, dry_run)
    return rep.__dict__


@router.get("/corrections", tags=["admin"])
def api_corrections(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.corrections(conn, user)


@router.get("/data", tags=["admin"])
def api_data(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return admin_svc.data_status(conn, user)
