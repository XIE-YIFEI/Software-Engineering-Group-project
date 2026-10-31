"""API: guardhouse (identity, replay cards, decisions, inside now, access log), visitor passes and the lists."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..deps import current_user, get_conn
from ..models import AccessEventOut, Identity, InsideOut, ListEntryOut, PassOut
from ..security import User
from ..services import gate as gate_svc
from ..services import lists as list_svc

router = APIRouter()


@router.get("/gate/identify", response_model=Identity, tags=["gate"])
def api_identify(plate: str, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.identify(conn, user, plate)


@router.get("/gate/replay", tags=["gate"])
def api_gate_replay(run_id: Optional[int] = None, user: User = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.replay(conn, user, run_id)


class DecisionIn(BaseModel):
    plate: str
    decision: str                      # allowed | stopped | exited
    note: str = ""
    sighting_id: Optional[int] = None
    unit_no: str = ""


@router.post("/gate/decisions", response_model=AccessEventOut, tags=["gate"])
def api_decide(body: DecisionIn, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.decide(conn, user, body.plate, body.decision, body.note, body.sighting_id, body.unit_no)


@router.get("/gate/inside", response_model=List[InsideOut], tags=["gate"])
def api_inside(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.inside(conn, user)


@router.get("/gate/access-log", response_model=List[AccessEventOut], tags=["gate"])
def api_access_log(plate: str = "", start: str = "", end: str = "", decision: str = "", identity: str = "",
                   user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.access_log(conn, user, plate, start, end, decision, identity)


@router.get("/gate/access-log.csv", response_class=PlainTextResponse, tags=["gate"])
def api_access_log_csv(plate: str = "", start: str = "", end: str = "", decision: str = "", identity: str = "",
                       user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return PlainTextResponse(gate_svc.access_log_csv(conn, user, plate=plate, start=start, end=end, decision=decision,
                                                     identity=identity), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=access_log.csv"})


@router.get("/passes", response_model=List[PassOut], tags=["gate"])
def api_passes(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.passes(conn, user)


class PassIn(BaseModel):
    plate: str
    unit_no: str
    visitor_label: str = ""
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    hours: Optional[float] = None


@router.post("/passes", response_model=PassOut, tags=["gate"])
def api_issue_pass(body: PassIn, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.issue_pass(conn, user, body.plate, body.unit_no, body.visitor_label, body.valid_from, body.valid_to,
                               body.hours)


@router.post("/passes/{pass_id}/cancel", response_model=PassOut, tags=["gate"])
def api_cancel_pass(pass_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return gate_svc.cancel_pass(conn, user, pass_id)


@router.get("/lists/{lst}", response_model=List[ListEntryOut], tags=["lists"])
def api_list(lst: str, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return list_svc.entries(conn, user, lst)


class EntryIn(BaseModel):
    plate: str
    reason: str = ""
    label: str = ""
    unit_no: str = ""
    valid_to: Optional[str] = None


@router.post("/lists/{lst}", response_model=ListEntryOut, tags=["lists"])
def api_list_add(lst: str, body: EntryIn, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return list_svc.add(conn, user, lst, body.plate, body.reason, body.label, body.unit_no, body.valid_to)


class RemoveIn(BaseModel):
    reason: str = ""


@router.post("/lists/entries/{entry_id}/deactivate", response_model=ListEntryOut, tags=["lists"])
def api_list_remove(entry_id: int, body: RemoveIn, user: User = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    return list_svc.deactivate(conn, user, entry_id, body.reason)
