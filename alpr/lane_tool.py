"""Draw a camera's lanes, solid lines and calibration points by clicking, and check them on screen.

    python -m alpr.lane_tool --profile malaysia_selayang_lanes            # draw (OpenCV window)
    python -m alpr.lane_tool --profile malaysia_selayang_lanes --show out.jpg   # render the config

Drawing works on an *empty-road* background: the per-pixel median of ~80 frames spread over the
clip, where moving vehicles disappear and the lane markings show.

Keys while drawing:  left click = add point,  right click / Backspace = undo,  Enter = finish the
shape (you are asked for its name in the console),  l = lane mode,  s = solid-line mode,
c = calibration-point mode (you are asked for the point's road position in metres),  q = quit.
The result is printed as a YAML block to paste into the profile in alpr/config.yaml (all points are
fractions of the frame, so the profile works at any resolution).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

import cv2
import numpy as np

from .config import DEFAULT_PATH, load_config
from .lanes import Calibration, LaneMap

LANE_BGR = [(255, 159, 10), (90, 200, 250), (48, 209, 88), (255, 214, 10), (191, 90, 242), (255, 55, 95),
            (100, 210, 255), (172, 142, 104)]


def empty_road(source: str, n: int = 80, start_sec: float = 0.0, duration: float = 0.0,
               crop: Optional[List[float]] = None) -> np.ndarray:
    """Per-pixel median of `n` frames spread over the window: the road without moving vehicles."""
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise FileNotFoundError(source)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    a = int(start_sec * fps)
    b = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - 1 if duration <= 0 else int((start_sec + duration) * fps)
    frames = []
    for i in np.linspace(a, max(a, b), n).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            if crop:
                h, w = f.shape[:2]
                f = f[int(crop[1] * h):int(crop[3] * h), int(crop[0] * w):int(crop[2] * w)]
            frames.append(f)
    cap.release()
    return np.median(np.stack(frames), axis=0).astype(np.uint8)


def render_lanes(img: np.ndarray, cfg, count_line: bool = True) -> np.ndarray:
    """The profile's lanes (tinted, labelled, direction arrow), solid lines, count line and calibration
    points drawn over `img`."""
    h, w = img.shape[:2]
    lm = LaneMap.from_config(cfg.lanes, cfg.solid_lines, w, h, cfg.lane_vanishing_point, cfg.lane_ref_shift)
    out = img.copy()
    tint = img.copy()
    for i, lane in enumerate(lm.lanes):
        cv2.fillPoly(tint, [np.int32(lane.polygon)], LANE_BGR[i % len(LANE_BGR)][::-1])
    out = cv2.addWeighted(tint, 0.28, out, 0.72, 0)
    for i, lane in enumerate(lm.lanes):
        col = LANE_BGR[i % len(LANE_BGR)][::-1]
        cv2.polylines(out, [np.int32(lane.polygon)], True, col, 2, cv2.LINE_AA)
        pts = np.asarray(lane.polygon)
        cx, cy = pts.mean(axis=0)
        a = np.asarray(lane.axis) * 28
        cv2.arrowedLine(out, (int(cx - a[0]), int(cy - a[1])), (int(cx + a[0]), int(cy + a[1])), (255, 255, 255), 2,
                        cv2.LINE_AA, tipLength=0.4)
        cv2.putText(out, lane.name, (int(cx) - 18, int(cy) + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
                    cv2.LINE_AA)
    for s in lm.solid_lines:
        cv2.polylines(out, [np.int32(s.points)], False, (60, 60, 255), 3, cv2.LINE_AA)
    if count_line and cfg.count_line_y > 0:
        y = int(cfg.count_line_y * h)
        cv2.line(out, (0, y), (w, y), (255, 255, 255), 1, cv2.LINE_AA)
    if cfg.calibration:
        for x, y in np.asarray(cfg.calibration["image_points"]) * [w, h]:
            cv2.circle(out, (int(x), int(y)), 5, (0, 255, 255), -1, cv2.LINE_AA)
    if lm.vp is not None:
        cv2.drawMarker(out, (int(lm.vp[0]), int(lm.vp[1])), (0, 255, 255), cv2.MARKER_CROSS, 16, 2)
    return out


def calibration_report(cfg, w: int, h: int) -> List[str]:
    """Human-readable checks of the calibration: fit residual and the 1-sigma scale uncertainty."""
    if not cfg.calibration:
        return ["no calibration: speeds and gaps in metres are not reported"]
    cal = Calibration.from_config(cfg.calibration, w, h)
    img = np.asarray(cfg.calibration["image_points"]) * [w, h]
    road = np.asarray(cfg.calibration["road_points"], np.float64)
    res = cal.to_road(img) - road
    return [f"calibration: {len(img)} points, fit residual rms {np.sqrt((res ** 2).sum(1).mean()):.2f} m, "
            f"max {np.abs(res).max():.2f} m",
            f"scale uncertainty (Monte Carlo, 1 sigma): {100 * cal.rel_sigma:.1f} % of every distance and speed"]


class _Drawer:
    def __init__(self, img: np.ndarray):
        self.img = img
        self.mode = "lane"
        self.pts: List[tuple] = []
        self.shapes: List[dict] = []

    def on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.pts.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and self.pts:
            self.pts.pop()

    def frame(self) -> np.ndarray:
        out = self.img.copy()
        for s in self.shapes:
            col = {"lane": (10, 159, 255), "solid": (60, 60, 255), "calib": (0, 255, 255)}[s["kind"]]
            p = np.int32(s["px"])
            if s["kind"] == "lane":
                cv2.polylines(out, [p], True, col, 2, cv2.LINE_AA)
            elif s["kind"] == "solid":
                cv2.polylines(out, [p], False, col, 3, cv2.LINE_AA)
            else:
                cv2.circle(out, tuple(p[0]), 5, col, -1)
        for i, p in enumerate(self.pts):
            cv2.circle(out, p, 4, (255, 255, 255), -1)
            if i:
                cv2.line(out, self.pts[i - 1], p, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, f"mode: {self.mode}  (l lane / s solid / c calibration, Enter finish, q quit)", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        return out

    def finish(self, w: int, h: int) -> None:
        if not self.pts:
            return
        frac = [[round(x / w, 4), round(y / h, 4)] for x, y in self.pts]
        if self.mode == "lane" and len(self.pts) >= 3:
            name = input("lane name (e.g. In1): ").strip() or f"Lane{len(self.shapes) + 1}"
            group = input("carriageway / group (e.g. Inbound): ").strip()
            direction = input("direction of travel in the image [down/up/left/right]: ").strip() or "down"
            self.shapes.append({"kind": "lane", "px": list(self.pts), "name": name, "group": group,
                                "direction": direction, "polygon": frac})
        elif self.mode == "solid" and len(self.pts) >= 2:
            name = input("solid line name: ").strip() or f"solid {len(self.shapes) + 1}"
            self.shapes.append({"kind": "solid", "px": list(self.pts), "name": name, "points": frac})
        elif self.mode == "calib":
            for p, f in zip(self.pts, frac):
                xy = input(f"road position of point {p} in metres 'x y' (x across, y along): ").split()
                self.shapes.append({"kind": "calib", "px": [p], "image": f, "road": [float(v) for v in xy[:2]]})
        self.pts = []

    def yaml(self) -> str:
        out = []
        lanes = [s for s in self.shapes if s["kind"] == "lane"]
        if lanes:
            out.append("    lanes:")
            out += [f"      - {{name: {s['name']}, group: {s['group']}, direction: {s['direction']}, "
                    f"polygon: {s['polygon']}}}" for s in lanes]
        solids = [s for s in self.shapes if s["kind"] == "solid"]
        if solids:
            out.append("    solid_lines:")
            out += [f"      - {{name: {s['name']}, points: {s['points']}}}" for s in solids]
        cal = [s for s in self.shapes if s["kind"] == "calib"]
        if cal:
            out += ["    calibration:", f"      image_points: {[s['image'] for s in cal]}",
                    f"      road_points: {[s['road'] for s in cal]}"]
        return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--config", default=DEFAULT_PATH)
    ap.add_argument("--source", help="override the profile's video")
    ap.add_argument("--show", metavar="IMAGE", help="render the configured lanes to IMAGE instead of drawing")
    ap.add_argument("--background", metavar="IMAGE", help="use this image instead of the median of the video")
    args = ap.parse_args(argv)
    cfg = load_config(args.profile, args.config)
    src = args.source or cfg.source
    if args.background:
        bg = cv2.imread(args.background)
    else:
        print(f"building the empty-road background from {src} ...")
        bg = empty_road(src, start_sec=cfg.start_sec, duration=cfg.duration, crop=cfg.frame_crop or None)
    h, w = bg.shape[:2]
    if args.show:
        os.makedirs(os.path.dirname(args.show) or ".", exist_ok=True)
        cv2.imwrite(args.show, render_lanes(bg, cfg))
        for line in calibration_report(cfg, w, h):
            print(line)
        print(f"lanes -> {args.show}")
        return 0
    d = _Drawer(render_lanes(bg, cfg) if cfg.lanes else bg)
    win = "SECS lane tool"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, d.on_mouse)
    while True:
        cv2.imshow(win, d.frame())
        k = cv2.waitKey(30) & 0xFF
        if k in (ord("q"), 27):
            break
        if k in (13, 10):
            d.finish(w, h)
        elif k == 8 and d.pts:
            d.pts.pop()
        elif k in (ord("l"), ord("s"), ord("c")):
            d.mode = {ord("l"): "lane", ord("s"): "solid", ord("c"): "calib"}[k]
    cv2.destroyAllWindows()
    print("\n# paste into the profile in alpr/config.yaml\n" + d.yaml())
    return 0


if __name__ == "__main__":
    sys.exit(main())
