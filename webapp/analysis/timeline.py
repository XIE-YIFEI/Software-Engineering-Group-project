"""Cross-camera timeline of one plate: its sightings in time order, with the distance, time gap and implied speed
between consecutive ones, and impossible travel flagged with the cloned-plate rule of alpr/registry.py
(`check_travel`: faster than `max_speed_kmh`; legs under 2 km - the same site - never count)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from alpr.registry import Sighting, check_travel, haversine_km


@dataclass
class Point:
    seen_at: datetime
    place: str
    lat: Optional[float]
    lon: Optional[float]
    provenance: str                   # camera | inserted_plate | synthetic
    source: str                       # "camera" (our sightings) or "registry" (external sighting)
    sighting_id: Optional[int] = None
    run_label: str = ""


@dataclass
class Leg:
    km: Optional[float]               # None when a camera has no position
    seconds: float
    speed_kmh: Optional[float]        # None: no position, or no time between the sightings
    impossible: bool
    message: str


@dataclass
class Chain:
    points: List[Point]
    legs: List[Leg] = field(default_factory=list)

    @property
    def impossible(self) -> bool:
        return any(l.impossible for l in self.legs)


def build(points: List[Point], max_speed_kmh: float = 250.0, min_km: float = 2.0) -> Chain:
    pts = sorted(points, key=lambda p: p.seen_at)
    chain = Chain(pts)
    for a, b in zip(pts, pts[1:]):
        secs = (b.seen_at - a.seen_at).total_seconds()
        if None in (a.lat, a.lon, b.lat, b.lon):
            chain.legs.append(Leg(None, secs, None, False, "distance unknown (a camera has no position)"))
            continue
        km = haversine_km((a.lat, a.lon), (b.lat, b.lon))
        alert = check_travel(b.seen_at, (b.lat, b.lon), [Sighting("", a.seen_at, a.place, a.lat, a.lon)],
                             max_speed_kmh, min_km)
        speed = km / (secs / 3600.0) if secs > 0 else None
        if km < min_km:
            msg = f"{km:.0f} km (same site)"
        elif alert is not None:
            msg = (f"{km:.0f} km in {_dur(secs)}: {speed:.0f} km/h needed" if speed else f"{km:.0f} km at the same time"
                   ) + f" - impossible (more than {max_speed_kmh:.0f} km/h): cloned plate?"
        else:
            msg = f"{km:.0f} km in {_dur(secs)}: {speed:.0f} km/h" if speed else f"{km:.0f} km"
        chain.legs.append(Leg(round(km, 1), secs, round(speed, 1) if speed else None, alert is not None, msg))
    return chain


def _dur(secs: float) -> str:
    secs = abs(secs)
    h, rem = divmod(int(round(secs)), 3600)
    m, s = divmod(rem, 60)
    return (f"{h} h {m} min" if h else f"{m} min {s} s" if m else f"{s} s")
