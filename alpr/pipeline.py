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

Lanes (``lanes`` in the profile, alpr/lanes.py + alpr/behaviour.py): every vehicle gets a smoothed
lane, the count line counts per lane, LaneMetrics adds occupancy and density, and the behaviour rules
raise lane events tied to the vehicle's plate log entry (its plate, once read). Violations of camera
vehicles are withheld on public footage; ``behaviour_sim_tracks`` adds SIMULATED vehicles that show
the violation rules (never counted in the flow).
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from .behaviour import BehaviourMonitor, LaneEvent, RuleParams, SimulatedTraffic
from .config import AlprConfig
from .evaluate import edit_distance
from .flow import LaneMetrics, LineCounter
from .hud import plate_image, render_dashboard, render_hud, render_summary
from .lanes import Calibration, LaneMap
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


def openvino_dir(weights: str) -> str:
    """Where `model.export(format="openvino")` puts the export of a .pt: <name>_openvino_model/ beside it."""
    return os.path.splitext(weights)[0] + "_openvino_model"


def load_detector(weights: str, backend: str = "pt", device: str = "CPU") -> Tuple[YOLO, Dict]:
    """A YOLO detector plus the extra arguments for its predict / track calls.

    backend "openvino": the OpenVINO export of the .pt (exported here on first use, dynamic input shape so
    the letterboxing is the same as the .pt's). On the CPU it runs in FP32 and gives the same detections
    as the .pt (measured, TEAM_GUIDE.md section 11). Falls back to the .pt when the export is missing and
    cannot be made (openvino not installed)."""
    if backend == "openvino":
        ov = openvino_dir(weights)
        if not os.path.isdir(ov):
            try:
                print(f"exporting {weights} to OpenVINO -> {ov}")
                YOLO(weights).export(format="openvino", dynamic=True, half=False, verbose=False)
            except Exception as e:                       # openvino missing, export error: keep the .pt
                print(f"OpenVINO export of {weights} failed ({e}); using the .pt")
        if os.path.isdir(ov):
            return YOLO(ov, task="detect"), {"device": f"intel:{device.lower()}"}
    elif backend != "pt":
        raise ValueError(f"model_backend must be 'pt' or 'openvino', not '{backend}'")
    return YOLO(weights), {}


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
    best_scene: Optional[Dict] = None   # evidence_dir: the frame of best_crop (JPEG bytes) + its boxes and frame number
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
    scene: Optional[Dict] = None                   # evidence_dir: the frame `crop` was cut from (TrackState.best_scene)
    colour: str = ""
    alerts: List[Alert] = field(default_factory=list)
    apart: Set[int] = field(default_factory=set)   # tracks seen in view at the same time: other vehicles
    simulated: bool = False                        # injected SIMULATED event (registry_sim_events)
    checked: tuple = ()                            # inputs of the last registry check


class AlprPipeline:
    def __init__(self, cfg: AlprConfig, ocr=None, fps: float = 25.0):
        self.cfg = cfg
        self.fps = fps
        self.vehicle_model, self._vehicle_args = load_detector(cfg.vehicle_model, cfg.model_backend, cfg.openvino_device)
        self.plate_model = self.plate_fallback = self.ocr = None
        self._plate_args: Dict = {}
        self._fallback_args: Dict = {}
        if cfg.read_plates:
            self.plate_model, self._plate_args = load_detector(cfg.plate_model, cfg.model_backend, cfg.openvino_device)
            if cfg.plate_model_fallback:
                self.plate_fallback, self._fallback_args = load_detector(cfg.plate_model_fallback, cfg.model_backend,
                                                                         cfg.openvino_device)
            self.ocr = ocr or make_ocr(cfg.ocr_backend, cfg.ocr_model, cfg.ocr_threads)
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
        self._id_offset = 0                 # added to tracker ids; raised by reset_tracking (rotating views)
        # lanes are built on the first frame (they need the frame size)
        self.lane_map: Optional[LaneMap] = None
        self.calibration: Optional[Calibration] = None
        self.behaviour: Optional[BehaviourMonitor] = None
        self.lane_metrics: Optional[LaneMetrics] = None
        self.sim_traffic: Optional[SimulatedTraffic] = None

    def _init_lanes(self, w: int, h: int) -> None:
        cfg = self.cfg
        self.lane_map = LaneMap.from_config(cfg.lanes, cfg.solid_lines, w, h, cfg.lane_vanishing_point,
                                            cfg.lane_ref_shift)
        self.calibration = Calibration.from_config(cfg.calibration, w, h) if cfg.calibration else None
        self.behaviour = BehaviourMonitor(self.lane_map, self.fps, RuleParams.from_config(cfg), self.calibration, h)
        zone = {}
        if self.calibration is not None:                       # lane zone length (m) for density in veh/km
            for lane in self.lane_map.lanes:
                y1, y2 = lane.y_range()
                a, b = lane.span_at(y1 + 1), lane.span_at(y2 - 1)
                if a and b:
                    zone[lane.name] = self.calibration.distance(((a[0] + a[1]) / 2, y1 + 1), ((b[0] + b[1]) / 2, y2 - 1))
        self.lane_metrics = LaneMetrics(self.lane_map.names(), self.fps, zone)
        self.sim_traffic = (SimulatedTraffic(cfg.behaviour_sim_tracks, self.calibration, w, h, self.fps, cfg.start_sec)
                            if cfg.behaviour_sim_tracks else None)

    def _lanes_frame(self, vehicles: List[Dict], idx: int, h: int) -> List[Dict]:
        """Lane + behaviour update for this frame. Sets v["lane"] on every vehicle and returns the
        SIMULATED vehicles of this frame (drawn, checked by the rules, never counted)."""
        sims = self.sim_traffic.vehicles(idx) if self.sim_traffic is not None else []
        for s in sims:
            s["result"] = PlateResult(s["plate"], normalize(s["plate"]), 1.0, 0, CONFIRMED) if s["plate"] else None
            s.update(plate_box=None, crop=None, alerts=[])
        new = self.behaviour.update(idx, vehicles + sims)
        plates = self._plate_by_track()
        for ev in new:
            ev.plate = next((s["plate"] for s in sims if s["track_id"] == ev.track_id), "") if ev.simulated \
                else ("" if ev.withheld else plates.get(ev.track_id, ""))
            t = self.cfg.start_sec + ev.frame / self.fps
            tag = " [SIMULATED]" if ev.simulated else (" [withheld: camera vehicle, public footage]" if ev.withheld else "")
            print(f"  EVENT {t:6.1f}s  {ev.code:<11} {ev.lane:<5} #{ev.track_id:<5} {ev.message}{tag}")
        line = self.cfg.count_line_y * h
        occupied, in_zone = {}, {}
        for v in vehicles + sims:
            v["lane"] = self.behaviour.lane_of(v["track_id"])
        for v in vehicles:
            raw = self.lane_map.lane_at(self.lane_map.ref_point(v["box"]))
            if raw is not None:
                in_zone[raw.name] = in_zone.get(raw.name, 0) + 1
            lane = v["lane"] or (raw.name if raw else None)
            if lane and v["box"][1] <= line <= v["box"][3]:
                occupied[lane] = True
        self.lane_metrics.update(idx, occupied, in_zone)
        return sims

    def _plate_by_track(self) -> Dict[int, str]:
        """Track id -> plate text of its plate-log entry (camera vehicles whose plate was read)."""
        return {t: e.result.text for e in self.log if not e.simulated for t in e.track_ids}

    def lane_events(self) -> List[LaneEvent]:
        """Visible lane events, plates refreshed (a plate read after the event still links to it)."""
        if self.behaviour is None:
            return []
        plates = self._plate_by_track()
        evs = self.behaviour.visible_events()
        for ev in evs:
            if not ev.simulated and not ev.plate:
                ev.plate = plates.get(ev.track_id, "")
        return evs

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
                                       agnostic_nms=True, verbose=False, **self._vehicle_args)[0]
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
            tid = int(tid) + self._id_offset
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

    def reset_tracking(self) -> None:
        """Start new tracks (a rotating camera came back to the view after showing others): the tracker
        is cleared and new track ids continue above every id used so far, because counting, lanes and
        plate voting are keyed by track id."""
        predictor = getattr(self.vehicle_model, "predictor", None)
        for tracker in (getattr(predictor, "trackers", None) or []):
            tracker.reset()
        self._id_offset = max(self.states, default=self._id_offset) + 1

    def _detect_plates(self, crops: List[np.ndarray], offsets: List[Tuple[int, int]],
                       frame_shape: Tuple[int, ...]) -> List[List[Tuple[Tuple[int, int, int, int], float]]]:
        """Plate candidates per vehicle crop (best first), all crops of a frame in one model call
        (~35% faster on CPU than one call per vehicle). The fallback model only sees the misses."""
        cands = self._plate_candidates(self.plate_model, crops, offsets, frame_shape, self._plate_args)
        if self.plate_fallback is not None:
            miss = [i for i, c in enumerate(cands) if not c]
            if miss:
                fb = self._plate_candidates(self.plate_fallback, [crops[i] for i in miss],
                                            [offsets[i] for i in miss], frame_shape, self._fallback_args)
                for i, c in zip(miss, fb):
                    cands[i] = c
        return cands

    def _plate_candidates(self, model, crops, offsets, frame_shape, model_args):
        if not crops:
            return []
        cfg = self.cfg
        H, W = frame_shape[:2]
        results = model.predict(crops, imgsz=cfg.plate_imgsz, conf=cfg.plate_conf, verbose=False, **model_args)
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
            crops = [self._crop(frame, (vx1 + b[0], vy1 + b[1], vx1 + b[2], vy1 + b[3])) for b in group]
            # OCR only when a reading would be taken from one of them: a far / blurry plate, or a vehicle
            # past its OCR budget, costs no OCR call here (the most confident box is shown instead)
            if any(self._wants_read(st, assess(c, cfg.min_plate_w_px, cfg.min_plate_h_px, cfg.min_sharpness,
                                               cfg.sharp_ref), b[2] - b[0]) for b, c in zip(group, crops)):
                options = []
                for b, c in zip(group, crops):
                    r = self.ocr.read_plate(c)
                    options.append(((r is not None and r.valid, r.conf if r else 0.0), b, r))
                _, chosen, read = max(options, key=lambda o: o[0])
        px1, py1, px2, py2 = chosen
        st.plate_rel, st.last_hit = (px1 / vw, py1 / vh, px2 / vw, py2 / vh), idx
        crop = self._crop(frame, (vx1 + px1, vy1 + py1, vx1 + px2, vy1 + py2))
        q = assess(crop, cfg.min_plate_w_px, cfg.min_plate_h_px, cfg.min_sharpness, cfg.sharp_ref)
        if q.score > st.best_crop_q:
            st.best_crop, st.best_crop_q = crop.copy(), q.score
            if cfg.evidence_dir:            # kept JPEG-encoded: one full frame per track would not fit in memory
                st.best_scene = {"jpg": cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes(),
                                 "frame": idx, "vehicle_box": [int(c) for c in v["box"]],
                                 "plate_box": [int(vx1 + px1), int(vy1 + py1), int(vx1 + px2), int(vy1 + py2)]}

        pw = px2 - px1
        if self._wants_read(st, q, pw):
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

    def _wants_read(self, st: TrackState, q, pw: int) -> bool:
        """Is a crop of quality `q` and width `pw` px read now? The OCR budget only bounds low-resolution
        reads (far plates are misread, and consistently so). Once a plate reaches full resolution it is
        read every time: those are the reads that count."""
        cfg = self.cfg
        return q.ok and (st.n_reads < cfg.ocr_max_readings or pw >= cfg.ocr_ref_plate_w
                         or pw >= st.best_read_w * REREAD_GROWTH or q.score > st.best_ocr_q + 0.1)

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
                mine.crop, mine.scene = st.best_crop, st.best_scene
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
        (or clears) an alert. When its twins (the same plate on a vehicle seen apart from it) change, the
        twins are re-checked too: a cloned plate is flagged on both vehicles even when one of them has
        left the view and gets no update of its own."""
        if self.checker is None or (not e.simulated and e.result.status != CONFIRMED):
            return
        twins = sorted(t for o in self.log if o is not e and not o.simulated and o.result.compact == e.result.compact
                       for t in o.track_ids & e.apart)
        key = (e.result.compact, e.result.valid, e.vtype, e.colour, tuple(twins))
        if key == e.checked:
            return
        changed = set(twins) ^ set(e.checked[4] if len(e.checked) == 5 else ())
        e.checked = key
        before = {a.code for a in e.alerts}
        e.alerts = self.checker.check(e.result.compact, valid=e.result.valid, when=self._clock(e.first_frame),
                                      vtype=e.vtype, colour=e.colour or None, coexisting_tracks=twins)
        for a in e.alerts:
            if a.code not in before:
                print(f"  ALERT {self.cfg.start_sec + e.first_frame / self.fps:6.1f}s  {e.result.text:<11} "
                      f"{a.code:<14} {a.message}{'  [SIMULATED]' if e.simulated else ''}")
        if changed:
            for o in self.log:
                if o is not e and not o.simulated and o.track_ids & changed:
                    self._check(o)

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
        if self.cfg.lanes and self.lane_map is None:
            self._init_lanes(w, h)
        vehicles = self._track_vehicles(frame, idx)
        sims = self._lanes_frame(vehicles, idx, h) if self.lane_map is not None else []
        boxes = {v["track_id"]: v["box"] for v in vehicles}
        # every pair of vehicles in view at once, clearly apart: two vehicles, even if one of them only
        # gets its plate read later (a cloned plate must not be merged as "the same car re-acquired")
        ids = sorted(boxes)
        for k, a in enumerate(ids):
            for b in ids[k + 1:]:
                if _contain(boxes[a], boxes[b]) < APART_MAX_OVERLAP:
                    self._note_apart(a, b, idx)
        # plate detection for all vehicles due this frame in one batched model call
        jobs = [(v, self.states[v["track_id"]]) for v in vehicles] if self.cfg.read_plates else []
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
                lane = None
                if self.lane_map is not None:
                    raw = self.lane_map.lane_at(self.lane_map.ref_point(v["box"]))
                    lane = v.get("lane") or (raw.name if raw else None)
                self.counter.update(v["track_id"], v["box"], st.vtype, idx, h, lane=lane)
        self._inject_sim_events(idx)
        alerts_by_track = {t: e.alerts for e in self.log for t in e.track_ids}
        for v in vehicles:
            v["alerts"] = alerts_by_track.get(v["track_id"], [])
        if self.behaviour is not None:                       # recent lane events, for the vehicle labels
            recent = [e for e in self.behaviour.visible_events() if idx - max(e.frame, e.emitted) <= 3 * self.fps]
            for v in vehicles + sims:
                v["events"] = [e for e in recent if e.track_id == v["track_id"]]

        if self.cfg.headless:                                # analysis only: nothing drawn
            return None, vehicles
        if self.cfg.hud_mode == "dashboard":
            annotated = render_dashboard(frame, vehicles + sims, self.log, self._stats(idx), self.cfg,
                                         lanes=self._lane_view(idx))
        else:
            lead_id = self.lead.pick(vehicles, w, h)
            lead = next((v for v in vehicles if v["track_id"] == lead_id), None)
            secondary = [v for v in vehicles if v is not lead]
            annotated = render_hud(frame.copy(), lead, secondary, self.fps_ema, len(self.states), self.cfg.hud_title)
        return annotated, vehicles

    def _lane_view(self, idx: int, session: bool = False) -> Optional[Dict]:
        """What the dashboard needs about the lanes: geometry, per-lane flow, events (None = no lanes).
        Live: rates over the last minute, occupancy / density / speed over the last 30 s / 5 vehicles.
        session=True (end card): everything over the whole session."""
        if self.lane_map is None:
            return None
        c = self.counter
        by_lane = c.by_lane() if c else {}
        win = max(1, idx) / self.fps if session else 30.0
        rows = []
        for lane in self.lane_map.lanes:
            dens, unit = self.lane_metrics.density(lane.name, window_sec=win)
            sp = self.behaviour.crossing_speeds(lane.name)
            sp = sp if session else sp[-5:]
            n = by_lane.get(lane.name, 0)
            rows.append({"name": lane.name, "group": lane.group, "direction": lane.direction, "polygon": lane.polygon,
                         "count": n, "rate": c.rate_per_min(idx, self.fps, lane=lane.name) if c else 0.0,
                         "veh_per_min": n * 60.0 / win if session else None,
                         "occupancy": self.lane_metrics.occupancy(lane.name, window_sec=win), "density": dens,
                         "density_unit": unit, "speed": sum(sp) / len(sp) if sp else None})
        if not session:
            for r in rows:
                r.pop("veh_per_min")
        return {"map": self.lane_map, "rows": rows, "events": self.lane_events(),
                "withheld": self.behaviour.withheld_counts(), "calibrated": self.calibration is not None,
                "simulated": self.sim_traffic is not None, "fps": self.fps, "t0": self.cfg.start_sec}

    def lane_report(self) -> Optional[Dict]:
        """Lanes section of the results JSON: per-lane totals, crossings (for alpr.lane_eval), per-lane
        10 s time series, visible events with plates, withheld counts, calibration checks."""
        if self.lane_map is None:
            return None
        c = self.counter
        fps, t0 = self.fps, self.cfg.start_sec
        speeds = {tid: v for tid, (v, _) in self.behaviour.final_speed.items() if tid not in self.behaviour.sim_ids}
        n_frames = max(1, len(self.lane_metrics._frames))
        per_lane = {}
        for lane in self.lane_map.lanes:
            dens, unit = self.lane_metrics.density(lane.name, window_sec=n_frames / fps)
            vs = [speeds[x.track_id] for x in (c.crossings if c else []) if x.lane == lane.name and x.track_id in speeds]
            per_lane[lane.name] = {
                "group": lane.group, "direction": lane.direction,
                "count": sum(1 for x in (c.crossings if c else []) if x.lane == lane.name),
                "by_type": dict(Counter(x.vtype for x in (c.crossings if c else []) if x.lane == lane.name)),
                "veh_per_min": round(sum(1 for x in (c.crossings if c else []) if x.lane == lane.name)
                                     * 60.0 * fps / n_frames, 2),
                "occupancy": round(self.lane_metrics.occupancy(lane.name, window_sec=n_frames / fps), 3),
                "density": round(dens, 2), "density_unit": unit,
                "zone_length_m": round(self.lane_metrics.zone_len_m.get(lane.name, 0.0), 1) or None,
                "mean_speed_kmh": round(sum(vs) / len(vs), 1) if vs else None}
        rep = {"lanes": [l.name for l in self.lane_map.lanes], "count_line_y": self.cfg.count_line_y,
               "ref_shift": self.lane_map.ref_shift, "per_lane": per_lane,
               "crossings": [{"time_sec": round(t0 + x.frame / fps, 2), "lane": x.lane or "", "vtype": x.vtype,
                              "track_id": x.track_id, "direction": x.direction} for x in (c.crossings if c else [])],
               "per_10s": self.lane_metrics.time_series(c, 10.0, t0, speeds) if c else {},
               "events": [e.to_dict(fps, t0) for e in self.lane_events()],
               "withheld_violations": self.behaviour.withheld_counts(),
               "withheld_note": "violations of camera vehicles on public footage: counted, not attributed"}
        if self.calibration is not None:
            rep["calibration"] = {"note": "speeds in km/h with 1-sigma error; scale from lane width 3.5 m + "
                                          "measured dash cycle", "speed_zone_y": list(self.cfg.speed_zone_y)}
        return rep

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

    def write_evidence(self, rows: List[Dict], out_dir: str, start_frame: int = 0) -> None:
        """evidence_dir: save each plate-log entry's best plate crop and the full frame it came from to `out_dir`
        and add their paths (+ frame number, time, vehicle and plate boxes in that frame) to its row of
        `summary()` as `evidence`. Rows whose vehicle never had a sharp enough crop get no `evidence`."""
        os.makedirs(out_dir, exist_ok=True)
        entries = sorted((e for e in self.log if not e.simulated), key=lambda e: e.first_frame)
        for i, (e, row) in enumerate(zip(entries, rows), 1):
            if e.crop is None or e.scene is None:
                continue
            stem = os.path.join(out_dir, f"{i:03d}_{e.result.compact}").replace("\\", "/")
            cv2.imwrite(stem + "_crop.jpg", e.crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
            with open(stem + "_scene.jpg", "wb") as f:
                f.write(e.scene["jpg"])
            row["evidence"] = {"crop": stem + "_crop.jpg", "scene": stem + "_scene.jpg", "frame": e.scene["frame"],
                               "time_sec": round((start_frame + e.scene["frame"]) / self.fps, 2),
                               "vehicle_box": e.scene["vehicle_box"], "plate_box": e.scene["plate_box"]}


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


class ViewGate:
    """Is a frame the wanted view of a camera that shows several views in turn (a live stream cycling
    through cameras)? The view is recognised by a template - e.g. its on-screen label, which looks the
    same day and night - found by normalised cross-correlation near `box` (frame fractions)."""

    def __init__(self, template: np.ndarray, box: Tuple[float, float, float, float], frame_wh: Tuple[int, int],
                 min_score: float = 0.6, margin: float = 0.02, binary_level: int = 0):
        w, h = frame_wh
        self.template = template if template.ndim == 2 else cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        x1, y1, x2, y2 = box
        self.region = (max(0, int((x1 - margin) * w)), max(0, int((y1 - margin) * h)),
                       min(w, int((x2 + margin) * w)), min(h, int((y2 + margin) * h)))
        self.min_score = min_score
        self.binary_level = binary_level     # > 0: compare only pixels brighter than this (white overlay text)
        if binary_level:
            self.template = np.where(self.template >= binary_level, 255, 0).astype(np.uint8)

    @classmethod
    def from_config(cls, gate: Dict, w: int, h: int) -> "ViewGate":
        path = gate["template"]
        if not os.path.isabs(path) and not os.path.exists(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", path)
        tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"view_gate template not found: {gate['template']}")
        return cls(tpl, tuple(gate["box"]), (w, h), float(gate.get("min_score", 0.6)), float(gate.get("margin", 0.02)),
                   int(gate.get("binary_level", 0)))

    def score(self, frame: np.ndarray) -> float:
        x1, y1, x2, y2 = self.region
        roi = frame[y1:y2, x1:x2]
        roi = roi if roi.ndim == 2 else cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        if self.binary_level:
            roi = np.where(roi >= self.binary_level, 255, 0).astype(np.uint8)
        th, tw = self.template.shape[:2]
        if roi.shape[0] < th or roi.shape[1] < tw:
            return 0.0
        return float(cv2.matchTemplate(roi, self.template, cv2.TM_CCOEFF_NORMED).max())

    def __call__(self, frame: np.ndarray) -> bool:
        return self.score(frame) >= self.min_score


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
    fps_in = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(cfg.frame_step))
    fps = fps_in / step                                      # processed frames per video second
    start = int(cfg.start_sec * fps_in)
    total = (int(cfg.duration * fps_in) if cfg.duration > 0 else int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - start) // step
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    os.makedirs(os.path.dirname(cfg.out) or ".", exist_ok=True)
    out_size = tuple(cfg.dashboard_size) if cfg.hud_mode == "dashboard" else (width, height)
    writer = None if cfg.headless else VideoOut(cfg.out, fps, out_size)

    print("=" * 64)
    print(f"SECS ALPR  profile={cfg.profile}  {width}x{height} @ {fps:.1f} FPS"
          + (f" (every {step}th of {fps_in:.0f})" if step > 1 else "")
          + (f"  ocr={cfg.ocr_backend}" if cfg.read_plates else "  plates off")
          + f"  models={cfg.model_backend}" + (f":{cfg.openvino_device}" if cfg.model_backend == "openvino" else "")
          + ("  headless" if cfg.headless else ""))
    print(f"window {cfg.start_sec}s -> {cfg.start_sec + total / fps:.1f}s ({total} frames) -> "
          + (os.path.splitext(cfg.out)[0] + ".plates.json" if cfg.headless else cfg.out))
    print("=" * 64)

    pipe = AlprPipeline(cfg, fps=fps)
    gate = ViewGate.from_config(cfg.view_gate, width, height) if cfg.view_gate else None
    in_view, n_in_view = True, 0
    t_start, n, last_frame = time.time(), 0, None
    while n < total:
        ok, frame = cap.read()
        if not ok:
            break
        for _ in range(step - 1):                              # skipped frames: decode-free grab
            cap.grab()
        n += 1
        if crop:
            frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
        if gate is not None:                                   # rotating camera: another view is on screen
            if not gate(frame):
                in_view = False
                continue
            if not in_view:
                pipe.reset_tracking()
                in_view = True
            n_in_view += 1
        last_frame = frame
        annotated, vehicles = pipe.process_frame(frame, n)
        if writer is not None:
            writer.write(annotated)
        if n % 50 == 0:
            st = pipe._stats(n)
            last = next((e.result for e in reversed(pipe.log) if not e.simulated), None)
            print(f"[{n}/{total}] {n / fps:.1f}s | active {len(vehicles)} | counted {st['count']} | "
                  f"plates {st['plates']} | last {last.text + ' ' + last.status if last else '-'}")
    cap.release()
    elapsed = max(1e-3, time.time() - t_start)                 # frames only (the end card is not timed)
    n_run = n_in_view if gate is not None else n               # frames that went through the models
    if writer is not None and cfg.hud_mode == "dashboard" and cfg.summary_sec > 0:   # end card: every vehicle
        card = render_summary(pipe.log, pipe._stats(n), cfg, frame=last_frame, lanes=pipe._lane_view(n, session=True))
        for _ in range(int(cfg.summary_sec * fps)):
            writer.write(card)
    if writer is not None:
        writer.close()

    start = int(cfg.start_sec * fps)                           # in processed frames
    rows = pipe.summary(start)
    if cfg.uncertain_dir:
        from .diagnostics import write_uncertain_diagnostics
        diagnostic_path = write_uncertain_diagnostics(pipe.states, pipe.voter, pipe.log,
                                                      cfg.uncertain_dir, start_frame=start, fps=fps)
        print(f"  FR03 uncertain diagnostics -> {diagnostic_path}")
    if cfg.evidence_dir:
        pipe.write_evidence(rows, cfg.evidence_dir, start)
    report = {"source": cfg.source, "profile": cfg.profile, "fps": fps, "frames": n,
              "window_sec": [cfg.start_sec, round(cfg.start_sec + n / fps, 2)], "plates": rows,
              "processing": {"model_backend": "openvino" if pipe._vehicle_args else "pt",
                             "openvino_device": cfg.openvino_device if pipe._vehicle_args else None,
                             "headless": cfg.headless, "read_plates": cfg.read_plates,
                             "frames_per_sec": round(n_run / elapsed, 2)}}
    if gate is not None:                                       # other views are read and skipped, not analysed
        report["view_gate"] = {"frames_in_view": n_in_view, "frames": n, "template": cfg.view_gate.get("template"),
                               "video_frames_per_sec": round(n / elapsed, 2)}
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
    lanes = pipe.lane_report()
    if lanes is not None:
        report["lanes"] = lanes
    json_path = os.path.splitext(cfg.out)[0] + ".plates.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("=" * 64)
    print(f"done: {n} frames in {elapsed:.1f}s ({n_run / elapsed:.1f} FPS)"
          + (f"  view gate: {n_in_view} of {n} frames analysed, video read at {n / elapsed:.1f} FPS"
             if gate is not None else "")
          + (f"  video -> {cfg.out} ({writer.codec})" if writer is not None else "  (headless: no video)"))
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
    if lanes is not None:
        for name, r in lanes["per_lane"].items():
            sp = f"  {r['mean_speed_kmh']:.0f} km/h" if r["mean_speed_kmh"] else ""
            print(f"  lane {name:<5} {r['count']:>4} veh  {r['veh_per_min']:5.1f}/min  occupancy {r['occupancy']:.0%}  "
                  f"density {r['density']:.1f} {r['density_unit']}{sp}")
        print(f"lane events shown: {len(lanes['events'])}  withheld (camera vehicles): {lanes['withheld_violations']}")
    print(f"results -> {json_path}")
    print("=" * 64)
    return 0
