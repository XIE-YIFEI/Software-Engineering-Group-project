"""Helpers shared by the services: camera scoping per role, media URLs, video references, alert titles."""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from .. import db
from ..models import PROVENANCE_LABEL, VideoRef
from ..security import User

# Camera kinds each role may see (purpose limitation): guards only their own site's gate.
ROLE_CAMERA_KINDS = {"police": ("anpr", "lane", "congestion"), "traffic": ("anpr", "lane", "congestion"),
                     "admin": ("anpr", "gate", "lane", "congestion"), "guard": ("gate",)}

ALERT_TITLE = {"UNMATCHED": "Not registered", "EXPIRED": "Road tax expired", "ILLEGAL_FORMAT": "Illegal format",
               "BLACKLIST": "Blacklisted", "TYPE_MISMATCH": "Type mismatch", "DUPLICATE": "Cloned plate",
               "WATCHLIST": "Watch-list hit", "BANNED_AT_SITE": "Banned at this site",
               "WRONG_WAY": "Wrong way", "SPEEDING": "Speeding", "SOLID_LINE": "Crossed a solid line",
               "TAILGATING": "Tailgating", "STOPPED": "Stopped in a live lane", "LANE_CHANGE": "Lane change"}
RANK = {"high": 1, "violation": 1, "hazard": 2, "medium": 3}
RANK_NAME = {1: "High", 2: "Hazard", 3: "Medium"}
UNMATCHED_NOTE = "Not in the SIMULATED registry. This does not mean a real vehicle is unregistered."


def title(code: str) -> str:
    return ALERT_TITLE.get(code, code.replace("_", " ").capitalize())


def provenance_label(p: str) -> str:
    return PROVENANCE_LABEL.get(p, "")


def visible_camera_ids(conn: sqlite3.Connection, user: User) -> List[int]:
    kinds = ROLE_CAMERA_KINDS.get(user.role, ())
    if not kinds:
        return []
    marks = ",".join("?" * len(kinds))
    if user.role == "guard":
        return [r["id"] for r in db.rows(conn, f"SELECT id FROM cameras WHERE kind IN ({marks}) AND site_id = ?",
                                         (*kinds, user.site_id))]
    return [r["id"] for r in db.rows(conn, f"SELECT id FROM cameras WHERE kind IN ({marks})", kinds)]


def in_clause(ids: List[int]) -> str:
    return "(" + (",".join(str(int(i)) for i in ids) or "NULL") + ")"


def media_url(rel: Optional[str]) -> Optional[str]:
    return f"/media/files/{rel}" if rel else None


def run_video_url(run_id: int) -> str:
    return f"/media/runs/{run_id}/video"


PLATE_CAMERAS = ("anpr", "gate")


def can_play(user: User, camera_kind: Optional[str]) -> bool:
    """Masked roles get no video of plate cameras: the annotated video has the full plates drawn into it (and the
    source video shows them). Lane and congestion cameras' videos are fine."""
    return not (user.masked and camera_kind in PLATE_CAMERAS)


def video_ref(run: Dict, at_sec: Optional[float], user: Optional[User] = None) -> Optional[VideoRef]:
    """Seek target for a record at source second `at_sec`: one second before it, in the played video's time."""
    if not run.get("video_path") or at_sec is None:
        return None
    if user is not None and not can_play(user, run.get("camera_kind")):
        return None
    seek = max(0.0, float(at_sec) - float(run.get("video_offset_sec") or 0.0) - 1.0)
    return VideoRef(url=run_video_url(run["id"]), seek_sec=round(seek, 2), kind=run.get("video_kind") or "annotated")
