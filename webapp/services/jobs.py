"""Upload & analyse: an uploaded image or video becomes a job; the background worker (webapp/worker.py) runs the
existing pipeline on it and imports the results as a new run."""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import BinaryIO, List, Optional

from .. import clock, db
from ..models import JobOut
from ..privacy import audit
from ..security import User, require
from ..settings import active
from .common import in_clause, visible_camera_ids

ROLES = ("police", "traffic", "admin")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
VIDEO_EXT = (".mp4", ".avi", ".mov", ".mkv", ".m4v")
MAX_BYTES = 2 * 1024 ** 3
# Scene templates for uploads: the alpr profile whose thresholds and scene geometry are used, without its
# demo-specific parts (frame crop of the vendor screen recording, simulated events).
SCENES = {
    "roadside": {"label": "Roadside ANPR (full frame)", "profile": "malaysia_road_anpr", "frame_crop": False,
                 "registry": True},
    "gate": {"label": "Guardhouse gate (full frame)", "profile": "malaysia_guardhouse", "frame_crop": False,
             "registry": False},
    "lilin": {"label": "LILIN demo camera (crops the vendor's screen recording)", "profile": "malaysia_road_anpr",
              "frame_crop": True, "registry": True},
}
# Registry rules on uploads: the registry is SIMULATED, so a real plate "not in the registry" (UNMATCHED) says
# nothing about the vehicle; road tax and type checks only apply to records, which real plates do not have.
UPLOAD_RULES = ["ILLEGAL_FORMAT", "DUPLICATE"]


def _out(j: dict) -> JobOut:
    return JobOut(id=j["id"], kind=j["kind"], status=j["status"], progress=j["progress"], message=j["message"],
                  original_name=j["original_name"], scene=j["scene"], render_video=bool(j["render_video"]),
                  camera_id=j["camera_id"], camera_name=j.get("camera_name") or "", recorded_at=j["recorded_at"],
                  created_by=j.get("username") or "", created_at=j["created_at"], started_at=j["started_at"],
                  finished_at=j["finished_at"], run_id=j["run_id"], error=j["error"])


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:80]
    return stem or "upload"


def create(conn: sqlite3.Connection, user: User, stream: BinaryIO, filename: str, camera_id: int, scene: str = "roadside",
           render_video: bool = False, recorded_at: Optional[str] = None) -> JobOut:
    """Store the upload and queue a job. Images run run_image.py; videos run run_alpr.py (headless, OpenVINO;
    with render_video also the annotated dashboard video, about 3x slower)."""
    require(user, *ROLES)
    if camera_id not in visible_camera_ids(conn, user):
        raise LookupError("no such camera")
    ext = Path(filename).suffix.lower()
    kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
    if kind is None:
        raise ValueError(f"upload an image ({', '.join(IMAGE_EXT)}) or a video ({', '.join(VIDEO_EXT)})")
    if scene not in SCENES:
        raise ValueError(f"scene must be one of {', '.join(SCENES)}")
    when = clock.iso(clock.parse(recorded_at)) if recorded_at else clock.now_iso()
    st = active()
    st.ensure_dirs()
    # the job row is only created once the file is safely on disk
    dst = st.upload_dir / f"{clock.now().strftime('%Y%m%d_%H%M%S_%f')}_{_safe_name(filename)}"
    size = 0
    with open(dst, "wb") as f:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                f.close()
                dst.unlink(missing_ok=True)
                raise ValueError("the file is larger than 2 GB")
            f.write(chunk)
    if size == 0:
        dst.unlink(missing_ok=True)
        raise ValueError("the file is empty")
    with db.transaction(conn):
        jid = db.insert(conn, "jobs", {"kind": kind, "status": "queued", "progress": 0, "message": "queued",
                                       "input_path": str(dst), "original_name": Path(filename).name[:120], "scene": scene,
                                       "render_video": int(render_video and kind == "video"), "camera_id": camera_id,
                                       "recorded_at": when, "created_by": user.id, "created_at": clock.now_iso()})
        audit(conn, user, "upload", "job", jid, {"file": Path(filename).name[:120], "bytes": size, "kind": kind,
                                                 "scene": scene, "camera_id": camera_id})
    return get(conn, user, jid)


def list_jobs(conn: sqlite3.Connection, user: User, limit: int = 50) -> List[JobOut]:
    require(user, *ROLES)
    cams = visible_camera_ids(conn, user)
    rs = db.rows(conn, f"SELECT j.*, c.name AS camera_name, u.username FROM jobs j JOIN cameras c ON c.id = j.camera_id "
                       f"LEFT JOIN users u ON u.id = j.created_by WHERE j.camera_id IN {in_clause(cams)} "
                       f"ORDER BY j.id DESC LIMIT ?", (limit,))
    return [_out(j) for j in rs]


def get(conn: sqlite3.Connection, user: User, job_id: int) -> JobOut:
    require(user, *ROLES)
    j = db.row(conn, "SELECT j.*, c.name AS camera_name, u.username FROM jobs j JOIN cameras c ON c.id = j.camera_id "
                     "LEFT JOIN users u ON u.id = j.created_by WHERE j.id = ?", (job_id,))
    if j is None or j["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such job")
    return _out(j)


def cancel(conn: sqlite3.Connection, user: User, job_id: int) -> JobOut:
    j = get(conn, user, job_id)
    if j.status not in ("queued", "running"):
        raise ValueError(f"the job is {j.status}")
    conn.execute("UPDATE jobs SET status = 'cancelled', message = 'cancelled', finished_at = ? WHERE id = ? AND status IN "
                 "('queued', 'running')", (clock.now_iso(), job_id))
    from ..worker import WORKER
    if WORKER is not None:
        WORKER.cancel(job_id)
    audit(conn, user, "job_cancel", "job", job_id)
    return get(conn, user, job_id)


def scenes() -> dict:
    return {k: v["label"] for k, v in SCENES.items()}


def remove_job_files(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)
