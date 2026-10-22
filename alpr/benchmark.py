"""Speed benchmark: frames per second of the video pipeline and where the time goes, per stage.

    python -m alpr.benchmark --profile malaysia_road_anpr --frames 300
    python -m alpr.benchmark --profile malaysia_selayang_lanes --frames 300 --configs pt,openvino-headless

Each configuration runs the same frames (the profile's window, from its start) through AlprPipeline:
  original            as before the speed-up: .pt models, rendered + encoded, ONNX Runtime default threads
  pt                  the .pt models with the dashboard rendered and the video encoded (OCR on 2 threads)
  pt-headless         the .pt models, nothing rendered or encoded (JSON only)
  openvino            OpenVINO models (CPU, FP32), rendered + encoded
  openvino-headless   OpenVINO models, JSON only
Stages are timed by wrapping the pipeline's own methods (decode, vehicle detection + tracking, plate
detection, OCR, rendering, video encoding; "other" = lanes, voting, plate log). The first
`--warmup` frames are processed but not timed (model compilation, first-call allocations).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import defaultdict
from typing import Dict

import cv2
import numpy as np

from . import pipeline as P
from .config import DEFAULT_PATH, load_config

CONFIGS = {"original": ("pt", False, 0),          # as before this change: ONNX Runtime default threads
           "pt": ("pt", False, 2), "pt-headless": ("pt", True, 2),
           "openvino": ("openvino", False, 2), "openvino-headless": ("openvino", True, 2)}
STAGES = ["decode", "vehicles", "plates", "ocr", "render", "encode", "other"]


class _Timer:
    def __init__(self):
        self.t: Dict[str, float] = defaultdict(float)
        self.on = False

    def wrap(self, obj, name: str, stage: str):
        fn = getattr(obj, name)

        def timed(*a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                if self.on:
                    self.t[stage] += time.perf_counter() - t0
        setattr(obj, name, timed)


def bench(cfg, n_frames: int, warmup: int, ov_device: str = "CPU") -> Dict:
    cap = cv2.VideoCapture(cfg.source)
    if not cap.isOpened():
        raise FileNotFoundError(cfg.source)
    fps_in = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(cfg.frame_step))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(cfg.start_sec * fps_in))
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    crop = None
    if cfg.frame_crop:
        fx1, fy1, fx2, fy2 = cfg.frame_crop
        crop = (int(fx1 * W), int(fy1 * H), int(fx2 * W), int(fy2 * H))
    T = _Timer()
    pipe = P.AlprPipeline(cfg, fps=fps_in / step)
    T.wrap(pipe, "_track_vehicles", "vehicles")
    T.wrap(pipe, "_detect_plates", "plates")
    if pipe.ocr is not None:
        T.wrap(pipe.ocr, "read_plate", "ocr")
    originals = {name: getattr(P, name) for name in ("render_dashboard", "render_hud")}
    for name in originals:                                         # module-level names used by process_frame
        T.wrap(P, name, "render")
    writer = None
    tmp = None
    if not cfg.headless:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
        size = tuple(cfg.dashboard_size) if cfg.hud_mode == "dashboard" else (crop[2] - crop[0], crop[3] - crop[1]) \
            if crop else (W, H)
        writer = P.VideoOut(tmp, fps_in / step, size)
    total = 0.0
    n = 0
    try:
        while n < warmup + n_frames:
            T.on = n >= warmup
            t0 = time.perf_counter()
            ok, frame = cap.read()
            for _ in range(step - 1):
                cap.grab()
            if not ok:
                break
            if crop:
                frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
            t1 = time.perf_counter()
            annotated, _ = pipe.process_frame(frame, n + 1)
            t2 = time.perf_counter()
            if writer is not None:
                writer.write(annotated)
            t3 = time.perf_counter()
            if T.on:
                T.t["decode"] += t1 - t0
                T.t["encode"] += t3 - t2
                total += t3 - t0
            n += 1
    finally:
        cap.release()
        if writer is not None:
            writer.close()
            os.remove(tmp)
        for name, fn in originals.items():                     # unwrap (other configs run in this process)
            setattr(P, name, fn)
    timed = max(1, n - warmup)
    st = {k: T.t.get(k, 0.0) for k in STAGES[:-1]}
    st["other"] = max(0.0, total - sum(st.values()))
    return {"frames": timed, "fps": timed / total if total else 0.0,
            "ms_per_frame": {k: round(1000 * v / timed, 1) for k, v in st.items()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--frames", type=int, default=300, help="timed frames per configuration")
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--configs", default="original,pt,pt-headless,openvino,openvino-headless")
    ap.add_argument("--ov-device", default="CPU")
    ap.add_argument("--json", help="also write the results here")
    args = ap.parse_args(argv)
    results = {}
    for name in args.configs.split(","):
        backend, headless, ocr_threads = CONFIGS[name]
        cfg = load_config(args.profile, args.config)
        cfg.model_backend, cfg.headless, cfg.openvino_device = backend, headless, args.ov_device
        cfg.ocr_threads = ocr_threads
        r = bench(cfg, args.frames, args.warmup)
        results[name] = r
        ms = r["ms_per_frame"]
        print(f"{args.profile:<26} {name:<18} {r['fps']:5.2f} FPS  "
              + "  ".join(f"{k} {ms[k]:6.1f}" for k in STAGES) + "  (ms / frame)", flush=True)
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"profile": args.profile, "frames": args.frames, "results": results}, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
