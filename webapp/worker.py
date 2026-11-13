"""Background worker: runs queued analysis jobs one at a time with the existing pipeline, in a subprocess of the
project venv (torch never loads into the web process, and a crashed analysis cannot take the server down), reads
the pipeline's progress lines into the job row, then imports the results as a new run.

    video:  run_alpr.py --profile <scene> --config <job config> --backend openvino [--headless]
    image:  run_image.py --source <file> --out <job folder>
"""
from __future__ import annotations

import os
import json
import queue
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
        if self._job_id is not None:
            self.cancel(self._job_id)
        if self._thread is not None:
            self._thread.join(timeout=self.settings.cancel_timeout_sec + self.settings.terminate_timeout_sec + 10)

    def cancel(self, job_id: int) -> None:
        # The DB flag is authoritative; _run also sees requests racing with spawn.
        if self._job_id == job_id:
            folder = self.settings.jobs_dir / f"job_{job_id}"
            if folder.exists():
                (folder / "stop.request").touch(exist_ok=True)

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        conn = db.connect(self.settings.db_path)
        # After an abrupt server crash we cannot prove the old child has exited.
        # Leave its files intact, but never import a potentially active snapshot.
        conn.execute("UPDATE jobs SET status = CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END, "
                     "message='Server interrupted; automatic recovery unavailable', finished_at=? "
                     "WHERE status='running'", (clock.now_iso(),))
        while not self._stop.is_set():
            job = self._claim(conn)
            if job is None:
                self._stop.wait(self.poll_sec)
                continue
            self._job_id = job["id"]
            try:
                self.run_job(conn, job)
            except Exception as e:                                       # noqa: BLE001 - the job fails, not the worker
                self._recover(conn, job, self.settings.jobs_dir / f"job_{job['id']}",
                              f"{type(e).__name__}: {e}")
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
        if self._cancelled(conn, job["id"]):
            self._recover(conn, job, job_dir, "Cancelled before launch")
            return
        if job_dir.exists():
            self._fail(conn, job["id"], "Execution directory already exists; create a new job")
            return  # Do not recover stale output from a different execution.
        job_dir.mkdir(parents=True, exist_ok=False)
        if job["kind"] == "image":
            self._image(conn, job, job_dir)
        else:
            self._video(conn, job, job_dir)

    def _cancelled(self, conn, job_id: int) -> bool:
        return bool(db.scalar(conn, "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)))

    def _run(self, conn, job: dict, cmd, log_path: Path, total_hint: Optional[int] = None,
             *, cooperative_stop: bool = True, timeout_sec: Optional[float] = None) -> int:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        total = total_hint
        requested_at = terminated_at = None
        started_at = time.monotonic()
        lines = queue.Queue()
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(" ".join(map(str, cmd)) + "\n")
            proc = subprocess.Popen([str(c) for c in cmd], cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
            self._proc = proc
            def read_lines():
                try:
                    for line in proc.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)
            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            eof = False
            try:
                while proc.poll() is None or not eof:
                    now = time.monotonic()
                    timed_out = timeout_sec is not None and now - started_at >= timeout_sec
                    if proc.poll() is None and (self._cancelled(conn, job["id"]) or self._stop.is_set()
                                                or timed_out):
                        if requested_at is None:
                            requested_at = now
                            if cooperative_stop:
                                (log_path.parent / "stop.request").touch(exist_ok=True)
                        if terminated_at is None and (not cooperative_stop or timed_out or job["kind"] == "image" or
                                now - requested_at >= self.settings.cancel_timeout_sec):
                            proc.terminate()
                            terminated_at = now
                            log.write("Stopping child process: cancellation, shutdown or timeout\n")
                        elif terminated_at is not None and now - terminated_at >= self.settings.terminate_timeout_sec:
                            proc.kill()
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        eof = True
                        continue
                    log.write(line)
                    log.flush()
                    m = PROGRESS.match(line.strip())
                    if m:
                        n, total = map(int, m.groups())
                        conn.execute("UPDATE jobs SET expected_frames = ? WHERE id = ?", (total, job["id"]))
                        conn.execute("UPDATE jobs SET progress = ?, message = ? WHERE id = ? AND cancel_requested = 0",
                                     (min(0.99, n / max(1, total)), f"{n}/{total} frames", job["id"]))
                rc = proc.wait()
                # A terminated transcoder may handle the signal and return zero;
                # that does not make its interrupted output a completed copy.
                return 1 if not cooperative_stop and terminated_at is not None and rc == 0 else rc
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                reader.join(timeout=2)
                proc.stdout.close()
                self._proc = None

    def _recover(self, conn, job: dict, job_dir: Path, reason: str) -> None:
        with db.transaction(conn):
            self._recover_locked(conn, job, job_dir, reason)

    def _recover_locked(self, conn, job: dict, job_dir: Path, reason: str) -> None:
        """One terminal snapshot; importing it must never mark a job successful."""
        from .importer import import_results
        cancelled = self._cancelled(conn, job["id"])
        status = "cancelled" if cancelled else "failed"
        run_id, frames, window = None, None, None
        message = "No saved results"
        partial = job_dir / "result.partial.json"
        conn.execute("SAVEPOINT recovery_import")
        try:
            if job["kind"] == "video" and partial.exists():
                d = json.loads(partial.read_text(encoding="utf-8"))
                if d.get("complete") is not False:
                    raise ValueError("Snapshot is not marked incomplete")
                if d.get("frames_processed") == 0:
                    message = "No saved results (zero processed frames)"
                else:
                    with db.transaction(conn):
                        run_id = import_results(conn, self.settings, code=f"upload-{job['id']}",
                            label=f"Upload #{job['id']}: {job['original_name']} (incomplete)",
                            camera_id=job["camera_id"], results=partial, evidence=partial,
                            clock_origin=clock.parse(job["recorded_at"]), origin="upload",
                            video=Path(job["input_path"]) if Path(job["input_path"]).exists() else None,
                            video_kind="source", video_offset=0.0, job_id=job["id"],
                            note="Incomplete analysis; only saved records are available.")
                    frames, window = d["frames_processed"], d["window_sec"]
                    count = db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE run_id = ?", (run_id,))
                    message = f"Incomplete results: {frames} saved frames, {count} plate records"
                    if count == 0:
                        message += " — no plate records saved in the processed portion"
                    message += self._evidence_note(conn, run_id)
        except Exception as exc:
            conn.execute("ROLLBACK TO recovery_import")
            run_id, frames, window = None, None, None
            reason += f"; partial recovery failed: {type(exc).__name__}: {exc}"
            message = "Saved results could not be recovered"
        finally:
            conn.execute("RELEASE recovery_import")
        expected = db.scalar(conn, "SELECT expected_frames FROM jobs WHERE id = ?", (job["id"],))
        progress = min(0.99, frames / expected) if frames and expected else 0
        conn.execute("UPDATE jobs SET status=?, message=?, error=?, finished_at=?, run_id=?, "
                     "result_complete=?, saved_frames=?, saved_window=?, progress=? WHERE id=?",
                     (status, message, reason[-2000:] if not cancelled or 'failed:' in reason else None,
                      clock.now_iso(), run_id, 0 if run_id else None, frames,
                      db.js(window) if window else None, progress, job["id"]))

    def _fail(self, conn, job_id: int, msg: str) -> None:
        conn.execute("UPDATE jobs SET status = CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END, "
                     "message = 'Analysis stopped without imported results', error = ?, finished_at = ? WHERE id = ?",
                     (msg[-2000:], clock.now_iso(), job_id))

    def _done(self, conn, job_id: int, run_id: int, msg: str) -> None:
        conn.execute("UPDATE jobs SET status = 'done', progress = 1, message = ?, run_id = ?, finished_at = ? WHERE id = ?",
                     (msg, run_id, clock.now_iso(), job_id))

    @staticmethod
    def _evidence_note(conn, run_id: int) -> str:
        missing = db.rows(conn, "SELECT scene_info FROM sightings WHERE run_id = ? AND "
                          "(crop_path IS NULL OR scene_path IS NULL)", (run_id,))
        if not missing:
            return ""
        reasons = sorted({info["unavailable_reason"] for row in missing
                          if (info := db.unjs(row["scene_info"], {})).get("unavailable_reason")})
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        return f"; evidence unavailable for {len(missing)} records{detail}"

    def _image(self, conn, job: dict, job_dir: Path) -> None:
        from .importer import import_image_results
        out = job_dir / "out"
        conn.execute("UPDATE jobs SET progress = 0.3, message = 'reading plates' WHERE id = ?", (job["id"],))
        rc = self._run(conn, job, [self.settings.python, ROOT / "run_image.py", "--source", job["input_path"],
                                   "--out", out], job_dir / "log.txt")
        if self._cancelled(conn, job["id"]):
            return self._recover(conn, job, job_dir, "Cancelled")
        if rc != 0 or not (out / "results.json").exists():
            return self._fail(conn, job["id"], _tail(job_dir / "log.txt"))
        with db.transaction(conn):
            if self._cancelled(conn, job["id"]):
                return self._recover(conn, job, job_dir, "Cancelled")
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
                    evidence_dir=str((job_dir / "evidence").resolve()),
                    stop_file=str((job_dir / "stop.request").resolve()), registry_sim_events=[], video_start_time=job["recorded_at"][:19], camera_name=cam["name"],
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
        self._finish_video(conn, job, job_dir, rc, scene)

    def _finish_video(self, conn, job, job_dir, rc, scene):
        from .importer import import_results
        results, out = job_dir / "result.plates.json", job_dir / "result.mp4"
        if rc != 0 or self._cancelled(conn, job["id"]) or self._stop.is_set() or not results.exists():
            return self._recover(conn, job, job_dir, "Cancelled" if self._cancelled(conn, job["id"]) else
                                 _tail(job_dir / "log.txt"))
        try:
            # Conversion may take time; cancellation remains available during it.
            video, kind = (out, "annotated") if job["render_video"] and out.exists() else (
                playback_copy(Path(job["input_path"]), job_dir,
                              lambda cmd: self._run(conn, job, cmd, job_dir / "playback.log",
                                                    cooperative_stop=False, timeout_sec=3600)), "source")
            # Serialize the final import/status change with the cancel request.
            with db.transaction(conn):
                if self._cancelled(conn, job["id"]) or self._stop.is_set():
                    return self._recover(conn, job, job_dir, "Cancelled")
                d = json.loads(results.read_text(encoding="utf-8"))
                if d.get("complete") is False or not isinstance(d.get("plates"), list) or type(d.get("frames")) is not int:
                    raise ValueError("Invalid final report")
                run_id = import_results(conn, self.settings, code=f"upload-{job['id']}",
                    label=f"Upload #{job['id']}: {job['original_name']}", camera_id=job["camera_id"],
                    results=results, evidence=results, clock_origin=clock.parse(job["recorded_at"]),
                    origin="upload", video=video, video_kind=kind, video_offset=0.0,
                    note=f"Uploaded video, scene: {scene['label']}", job_id=job["id"])
                n = db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE run_id = ?", (run_id,))
                conn.execute("UPDATE jobs SET result_complete=1, saved_frames=?, saved_window=? WHERE id=?",
                             (d["frames"], db.js(d.get("window_sec")), job["id"]))
                self._done(conn, job["id"], run_id, f"done · {n} plate records" + self._evidence_note(conn, run_id))
        except Exception as exc:
            self._recover(conn, job, job_dir, f"Final import failed: {type(exc).__name__}: {exc}")


def playback_copy(src: Path, job_dir: Path, run_command) -> Path:
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
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-an", "-c:v",
               "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               str(dst)]
    except (ImportError, OSError, RuntimeError):
        return src
    # The worker owns cancellation, timeout escalation and reaping this child.
    # Let unrelated runner/database errors reach the existing recovery boundary.
    return dst if run_command(cmd) == 0 else src


def _tail(path: Path, n: int = 15) -> str:
    try:
        return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-n:])
    except OSError:
        return "the analysis failed (no log)"
