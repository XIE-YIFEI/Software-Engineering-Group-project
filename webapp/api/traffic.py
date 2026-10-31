"""API: traffic analytics and the congestion forecast (with the what-if)."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from ..deps import current_user, get_conn
from ..models import Accuracy, ForecastDay, ForecastState, LaneEventOut, RunOut, Series, TrafficSummary, WhatIfOut
from ..security import User
from ..services import forecast as forecast_svc
from ..services import traffic as traffic_svc

router = APIRouter()


@router.get("/traffic/runs", response_model=List[RunOut], tags=["traffic"])
def api_traffic_runs(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return traffic_svc.runs(conn, user)


@router.get("/traffic/summary", response_model=TrafficSummary, tags=["traffic"])
def api_traffic_summary(run_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return traffic_svc.summary(conn, user, run_id)


@router.get("/traffic/flow", response_model=Series, tags=["traffic"])
def api_flow(run_id: int, by: str = Query("type", pattern="^(type|lane|time)$"), bin: float = Query(10.0, ge=1, le=600),
             user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return traffic_svc.flow_series(conn, user, run_id, by, bin)


@router.get("/traffic/lanes/series", response_model=Series, tags=["traffic"])
def api_lane_series(run_id: int, metric: str = "occupancy", user: User = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    return traffic_svc.lane_series(conn, user, run_id, metric)


@router.get("/traffic/events", response_model=List[LaneEventOut], tags=["traffic"])
def api_events(run_id: int, user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return traffic_svc.events(conn, user, run_id)


@router.get("/traffic/export.csv", response_class=PlainTextResponse, tags=["traffic"])
def api_traffic_csv(run_id: int, what: str = "crossings", user: User = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    return PlainTextResponse(traffic_svc.export_csv(conn, user, run_id, what), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=traffic_{run_id}_{what}.csv"})


@router.get("/forecast/days", response_model=List[ForecastDay], tags=["forecast"])
def api_forecast_days(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return forecast_svc.days(conn, user)


@router.get("/forecast/state", response_model=ForecastState, tags=["forecast"])
def api_forecast_state(day: str, at: Optional[str] = Query(None, pattern=r"^\d\d:\d\d$"),
                       user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return forecast_svc.state(conn, user, day, at)


@router.get("/forecast/accuracy", response_model=Accuracy, tags=["forecast"])
def api_forecast_accuracy(day: str, section: str = "To CIQ", horizon: int = 30, user: User = Depends(current_user),
                          conn: sqlite3.Connection = Depends(get_conn)):
    return forecast_svc.accuracy(conn, user, day, section, horizon)


@router.get("/forecast/whatif", response_model=WhatIfOut, tags=["forecast"])
def api_whatif(day: str, section: str = "To CIQ", capacity_pct: float = Query(20.0, ge=0, le=100),
               mu: Optional[float] = Query(None, gt=0), start: Optional[str] = Query(None, pattern=r"^\d\d:\d\d$"),
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Estimate under stated assumptions: projected queue with `capacity_pct` % more discharge capacity."""
    return forecast_svc.whatif(conn, user, day, section, capacity_pct, mu, start)
