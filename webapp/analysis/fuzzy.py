"""OCR-aware fuzzy plate matching.

A plain edit distance counts every wrong character as 1. OCR errors are not random: look-alike characters (O / 0,
B / 8, H / R ...) are confused far more often than others, so substituting one for its look-alike costs only 0.5
here: searching "XSD 4OO4" (two letters O typed for zeros) finds XSD 4004 at distance 1.0, so a tolerance of 1
covers two look-alike swaps or one other wrong character. Insertions and deletions cost 1.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

LOOK_ALIKE = [("O", "0"), ("Q", "0"), ("D", "0"), ("O", "D"), ("O", "Q"), ("I", "1"), ("L", "1"), ("T", "1"),
              ("I", "L"), ("B", "8"), ("S", "5"), ("Z", "2"), ("G", "6"), ("A", "4"), ("H", "R"), ("M", "W"),
              ("U", "V"), ("V", "Y"), ("E", "F"), ("P", "R"), ("C", "G"), ("K", "X")]
_CHEAP = {frozenset(p) for p in LOOK_ALIKE}
LOOK_ALIKE_COST = 0.5


def compact(text: str) -> str:
    """'vjf-1234 ' -> 'VJF1234' (the canonical form used everywhere)."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return LOOK_ALIKE_COST if frozenset((a, b)) in _CHEAP else 1.0


def plate_distance(a: str, b: str) -> float:
    """Weighted edit distance between two compact plates (look-alike substitutions cost 0.5)."""
    a, b = compact(a), compact(b)
    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)] + [0.0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + sub_cost(ca, cb))
        prev = cur
    return prev[-1]


def best_matches(query: str, candidates: Iterable[str], tolerance: float) -> Dict[str, float]:
    """{candidate: distance} for every candidate within `tolerance` of the query."""
    return PlateIndex(list(candidates)).matches(query, tolerance)


# ---------------------------------------------------------------------------- many candidates at once
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_CODE = {ch: i for i, ch in enumerate(ALPHABET)}
_PAD = len(ALPHABET)


def _cost_matrix():
    import numpy as np
    m = np.ones((_PAD + 1, _PAD + 1), np.float32)
    np.fill_diagonal(m, 0.0)
    for a, b in LOOK_ALIKE:
        m[_CODE[a], _CODE[b]] = m[_CODE[b], _CODE[a]] = LOOK_ALIKE_COST
    return m


class PlateIndex:
    """The same weighted edit distance as `plate_distance`, for all plates at once: the Wagner-Fischer table is
    filled for every candidate in parallel with numpy (one row per query character, one vectorised step per
    column), so 100,000 distinct plates take tens of milliseconds instead of seconds."""

    def __init__(self, plates: List[str]):
        import numpy as np
        self.plates = [compact(p) for p in plates]
        self.width = max((len(p) for p in self.plates), default=1)
        self.codes = np.full((len(self.plates), self.width), _PAD, np.uint8)
        for i, p in enumerate(self.plates):
            self.codes[i, :len(p)] = [_CODE[ch] for ch in p]
        self.lengths = np.array([len(p) for p in self.plates], np.int32)
        self.cost = _cost_matrix()

    def distances(self, query: str):
        import numpy as np
        q = [_CODE[ch] for ch in compact(query) if ch in _CODE]
        n, w = len(self.plates), self.width
        prev = np.tile(np.arange(w + 1, dtype=np.float32), (n, 1))          # row 0: insert j candidate characters
        for i, qc in enumerate(q, 1):
            cur = np.empty_like(prev)
            cur[:, 0] = i
            sub = self.cost[qc][self.codes]                                  # (n, w) substitution costs
            for j in range(1, w + 1):
                cur[:, j] = np.minimum(np.minimum(prev[:, j] + 1, cur[:, j - 1] + 1), prev[:, j - 1] + sub[:, j - 1])
            prev = cur
        return prev[np.arange(n), self.lengths]

    def matches(self, query: str, tolerance: float) -> Dict[str, float]:
        if not self.plates:
            return {}
        q = compact(query)
        if tolerance <= 0:
            return {p: 0.0 for p in self.plates if p == q}
        d = self.distances(q)
        return {self.plates[i]: float(d[i]) for i in (d <= tolerance + 1e-6).nonzero()[0]}


def wildcard_regex(pattern: str) -> "re.Pattern":
    """'XS*' / 'W?1234' (the * and ? wildcards) -> a regex over compact plates."""
    body = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in re.sub(r"[^A-Z0-9*?]", "",
                                                                                               pattern.upper()))
    return re.compile(f"^{body}$")


def sorted_matches(matches: Dict[str, float]) -> List[Tuple[str, float]]:
    return sorted(matches.items(), key=lambda kv: (kv[1], kv[0]))
