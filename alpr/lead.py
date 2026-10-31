"""Lead-target selection with ego-lane bias and hysteresis (pure logic).

The old rule (largest box wins) let a big vehicle in the next lane steal the HUD focus and made it
flicker between two similar-sized vehicles. Score = relative area x lane-centering factor, and the
current lead is kept unless a rival beats it by `hysteresis`.
"""
from __future__ import annotations

from typing import Dict, List, Optional


class LeadSelector:
    def __init__(self, lane_weight: float = 0.5, hysteresis: float = 1.25, min_width_frac: float = 0.0):
        self.lane_weight = lane_weight
        self.hysteresis = hysteresis
        self.min_width_frac = min_width_frac
        self.current: Optional[int] = None

    def _score(self, box, frame_w: int, frame_h: int) -> float:
        x1, y1, x2, y2 = box
        area = max(0, x2 - x1) * max(0, y2 - y1) / float(frame_w * frame_h)
        off = min(1.0, abs((x1 + x2) / 2.0 - frame_w / 2.0) / (frame_w / 2.0))
        return area * (1.0 - self.lane_weight * off)

    def pick(self, vehicles: List[Dict], frame_w: int, frame_h: int) -> Optional[int]:
        """vehicles: dicts with 'track_id' and 'box'. Returns the lead track_id (or None)."""
        scored = {}
        for v in vehicles:
            x1, _, x2, _ = v["box"]
            if (x2 - x1) >= self.min_width_frac * frame_w:
                scored[v["track_id"]] = self._score(v["box"], frame_w, frame_h)
        if not scored:
            self.current = None
            return None
        best = max(scored, key=scored.get)
        if self.current in scored and scored[self.current] * self.hysteresis >= scored[best]:
            return self.current
        self.current = best
        return best
