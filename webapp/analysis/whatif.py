"""What-if "open more lanes": a deterministic point-queue model of extra discharge capacity.

    Q'(t) = max(0, Q'(t-1) + dQ(t) - p * mu)      for t after the start (the warning time); Q' = Q before it

Q is the measured queue (fill x jam capacity of the section: vehicles inside the camera's view), dQ its measured
change per minute, mu the section's discharge capacity (vehicles per minute) and p the extra share (0.1 = +10 %).
The CIQ cameras measure how full the road is, not vehicles per minute (TEAM_GUIDE.md section 10.6), so mu is either
an assumption typed in by the user or estimated from the fastest sustained clearing of the queue - a lower bound,
because vehicles keep arriving while the queue clears. It is an ESTIMATE UNDER STATED ASSUMPTIONS (ASSUMPTIONS).
More capacity never lengthens the queue: Q'(t) <= Q(t) for every t.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

ASSUMPTIONS = [
    "Point queue: the vehicles in the section's zones form one queue that discharges at its head (the CIQ end).",
    "Arrivals are unchanged by the measure: no extra traffic is attracted and nobody changes route or time.",
    "The extra capacity is fully available from the start time on, with no ramp-up.",
    "The discharge capacity mu is an assumption, or roughly estimated from the fastest sustained clearing of the "
    "queue, which is a lower bound (vehicles keep arriving while it clears).",
    "Only the part of the queue inside the camera's view is modelled. When the queue is longer than the view, the "
    "measured fill stops growing (it saturates) and the projection overstates the gain.",
    "The measured queue has an error of 22-33 % per frame (less after the 1-minute averaging) and the model was "
    "validated on 2 days of one road only.",
]


@dataclass
class WhatIf:
    capacity_pct: float
    mu: float
    mu_source: str                      # estimated | assumed
    mu_estimate: Optional[float]
    mu_estimated_at: Optional[int]      # index of the minute that ends the fastest clearing window
    start: int                          # index of the first minute with extra capacity
    baseline: List[Optional[float]]     # measured queue (vehicles)
    projected: List[Optional[float]]
    threshold: float                    # congestion: queue >= this (vehicles) = fill >= 0.5
    congested_minutes: Tuple[int, int]  # (baseline, projected), from the start on
    peak: Tuple[float, float]
    assumptions: List[str] = field(default_factory=lambda: list(ASSUMPTIONS))


def estimate_mu(queue: Sequence[Optional[float]], window: int = 10) -> Tuple[Optional[float], Optional[int]]:
    """Fastest sustained decrease of the queue over `window` minutes (vehicles per minute) and where it ends."""
    best, at = None, None
    for i in range(window, len(queue)):
        a, b = queue[i - window], queue[i]
        if a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
            continue
        rate = (a - b) / window
        if rate > 0 and (best is None or rate > best):
            best, at = rate, i
    return best, at


def project(queue: Sequence[Optional[float]], start: int, capacity_pct: float, mu: float) -> List[Optional[float]]:
    """Q' of the module docstring. Missing minutes (None) keep the projected queue unchanged."""
    out: List[Optional[float]] = []
    last_q, last_p = None, None
    for i, q in enumerate(queue):
        if q is None or (isinstance(q, float) and math.isnan(q)):
            out.append(None)
            continue
        if i < start or last_p is None:
            p = q
        else:
            p = max(0.0, last_p + (q - last_q) - capacity_pct / 100.0 * mu)
            p = min(p, q)                                   # extra capacity never makes the queue longer
        out.append(round(p, 2))
        last_q, last_p = q, p
    return out


def run(queue: Sequence[Optional[float]], start: int, capacity_pct: float, threshold: float,
        mu: Optional[float] = None) -> WhatIf:
    est, est_at = estimate_mu(queue)
    if mu is None:
        if est is None:
            raise ValueError("the queue never clears on this day, so mu cannot be estimated: enter an assumed value")
        mu_used, src = est, "estimated"
    else:
        if mu <= 0:
            raise ValueError("mu must be positive (vehicles per minute)")
        mu_used, src = float(mu), "assumed"
    proj = project(queue, start, capacity_pct, mu_used)
    base = [None if q is None or (isinstance(q, float) and math.isnan(q)) else round(float(q), 2) for q in queue]
    tail = range(start, len(base))
    cong = (sum(1 for i in tail if base[i] is not None and base[i] >= threshold),
            sum(1 for i in tail if proj[i] is not None and proj[i] >= threshold))
    peak = (max((base[i] for i in tail if base[i] is not None), default=0.0),
            max((proj[i] for i in tail if proj[i] is not None), default=0.0))
    return WhatIf(capacity_pct, round(mu_used, 3), src, None if est is None else round(est, 3), est_at, start,
                  base, proj, threshold, cong, peak)
