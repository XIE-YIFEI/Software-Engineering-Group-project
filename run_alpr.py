#!/usr/bin/env python3
"""Run Malaysian ALPR on a video.

    python run_alpr.py --profile dashcam
    python run_alpr.py --profile cctv --start-sec 8 --duration 8
    python run_alpr.py --profile dashcam --source other.mp4 --out outputs/other.mp4
    python run_alpr.py --profile malaysia_road_anpr          # CCTV dashboard demo (30 s)
    python run_alpr.py --profile malaysia_road_anpr --backend openvino --headless   # fast, JSON only

Profiles and every threshold live in alpr/config.yaml. Writes the annotated video and a
`<out>.plates.json` (one row per vehicle + traffic-flow counts) for `python -m alpr.evaluate`.
--headless skips the rendering and the video and writes only the JSON (plates, flow, lane series).
--backend openvino runs the YOLO models through OpenVINO (exported on first use, see TEAM_GUIDE.md).
"""
import argparse
import sys

from alpr.config import DEFAULT_PATH, load_config
from alpr.pipeline import run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="profile name in the config (dashcam, cctv)")
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--source")
    ap.add_argument("--start-sec", type=float)
    ap.add_argument("--duration", type=float, help="seconds; 0 = to end of video")
    ap.add_argument("--out")
    ap.add_argument("--plate-model", help="override plate_model")
    ap.add_argument("--plate-model-fallback", help="override plate_model_fallback ('' = off)")
    ap.add_argument("--frame-step", type=int, help="process every Nth frame")
    ap.add_argument("--backend", dest="model_backend", choices=["pt", "openvino"], help="override model_backend")
    ap.add_argument("--ov-device", dest="openvino_device", help="OpenVINO device: CPU (default) | GPU | NPU | AUTO")
    ap.add_argument("--headless", action="store_true", default=None, help="no rendering / video, JSON only")
    ap.add_argument("--no-plates", dest="read_plates", action="store_false", default=None,
                    help="vehicles, flow and lanes only (no plate detection / OCR)")
    args = ap.parse_args(argv)

    cfg = load_config(args.profile, args.config)
    for name in ("source", "start_sec", "duration", "out", "plate_model", "plate_model_fallback", "frame_step",
                 "model_backend", "openvino_device", "headless", "read_plates"):
        value = getattr(args, name)
        if value is not None:
            setattr(cfg, name, value)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
