"""Lane-level driving behaviour rules on tracked vehicles (pure logic, no models).

Rules, easiest first (each is unit-tested on synthetic trajectories in alpr/tests/test_alpr.py):

* LANE_CHANGE (info): the smoothed lane (alpr.lanes.LaneAssigner) changes within one carriageway
  (not for motorcycles, which filter along the lane lines).
* SOLID_LINE (violation): the smoothed reference point (LaneMap.ref_point) moves to the other side of a solid line
  and stays there (a lane change across a solid line, or cutting across an edge line / hatched area).
* WRONG_WAY (violation): the vehicle moves against its lane's direction of travel by at least
  ``wrong_way_min_disp`` box heights within ``wrong_way_sec``.
* STOPPED (hazard): the vehicle stands still in a lane for ``stopped_sec`` while the rest of its
  carriageway is moving. Standing still in a queue (the carriageway is slow too) is congestion, not
  an event; anything outside the lane polygons (lay-bys, shoulders) is never "on the carriageway".
* SPEEDING / TAILGATING (violation) need a calibration (alpr.lanes.Calibration). A speed is the slope
  of a straight-line fit of road position against time; it always carries an error bar and SPEEDING
  needs the *lower* end of the 95 % interval above the limit. Tailgating uses the time headway at the
  count line (front-to-front, the same point on both vehicles) and the follower's calibrated speed.

Motion thresholds are in box heights per second, so they hold near and far from the camera without
a calibration.

Public footage: a violation on a real (camera) vehicle is *withheld* unless ``show_real_violations``:
it is counted, but not drawn, not tied to a plate and not written per vehicle. Only SIMULATED
vehicles (synthetic trajectories, clearly marked) are shown as violators.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .lanes import Calibration, LaneAssigner, LaneMap, side_of_polyline

LANE_CHANGE, SOLID_LINE, WRONG_WAY, STOPPED, SPEEDING, TAILGATING = (
    "LANE_CHANGE", "SOLID_LINE", "WRONG_WAY", "STOPPED", "SPEEDING", "TAILGATING")
ALL_RULES = (LANE_CHANGE, SOLID_LINE, WRONG_WAY, STOPPED, SPEEDING, TAILGATING)
SPEEDING_CONFIRM = 3              # consecutive speed estimates (each a fit over speed_window_sec)
# Motorcycles filter between queued cars along the lane lines (common in Malaysia): their "lane" there is
# not meaningful, so they get no LANE_CHANGE events (they are still counted per lane and checked by the
# other rules).
LANE_CHANGE_IGNORE = ("Motorcycle",)
# Tailgating is about the car in front in the same lane. Motorcycles ride beside cars within a lane, so they
# are neither leader nor follower; and a front-to-front distance shorter than a car is a vehicle beside (or
# a second box on the same vehicle), not one in front.
HEADWAY_IGNORE = ("Motorcycle",)
MIN_HEADWAY_GAP_M = 4.0
SEVERITY = {LANE_CHANGE: "info", STOPPED: "hazard", SOLID_LINE: "violation", WRONG_WAY: "violation",
            SPEEDING: "violation", TAILGATING: "violation"}
EVENT_TITLE = {LANE_CHANGE: "Lane change", SOLID_LINE: "Solid line crossed", WRONG_WAY: "Wrong way",
               STOPPED: "Stopped in lane", SPEEDING: "Speeding", TAILGATING: "Tailgating"}


@dataclass
class LaneEvent:
    code: str
    track_id: int
    frame: int
    lane: str                          # lane where it happened (the new lane for a change)
    message: str
    from_lane: str = ""
    value: Optional[float] = None      # km/h (SPEEDING), headway s (TAILGATING), stopped s (STOPPED)
    error: Optional[float] = None      # 1-sigma error of `value`, km/h
    vtype: str = ""
    simulated: bool = False
    withheld: bool = False             # real vehicle + violation on public footage: counted only
    plate: str = ""                    # filled in by the pipeline when the plate was read
    emitted: int = -1                  # frame the rule fired (`frame` is when the behaviour began)

    @property
    def severity(self) -> str:
        return SEVERITY.get(self.code, "info")

    def to_dict(self, fps: float, t0: float = 0.0) -> dict:
        d = {"code": self.code, "severity": self.severity, "time_sec": round(t0 + self.frame / fps, 2),
             "lane": self.lane, "from_lane": self.from_lane, "message": self.message, "vtype": self.vtype,
             "track_id": self.track_id, "plate": self.plate, "simulated": self.simulated}
        if self.value is not None:
            d["value"] = round(self.value, 2)
        if self.error is not None:
            d["error_1sigma"] = round(self.error, 2)
        return d


@dataclass
class RuleParams:
    rules: Tuple[str, ...] = ALL_RULES
    lane_min_sec: float = 0.3          # a new lane must hold this long (LaneAssigner.min_frames)
    lane_margin: float = 0.12          # ... with the point this far inside it (x box width)
    smooth_sec: float = 0.2            # moving average of the bottom-centre point
    wrong_way_sec: float = 1.0
    wrong_way_min_disp: float = 0.8    # box heights against the lane direction within wrong_way_sec
    stopped_sec: float = 5.0
    still_speed: float = 0.08          # box heights / s: standing still
    moving_speed: float = 0.6          # box heights / s: the carriageway is flowing
    speed_window_sec: float = 1.0
    speed_zone_y: Tuple[float, float] = (0.0, 1.0)   # rows (fractions) where speed is measured
    speed_limit_kmh: float = 0.0       # 0 = SPEEDING off
    tailgate_headway_sec: float = 1.0
    tailgate_min_kmh: float = 50.0
    count_line_y: float = 0.0          # fraction; headways are measured on it
    show_real_violations: bool = False

    @classmethod
    def from_config(cls, cfg) -> "RuleParams":
        g = lambda k, d: getattr(cfg, k, d)     # noqa: E731
        return cls(rules=tuple(g("lane_rules", ALL_RULES)), lane_min_sec=g("lane_min_sec", 0.3),
                   lane_margin=g("lane_margin", 0.12), smooth_sec=g("smooth_sec", 0.2),
                   wrong_way_sec=g("wrong_way_sec", 1.0),
                   wrong_way_min_disp=g("wrong_way_min_disp", 0.8), stopped_sec=g("stopped_sec", 5.0),
                   still_speed=g("still_speed", 0.08), moving_speed=g("moving_speed", 0.6),
                   speed_window_sec=g("speed_window_sec", 1.0),
                   speed_zone_y=tuple(g("speed_zone_y", (0.0, 1.0))), speed_limit_kmh=g("speed_limit_kmh", 0.0),
                   tailgate_headway_sec=g("tailgate_headway_sec", 1.0), tailgate_min_kmh=g("tailgate_min_kmh", 50.0),
                   count_line_y=g("count_line_y", 0.0), show_real_violations=g("show_real_violations", False))


@dataclass
class _Track:
    pts: Deque[Tuple[int, float, float, float, float]] = field(default_factory=lambda: deque(maxlen=600))
    vtype: str = ""
    simulated: bool = False
    last_frame: int = 0
    sides: Dict[int, List[int]] = field(default_factory=dict)   # line -> [stable side, candidate, run]
    fired: set = field(default_factory=set)
    wrong_run: int = 0
    stop_frames: int = 0
    rel_speed: Optional[float] = None
    speeds: List[Tuple[float, float]] = field(default_factory=list)  # (km/h, 1-sigma)
    crossed: bool = False
    headway: Optional[Tuple[float, int, int, str]] = None   # (s, leader id, frame, lane) awaiting a speed


class BehaviourMonitor:
    def __init__(self, lane_map: LaneMap, fps: float, params: Optional[RuleParams] = None,
                 calibration: Optional[Calibration] = None, frame_h: int = 1080):
        self.map = lane_map
        self.fps = fps
        self.p = params or RuleParams()
        self.cal = calibration
        self.frame_h = frame_h
        self.assigner = LaneAssigner(lane_map, max(1, int(round(self.p.lane_min_sec * fps))), self.p.lane_margin)
        self.tracks: Dict[int, _Track] = {}
        self.events: List[LaneEvent] = []
        self.crossings: Dict[str, List[Tuple[float, int]]] = {}   # lane -> (time s, track) at the count line
        self._last_cross: Dict[str, Tuple[float, int]] = {}        # lane -> last leader candidate (no motorcycles)
        self.final_speed: Dict[int, Tuple[float, float]] = {}     # track -> median (km/h, 1-sigma) so far
        self.sim_ids: set = set()                                   # SIMULATED tracks (kept out of statistics)
        self._now = 0
        self._smooth = max(1, int(round(self.p.smooth_sec * fps)))
        self._win = max(2, int(round(self.p.wrong_way_sec * fps)))
        self._swin = max(3, int(round(self.p.speed_window_sec * fps)))

    # ------------------------------------------------------------------ helpers
    def _smoothed(self, t: _Track, k: Optional[int] = None) -> Tuple[float, float]:
        k = k or self._smooth
        pts = list(t.pts)[-k:]
        return float(np.mean([p[1] for p in pts])), float(np.mean([p[2] for p in pts]))

    def _rel_speed(self, t: _Track) -> Optional[float]:
        """Speed of the smoothed point in box heights per second, over ~wrong_way_sec."""
        if len(t.pts) < self._win + self._smooth:
            return None
        pts = list(t.pts)
        a = pts[-self._win - self._smooth:-self._win]
        b = pts[-self._smooth:]
        ax, ay = np.mean([p[1] for p in a]), np.mean([p[2] for p in a])
        bx, by = np.mean([p[1] for p in b]), np.mean([p[2] for p in b])
        dt = (b[-1][0] - a[-1][0]) / self.fps
        hgt = max(1.0, float(np.mean([p[3] for p in b])))
        return float(np.hypot(bx - ax, by - ay)) / hgt / max(1e-6, dt)

    def _emit(self, code: str, tid: int, t: _Track, frame: int, lane: str, msg: str, **kw) -> Optional[LaneEvent]:
        if code not in self.p.rules:
            return None
        ev = LaneEvent(code, tid, frame, lane or "", msg, vtype=t.vtype, simulated=t.simulated, emitted=self._now, **kw)
        ev.withheld = (not t.simulated and ev.severity == "violation" and not self.p.show_real_violations)
        self.events.append(ev)
        return ev

    def lane_of(self, tid: int) -> Optional[str]:
        return self.assigner.lane_of(tid)

    def speed_of(self, tid: int) -> Optional[Tuple[float, float]]:
        """Median calibrated speed of a track so far, (km/h, 1-sigma)."""
        t = self.tracks.get(tid)
        if not t or not t.speeds:
            return None
        v = np.asarray(t.speeds)
        i = int(np.argsort(v[:, 0])[len(v) // 2])
        return float(v[i, 0]), float(v[i, 1])

    # ------------------------------------------------------------------ per frame
    def update(self, frame: int, vehicles: Sequence[dict]) -> List[LaneEvent]:
        """vehicles: dicts with track_id, box, vtype and optionally simulated. Returns the new events."""
        new: List[LaneEvent] = []
        seen = set()
        self._now = frame
        for v in vehicles:
            tid = int(v["track_id"])
            seen.add(tid)
            t = self.tracks.setdefault(tid, _Track())
            t.vtype, t.simulated, t.last_frame = v.get("vtype", ""), bool(v.get("simulated", False)), frame
            if t.simulated:
                self.sim_ids.add(tid)
            x, y = self.map.ref_point(v["box"])
            t.pts.append((frame, x, y, float(v["box"][3] - v["box"][1]), float(v["box"][2] - v["box"][0])))
            lane, change = self.assigner.update(tid, v["box"], frame)
            t.rel_speed = self._rel_speed(t)
            if change is not None and t.vtype not in LANE_CHANGE_IGNORE:
                a, b = change
                la, lb = self.map.by_name.get(a), self.map.by_name.get(b)
                if la is not None and lb is not None and la.group == lb.group:
                    ev = self._emit(LANE_CHANGE, tid, t, frame, b, f"Lane change {a} → {b}", from_lane=a)
                    if ev:
                        new.append(ev)
            for ev in (self._solid(tid, t, frame, lane), self._wrong_way(tid, t, frame, lane),
                       self._speed(tid, t, frame, lane), self._headway(tid, t, frame, lane)):
                if ev:
                    new.append(ev)
        # STOPPED needs every vehicle's speed of this frame (is the rest of the carriageway moving?)
        for v in vehicles:
            ev = self._stopped(int(v["track_id"]), frame)
            if ev:
                new.append(ev)
        for tid in [k for k, t in self.tracks.items() if k not in seen and frame - t.last_frame > 5 * self.fps]:
            self.tracks.pop(tid)
            self.assigner.forget(tid)
        return new

    # ------------------------------------------------------------------ rules
    def _solid(self, tid: int, t: _Track, frame: int, lane: Optional[str]) -> Optional[LaneEvent]:
        if SOLID_LINE not in self.p.rules or not self.map.solid_lines or len(t.pts) < self._smooth:
            return None
        p = self._smoothed(t)
        need = self.assigner.min_frames
        for i, line in enumerate(self.map.solid_lines):
            s = side_of_polyline(p, line.points)
            if s == 0:
                continue
            st = t.sides.setdefault(i, [0, 0, 0])            # [stable side, candidate side, run]
            if st[0] == 0:
                st[1], st[2] = (s, st[2] + 1) if st[1] == s else (s, 1)
                if st[2] >= need:
                    st[0], st[1], st[2] = s, 0, 0
                continue
            if s == st[0]:
                st[1], st[2] = 0, 0
                continue
            st[1], st[2] = (s, st[2] + 1) if st[1] == s else (s, 1)
            if st[2] >= need:
                st[0], st[1], st[2] = s, 0, 0
                key = (SOLID_LINE, i)
                if key in t.fired:
                    continue
                t.fired.add(key)
                hist = self.assigner.tracks.get(tid)
                prev = hist.history[-2][1] if hist and len(hist.history) >= 2 else ""
                where = f" ({prev} → {lane})" if prev and lane and prev != lane else ""
                return self._emit(SOLID_LINE, tid, t, frame - need + 1, lane or prev,
                                  f"Crossed the solid line: {line.name}{where}", from_lane=prev)
        return None

    def _wrong_way(self, tid: int, t: _Track, frame: int, lane: Optional[str]) -> Optional[LaneEvent]:
        if WRONG_WAY not in self.p.rules or WRONG_WAY in t.fired or len(t.pts) < self._win + self._smooth:
            return None
        pts = list(t.pts)
        a, b = pts[-self._win - self._smooth:-self._win], pts[-self._smooth:]
        pa = (float(np.mean([p[1] for p in a])), float(np.mean([p[2] for p in a])))
        pb = (float(np.mean([p[1] for p in b])), float(np.mean([p[2] for p in b])))
        # inside the same lane at both ends of the window: a vehicle that has left the lanes (a slip road
        # curving back towards the camera) keeps its last lane in the assigner but is not in that lane
        ln = self.map.lane_at(pb)
        if ln is None or self.map.lane_at(pa) is not ln:
            t.wrong_run = 0
            return None
        d = np.array([pb[0] - pa[0], pb[1] - pa[1]])
        along = float(np.dot(d, ln.axis))
        hgt = max(1.0, float(np.mean([p[3] for p in b])))
        if along <= -self.p.wrong_way_min_disp * hgt:
            t.wrong_run += 1
        else:
            t.wrong_run = 0
        if t.wrong_run >= self.assigner.min_frames:
            t.fired.add(WRONG_WAY)
            return self._emit(WRONG_WAY, tid, t, frame - self._win, ln.name,
                              f"Moving against the traffic in {ln.name} ({ln.group or ln.direction})")
        return None

    def _stopped(self, tid: int, frame: int) -> Optional[LaneEvent]:
        t = self.tracks.get(tid)
        if STOPPED not in self.p.rules or t is None or t.rel_speed is None or STOPPED in t.fired:
            return None
        x, y = t.pts[-1][1], t.pts[-1][2]
        ln = self.map.lane_at((x, y))
        if ln is None:                                          # lay-by, shoulder, median: not a lane
            t.stop_frames = 0
            return None
        if t.rel_speed > 2 * self.p.still_speed:
            t.stop_frames = 0
            return None
        if t.rel_speed > self.p.still_speed:
            return None
        group = [o for k, o in self.tracks.items()
                 if k != tid and o.last_frame == frame and o.rel_speed is not None and o.vtype != "Motorcycle"
                 and (lambda l: l is not None and l.group == ln.group)(self.map.lane_at((o.pts[-1][1], o.pts[-1][2])))]
        flowing = (not group) or float(np.median([o.rel_speed for o in group])) >= self.p.moving_speed
        if flowing:
            t.stop_frames += 1
        if t.stop_frames >= self.p.stopped_sec * self.fps:
            t.fired.add(STOPPED)
            secs = t.stop_frames / self.fps
            return self._emit(STOPPED, tid, t, frame - t.stop_frames + 1, ln.name,
                              f"Stopped in {ln.name} for {secs:.0f} s while traffic flows", value=secs)
        return None

    def _speed(self, tid: int, t: _Track, frame: int, lane: Optional[str]) -> Optional[LaneEvent]:
        if self.cal is None or len(t.pts) < self._swin:
            return None
        pts = list(t.pts)[-self._swin:]
        lo, hi = self.p.speed_zone_y
        if not all(lo * self.frame_h <= p[2] <= hi * self.frame_h for p in pts):
            return None
        if pts[-1][2] >= self.frame_h - 2:                      # box cut by the frame edge
            return None
        est = self.cal.speed([p[0] / self.fps for p in pts], [(p[1], p[2]) for p in pts])
        if est is None:
            return None
        v, s = est
        t.speeds.append((v, s))
        self.final_speed[tid] = self.speed_of(tid)
        ev = self._tailgate(tid, t, frame)
        if ev:
            return ev
        # each estimate is a fit over speed_window_sec; SPEEDING_CONFIRM in a row, all beyond doubt
        if (SPEEDING in self.p.rules and self.p.speed_limit_kmh > 0 and SPEEDING not in t.fired
                and len(t.speeds) >= SPEEDING_CONFIRM
                and all(sv - 2 * ss > self.p.speed_limit_kmh for sv, ss in t.speeds[-SPEEDING_CONFIRM:])):
            t.fired.add(SPEEDING)
            vm, sm = self.speed_of(tid)
            return self._emit(SPEEDING, tid, t, frame, lane or "",
                              f"{vm:.0f} ± {2 * sm:.0f} km/h in a {self.p.speed_limit_kmh:.0f} km/h zone",
                              value=vm, error=sm)
        return None

    def _headway(self, tid: int, t: _Track, frame: int, lane: Optional[str]) -> Optional[LaneEvent]:
        """Time headway at the count line, same lane, same direction (front-to-front). The tailgating
        decision waits for the follower's calibrated speed when it is not measured yet."""
        if self.p.count_line_y <= 0 or t.crossed or len(t.pts) < 2 or lane is None:
            return None
        ln = self.map.by_name.get(lane)
        line = self.p.count_line_y * self.frame_h
        (f0, _, y0, _, _), (f1, _, y1, _, _) = t.pts[-2], t.pts[-1]
        down = ln.axis[1] >= 0
        if not ((down and y0 < line <= y1) or (not down and y0 > line >= y1)):
            return None
        t.crossed = True
        tc = (f0 + (f1 - f0) * (line - y0) / (y1 - y0)) / self.fps if y1 != y0 else f1 / self.fps
        self.crossings.setdefault(lane, []).append((tc, tid))
        if t.vtype in HEADWAY_IGNORE:
            return None
        lead = self._last_cross.get(lane)
        self._last_cross[lane] = (tc, tid)
        if TAILGATING not in self.p.rules or self.cal is None or lead is None:
            return None
        t.headway = (tc - lead[0], lead[1], frame, lane)
        return self._tailgate(tid, t, frame)

    def _tailgate(self, tid: int, t: _Track, frame: int) -> Optional[LaneEvent]:
        if t.headway is None or not t.speeds:
            return None
        h, lead_id, f_cross, lane = t.headway
        if frame - f_cross > 3 * self.fps:                     # no speed within 3 s: undecided, dropped
            t.headway = None
            return None
        v, s = self.speed_of(tid)
        t.headway = None
        if h * v / 3.6 < MIN_HEADWAY_GAP_M:                    # beside it, not behind it
            return None
        if h < self.p.tailgate_headway_sec and v - 2 * s >= self.p.tailgate_min_kmh:
            return self._emit(TAILGATING, tid, t, f_cross, lane,
                              f"{h:.2f} s behind #{lead_id} at {v:.0f} km/h (≈{h * v / 3.6:.0f} m, front to front)",
                              value=h, error=None)
        return None

    def crossing_speeds(self, lane: str) -> List[float]:
        """Calibrated speeds (km/h) of the camera vehicles that crossed the count line in `lane`."""
        return [self.final_speed[tid][0] for _, tid in self.crossings.get(lane, [])
                if tid in self.final_speed and tid not in self.sim_ids]

    # ------------------------------------------------------------------ reporting
    def visible_events(self) -> List[LaneEvent]:
        return [e for e in self.events if not e.withheld]

    def withheld_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.events:
            if e.withheld:
                out[e.code] = out.get(e.code, 0) + 1
        return out


# ---------------------------------------------------------------------------- simulated vehicles
# length, width, height (m): footprint for the synthetic box
VEHICLE_DIMS = {"Car": (4.5, 1.8, 1.5), "Motorcycle": (2.0, 0.8, 1.4), "Bus": (12.0, 2.5, 3.2),
                "Truck": (8.0, 2.5, 3.2)}


class SimulatedTraffic:
    """SIMULATED vehicles that demonstrate the violation rules on real footage without accusing any
    real vehicle. Each one is a synthetic trajectory (purple on the dashboard, labelled "Simulated",
    not in the camera image) with a fictitious X-series plate; it goes through the same rules as the
    camera's vehicles but is never counted in the traffic flow.

        behaviour_sim_tracks:
          - {plate: XWW 1001, vtype: Car, label: Wrong-way driver, start_sec: 20,
             path: [[0, x_m, y_m], [4, x_m, y_m]]}      # seconds from start_sec, road metres (calibrated)
          - {..., path_img: [[0, x, y, w], ...]}        # or bottom-centre + box width, frame fractions

    Positions are interpolated linearly between the path points.
    """

    def __init__(self, specs: Sequence[dict], calibration: Optional[Calibration], w: int, h: int, fps: float,
                 t0: float = 0.0):
        self.specs = list(specs)
        self.cal = calibration
        self.w, self.h, self.fps, self.t0 = w, h, fps, t0

    def _box_road(self, x: float, y: float, vtype: str) -> List[float]:
        L, Wd, Hg = VEHICLE_DIMS.get(vtype, VEHICLE_DIMS["Car"])
        corners = [(x - Wd / 2, y - L / 2), (x + Wd / 2, y - L / 2), (x + Wd / 2, y + L / 2), (x - Wd / 2, y + L / 2)]
        q = self.cal.to_image(corners)
        x1, x2 = float(q[:, 0].min()), float(q[:, 0].max())
        y_far, y2 = float(q[:, 1].min()), float(q[:, 1].max())
        top = y_far - 0.40 * (Hg / Wd) * (x2 - x1)          # body height; 0.40 matches real car boxes on the
        return [x1, top, x2, y2]                              # Selayang camera (w and h within ~5 %)

    def _box_img(self, x: float, y: float, bw: float) -> List[float]:
        cx, by, w = x * self.w, y * self.h, bw * self.w
        return [cx - w / 2, by - 0.8 * w, cx + w / 2, by]

    def vehicles(self, frame: int) -> List[dict]:
        t = self.t0 + frame / self.fps
        out = []
        for k, s in enumerate(self.specs):
            path = s.get("path") if self.cal is not None else None
            pts = path or s.get("path_img")
            if not pts:
                continue
            rel = t - float(s.get("start_sec", 0.0))
            if rel < pts[0][0] or rel > pts[-1][0]:
                continue
            j = max(i for i in range(len(pts)) if pts[i][0] <= rel)
            a, b = pts[j], pts[min(j + 1, len(pts) - 1)]
            f = 0.0 if b[0] == a[0] else (rel - a[0]) / (b[0] - a[0])
            p = [a[i] + f * (b[i] - a[i]) for i in range(1, len(a))]
            vtype = s.get("vtype", "Car")
            box = self._box_road(p[0], p[1], vtype) if path else self._box_img(p[0], p[1], p[2])
            out.append({"track_id": -(k + 1), "box": [int(round(v)) for v in box], "vtype": vtype,
                        "simulated": True, "plate": str(s.get("plate", "")), "sim_label": str(s.get("label", ""))})
        return out
