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

    vehicle_model: str = "yolov8s.pt"
    plate_model: str = "models/plate_malaysia.pt"
    plate_model_fallback: str = ""     # optional 2nd plate detector, run only when the first finds nothing
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
    ocr_tta: bool = False                        # images: average OCR over several crop paddings
    ocr_max_readings: int = 15
    ocr_ref_plate_w: int = 100         # plate width (px) at which a reading gets full voting weight
    min_sharpness: float = 30.0
    sharp_ref: float = 300.0
    max_plate_corrections: int = 1
    resolve_ligatures: bool = True     # align visual ligatures like W <-> VJ in slanted plate fonts
    min_votes: int = 3
    confirm_conf: float = 0.6

    merge_gap_sec: float = 10.0        # same plate on two tracks within this gap = one vehicle
    log_min_status: str = "CONFIRMED"  # a vehicle enters the plate log / results once its plate reaches this
    count_line_y: float = 0.0          # traffic-flow counting line (fraction of frame height); 0 = off

    hud_mode: str = "lead"             # lead = dashcam AR card on the main target; dashboard = CCTV layout
    dashboard_size: List[int] = field(default_factory=lambda: [1920, 1080])
    lane_weight: float = 0.6
    lead_hysteresis: float = 1.25
    min_lead_w_frac: float = 0.104
    hud_title: str = "SECS AI VISION  |  MALAYSIA ALPR"


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
