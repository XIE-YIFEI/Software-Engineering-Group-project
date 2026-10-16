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

Registry checks (``registry_enabled``, alpr/registry.py): a plate-log entry is checked whenever its
CONFIRMED reading or vehicle type changes, and its alerts go to the dashboard and *.plates.json. Two
vehicles in view at once, clearly apart, are never merged into one entry; if they carry the identical
plate that is a DUPLICATE (cloned plate). ``registry_sim_events`` adds SIMULATED fictitious sightings
to the log (demo only; kept apart from the camera's plates in the JSON).
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from .config import AlprConfig
from .evaluate import edit_distance
from .flow import LineCounter
from .hud import plate_image, render_dashboard, render_hud, render_summary
from .lead import LeadSelector
from .ocr import make_ocr
from .plates import normalize, parse_plate
from .quality import assess
from .registry import Alert, PlateChecker, estimate_colour
from .voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateResult, PlateVoter

VEHICLE_NAMES = {2: "Car", 3: "Motorcycle", 5: "Bus", 7: "Truck"}
HEAVY = (5, 7)
REREAD_GROWTH = 1.10      # keep reading past the OCR budget whenever the plate is >=10% wider than before
NONSTD_MIN_W_FRAC = 0.8   # non-standard (illegal-format) readings count from 80% of ocr_ref_plate_w
STATUS_RANK = {ACQUIRING: 0, TENTATIVE: 1, CONFIRMED: 2}
TRACKER_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trackers", "bytetrack_alpr.yaml")
APART_MAX_OVERLAP = 0.3   # two boxes overlapping less than this (of the smaller box) are two vehicles
APART_MIN_FRAMES = 5      # ... seen like that in this many frames: never merged; identical plate = DUPLICATE


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
    colour_votes: Dict[str, int] = field(default_factory=lambda: defaultdict(int))   # registry_check_colour

    @property
    def colour(self) -> str:
        return max(self.colour_votes, key=self.colour_votes.get) if self.colour_votes else ""

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
    colour: str = ""
    alerts: List[Alert] = field(default_factory=list)
    apart: Set[int] = field(default_factory=set)   # tracks seen in view at the same time: other vehicles
    simulated: bool = False                        # injected SIMULATED event (registry_sim_events)
    checked: tuple = ()                            # inputs of the last registry check


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
        self.checker = PlateChecker.from_config(cfg) if cfg.registry_enabled else None
        self.clock0 = (datetime.fromisoformat(cfg.video_start_time).replace(tzinfo=None) if cfg.video_start_time
                       else datetime.now())
        self._sim_pending = sorted(cfg.registry_sim_events, key=lambda ev: float(ev["at_sec"])) if self.checker else []
        self._reset_apart()
        self._last_t: Optional[float] = None

    def _reset_apart(self) -> None:
        """Co-visibility bookkeeping: for each track pair, in how many frames both were in view and
        clearly apart (counts, not frame lists: constant memory per pair on long videos), and per track
        the tracks it has been seen apart from >= APART_MIN_FRAMES times (O(1) lookup per update)."""
        self._together: Dict[Tuple[int, int], int] = defaultdict(int)
        self._together_last: Dict[Tuple[int, int], int] = {}
        self._apart_of: Dict[int, Set[int]] = defaultdict(set)

    def _note_apart(self, a: int, b: int, idx: int) -> None:
        pair = (min(a, b), max(a, b))
        if self._together_last.get(pair) == idx:          # counted once per frame
            return
        self._together_last[pair] = idx
        self._together[pair] += 1
        if self._together[pair] == APART_MIN_FRAMES:
            self._apart_of[a].add(b)
            self._apart_of[b].add(a)

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

    def _detect_plates(self, crops: List[np.ndarray], offsets: List[Tuple[int, int]],
                       frame_shape: Tuple[int, ...]) -> List[List[Tuple[Tuple[int, int, int, int], float]]]:
        """Plate candidates per vehicle crop (best first), all crops of a frame in one model call
        (~35% faster on CPU than one call per vehicle). The fallback model only sees the misses."""
        cands = self._plate_candidates(self.plate_model, crops, offsets, frame_shape)
        if self.plate_fallback is not None:
            miss = [i for i, c in enumerate(cands) if not c]
            if miss:
                fb = self._plate_candidates(self.plate_fallback, [crops[i] for i in miss],
                                            [offsets[i] for i in miss], frame_shape)
                for i, c in zip(miss, fb):
                    cands[i] = c
        return cands

    def _plate_candidates(self, model, crops, offsets, frame_shape):
        if not crops:
            return []
        cfg = self.cfg
        H, W = frame_shape[:2]
        results = model.predict(crops, imgsz=cfg.plate_imgsz, conf=cfg.plate_conf, verbose=False)
        out = []
        for crop, (ox, oy), res in zip(crops, offsets, results):
            vh, vw = crop.shape[:2]
            cands = []
            for b in (res.boxes if res.boxes is not None else []):
                px1, py1, px2, py2 = [int(v) for v in b.xyxy[0].tolist()]
                pw, ph = px2 - px1, py2 - py1
                if pw < cfg.min_plate_w_px or ph < cfg.min_plate_h_px:
                    continue
                if pw > cfg.max_plate_w_frac * vw or ph > cfg.max_plate_h_frac * vh:
                    continue
                if not cfg.plate_aspect_min <= pw / max(1, ph) <= cfg.plate_aspect_max:
                    continue
                cx, cy = (ox + (px1 + px2) / 2) / W, (oy + (py1 + py2) / 2) / H
                if any(z[0] <= cx <= z[2] and z[1] <= cy <= z[3] for z in cfg.ignore_zones):
                    continue                                    # watermark / overlay text, not a plate
                cands.append(((px1, py1, px2, py2), float(b.conf[0])))
            out.append(sorted(cands, key=lambda c: -c[1]))
        return out

    # ------------------------------------------------------------ plate reading
    @staticmethod
    def _crop(frame: np.ndarray, box) -> np.ndarray:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        padx, pady = int(0.04 * (x2 - x1)) + 2, int(0.10 * (y2 - y1)) + 1
        return frame[max(0, y1 - pady):min(h, y2 + pady), max(0, x1 - padx):min(w, x2 + padx)]

    def _plate_job(self, frame: np.ndarray, v: Dict, st: TrackState, idx: int):
        """The vehicle box (clipped) if plate detection should run on this vehicle this frame."""
        cfg = self.cfg
        h, w = frame.shape[:2]
        vx1, vy1, vx2, vy2 = max(0, v["box"][0]), max(0, v["box"][1]), min(w, v["box"][2]), min(h, v["box"][3])
        if vx2 - vx1 < cfg.min_vehicle_w_frac * w or vy2 - vy1 < cfg.min_vehicle_h_frac * h:
            return None
        if idx - st.last_det < cfg.plate_det_interval:
            return None
        st.last_det = idx
        if self.checker is not None and self.checker.check_colour:
            colour = estimate_colour(frame[vy1:vy2, vx1:vx2])
            if colour:
                st.colour_votes[colour] += 1
        return vx1, vy1, vx2, vy2

    def _read_plate(self, frame: np.ndarray, v: Dict, st: TrackState, idx: int, vbox, cands) -> None:
        cfg = self.cfg
        vx1, vy1, vx2, vy2 = vbox
        vw, vh = vx2 - vx1, vy2 - vy1
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
            # A reading the OCR marks as no legal plate counts as evidence of a non-standard plate only
            # at (nearly) full resolution: far, blurry plates are often read as junk.
            if read and (read.valid or pw >= NONSTD_MIN_W_FRAC * cfg.ocr_ref_plate_w):
                # a far plate (few pixels) is misread consistently, so its vote counts with resolution^2
                weight_q = q.score * min(1.0, pw / cfg.ocr_ref_plate_w) ** 2
                self.voter.add(v["track_id"], read.text, read.conf, weight_q, valid=read.valid)

    def _plate_box(self, v: Dict, st: TrackState, idx: int):
        if st.plate_rel is None or idx - st.last_hit > self.cfg.plate_ttl_frames:
            return None
        x1, y1, x2, y2 = v["box"]
        vw, vh = x2 - x1, y2 - y1
        fx1, fy1, fx2, fy2 = st.plate_rel
        return (int(x1 + fx1 * vw), int(y1 + fy1 * vh), int(x1 + fx2 * vw), int(y1 + fy2 * vh))

    # ------------------------------------------------------------ plate log
    def _update_log(self, tid: int, st: TrackState, result: Optional[PlateResult], idx: int,
                    boxes: Optional[Dict[int, List[int]]] = None) -> None:
        """boxes: {track_id: box} of the vehicles in view this frame; tells a re-acquired vehicle from
        a second vehicle with the same (or a near-identical) plate."""
        mine = next((e for e in self.log if tid in e.track_ids), None)
        if mine is not None and len(mine.track_ids) > 1:
            # merged as "the same vehicle" while the two boxes still overlapped (one car behind the
            # other), but seen apart since: two vehicles. The one that appeared later leaves the row
            # (and gets its own on its next update).
            for t in [t for t in mine.track_ids if t != tid and t in self._apart_of.get(tid, ())]:
                other = self.states.get(t)
                later = t if other is not None and other.first_frame > st.first_frame else tid
                mine.track_ids.discard(later)
                mine.checked = ()
            if not mine.track_ids:
                self.log.remove(mine)
            if tid not in mine.track_ids:
                mine = None
        if result is None or (mine is None and STATUS_RANK[result.status] < STATUS_RANK[self.cfg.log_min_status]):
            return
        gap = self.cfg.merge_gap_sec * self.fps
        apart = (mine.apart if mine else set()) | self._apart_of.get(tid, set())
        same = next((e for e in self.log if e is not mine and not e.simulated and not (e.track_ids & apart)
                     and _same_plate(e.result.compact, result.compact) and idx - e.last_frame <= gap), None)
        if same is not None and self._in_view_apart(same, tid, boxes or {}, idx):
            same = None                            # that vehicle is in view elsewhere: this is another one
        if same is not None:                       # same plate seen recently: the same vehicle re-acquired
            if mine is not None:
                same.track_ids |= mine.track_ids
                same.apart |= mine.apart
                self.log.remove(mine)
            mine = same
        elif mine is None:
            mine = LogEntry(result, st.vtype, "", st.first_frame, idx)
            self.log.append(mine)
        mine.track_ids.add(tid)
        mine.last_frame = idx
        new_apart = apart - mine.apart - mine.track_ids
        if new_apart:                              # "different vehicles", recorded on both sides
            mine.apart |= new_apart
            for e in self.log:
                if e is not mine and e.track_ids & new_apart:
                    e.apart |= mine.track_ids
                    self._check(e)
        if len(mine.track_ids) == 1 or result.votes >= mine.result.votes:   # best-supported reading wins
            parse = parse_plate(result.compact, 0)
            mine.result, mine.vtype = result, st.vtype
            mine.region = parse.region if parse and result.valid else ""      # no state for an illegal plate
            mine.colour = st.colour
            if st.best_crop is not None:
                mine.crop = st.best_crop
        self._check(mine)

    def _in_view_apart(self, entry: LogEntry, tid: int, boxes: Dict[int, List[int]], idx: int) -> bool:
        """Is a vehicle of `entry` in view now, clearly apart from vehicle `tid`? (counted per track pair)"""
        box = boxes.get(tid)
        found = False
        for t in entry.track_ids:
            if t != tid and box is not None and t in boxes and _contain(boxes[t], box) < APART_MAX_OVERLAP:
                self._note_apart(t, tid, idx)
                found = True
        return found

    # ------------------------------------------------------------ registry checks
    def _clock(self, idx: int) -> datetime:
        return self.clock0 + timedelta(seconds=self.cfg.start_sec + idx / self.fps)

    def _check(self, e: LogEntry) -> None:
        """(Re)run the registry rules on a log entry when its plate, type, colour or twins changed. A
        camera plate is only checked while its reading is CONFIRMED, so a passing misread never raises
        (or clears) an alert."""
        if self.checker is None or (not e.simulated and e.result.status != CONFIRMED):
            return
        twins = sorted(t for o in self.log if o is not e and not o.simulated and o.result.compact == e.result.compact
                       for t in o.track_ids & e.apart)
        key = (e.result.compact, e.result.valid, e.vtype, e.colour, tuple(twins))
        if key == e.checked:
            return
        e.checked = key
        before = {a.code for a in e.alerts}
        e.alerts = self.checker.check(e.result.compact, valid=e.result.valid, when=self._clock(e.first_frame),
                                      vtype=e.vtype, colour=e.colour or None, coexisting_tracks=twins)
        for a in e.alerts:
            if a.code not in before:
                print(f"  ALERT {self.cfg.start_sec + e.first_frame / self.fps:6.1f}s  {e.result.text:<11} "
                      f"{a.code:<14} {a.message}{'  [SIMULATED]' if e.simulated else ''}")

    def _inject_sim_events(self, idx: int) -> None:
        """Add the SIMULATED sightings due by this frame (fictitious plates that demonstrate the alerts)."""
        t = self.cfg.start_sec + idx / self.fps
        while self._sim_pending and float(self._sim_pending[0]["at_sec"]) <= t:
            ev = self._sim_pending.pop(0)
            if float(ev["at_sec"]) < self.cfg.start_sec:
                continue                                   # before this window
            parse = parse_plate(str(ev["plate"]), 0)
            text, compact = (parse.text, parse.compact) if parse else (normalize(str(ev["plate"])),) * 2
            e = LogEntry(PlateResult(text, compact, 1.0, 0, CONFIRMED), ev.get("vtype", "Car"),
                         parse.region if parse else "", idx, idx, crop=plate_image(text),
                         colour=ev.get("colour", ""), simulated=True)
            self.log.append(e)
            self._check(e)

    # ------------------------------------------------------------ per frame
    def process_frame(self, frame: np.ndarray, idx: int) -> Tuple[np.ndarray, List[Dict]]:
        now = time.time()                       # FPS = full frame rate (detection + rendering + video I/O)
        if self._last_t is not None:
            dt = max(1e-3, now - self._last_t)
            self.fps_ema = 1.0 / dt if self.fps_ema == 0 else 0.9 * self.fps_ema + 0.1 / dt
        self._last_t = now
        h, w = frame.shape[:2]
        vehicles = self._track_vehicles(frame, idx)
        boxes = {v["track_id"]: v["box"] for v in vehicles}
        # every pair of vehicles in view at once, clearly apart: two vehicles, even if one of them only
        # gets its plate read later (a cloned plate must not be merged as "the same car re-acquired")
        ids = sorted(boxes)
        for k, a in enumerate(ids):
            for b in ids[k + 1:]:
                if _contain(boxes[a], boxes[b]) < APART_MAX_OVERLAP:
                    self._note_apart(a, b, idx)
        # plate detection for all vehicles due this frame in one batched model call
        jobs = [(v, self.states[v["track_id"]]) for v in vehicles]
        jobs = [(v, st, b) for v, st in jobs if (b := self._plate_job(frame, v, st, idx)) is not None]
        cands = self._detect_plates([frame[b[1]:b[3], b[0]:b[2]] for _, _, b in jobs],
                                    [(b[0], b[1]) for _, _, b in jobs], frame.shape)
        for (v, st, b), c in zip(jobs, cands):
            self._read_plate(frame, v, st, idx, b, c)
        for v in vehicles:
            st = self.states[v["track_id"]]
            v["plate_box"] = self._plate_box(v, st, idx)
            v["result"] = self.voter.result(v["track_id"])
            v["crop"] = st.best_crop
            self._update_log(v["track_id"], st, v["result"], idx, boxes)
            if self.counter is not None:
                self.counter.update(v["track_id"], v["box"], st.vtype, idx, h)
        self._inject_sim_events(idx)
        alerts_by_track = {t: e.alerts for e in self.log for t in e.track_ids}
        for v in vehicles:
            v["alerts"] = alerts_by_track.get(v["track_id"], [])

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
        camera = [e for e in self.log if not e.simulated]
        return {"fps": self.fps_ema, "fps_video": self.fps, "time_sec": self.cfg.start_sec + idx / self.fps,
                "count": c.total if c else 0, "by_type": c.by_type() if c else {},
                "rate_per_min": c.rate_per_min(idx, self.fps) if c else 0.0,
                "plates": len(camera), "confirmed": sum(e.result.status == CONFIRMED for e in camera),
                "alerts": sum(len(e.alerts) for e in self.log) if self.checker else None,
                "valid": sum(e.result.status == CONFIRMED and not e.alerts for e in camera) if self.checker else None,
                "flagged": sum(bool(e.alerts) for e in self.log) if self.checker else None}

    def summary(self, start_frame: int = 0) -> List[Dict]:
        """One row per vehicle in the plate log (duplicates merged); SIMULATED events are not included."""
        rows = []
        for e in sorted((e for e in self.log if not e.simulated), key=lambda e: e.first_frame):
            r = e.result
            row = {"plate": r.text, "compact": r.compact, "confidence": r.confidence, "votes": r.votes,
                   "status": r.status, "valid_format": r.valid, "vtype": e.vtype, "region": e.region,
                   "track_ids": sorted(e.track_ids), "first_frame": e.first_frame, "last_frame": e.last_frame,
                   "first_sec": round((start_frame + e.first_frame) / self.fps, 2),
                   "last_sec": round((start_frame + e.last_frame) / self.fps, 2)}
            if self.checker is not None:
                row["alerts"] = [a.to_dict() for a in e.alerts]
            rows.append(row)
        return rows

    def simulated_events(self, start_frame: int = 0) -> List[Dict]:
        """The injected SIMULATED sightings (fictitious plates) and their alerts."""
        return [{"plate": e.result.text, "compact": e.result.compact, "vtype": e.vtype, "simulated": True,
                 "time_sec": round((start_frame + e.first_frame) / self.fps, 2),
                 "alerts": [a.to_dict() for a in e.alerts]}
                for e in sorted((e for e in self.log if e.simulated), key=lambda e: e.first_frame)]


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


class VideoOut:
    """Annotated video writer. H.264 straight through the ffmpeg binary bundled with imageio-ffmpeg
    (one encode; plays in browsers / PowerPoint); OpenCV MPEG-4 when imageio-ffmpeg is missing."""

    def __init__(self, path: str, fps: float, size: Tuple[int, int], crf: int = 20):
        self.w, self.h = size[0] // 2 * 2, size[1] // 2 * 2         # yuv420p needs even dimensions
        try:
            import imageio_ffmpeg
            self._ff = imageio_ffmpeg.write_frames(path, (self.w, self.h), fps=fps, codec="libx264", quality=None,
                                                   pix_fmt_out="yuv420p", macro_block_size=1,
                                                   output_params=["-crf", str(crf), "-preset", "medium",
                                                                  "-movflags", "+faststart"])
            self._ff.send(None)
            self._cv, self.codec = None, "H.264"
        except ImportError:
            self._ff = None
            self._cv = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (self.w, self.h))
            if not self._cv.isOpened():
                raise RuntimeError(f"could not open a video writer for {path}")
            self.codec = "MPEG-4"

    def write(self, bgr: np.ndarray) -> None:
        bgr = bgr[:self.h, :self.w]
        if self._ff is not None:
            self._ff.send(np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
        else:
            self._cv.write(np.ascontiguousarray(bgr))

    def close(self) -> None:
        if self._ff is not None:
            self._ff.close()
        else:
            self._cv.release()


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
    writer = VideoOut(cfg.out, fps, out_size)

    print("=" * 64)
    print(f"SECS ALPR  profile={cfg.profile}  {width}x{height} @ {fps:.1f} FPS  ocr={cfg.ocr_backend}")
    print(f"window {cfg.start_sec}s -> {cfg.start_sec + total / fps:.1f}s ({total} frames) -> {cfg.out}")
    print("=" * 64)

    pipe = AlprPipeline(cfg, fps=fps)
    t_start, n, last = time.time(), 0, None
    while n < total:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        if crop:
            frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
        last = frame
        annotated, vehicles = pipe.process_frame(frame, n)
        writer.write(annotated)
        if n % 50 == 0:
            st = pipe._stats(n)
            last = next((e.result for e in reversed(pipe.log) if not e.simulated), None)
            print(f"[{n}/{total}] {n / fps:.1f}s | active {len(vehicles)} | counted {st['count']} | "
                  f"plates {st['plates']} | last {last.text + ' ' + last.status if last else '-'}")
    cap.release()
    if cfg.hud_mode == "dashboard" and cfg.summary_sec > 0:      # end card: every vehicle + verdict
        card = render_summary(pipe.log, pipe._stats(n), cfg, frame=last)
        for _ in range(int(cfg.summary_sec * fps)):
            writer.write(card)
    writer.close()
    elapsed = max(1e-3, time.time() - t_start)

    rows = pipe.summary(start)
    report = {"source": cfg.source, "profile": cfg.profile, "fps": fps, "frames": n,
              "window_sec": [cfg.start_sec, round(cfg.start_sec + n / fps, 2)], "plates": rows}
    if pipe.counter is not None:
        report["flow"] = {"count_line_y": cfg.count_line_y, "total": pipe.counter.total,
                          "by_type": pipe.counter.by_type(),
                          "per_10s": pipe.counter.time_series(fps, 10.0, cfg.start_sec)}
    if pipe.checker is not None:
        sims = pipe.simulated_events(start)
        report["registry"] = {"path": cfg.registry_path, "rules": sorted(pipe.checker.rules),
                              "disclaimer": pipe.checker.registry.get_meta("disclaimer") or "",
                              "alerts": sum(len(r["alerts"]) for r in rows),
                              "simulated_alerts": sum(len(ev["alerts"]) for ev in sims)}
        report["simulated_events"] = sims
    json_path = os.path.splitext(cfg.out)[0] + ".plates.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("=" * 64)
    print(f"done: {n} frames in {elapsed:.1f}s ({n / elapsed:.1f} FPS)  video -> {cfg.out} ({writer.codec})")
    for r in rows:
        print(f"  {r['first_sec']:>6.1f}s  {r['vtype']:<10} {r['plate']:<12} {r['status']:<10} "
              f"conf {r['confidence']:.0%}  reads {r['votes']:<3} {r['region']:<16} tracks {r['track_ids']}"
              + "".join(f"  ! {a['code']}" for a in r.get("alerts", [])))
    if not rows:
        print("  (no plates read)")
    for ev in report.get("simulated_events", []):
        print(f"  {ev['time_sec']:>6.1f}s  SIMULATED  {ev['plate']:<12} "
              + ("  ".join(f"! {a['code']}: {a['message']}" for a in ev["alerts"]) or "no alerts"))
    if pipe.counter is not None:
        print(f"flow: {pipe.counter.total} vehicles crossed the line {pipe.counter.by_type()}")
    print(f"results -> {json_path}")
    print("=" * 64)
    return 0
