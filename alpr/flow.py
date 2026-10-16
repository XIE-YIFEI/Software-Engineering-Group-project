"""Traffic-flow counting on tracked vehicles (pure logic, no models).

A vehicle is counted once, when the bottom-centre of its box crosses a horizontal counting line.
Moving down the image (towards a camera that faces oncoming traffic) is "inbound", moving up is
"outbound". Counts are kept per vehicle type and per time bin, which is the time series a later
traffic-flow prediction model can train on.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Crossing:
    frame: int
    track_id: int
    vtype: str
    direction: str        # "inbound" | "outbound"


class LineCounter:
    def __init__(self, y_frac: float = 0.75):
        self.y_frac = y_frac
        self._last_y: Dict[int, float] = {}
        self._counted: set = set()
        self.crossings: List[Crossing] = []

    def update(self, track_id: int, box, vtype: str, frame: int, frame_h: int) -> Optional[Crossing]:
        """Feed one tracked box; returns the Crossing if this update crossed the line."""
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
        c = Crossing(frame, track_id, vtype, direction)
        self.crossings.append(c)
        return c

    @property
    def total(self) -> int:
        return len(self.crossings)

    def by_type(self) -> Dict[str, int]:
        return dict(Counter(c.vtype for c in self.crossings))

    def rate_per_min(self, frame: int, fps: float, window_sec: float = 60.0, min_span_sec: float = 15.0) -> float:
        """Vehicles per minute over the last `window_sec`. Early on the elapsed time is short and one
        vehicle would read as a huge rate, so the span is at least `min_span_sec`."""
        span = max(min(window_sec, frame / fps), min_span_sec)
        recent = sum(1 for c in self.crossings if (frame - c.frame) / fps <= window_sec)
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
