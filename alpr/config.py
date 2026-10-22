"""Typed config loader: alpr/config.yaml -> AlprConfig (common + profile override, typo-safe)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import List

import yaml

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


@dataclass
class AlprConfig:
    profile: str = ""
    source: str = ""
    start_sec: float = 0.0
    duration: float = 0.0          # 0 = until the end of the video
    out: str = "outputs/alpr_out.mp4"
    frame_crop: List[float] = field(default_factory=list)   # [x1, y1, x2, y2] fractions; crop every frame
                                                           # first (removes recorder UI / overlays)
    frame_step: int = 1                # process every Nth frame (2 on 60 FPS footage = 30 FPS)
    congestion: dict = field(default_factory=dict)  # far / high cameras: zone occupancy from keyframes, see
                                       # alpr/congestion.py (edge_thresh, static_share, close_px, jam_occupancy)
    view_gate: dict = field(default_factory=dict)   # camera that shows several views in turn: analyse a frame only
                                       # when {template: image, box: [x1, y1, x2, y2] fractions} matches there
                                       # (e.g. the view's on-screen label; min_score 0.6); tracking restarts
                                       # each time the view comes back

    headless: bool = False             # no rendering, no video: only the results JSON (plates, flow, lane series)
    read_plates: bool = True           # False = vehicles, flow and lanes only (no plate detection / OCR)

    vehicle_model: str = "yolov8s.pt"
    plate_model: str = "models/plate_malaysia.pt"
    plate_model_fallback: str = ""     # optional 2nd plate detector, run only when the first finds nothing
    model_backend: str = "pt"          # pt | openvino: <name>_openvino_model/ next to each .pt (exported on first use;
                                       # the .pt is used when the export is missing and cannot be made)
    openvino_device: str = "CPU"       # CPU (FP32, same results as the .pt) | GPU (Intel iGPU, FP16) | NPU | AUTO
    imgsz: int = 640
    vehicle_classes: List[int] = field(default_factory=lambda: [2, 3, 5, 7])
    vehicle_conf: float = 0.35         # a track is shown once its best detection reaches this
    track_conf: float = 0.10           # detections passed to the tracker (low-score boxes keep tracks alive)
    tracker: str = ""                  # ByteTrack yaml; "" = alpr/trackers/bytetrack_alpr.yaml
    heavy_vehicle_conf: float = 0.55
    min_ground_y_frac: float = 0.0
    ignore_zones: List[List[float]] = field(default_factory=list)
    min_vehicle_w_frac: float = 0.078
    min_vehicle_h_frac: float = 0.074

    plate_conf: float = 0.30
    plate_imgsz: int = 480
    plate_aspect_min: float = 1.8
    plate_aspect_max: float = 6.0
    min_plate_w_px: int = 28
    min_plate_h_px: int = 10
    max_plate_w_frac: float = 0.50     # plate width / vehicle width ceiling (rejects watermark false-positives)
    max_plate_h_frac: float = 0.30     # plate height / vehicle height ceiling
    plate_det_interval: int = 2
    plate_ttl_frames: int = 15

    ocr_backend: str = "fast_plate_ocr"          # fast_plate_ocr | easyocr
    ocr_model: str = "cct-s-v2-global-model"     # fast-plate-ocr hub model
    ocr_threads: int = 2               # ONNX Runtime threads for the OCR; 0 = its default (all cores, spinning:
                                       # made the YOLO models 2-3x slower, TEAM_GUIDE.md section 11)
    ocr_tta: bool = False                        # images: average OCR over several crop paddings
    ocr_max_readings: int = 15
    ocr_ref_plate_w: int = 100         # plate width (px) at which a reading gets full voting weight
    min_sharpness: float = 30.0
    sharp_ref: float = 300.0
    max_plate_corrections: int = 1
    resolve_ligatures: bool = False    # W <-> VJ ligature fix for EasyOCR misreads (not needed with fast_plate_ocr)
    min_votes: int = 3
    confirm_conf: float = 0.6

    merge_gap_sec: float = 10.0        # same plate on two tracks within this gap = one vehicle
    log_min_status: str = "CONFIRMED"  # a vehicle enters the plate log / results once its plate reaches this
    count_line_y: float = 0.0          # traffic-flow counting line (fraction of frame height); 0 = off

    # lanes of a fixed camera (alpr/lanes.py) and driving-behaviour rules (alpr/behaviour.py). Points are
    # frame fractions; draw them with `python -m alpr.lane_tool --profile NAME`.
    lanes: List[dict] = field(default_factory=list)          # {name, group, direction: up|down, polygon}
    solid_lines: List[dict] = field(default_factory=list)    # {name, points}
    calibration: dict = field(default_factory=dict)          # {image_points, road_points (m), sigma_*}
    lane_vanishing_point: List[float] = field(default_factory=list)   # [x, y] where the lane lines meet
    lane_ref_shift: float = 0.0        # perspective correction of the bottom-centre point (0 = Paper 4 as is)
    lane_rules: List[str] = field(default_factory=lambda: [
        "LANE_CHANGE", "SOLID_LINE", "WRONG_WAY", "STOPPED", "SPEEDING", "TAILGATING"])
    lane_min_sec: float = 0.3          # a new lane counts once the vehicle has been in it this long ...
    lane_margin: float = 0.12          # ... clearly inside it (x box width past the boundary)
    smooth_sec: float = 0.2            # moving average of the bottom-centre point before the rules
    wrong_way_sec: float = 1.0         # WRONG_WAY: moved against the lane direction within this time ...
    wrong_way_min_disp: float = 0.8    # ... by at least this many box heights
    stopped_sec: float = 5.0           # STOPPED: standing still in a lane this long while traffic flows
    still_speed: float = 0.08          # box heights / s = standing still
    moving_speed: float = 0.6          # box heights / s = the carriageway is flowing (not a queue)
    speed_zone_y: List[float] = field(default_factory=lambda: [0.0, 1.0])   # rows where speed is measured
    speed_window_sec: float = 1.0      # each speed estimate is a straight-line fit over this much track
    speed_limit_kmh: float = 0.0       # SPEEDING (needs `calibration`); 0 = off
    tailgate_headway_sec: float = 1.0  # TAILGATING (needs `calibration`): headway at the count line below this
    tailgate_min_kmh: float = 50.0     # ... at or above this speed
    show_real_violations: bool = False  # public footage: violations of camera vehicles are counted, not shown
    behaviour_sim_tracks: List[dict] = field(default_factory=list)   # SIMULATED vehicles (demo of the rules)

    # plate verification against a (SIMULATED) vehicle registry, alpr/registry.py
    registry_enabled: bool = False
    registry_path: str = "data/registry/demo_registry.db"   # missing -> the demo registry is built there
    registry_rules: List[str] = field(default_factory=lambda: [
        "UNMATCHED", "EXPIRED", "ILLEGAL_FORMAT", "BLACKLIST", "TYPE_MISMATCH", "DUPLICATE"])
    registry_check_colour: bool = False     # TYPE_MISMATCH also compares an HSV colour estimate
    registry_max_speed_kmh: float = 250.0   # DUPLICATE: faster implied travel between cameras = cloned plate
    registry_misread_tolerance: bool = True  # 1 char off a registered plate = OCR misread, not UNMATCHED
    registry_sim_events: List[dict] = field(default_factory=list)   # SIMULATED sightings injected into the log
    video_start_time: str = ""         # wall-clock time of video second 0 (ISO); "" = now. Road-tax checks
    camera_name: str = ""
    camera_latlon: List[float] = field(default_factory=list)       # [lat, lon]; needed for the travel check

    hud_mode: str = "lead"             # lead = dashcam AR card on the main target; dashboard = CCTV layout
    dashboard_size: List[int] = field(default_factory=lambda: [1920, 1080])
    hud_banner: str = ""               # dashboard: disclosure strip over the camera view (e.g. SIMULATION ...)
    summary_sec: float = 0.0           # dashboard: append an end card listing every vehicle + verdict
    lane_weight: float = 0.6
    lead_hysteresis: float = 1.25
    min_lead_w_frac: float = 0.104
    hud_title: str = "SECS Vision · Malaysia ALPR"


def load_config(profile: str, path: str = DEFAULT_PATH) -> AlprConfig:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    profiles = raw.get("profiles", {})
    if profile not in profiles:
        raise ValueError(f"unknown profile '{profile}'; available: {', '.join(profiles) or 'none'}")
    merged = {**raw.get("common", {}), **(profiles[profile] or {})}
    known = {f.name for f in fields(AlprConfig)}
    unknown = set(merged) - known
    if unknown:
        raise ValueError(f"unknown config key(s) in {path}: {sorted(unknown)}")
    return AlprConfig(profile=profile, **merged)
