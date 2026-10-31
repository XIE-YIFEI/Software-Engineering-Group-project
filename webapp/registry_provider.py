"""Where vehicle registration data comes from.

Today: the SIMULATED demo registry of alpr/registry.py (fictitious X-series vehicles + neutral placeholders for the
real plates of the demo footage; Malaysian JPJ registration data is not public). An authorised data source would be
another class with the same four members, chosen in `get_provider()`; no other code needs to change.
"""
from __future__ import annotations

import threading
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Protocol


class RegistryProvider(Protocol):
    name: str
    is_simulated: bool

    def lookup(self, compact: str) -> Optional[Dict]:
        """The registration record of a plate (body_type, colour, make_model, road_tax_expiry, status, note) or None."""

    def sightings(self, compact: str) -> List[Dict]:
        """Sightings of the plate known to the registry side (other operators' cameras): seen_at, location, lat, lon."""


class SimulatedRegistry:
    """alpr.registry.Registry (SQLite) behind the provider interface. Built on first use when missing. One
    connection per thread: sqlite3 connections of alpr.registry may only be used by the thread that opened them."""
    name = "SIMULATED demo registry (alpr/registry.py)"
    is_simulated = True

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self.disclaimer = self._reg.get_meta("disclaimer") or ""

    @property
    def _reg(self):
        reg = getattr(self._local, "reg", None)
        if reg is None:
            from alpr.registry import Registry
            reg = self._local.reg = Registry.open(str(self.path))
        return reg

    def lookup(self, compact: str) -> Optional[Dict]:
        rec = self._reg.lookup(compact)
        if rec is None:
            return None
        d = asdict(rec)
        d["road_tax_expiry"] = rec.road_tax_expiry.isoformat() if rec.road_tax_expiry else None
        return d

    def sightings(self, compact: str) -> List[Dict]:
        return [{"plate": s.plate, "seen_at": s.seen_at.isoformat(), "location": s.location, "lat": s.lat,
                 "lon": s.lon, "simulated": True} for s in self._reg.sightings(compact)]


@lru_cache(maxsize=4)
def get_provider(path: str) -> SimulatedRegistry:
    return SimulatedRegistry(Path(path))
