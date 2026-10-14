"""ALPR pipeline: vehicle detection + ByteTrack, plate detection, quality gate, OCR, per-track voting,
plate log (one entry per vehicle) and traffic-flow counting.

`AlprPipeline.process_frame` is model-driven but side-effect free w.r.t. I/O; `run` handles the video.

Tracking notes (each fixed a failure seen on real Malaysian footage):
* class-agnostic NMS: a pickup is often detected as *car* and *truck* at once; class-aware NMS kept
  both boxes and the tracker turned one vehicle into two tracks.
* low-confidence boxes are passed to ByteTrack (``track_conf``) so its second association stage can
  keep a track alive through partial occlusion (trees, other cars); a track is only *shown* once its
  best detection reaches ``vehicle_conf`` (``heavy_vehicle_conf`` if it is mostly a bus/truck).
* vehicle type = confidence-weighted majority over the track, so the label does not flicker.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from .config import AlprConfig
from .evaluate import edit_distance
from .flow import LineCounter
from .hud import render_dashboard, render_hud
from .lead import LeadSelector
from .ocr import make_ocr
from .plates import parse_plate
from .quality import assess
from .voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateResult, PlateVoter

VEHICLE_NAMES = {2: "Car", 3: "Motorcycle", 5: "Bus", 7: "Truck"}
HEAVY = (5, 7)
REREAD_GROWTH = 1.10      # keep reading past the OCR budget whenever the plate is >=10% wider than before
STATUS_RANK = {ACQUIRING: 0, TENTATIVE: 1, CONFIRMED: 2}
TRACKER_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trackers", "bytetrack_alpr.yaml")


@dataclass
class TrackState:
    first_frame: int
    last_frame: int
    cls_conf: Dict[int, float] = field(default_factory=lambda: defaultdict(float))
    max_conf: float = 0.0
    plate_rel: Optional[Tuple[float, float, float, float]] = None   # plate box as fractions of the vehicle box
    last_hit: int = -10**9          # last frame a plate was actually detected
    last_det: int = -10**9          # last frame plate detection was attempted
    n_reads: int = 0                # OCR attempts
    best_ocr_q: float = -1.0
    best_read_w: int = 0            # widest plate crop OCR'd so far (px)
    best_crop: Optional[np.ndarray] = None
    best_crop_q: float = -1.0

    @property
    def cls(self) -> int:
        return max(self.cls_conf, key=self.cls_conf.get) if self.cls_conf else 2

    @property
    def vtype(self) -> str:
        return VEHICLE_NAMES.get(self.cls, "Vehicle")


@dataclass
class LogEntry:
    """One vehicle in the plate log. A track enters the log once its plate reaches `log_min_status`.
    Tracks that read the same (or a near-identical) plate within `merge_gap_sec` - the same vehicle
    re-acquired after an occlusion - share one entry; the best-supported reading is shown."""
    result: PlateResult
    vtype: str
    region: str
    first_frame: int
    last_frame: int
    track_ids: Set[int] = field(default_factory=set)
    crop: Optional[np.ndarray] = None


class AlprPipeline:
    def __init__(self, cfg: AlprConfig, ocr=None, fps: float = 25.0):
        self.cfg = cfg
        self.fps = fps
        self.vehicle_model = YOLO(cfg.vehicle_model)
        self.plate_model = YOLO(cfg.plate_model)
        self.plate_fallback = YOLO(cfg.plate_model_fallback) if cfg.plate_model_fallback else None
        self.ocr = ocr or make_ocr(cfg.ocr_backend, cfg.ocr_model)
        self.voter = PlateVoter(cfg.min_votes, cfg.confirm_conf, max_corrections=cfg.max_plate_corrections,
                                resolve_ligatures=cfg.resolve_ligatures)
        self.lead = LeadSelector(cfg.lane_weight, cfg.lead_hysteresis, cfg.min_lead_w_frac)
        self.counter = LineCounter(cfg.count_line_y) if cfg.count_line_y > 0 else None
        self.states: Dict[int, TrackState] = {}
        self.log: List[LogEntry] = []
        self.fps_ema = 0.0

    # ------------------------------------------------------------ detection + tracking
    def _track_vehicles(self, frame: np.ndarray, idx: int) -> List[Dict]:
        cfg = self.cfg
        h, w = frame.shape[:2]
        res = self.vehicle_model.track(frame, persist=True, tracker=cfg.tracker or TRACKER_CFG,
                                       classes=cfg.vehicle_classes, conf=cfg.track_conf, imgsz=cfg.imgsz,
                                       agnostic_nms=True, verbose=False)[0]
        out: List[Dict] = []
        if res.boxes is None or res.boxes.id is None:
            return out
        for xyxy, tid, conf, cls in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.id.cpu().numpy(),
                                        res.boxes.conf.cpu().numpy(), res.boxes.cls.cpu().numpy()):
            x1, y1, x2, y2 = [int(v) for v in xyxy]
            if y2 < cfg.min_ground_y_frac * h:
                continue
            cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
            if any(z[0] <= cx <= z[2] and z[1] <= cy <= z[3] for z in cfg.ignore_zones):
                continue
            tid = int(tid)
            st = self.states.get(tid)
            if st is None:
                st = self.states[tid] = TrackState(idx, idx)
            st.last_frame = idx
            st.cls_conf[int(cls)] += float(conf)
            st.max_conf = max(st.max_conf, float(conf))
            if st.max_conf < (cfg.heavy_vehicle_conf if st.cls in HEAVY else cfg.vehicle_conf):
                continue                                    # not a confirmed vehicle (yet)
            out.append({"track_id": tid, "box": [x1, y1, x2, y2], "vtype": st.vtype, "conf": float(conf)})
        return out

    def _detect_plates(self, car_crop: np.ndarray) -> List[Tuple[Tuple[int, int, int, int], float]]:
        """Plate candidates in a vehicle crop that pass the size / aspect checks, best first."""
        out = self._plate_candidates(self.plate_model, car_crop)
        if not out and self.plate_fallback is not None:          # second opinion only when the first finds nothing
            out = self._plate_candidates(self.plate_fallback, car_crop)
        return out

    def _plate_candidates(self, model, car_crop: np.ndarray):
        cfg = self.cfg
        vh, vw = car_crop.shape[:2]
        res = model.predict(car_crop, imgsz=cfg.plate_imgsz, conf=cfg.plate_conf, verbose=False)[0]
        out = []
        for b in (res.boxes if res.boxes is not None else []):
            px1, py1, px2, py2 = [int(v) for v in b.xyxy[0].tolist()]
            pw, ph = px2 - px1, py2 - py1
            if pw < cfg.min_plate_w_px or ph < cfg.min_plate_h_px:
                continue
            if pw > cfg.max_plate_w_frac * vw or ph > cfg.max_plate_h_frac * vh:
                continue
            if not cfg.plate_aspect_min <= pw / max(1, ph) <= cfg.plate_aspect_max:
                continue
            out.append(((px1, py1, px2, py2), float(b.conf[0])))
        return sorted(out, key=lambda c: -c[1])

    # ------------------------------------------------------------ plate reading
    @staticmethod
    def _crop(frame: np.ndarray, box) -> np.ndarray:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        padx, pady = int(0.04 * (x2 - x1)) + 2, int(0.10 * (y2 - y1)) + 1
        return frame[max(0, y1 - pady):min(h, y2 + pady), max(0, x1 - padx):min(w, x2 + padx)]

    def _read_plate(self, frame: np.ndarray, v: Dict, st: TrackState, idx: int) -> None:
        cfg = self.cfg
        h, w = frame.shape[:2]
        vx1, vy1, vx2, vy2 = max(0, v["box"][0]), max(0, v["box"][1]), min(w, v["box"][2]), min(h, v["box"][3])
        vw, vh = vx2 - vx1, vy2 - vy1
        if vw < cfg.min_vehicle_w_frac * w or vh < cfg.min_vehicle_h_frac * h:
            return
        if idx - st.last_det < cfg.plate_det_interval:
            return
        st.last_det = idx
        cands = self._detect_plates(frame[vy1:vy2, vx1:vx2])
        if not cands:
            return
        # Overlapping candidates (e.g. the plate, and the plate + the black grille below it, which looks
        # like a two-row plate) are settled by OCR: the reading that is a legal plate wins.
        first = cands[0][0]
        group = [first] + [b for b, _ in cands[1:] if _contain(b, first) > 0.5][:2]
        chosen, read = first, None
        if len(group) > 1:
            options = []
            for b in group:
                r = self.ocr.read_plate(self._crop(frame, (vx1 + b[0], vy1 + b[1], vx1 + b[2], vy1 + b[3])))
                options.append(((r is not None and r.valid, r.conf if r else 0.0), b, r))
            _, chosen, read = max(options, key=lambda o: o[0])
        px1, py1, px2, py2 = chosen
        st.plate_rel, st.last_hit = (px1 / vw, py1 / vh, px2 / vw, py2 / vh), idx
        crop = self._crop(frame, (vx1 + px1, vy1 + py1, vx1 + px2, vy1 + py2))
        q = assess(crop, cfg.min_plate_w_px, cfg.min_plate_h_px, cfg.min_sharpness, cfg.sharp_ref)
        if q.score > st.best_crop_q:
            st.best_crop, st.best_crop_q = crop.copy(), q.score

        # The OCR budget only bounds low-resolution reads (far plates are misread, and consistently so).
        # Once a plate reaches full resolution it is read every time: those are the reads that count.
        pw = px2 - px1
        if q.ok and (st.n_reads < cfg.ocr_max_readings or pw >= cfg.ocr_ref_plate_w
                     or pw >= st.best_read_w * REREAD_GROWTH or q.score > st.best_ocr_q + 0.1):
            st.n_reads += 1
            st.best_ocr_q = max(st.best_ocr_q, q.score)
            st.best_read_w = max(st.best_read_w, pw)
            if read is None:
                read = self.ocr.read_plate(crop)
            if read:
                # a far plate (few pixels) is misread consistently, so its vote counts with resolution^2
                weight_q = q.score * min(1.0, pw / cfg.ocr_ref_plate_w) ** 2
                self.voter.add(v["track_id"], read.text, read.conf, weight_q)

    def _plate_box(self, v: Dict, st: TrackState, idx: int):
        if st.plate_rel is None or idx - st.last_hit > self.cfg.plate_ttl_frames:
            return None
        x1, y1, x2, y2 = v["box"]
        vw, vh = x2 - x1, y2 - y1
        fx1, fy1, fx2, fy2 = st.plate_rel
        return (int(x1 + fx1 * vw), int(y1 + fy1 * vh), int(x1 + fx2 * vw), int(y1 + fy2 * vh))

    # ------------------------------------------------------------ plate log
    def _update_log(self, tid: int, st: TrackState, result: Optional[PlateResult], idx: int) -> None:
        mine = next((e for e in self.log if tid in e.track_ids), None)
        if result is None or (mine is None and STATUS_RANK[result.status] < STATUS_RANK[self.cfg.log_min_status]):
            return
        gap = self.cfg.merge_gap_sec * self.fps
        same = next((e for e in self.log if e is not mine and _same_plate(e.result.compact, result.compact)
                     and idx - e.last_frame <= gap), None)
        if same is not None:                       # same plate seen recently: the same vehicle re-acquired
            if mine is not None:
                same.track_ids |= mine.track_ids
                self.log.remove(mine)
            mine = same
        elif mine is None:
            mine = LogEntry(result, st.vtype, "", st.first_frame, idx)
            self.log.append(mine)
        mine.track_ids.add(tid)
        mine.last_frame = idx
        if len(mine.track_ids) == 1 or result.votes >= mine.result.votes:   # best-supported reading wins
            parse = parse_plate(result.compact, 0)
            mine.result, mine.vtype, mine.region = result, st.vtype, (parse.region if parse else "")
            if st.best_crop is not None:
                mine.crop = st.best_crop

    # ------------------------------------------------------------ per frame
    def process_frame(self, frame: np.ndarray, idx: int) -> Tuple[np.ndarray, List[Dict]]:
        t0 = time.time()
        h, w = frame.shape[:2]
        vehicles = self._track_vehicles(frame, idx)
        for v in vehicles:
            st = self.states[v["track_id"]]
            self._read_plate(frame, v, st, idx)
            v["plate_box"] = self._plate_box(v, st, idx)
            v["result"] = self.voter.result(v["track_id"])
            v["crop"] = st.best_crop
            self._update_log(v["track_id"], st, v["result"], idx)
            if self.counter is not None:
                self.counter.update(v["track_id"], v["box"], st.vtype, idx, h)

        dt = max(1e-3, time.time() - t0)
        self.fps_ema = 1.0 / dt if self.fps_ema == 0 else 0.9 * self.fps_ema + 0.1 / dt
        if self.cfg.hud_mode == "dashboard":
            annotated = render_dashboard(frame, vehicles, self.log, self._stats(idx), self.cfg)
        else:
            lead_id = self.lead.pick(vehicles, w, h)
            lead = next((v for v in vehicles if v["track_id"] == lead_id), None)
            secondary = [v for v in vehicles if v is not lead]
            annotated = render_hud(frame.copy(), lead, secondary, self.fps_ema, len(self.states), self.cfg.hud_title)
        return annotated, vehicles

    def _stats(self, idx: int) -> Dict:
        c = self.counter
        return {"fps": self.fps_ema, "fps_video": self.fps, "time_sec": self.cfg.start_sec + idx / self.fps,
                "count": c.total if c else 0, "by_type": c.by_type() if c else {},
                "rate_per_min": c.rate_per_min(idx, self.fps) if c else 0.0,
                "plates": len(self.log), "confirmed": sum(e.result.status == CONFIRMED for e in self.log)}

    def summary(self, start_frame: int = 0) -> List[Dict]:
        """One row per vehicle in the plate log (duplicates merged)."""
        rows = []
        for e in sorted(self.log, key=lambda e: e.first_frame):
            r = e.result
            rows.append({"plate": r.text, "compact": r.compact, "confidence": r.confidence, "votes": r.votes,
                         "status": r.status, "vtype": e.vtype, "region": e.region,
                         "track_ids": sorted(e.track_ids), "first_frame": e.first_frame, "last_frame": e.last_frame,
                         "first_sec": round((start_frame + e.first_frame) / self.fps, 2),
                         "last_sec": round((start_frame + e.last_frame) / self.fps, 2)})
        return rows


def _same_plate(a: str, b: str) -> bool:
    """Two readings of one vehicle: identical, or a near-miss (<=2 edits, <=1 for short plates) - two
    different vehicles with near-identical plates within `merge_gap_sec` in one camera view are rare."""
    if a == b:
        return True
    return edit_distance(a, b) <= (1 if min(len(a), len(b)) <= 4 else 2)


def _contain(a, b) -> float:
    """Intersection area / area of the smaller box."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def _open_writer(path: str, fps: float, size: Tuple[int, int]) -> cv2.VideoWriter:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not writer.isOpened():
        raise RuntimeError(f"could not open a video writer for {path}")
    return writer


def to_h264(path: str) -> bool:
    """Re-encode an OpenCV 'mp4v' file to H.264 in place, so it plays in browsers / PowerPoint.
    Uses the ffmpeg binary bundled with imageio-ffmpeg when it is installed; otherwise a no-op."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    import subprocess
    tmp = os.path.splitext(path)[0] + ".h264.mp4"
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", path, "-c:v", "libx264",
           "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
    if subprocess.run(cmd).returncode != 0 or not os.path.exists(tmp):
        return False
    os.replace(tmp, path)
    return True


def run(cfg: AlprConfig) -> int:
    if not os.path.exists(cfg.source):
        print(f"Error: source not found: {cfg.source}")
        return 1
    cap = cv2.VideoCapture(cfg.source)
    if not cap.isOpened():
        print(f"Error: could not open {cfg.source}")
        return 1
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    crop = None
    if cfg.frame_crop:
        fx1, fy1, fx2, fy2 = cfg.frame_crop
        crop = (int(fx1 * width), int(fy1 * height), int(fx2 * width), int(fy2 * height))
        width, height = crop[2] - crop[0], crop[3] - crop[1]
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    start = int(cfg.start_sec * fps)
    total = int(cfg.duration * fps) if cfg.duration > 0 else int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - start
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    os.makedirs(os.path.dirname(cfg.out) or ".", exist_ok=True)
    out_size = tuple(cfg.dashboard_size) if cfg.hud_mode == "dashboard" else (width, height)
    writer = _open_writer(cfg.out, fps, out_size)

    print("=" * 64)
    print(f"SECS ALPR  profile={cfg.profile}  {width}x{height} @ {fps:.1f} FPS  ocr={cfg.ocr_backend}")
    print(f"window {cfg.start_sec}s -> {cfg.start_sec + total / fps:.1f}s ({total} frames) -> {cfg.out}")
    print("=" * 64)

    pipe = AlprPipeline(cfg, fps=fps)
    t_start, n = time.time(), 0
    while n < total:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        if crop:
            frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
        annotated, vehicles = pipe.process_frame(frame, n)
        writer.write(annotated)
        if n % 50 == 0:
            st = pipe._stats(n)
            last = pipe.log[-1].result if pipe.log else None
            print(f"[{n}/{total}] {n / fps:.1f}s | active {len(vehicles)} | counted {st['count']} | "
                  f"plates {st['plates']} | last {last.text + ' ' + last.status if last else '-'}")
    cap.release()
    writer.release()
    elapsed = max(1e-3, time.time() - t_start)
    h264 = to_h264(cfg.out)

    rows = pipe.summary(start)
    report = {"source": cfg.source, "profile": cfg.profile, "fps": fps, "frames": n,
              "window_sec": [cfg.start_sec, round(cfg.start_sec + n / fps, 2)], "plates": rows}
    if pipe.counter is not None:
        report["flow"] = {"count_line_y": cfg.count_line_y, "total": pipe.counter.total,
                          "by_type": pipe.counter.by_type(),
                          "per_10s": pipe.counter.time_series(fps, 10.0, cfg.start_sec)}
    json_path = os.path.splitext(cfg.out)[0] + ".plates.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("=" * 64)
    print(f"done: {n} frames in {elapsed:.1f}s ({n / elapsed:.1f} FPS)  video -> {cfg.out}"
          f" ({'H.264' if h264 else 'MPEG-4'})")
    for r in rows:
        print(f"  {r['first_sec']:>6.1f}s  {r['vtype']:<10} {r['plate']:<12} {r['status']:<10} "
              f"conf {r['confidence']:.0%}  reads {r['votes']:<3} {r['region']:<16} tracks {r['track_ids']}")
    if not rows:
        print("  (no plates read)")
    if pipe.counter is not None:
        print(f"flow: {pipe.counter.total} vehicles crossed the line {pipe.counter.by_type()}")
    print(f"results -> {json_path}")
    print("=" * 64)
    return 0
