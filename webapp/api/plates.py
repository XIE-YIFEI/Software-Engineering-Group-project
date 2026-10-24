"""API: plate search, records, reveal, cross-camera timeline, alerts and evidence packs."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..deps import current_user, get_conn
from ..models import AlertCounts, AlertDetail, AlertOut, Page, RevealOut, SightingDetail, SightingOut, SightingQuery, Timeline
from ..security import User
from ..services import alerts as alert_svc
from ..services import evidence as evidence_svc
from ..services import plates as plate_svc

router = APIRouter()


def _query(plate: str = "", tolerance: float = Query(1.0, ge=0, le=2), start: Optional[str] = None,
           end: Optional[str] = None, camera_id: Optional[int] = None, run_id: Optional[int] = None,
           vtype: Optional[str] = None, status: Optional[str] = None, provenance: Optional[str] = None,
           has_alerts: Optional[bool] = None, purpose: str = "", reference: str = "",
           reveal_reason: Optional[str] = None, page: int = Query(1, ge=1),
           page_size: int = Query(50, ge=1, le=500)) -> SightingQuery:
    return SightingQuery(plate=plate, tolerance=tolerance, start=start, end=end, camera_id=camera_id, run_id=run_id,
                         vtype=vtype, status=status, provenance=provenance, has_alerts=has_alerts, purpose=purpose,
                         reference=reference, reveal_reason=reveal_reason, page=page, page_size=page_size)


@router.get("/sightings", response_model=Page[SightingOut], tags=["plates"])
def api_search(q: SightingQuery = Depends(_query), user: User = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
    """Fuzzy plate search. `purpose` is required (audit log); masked roles searching by plate need `reveal_reason`."""
    return plate_svc.search(conn, user, q)


@router.get("/sightings/export.csv", response_class=PlainTextResponse, tags=["plates"])
def api_search_csv(q: SightingQuery = Depends(_query), user: User = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    return PlainTextResponse(plate_svc.export_csv(conn, user, q), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=sightings.csv"})


@router.get("/sightings/{sighting_id}", response_model=SightingDetail, tags=["plates"])
def api_sighting(sighting_id: int, purpose: str = "", user: User = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    return plate_svc.get(conn, user, sighting_id, purpose)


class RevealIn(BaseModel):
    reason_code: str
    note: str = ""


@router.post("/sightings/{sighting_id}/reveal", response_model=RevealOut, tags=["plates"])
def api_reveal(sighting_id: int, body: RevealIn, user: User = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
    """'Show full plate' for masked roles: a reason is required; audit-logged; lasts 15 minutes."""
    return plate_svc.reveal_sighting(conn, user, sighting_id, body.reason_code, body.note)


@router.get("/plates/{plate}/timeline", response_model=Timeline, tags=["plates"])
def api_timeline(plate: str, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return plate_svc.timeline_of(conn, user, plate)


@router.get("/alerts", response_model=Page[AlertOut], tags=["alerts"])
def api_alerts(status: Optional[str] = None, severity: Optional[str] = None, code: Optional[str] = None,
               camera_id: Optional[int] = None, include_simulated: bool = True, plate: str = "",
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return alert_svc.list_alerts(conn, user, status, severity, code, camera_id, include_simulated, plate, page, page_size)


@router.get("/alerts/counts", response_model=AlertCounts, tags=["alerts"])
def api_alert_counts(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return alert_svc.counts(conn, user)


@router.get("/alerts/{alert_id}", response_model=AlertDetail, tags=["alerts"])
def api_alert(alert_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return alert_svc.get_alert(conn, user, alert_id)


class TransitionIn(BaseModel):
    to: str
    note: str = ""
    corrected_plate: Optional[str] = None


@router.post("/alerts/{alert_id}/transition", response_model=AlertDetail, tags=["alerts"])
def api_transition(alert_id: int, body: TransitionIn, user: User = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
    return alert_svc.transition(conn, user, alert_id, body.to, body.note, body.corrected_plate)


@router.get("/alerts/{alert_id}/evidence", tags=["alerts"])
def api_evidence(alert_id: int, format: str = Query("html", pattern="^(html|pdf)$"), user: User = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Evidence pack (HTML or PDF) of one alert: police only; logged in evidence_exports and the audit log."""
    path = evidence_svc.export(conn, user, alert_id, format)
    return FileResponse(path, media_type="text/html; charset=utf-8" if format == "html" else "application/pdf",
                        filename=path.name, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"})
