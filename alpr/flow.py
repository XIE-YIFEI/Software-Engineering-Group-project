"""Traffic-flow counting on tracked vehicles (pure logic, no models).

A vehicle is counted once, when the bottom-centre of its box crosses a horizontal counting line.
Moving down the image (towards a camera that faces oncoming traffic) is "inbound", moving up is
"outbound". Counts are kept per vehicle type and per time bin, which is the time series a later
traffic-flow prediction model can train on.

With lanes (alpr/lanes.py) every crossing also carries the vehicle's lane, and `LaneMetrics` adds the
classic per-lane measures of a loop detector: flow (veh/min), time occupancy (share of time the
lane's stretch of the count line is covered by a vehicle) and density (vehicles in the lane zone,
per km once the camera is calibrated).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Crossing:
    frame: int
    track_id: int
    vtype: str
    direction: str        # "inbound" | "outbound"
    lane: Optional[str] = None


class LineCounter:
    def __init__(self, y_frac: float = 0.75):
        self.y_frac = y_frac
        self._last_y: Dict[int, float] = {}
        self._counted: set = set()
        self.crossings: List[Crossing] = []

    def update(self, track_id: int, box, vtype: str, frame: int, frame_h: int,
               lane: Optional[str] = None) -> Optional[Crossing]:
        """Feed one tracked box (and its lane, if known); returns the Crossing if this update crossed the line."""
        line = self.y_frac * frame_h
        y = float(box[3])
        prev = self._last_y.get(track_id)
        self._last_y[track_id] = y
        if prev is None or track_id in self._counted:      # each track is counted at most once
            return None
        if prev < line <= y:
            direction = "inbound"
        elif prev >= line > y:
            direction = "outbound"
        else:
            return None
        self._counted.add(track_id)
        c = Crossing(frame, track_id, vtype, direction, lane)
        self.crossings.append(c)
        return c

    @property
    def total(self) -> int:
        return len(self.crossings)

    def by_type(self) -> Dict[str, int]:
        return dict(Counter(c.vtype for c in self.crossings))

    def by_lane(self) -> Dict[str, int]:
        """Crossings per lane ("" = outside every lane)."""
        return dict(Counter(c.lane or "" for c in self.crossings))

    def rate_per_min(self, frame: int, fps: float, window_sec: float = 60.0, min_span_sec: float = 15.0,
                     lane: Optional[str] = None) -> float:
        """Vehicles per minute over the last `window_sec` (one lane if given). Early on the elapsed time
        is short and one vehicle would read as a huge rate, so the span is at least `min_span_sec`."""
        span = max(min(window_sec, frame / fps), min_span_sec)
        recent = sum(1 for c in self.crossings if (frame - c.frame) / fps <= window_sec
                     and (lane is None or c.lane == lane))
        return recent * 60.0 / span

    def time_series(self, fps: float, bin_sec: float = 10.0, start_sec: float = 0.0) -> List[dict]:
        """Counts per `bin_sec` bin: [{"t_start": s, "t_end": e, "total": n, "Car": k, ...}]."""
        bins: Dict[int, Counter] = defaultdict(Counter)
        for c in self.crossings:
            bins[int((c.frame / fps) // bin_sec)][c.vtype] += 1
        if not bins:
            return []
        out = []
        for b in range(0, max(bins) + 1):
            row = {"t_start": round(start_sec + b * bin_sec, 2), "t_end": round(start_sec + (b + 1) * bin_sec, 2),
                   "total": sum(bins[b].values())}
            row.update(bins[b])
            out.append(row)
        return out


class LaneMetrics:
    """Per-lane occupancy and density, accumulated frame by frame, plus the per-lane time series.

    Occupancy is image-plane time occupancy at the count line: the share of frames in which a vehicle
    of that lane covers the line (a virtual loop). It runs higher than a road loop for tall vehicles
    (the box includes the body above the road). Density is the mean number of vehicles whose
    bottom-centre is in the lane polygon, divided by the lane zone's length in km when calibrated.
    """

    def __init__(self, lanes: Sequence[str], fps: float, zone_len_m: Optional[Dict[str, float]] = None):
        self.lanes = list(lanes)
        self.fps = fps
        self.zone_len_m = zone_len_m or {}
        self._frames: List[int] = []
        self._occ: Dict[str, List[int]] = {l: [] for l in self.lanes}       # per frame: line covered 0/1
        self._n: Dict[str, List[int]] = {l: [] for l in self.lanes}         # per frame: vehicles in the zone

    def update(self, frame: int, occupied: Dict[str, bool], in_zone: Dict[str, int]) -> None:
        self._frames.append(frame)
        for l in self.lanes:
            self._occ[l].append(1 if occupied.get(l) else 0)
            self._n[l].append(int(in_zone.get(l, 0)))

    def _recent(self, series: List[int], window_sec: float) -> List[int]:
        k = max(1, int(round(window_sec * self.fps)))
        return series[-k:]

    def occupancy(self, lane: str, window_sec: float = 30.0) -> float:
        """Share of frames (0..1) the lane's stretch of the count line was covered, last `window_sec`."""
        vals = self._recent(self._occ.get(lane, []), window_sec)
        return sum(vals) / len(vals) if vals else 0.0

    def density(self, lane: str, window_sec: float = 30.0) -> Tuple[float, str]:
        """(value, unit): veh/km when the zone length is known, else mean vehicles in the lane zone."""
        vals = self._recent(self._n.get(lane, []), window_sec)
        mean = sum(vals) / len(vals) if vals else 0.0
        L = self.zone_len_m.get(lane)
        return (mean / (L / 1000.0), "veh/km") if L else (mean, "veh")

    def time_series(self, counter: LineCounter, bin_sec: float = 10.0, start_sec: float = 0.0,
                    speeds: Optional[Dict[int, float]] = None) -> Dict[str, List[dict]]:
        """{lane: [{t_start, t_end, count, veh_per_min, occupancy, mean_vehicles, density_veh_km?,
        mean_speed_kmh?}]} per `bin_sec` bin. speeds: track id -> km/h (calibrated cameras only)."""
        if not self._frames:
            return {l: [] for l in self.lanes}
        t_end = self._frames[-1] / self.fps
        bins: Dict[int, List[int]] = defaultdict(list)          # bin -> indices into the per-frame series
        for i, f in enumerate(self._frames):
            bins[int((f / self.fps) // bin_sec)].append(i)
        out: Dict[str, List[dict]] = {}
        for l in self.lanes:
            rows = []
            for b in range(int(t_end // bin_sec) + 1):
                lo, hi = b * bin_sec, (b + 1) * bin_sec
                idx = bins.get(b, [])
                cross = [c for c in counter.crossings if c.lane == l and lo <= c.frame / self.fps < hi]
                span = max(1e-6, min(hi, t_end + 1.0 / self.fps) - lo)
                n_mean = sum(self._n[l][i] for i in idx) / len(idx) if idx else 0.0
                row = {"t_start": round(start_sec + lo, 2), "t_end": round(start_sec + hi, 2), "count": len(cross),
                       "veh_per_min": round(len(cross) * 60.0 / span, 1),
                       "occupancy": round(sum(self._occ[l][i] for i in idx) / len(idx), 3) if idx else 0.0,
                       "mean_vehicles": round(n_mean, 2)}
                if self.zone_len_m.get(l):
                    row["density_veh_km"] = round(n_mean / (self.zone_len_m[l] / 1000.0), 1)
                vs = [speeds[c.track_id] for c in cross if speeds and c.track_id in speeds]
                if vs:
                    row["mean_speed_kmh"] = round(sum(vs) / len(vs), 1)
                rows.append(row)
            out[l] = rows
        return out
