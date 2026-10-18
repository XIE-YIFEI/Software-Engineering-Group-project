"""Lanes of one fixed camera: lane polygons, travel direction, solid lines, and the ground-plane
calibration used for speed (pure geometry, no models).

A camera profile in alpr/config.yaml lists its lanes (draw them with ``python -m alpr.lane_tool``):

    lanes:
      - {name: R1, group: Inbound, direction: down, polygon: [[x, y], ...]}    # frame fractions
    solid_lines:
      - {name: Median edge, points: [[x, y], ...]}

Anything outside every lane polygon (shoulder, lay-by, median, hatched area) is not a lane: vehicles
there are not counted per lane and cannot be "stopped on the carriageway".

Lane assignment follows Paper 4 of the Part 3 review (Hu et al., Information 2026): a vehicle is in the
lane that contains the bottom-centre of its box, the point closest to where the tyres touch the road
(robust to occlusion from above and to the box growing upwards for tall vehicles). The raw lane is
smoothed per track: a new lane only counts once the point is clearly inside it (a margin past the
boundary) for ``min_frames`` frames, so a car driving on a lane line does not flicker between lanes.

Calibration (speed, gaps in metres): a homography from the image to the road plane, fitted to four
points whose road positions are known from the lane width (~3.5 m) and the length of the dashed
lane markings. Its uncertainty is propagated by Monte Carlo (lane width, marking length, click
error), so every speed is reported with an error bar. Without a calibration no km/h is reported.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

Point = Tuple[float, float]


# ---------------------------------------------------------------------------- geometry helpers
def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Even-odd ray casting (pixel or fraction coordinates, any vertex order)."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                inside = not inside
    return inside


def _seg_dist(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return float(np.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)))


def edge_distance(p: Point, poly: Sequence[Point]) -> float:
    """Distance from p to the polygon outline."""
    return min(_seg_dist(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def side_of_polyline(p: Point, line: Sequence[Point]) -> int:
    """+1 / -1: which side of the polyline p is on (sign of the cross product with the segment
    spanning p's position along the line); 0 when p lies beyond either end of the line."""
    best = None
    for a, b in zip(line[:-1], line[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        if L2 == 0:
            continue
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2
        if -1e-9 <= t <= 1 + 1e-9:
            d = _seg_dist(p, a, b)
            if best is None or d < best[0]:
                cross = dx * (p[1] - a[1]) - dy * (p[0] - a[0])
                best = (d, 1 if cross > 0 else -1)
    return best[1] if best else 0


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def bottom_centre(box) -> Point:
    return (float(box[0] + box[2]) / 2.0, float(box[3]))


# ---------------------------------------------------------------------------- lanes
_DIRS = {"down": (0.0, 1.0), "up": (0.0, -1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


@dataclass
class Lane:
    name: str
    polygon: List[Point]            # pixels
    direction: str = "down"         # travel direction in the image: down | up | left | right
    group: str = ""                 # carriageway; lane changes only happen within one group
    axis: Tuple[float, float] = (0.0, 1.0)   # unit vector of travel (polygon's long axis, oriented)

    def __post_init__(self):
        pts = np.asarray(self.polygon, np.float64)
        want = np.asarray(_DIRS.get(self.direction, (0.0, 1.0)))
        if len(pts) >= 3:
            c = pts - pts.mean(axis=0)
            _, _, vt = np.linalg.svd(c, full_matrices=False)
            ax = vt[0]
        else:
            ax = want
        if float(np.dot(ax, want)) < 0:
            ax = -ax
        self.axis = (float(ax[0]), float(ax[1]))

    def contains(self, p: Point) -> bool:
        return point_in_polygon(p, self.polygon)

    def y_range(self) -> Tuple[float, float]:
        ys = [p[1] for p in self.polygon]
        return min(ys), max(ys)

    def width_at(self, y: float) -> float:
        """Horizontal width of the polygon at image row y (0 outside)."""
        xs = []
        n = len(self.polygon)
        for i in range(n):
            (x1, y1), (x2, y2) = self.polygon[i], self.polygon[(i + 1) % n]
            if (y1 <= y <= y2 or y2 <= y <= y1) and y1 != y2:
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        return max(xs) - min(xs) if len(xs) >= 2 else 0.0

    def span_at(self, y: float) -> Optional[Tuple[float, float]]:
        xs = []
        n = len(self.polygon)
        for i in range(n):
            (x1, y1), (x2, y2) = self.polygon[i], self.polygon[(i + 1) % n]
            if (y1 <= y <= y2 or y2 <= y <= y1) and y1 != y2:
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        return (min(xs), max(xs)) if len(xs) >= 2 else None


@dataclass
class SolidLine:
    name: str
    points: List[Point]             # pixels


class LaneMap:
    """All lanes and solid lines of one camera, in pixels of the processed frame.

    Reference point: the bottom-centre of the box (Paper 4), optionally corrected for perspective. A
    vehicle off to the side of the camera shows one flank, which widens its box towards the vanishing
    point, so its bottom-centre drifts towards the image centre - more the further out and the closer to
    the camera (measured on the Selayang camera: a car in the outer lane sat at 13-26 % of its lane
    instead of ~50 %, at 0.13 lane widths from the line near the bottom of the frame). The correction
    moves the point back out:  x' = x + ref_shift * (x - x_vp) * (y - y_vp) / frame_h  (ref_shift 0 = off).
    """

    def __init__(self, lanes: List[Lane], solid_lines: Sequence[SolidLine] = (),
                 vanishing_point: Optional[Point] = None, ref_shift: float = 0.0, frame_h: int = 1080):
        self.lanes = list(lanes)
        self.solid_lines = list(solid_lines)
        self.by_name = {l.name: l for l in self.lanes}
        self.vp = vanishing_point
        self.ref_shift = ref_shift if vanishing_point is not None else 0.0
        self.frame_h = frame_h

    @classmethod
    def from_config(cls, lanes_cfg: Sequence[dict], solid_cfg: Sequence[dict], w: int, h: int,
                    vanishing_point: Sequence[float] = (), ref_shift: float = 0.0) -> "LaneMap":
        """Config points are fractions of the frame (resolution independent)."""
        def px(pts):
            return [(float(x) * w, float(y) * h) for x, y in pts]
        lanes = [Lane(str(d["name"]), px(d["polygon"]), str(d.get("direction", "down")), str(d.get("group", "")))
                 for d in lanes_cfg]
        solids = [SolidLine(str(d.get("name", f"solid {i + 1}")), px(d["points"])) for i, d in enumerate(solid_cfg)]
        vp = (float(vanishing_point[0]) * w, float(vanishing_point[1]) * h) if len(vanishing_point) == 2 else None
        return cls(lanes, solids, vp, ref_shift, h)

    def ref_point(self, box) -> Point:
        """Where the vehicle is on the road plane, from its box (corrected bottom-centre)."""
        x, y = bottom_centre(box)
        if self.ref_shift and self.vp is not None:
            x += self.ref_shift * (x - self.vp[0]) * max(0.0, y - self.vp[1]) / self.frame_h
        return x, y

    def lane_at(self, p: Point) -> Optional[Lane]:
        for lane in self.lanes:
            if lane.contains(p):
                return lane
        return None

    def names(self) -> List[str]:
        return [l.name for l in self.lanes]

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for l in self.lanes:
            out.setdefault(l.group, []).append(l.name)
        return out


@dataclass
class _LaneTrack:
    raw: Deque[Optional[str]] = field(default_factory=lambda: deque(maxlen=64))
    stable: Optional[str] = None
    candidate: Optional[str] = None
    run: int = 0                    # frames the candidate has held (clearly inside it)
    history: List[Tuple[int, str]] = field(default_factory=list)   # (frame, lane) each time the stable lane changes


class LaneAssigner:
    """Per-track lane from the reference point (LaneMap.ref_point), smoothed.

    The first lane needs ``min_frames`` frames to be accepted. A change needs the point to be inside
    the new lane by at least ``margin`` x the box width (past the boundary, not on the line) for
    ``min_frames`` consecutive frames. Frames with the point outside every lane do not count either way.
    """

    def __init__(self, lane_map: LaneMap, min_frames: int = 6, margin: float = 0.12):
        self.map = lane_map
        self.min_frames = max(1, int(min_frames))
        self.margin = margin
        self.tracks: Dict[int, _LaneTrack] = {}

    def update(self, track_id: int, box, frame: int) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
        """-> (stable lane, (from, to) if the stable lane changed in this update)."""
        t = self.tracks.setdefault(track_id, _LaneTrack())
        p = self.map.ref_point(box)
        lane = self.map.lane_at(p)
        name = lane.name if lane else None
        t.raw.append(name)
        if name is None:
            return t.stable, None
        clear = edge_distance(p, lane.polygon) >= self.margin * max(1.0, float(box[2] - box[0]))
        if name == t.stable:
            t.candidate, t.run = None, 0
            return t.stable, None
        if t.stable is not None and not clear:
            return t.stable, None                     # on the line: not (yet) in the new lane
        if name == t.candidate:
            t.run += 1
        else:
            t.candidate, t.run = name, 1
        if t.run >= self.min_frames:
            old = t.stable
            t.stable, t.candidate, t.run = name, None, 0
            t.history.append((frame - self.min_frames + 1, name))
            return t.stable, ((old, name) if old is not None else None)
        return t.stable, None

    def lane_of(self, track_id: int) -> Optional[str]:
        t = self.tracks.get(track_id)
        return t.stable if t else None

    def raw_mode(self, track_id: int) -> Optional[str]:
        t = self.tracks.get(track_id)
        if not t:
            return None
        named = [r for r in t.raw if r]
        return Counter(named).most_common(1)[0][0] if named else None

    def forget(self, track_id: int) -> None:
        self.tracks.pop(track_id, None)


# ---------------------------------------------------------------------------- calibration
def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Direct linear transform for 4+ point pairs (no OpenCV needed). src/dst: (N, 2)."""
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        A.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.asarray(A, np.float64))
    H = vt[-1].reshape(3, 3)
    return H / H[2, 2]


def apply_h(H: np.ndarray, pts) -> np.ndarray:
    p = np.asarray(pts, np.float64).reshape(-1, 2)
    q = np.c_[p, np.ones(len(p))] @ H.T
    return q[:, :2] / q[:, 2:3]


@dataclass
class Calibration:
    """Image (pixels) -> road plane (metres). x_m across the road, y_m along it.

    image_points: 4+ pixels; road_points: their road positions in metres. ``samples`` are homographies
    of perturbed inputs (lane width, marking length, click position) for the error estimate."""
    H: np.ndarray
    samples: List[np.ndarray] = field(default_factory=list)
    note: str = ""
    rel_sigma: float = 0.0          # 1-sigma relative error of a distance (and so of a speed) along the road

    @classmethod
    def from_config(cls, cfg: dict, w: int, h: int, n_samples: int = 200, seed: int = 0) -> "Calibration":
        img = np.asarray(cfg["image_points"], np.float64) * [w, h]
        road = np.asarray(cfg["road_points"], np.float64)
        H = _homography(img, road)
        rng = np.random.default_rng(seed)
        s_w = float(cfg.get("sigma_width_frac", 0.05))      # lane width 3.5 m +- ~0.17 m (3.25-3.75 in use)
        s_l = float(cfg.get("sigma_length_frac", 0.08))     # marking stroke + gap length
        s_px = float(cfg.get("sigma_click_px", 2.0))
        samples = []
        for _ in range(n_samples):
            r = road * [rng.normal(1.0, s_w), rng.normal(1.0, s_l)]
            im = img + rng.normal(0.0, s_px, img.shape)
            samples.append(_homography(im, r))
        cal = cls(H, samples, str(cfg.get("note", "")))
        # the calibration error is (nearly) a scale error: measure it once, on along-road segments
        # spanning the calibrated area, instead of per vehicle and frame
        y0, y1 = road[:, 1].min(), road[:, 1].max()
        xs = np.linspace(road[:, 0].min(), road[:, 0].max(), 3)
        segs = [cal.to_image([(x, y0), (x, y1)]) for x in xs]
        ratios = [[np.linalg.norm(np.diff(apply_h(Hs, s), axis=0)) for s in segs] for Hs in samples]
        base = [np.linalg.norm(np.diff(apply_h(H, s), axis=0)) for s in segs]
        cal.rel_sigma = float(np.median(np.std(np.asarray(ratios) / np.asarray(base), axis=0))) if samples else 0.0
        return cal

    def to_road(self, pts) -> np.ndarray:
        return apply_h(self.H, pts)

    def to_image(self, pts) -> np.ndarray:
        return apply_h(np.linalg.inv(self.H), pts)

    def speed(self, times: Sequence[float], pts: Sequence[Point]) -> Optional[Tuple[float, float]]:
        """(speed km/h, 1-sigma error km/h) of a trajectory (image points over time), from a straight-line
        fit of road position vs time. Error = calibration spread (Monte Carlo) + fit residual."""
        t = np.asarray(times, np.float64)
        if len(t) < 3 or t[-1] - t[0] <= 0:
            return None

        def fit(Hm):
            q = apply_h(Hm, pts)
            vx, vy = np.polyfit(t, q[:, 0], 1)[0], np.polyfit(t, q[:, 1], 1)[0]
            return float(np.hypot(vx, vy)) * 3.6, q

        v, q = fit(self.H)
        # measurement noise: residual of the linear fit -> standard error of the slope
        res = []
        for k in (0, 1):
            coef = np.polyfit(t, q[:, k], 1)
            res.append(q[:, k] - np.polyval(coef, t))
        resid = np.hypot(res[0], res[1])
        dof = max(1, len(t) - 2)
        s_fit = float(np.sqrt((resid ** 2).sum() / dof) / np.sqrt(((t - t.mean()) ** 2).sum())) * 3.6
        return v, float(np.hypot(self.rel_sigma * v, s_fit))

    def distance(self, p: Point, q: Point) -> float:
        a, b = self.to_road([p, q])
        return float(np.linalg.norm(a - b))
