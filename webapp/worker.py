"""Background worker: runs queued analysis jobs one at a time with the existing pipeline, in a subprocess of the
project venv (torch never loads into the web process, and a crashed analysis cannot take the server down), reads
the pipeline's progress lines into the job row, then imports the results as a new run.

    video:  run_alpr.py --profile <scene> --config <job config> --backend openvino [--headless]
    image:  run_image.py --source <file> --out <job folder>
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import yaml

from . import clock, db
from .settings import ROOT, Settings

WORKER: Optional["Worker"] = None
PROGRESS = re.compile(r"^\[(\d+)/(\d+)\]")


class Worker:
    def __init__(self, settings: Settings, poll_sec: float = 1.0):
        self.settings = settings
        self.poll_sec = poll_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._job_id: Optional[int] = None

    def start(self) -> None:
        global WORKER
        WORKER = self
        self._thread = threading.Thread(target=self._loop, daemon=True, name="secs-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def cancel(self, job_id: int) -> None:
        if self._job_id == job_id and self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        conn = db.connect(self.settings.db_path)
        conn.execute("UPDATE jobs SET status = 'failed', error = 'the server stopped during the analysis', "
                     "finished_at = ? WHERE status = 'running'", (clock.now_iso(),))
        while not self._stop.is_set():
            job = self._claim(conn)
            if job is None:
                self._stop.wait(self.poll_sec)
                continue
            self._job_id = job["id"]
            try:
                self.run_job(conn, job)
            except Exception as e:                                       # noqa: BLE001 - the job fails, not the worker
                conn.execute("UPDATE jobs SET status = 'failed', error = ?, message = 'failed', finished_at = ? "
                             "WHERE id = ?", (f"{type(e).__name__}: {e}"[:2000], clock.now_iso(), job["id"]))
                traceback.print_exc()
            finally:
                self._job_id, self._proc = None, None
        conn.close()

    @staticmethod
    def _claim(conn) -> Optional[dict]:
        with db.transaction(conn):
            job = db.row(conn, "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1")
            if job:
                conn.execute("UPDATE jobs SET status = 'running', started_at = ?, message = 'starting' WHERE id = ?",
                             (clock.now_iso(), job["id"]))
        return job

    # ------------------------------------------------------------------ one job
    def run_job(self, conn, job: dict) -> None:
        job_dir = self.settings.jobs_dir / f"job_{job['id']}"
        job_dir.mkdir(parents=True, exist_ok=True)
        if job["kind"] == "image":
            self._image(conn, job, job_dir)
        else:
            self._video(conn, job, job_dir)

    def _cancelled(self, conn, job_id: int) -> bool:
        return db.scalar(conn, "SELECT status FROM jobs WHERE id = ?", (job_id,)) == "cancelled"

    def _run(self, conn, job: dict, cmd, log_path: Path, total_hint: Optional[int] = None) -> int:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        t0, last_update, total = time.time(), 0.0, total_hint
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(" ".join(map(str, cmd)) + "\n")
            self._proc = subprocess.Popen([str(c) for c in cmd], cwd=str(ROOT), stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                          env=env)
            for line in self._proc.stdout:
                log.write(line)
                m = PROGRESS.match(line.strip())
                now = time.time()
                if m and now - last_update > 1.0:
                    n, total = int(m.group(1)), int(m.group(2))
                    fps = n / max(1e-3, now - t0)
                    left = (total - n) / fps if fps > 0 else 0
                    conn.execute("UPDATE jobs SET progress = ?, message = ? WHERE id = ? AND status = 'running'",
                                 (min(0.99, n / max(1, total)),
                                  f"{n}/{total} frames · {fps:.1f} FPS · about {int(left // 60)}:{int(left % 60):02d} left",
                                  job["id"]))
                    last_update = now
                if self._cancelled(conn, job["id"]):
                    self._proc.terminate()
            return self._proc.wait()

    def _fail(self, conn, job_id: int, msg: str) -> None:
        conn.execute("UPDATE jobs SET status = 'failed', message = 'failed', error = ?, finished_at = ? WHERE id = ?",
                     (msg[-2000:], clock.now_iso(), job_id))

    def _done(self, conn, job_id: int, run_id: int, msg: str) -> None:
        conn.execute("UPDATE jobs SET status = 'done', progress = 1, message = ?, run_id = ?, finished_at = ? WHERE id = ?",
                     (msg, run_id, clock.now_iso(), job_id))

    def _image(self, conn, job: dict, job_dir: Path) -> None:
        from .importer import import_image_results
        out = job_dir / "out"
        conn.execute("UPDATE jobs SET progress = 0.3, message = 'reading plates' WHERE id = ?", (job["id"],))
        rc = self._run(conn, job, [self.settings.python, ROOT / "run_image.py", "--source", job["input_path"],
                                   "--out", out], job_dir / "log.txt")
        if self._cancelled(conn, job["id"]):
            return
        if rc != 0 or not (out / "results.json").exists():
            return self._fail(conn, job["id"], _tail(job_dir / "log.txt"))
        run_id = import_image_results(conn, self.settings, code=f"upload-{job['id']}",
                                      label=f"Upload #{job['id']}: {job['original_name']}", camera_id=job["camera_id"],
                                      results=out / "results.json", image_dir=out,
                                      recorded_at=clock.parse(job["recorded_at"]), job_id=job["id"])
        n = db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE run_id = ?", (run_id,))
        self._done(conn, job["id"], run_id, f"done · {n} plate{'s' if n != 1 else ''}")

    def _video(self, conn, job: dict, job_dir: Path) -> None:
        from .importer import import_results
        from .services.jobs import SCENES, UPLOAD_RULES
        scene = SCENES[job["scene"]]
        cam = db.row(conn, "SELECT * FROM cameras WHERE id = ?", (job["camera_id"],))
        with open(ROOT / "alpr" / "config.yaml", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        prof = dict(raw["profiles"][scene["profile"]])
        out = job_dir / "result.mp4"
        prof.update(source=job["input_path"], start_sec=0.0, duration=0.0, out=str(out), summary_sec=0.0, hud_banner="",
                    registry_sim_events=[], video_start_time=job["recorded_at"][:19], camera_name=cam["name"],
                    hud_title=f"SECS Vision · {cam['name']}")
        if not scene["frame_crop"]:
            prof.pop("frame_crop", None)
        if cam["lat"] is not None and cam["lon"] is not None:
            prof["camera_latlon"] = [cam["lat"], cam["lon"]]
        else:
            prof.pop("camera_latlon", None)
        prof["registry_enabled"] = bool(scene["registry"])
        if scene["registry"]:
            prof["registry_rules"] = list(UPLOAD_RULES)
            prof["registry_path"] = str(self.settings.registry_path)
        name = f"upload_job_{job['id']}"
        cfg_path = job_dir / "config.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"common": raw["common"], "profiles": {name: prof}}, f, allow_unicode=True, sort_keys=False)
        cmd = [self.settings.python, ROOT / "run_alpr.py", "--profile", name, "--config", cfg_path, "--backend", "openvino"]
        if not job["render_video"]:
            cmd.append("--headless")
        rc = self._run(conn, job, cmd, job_dir / "log.txt")
        if self._cancelled(conn, job["id"]):
            return
        results = job_dir / "result.plates.json"
        if rc != 0 or not results.exists():
            return self._fail(conn, job["id"], _tail(job_dir / "log.txt"))
        conn.execute("UPDATE jobs SET progress = 0.99, message = 'importing the results' WHERE id = ?", (job["id"],))
        if job["render_video"] and out.exists():
            video, kind = out, "annotated"
        else:
            video, kind = playback_copy(Path(job["input_path"]), job_dir), "source"
        run_id = import_results(conn, self.settings, code=f"upload-{job['id']}", label=f"Upload #{job['id']}: "
                                f"{job['original_name']}", camera_id=job["camera_id"], results=results,
                                clock_origin=clock.parse(job["recorded_at"]), origin="upload", video=video,
                                video_kind=kind, video_offset=0.0, note=f"Uploaded video, scene: {scene['label']}. "
                                f"Registry rules: {', '.join(UPLOAD_RULES) if scene['registry'] else 'off'}.",
                                job_id=job["id"])
        n = db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE run_id = ?", (run_id,))
        self._done(conn, job["id"], run_id, f"done · {n} plate{'s' if n != 1 else ''}")


def playback_copy(src: Path, job_dir: Path) -> Path:
    """The source video if the browser can play it (H.264 in MP4), else an H.264 copy made with the bundled ffmpeg."""
    import cv2
    cap = cv2.VideoCapture(str(src))
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    codec = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).lower()
    if src.suffix.lower() in (".mp4", ".m4v") and codec in ("avc1", "h264"):
        return src
    dst = job_dir / "playback.mp4"
    try:
        import imageio_ffmpeg
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-an", "-c:v",
                        "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        str(dst)], check=True, timeout=3600)
        return dst
    except Exception:                                                   # noqa: BLE001 - play the original then
        return src


def _tail(path: Path, n: int = 15) -> str:
    try:
        return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-n:])
    except OSError:
        return "the analysis failed (no log)"
