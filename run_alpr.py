#!/usr/bin/env python3
"""Run Malaysian ALPR on a video.

    python run_alpr.py --profile dashcam
    python run_alpr.py --profile cctv --start-sec 8 --duration 8
    python run_alpr.py --profile dashcam --source other.mp4 --out outputs/other.mp4
    python run_alpr.py --profile malaysia_road_anpr          # CCTV dashboard demo (30 s)

Profiles and every threshold live in alpr/config.yaml. Writes the annotated video and a
`<out>.plates.json` (one row per vehicle + traffic-flow counts) for `python -m alpr.evaluate`.
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
    args = ap.parse_args(argv)

    cfg = load_config(args.profile, args.config)
    for name in ("source", "start_sec", "duration", "out", "plate_model", "plate_model_fallback"):
        value = getattr(args, name)
        if value is not None:
            setattr(cfg, name, value)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
