"""Per-track multi-frame voting over plate OCR readings (pure logic, no models).

Single-frame OCR is noisy (blur, glare, angle), and overwriting the previous result with the
latest reading lets one bad frame decide the final plate. Instead every valid reading of a track
is kept with a weight (OCR confidence x crop quality, where the video pipeline's quality includes the
plate's resolution) and the final plate is a weighted
*character-wise* consensus over readings of the modal length.

The reported confidence is derived from the votes, not made up:
    confidence = weighted-mean OCR confidence  x  length agreement  x  character agreement
so a plate read identically in every frame scores close to the OCR confidence, while a plate
that wobbles between readings scores low and stays TENTATIVE.

Non-standard plates: a reading the OCR itself marks as no legal Malaysian plate (``valid=False``,
e.g. ``WXY 12345`` - five digits) is NOT forced into a legal layout (trimming the last character
would turn it into the legal ``WXY 1234``). Such readings are tallied separately; when they outweigh
the legal readings of the track, the track's result is that non-standard text with ``valid=False``,
which the registry checks report as ILLEGAL_FORMAT.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .plates import PlateParse, normalize, parse_plate

ACQUIRING, TENTATIVE, CONFIRMED = "ACQUIRING", "TENTATIVE", "CONFIRMED"


@dataclass(frozen=True)
class PlateResult:
    text: str              # display form, e.g. "VJF 1234"
    compact: str           # "VJF1234"
    confidence: float      # 0..1, see module docstring
    votes: int
    status: str            # ACQUIRING / TENTATIVE / CONFIRMED
    valid: bool = True     # False: consistently read as a non-standard (illegal-format) plate


@dataclass
class _Reading:
    compact: str
    parse: PlateParse
    conf: float
    weight: float


class PlateVoter:
    def __init__(self, min_votes: int = 3, confirm_conf: float = 0.6,
                 max_readings: int = 40, max_corrections: int = 1,
                 resolve_ligatures: bool = True, min_char_agreement: float = 0.6):
        self.min_votes = min_votes
        self.confirm_conf = confirm_conf
        self.max_readings = max_readings
        self.max_corrections = max_corrections
        self.resolve_ligatures = resolve_ligatures
        self.min_char_agreement = min_char_agreement
        self._readings: Dict[int, List[_Reading]] = defaultdict(list)
        self._nonstd: Dict[int, List[Tuple[str, float, float]]] = defaultdict(list)   # (text, conf, weight)

    def add(self, track_id: int, raw_text, conf: float, quality: float = 1.0,
            valid: Optional[bool] = None) -> Optional[PlateParse]:
        """Add one OCR reading. `valid=False`: the OCR says it is no legal plate - tallied as a
        non-standard reading. Otherwise readings that are not a plausible plate are ignored (None)."""
        if valid is False:
            text = normalize(raw_text)
            if len(text) >= 3:
                bucket = self._nonstd[track_id]
                bucket.append((text, min(max(conf, 0.0), 1.0), max(conf * min(max(quality, 0.0), 1.0), 1e-4)))
                if len(bucket) > self.max_readings:
                    bucket.remove(min(bucket, key=lambda r: r[2]))
            return None
        parse = parse_plate(raw_text, self.max_corrections)
        if parse is None:
            return None
        conf = min(max(conf, 0.0), 1.0)
        quality = min(max(quality, 0.0), 1.0)
        weight = max(conf * quality, 1e-4)
        bucket = self._readings[track_id]
        bucket.append(_Reading(parse.compact, parse, conf, weight))
        if len(bucket) > self.max_readings:
            bucket.remove(min(bucket, key=lambda r: r.weight))  # drop the weakest evidence
        return parse

    def votes(self, track_id: int) -> int:
        return len(self._readings.get(track_id, ()))

    def result(self, track_id: int) -> Optional[PlateResult]:
        readings = self._readings.get(track_id)
        nonstd = self._nonstd.get(track_id)
        if nonstd and sum(r[2] for r in nonstd) > sum(r.weight for r in readings or ()):
            return self._nonstd_result(nonstd)
        if not readings:
            return None

        # Align optical ligatures (e.g. 'W' <-> 'VJ', 'VF' <-> 'VJF') when an un-merged
        # higher-resolution candidate was observed for this track, or when strong co-evidence exists.
        if self.resolve_ligatures:
            compacts = {r.compact for r in readings}
            aligned = []
            for r in readings:
                compact, parse = r.compact, r.parse
                if len(compact) == 6:
                    for lig_prefix in ("VJ", "VV"):
                        cand = lig_prefix + compact[1:]
                        if cand in compacts:
                            if compact.startswith("W") or (compact.startswith(lig_prefix[0]) and compact[1:] == cand[2:]):
                                p = parse_plate(cand, max_corrections=0)
                                if p:
                                    compact, parse = p.compact, p
                                    break
                        elif compact.startswith("W"):
                            v_matches = [x for x in readings if x.compact.startswith(lig_prefix[0]) and x.compact[1:] == compact[1:]]
                            if len(v_matches) >= 2:
                                p = parse_plate(cand, max_corrections=0)
                                if p:
                                    compact, parse = p.compact, p
                                    break
                        elif compact.startswith(lig_prefix[0]) and compact[1:] == cand[2:]:
                            w_matches = [x for x in readings if x.compact.startswith("W") and x.compact[1:] == cand[2:]]
                            if len(w_matches) >= 2:
                                p = parse_plate(cand, max_corrections=0)
                                if p:
                                    compact, parse = p.compact, p
                                    break
                aligned.append(_Reading(compact, parse, r.conf, r.weight))
            readings = aligned

        total_w = sum(r.weight for r in readings)

        # Modal length by weight, then character-wise weighted vote among those readings.
        by_len: Dict[int, float] = defaultdict(float)
        for r in readings:
            by_len[len(r.compact)] += r.weight
        modal_len = max(by_len, key=by_len.get)
        length_share = by_len[modal_len] / total_w
        cands = [r for r in readings if len(r.compact) == modal_len]
        cand_w = sum(r.weight for r in cands)

        chars, agreements = [], []
        for pos in range(modal_len):
            tally: Dict[str, float] = defaultdict(float)
            for r in cands:
                tally[r.compact[pos]] += r.weight
            winner = max(tally, key=tally.get)
            chars.append(winner)
            agreements.append(tally[winner] / cand_w)
        consensus = "".join(chars)
        char_agreement = sum(agreements) / len(agreements)

        # Position-wise winners can in principle form an illegal plate; fall back to best string.
        parse = parse_plate(consensus, max_corrections=0)
        if parse is None:
            tally_s: Dict[str, float] = defaultdict(float)
            for r in cands:
                tally_s[r.compact] += r.weight
            best = max(tally_s, key=tally_s.get)
            parse = next(r.parse for r in cands if r.compact == best)

        mean_conf = sum(r.conf * r.weight for r in readings) / total_w
        confidence = mean_conf * length_share * char_agreement
        n = len(readings)
        if n < self.min_votes:
            status = ACQUIRING
        elif confidence >= self.confirm_conf and min(agreements) >= self.min_char_agreement:
            # every character needs a clear majority: one 50/50 character (H vs R) keeps the plate
            # TENTATIVE instead of flipping between two "confirmed" readings
            status = CONFIRMED
        else:
            status = TENTATIVE
        return PlateResult(parse.text, parse.compact, round(confidence, 4), n, status)

    def _nonstd_result(self, nonstd: List[Tuple[str, float, float]]) -> PlateResult:
        """Consensus over non-standard readings: the heaviest string, confidence scaled by its share."""
        tally: Dict[str, float] = defaultdict(float)
        for text, _, w in nonstd:
            tally[text] += w
        best = max(tally, key=tally.get)
        total = sum(tally.values())
        share = tally[best] / total
        mean_conf = sum(c * w for _, c, w in nonstd) / total
        confidence = mean_conf * share
        n = len(nonstd)
        if n < self.min_votes:
            status = ACQUIRING
        elif confidence >= self.confirm_conf and share >= self.min_char_agreement:
            status = CONFIRMED
        else:
            status = TENTATIVE
        return PlateResult(display_nonstandard(best), best, round(confidence, 4), n, status, valid=False)


def display_nonstandard(text: str) -> str:
    """'WXY12345' -> 'WXY 12345': a space at every letter/digit boundary."""
    return re.sub(r"(?<=[A-Z])(?=[0-9])|(?<=[0-9])(?=[A-Z])", " ", text)
