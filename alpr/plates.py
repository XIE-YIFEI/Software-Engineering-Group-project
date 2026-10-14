"""Malaysian plate syntax + position-aware correction of OCR output (pure functions).

A standard Malaysian plate is  PREFIX(1-3 letters) NUMBER(1-4 digits) [SUFFIX(1 letter)],
e.g. ``VJF 1234``, ``W 1234 A``, ``SAA 1234 B`` (Sabah), ``QAA 1234 B`` (Sarawak). Special
(vanity) series replace the prefix with a fixed word: ``GOLD 1460``, ``1M4U 792``, ``PUTRAJAYA 1``.
Two-row plates (``KFQ`` over ``1133``) are the same string once the rows are joined.

Two entry points, depending on what the OCR engine gives us:

* ``parse_plate(raw)``: a raw OCR *string*. It is matched against all legal (prefix, digits, suffix)
  layouts of the same length. In each slot the character type is known (letter or digit), so
  look-alikes are fixed by slot (``0<->O``, ``8<->B``, ``1<->I`` ...). The layout needing the fewest
  corrections wins, and a string that needs too many corrections (or fits no layout, e.g. ``LUMPUR``,
  ``AIA``) is rejected.
* ``decode_slots(probs, alphabet)``: the per-slot probability matrix of a fixed-slot plate OCR model
  (fast-plate-ocr). Instead of correcting the top-1 string afterwards, the most probable *legal*
  plate is searched directly, so a slot can fall back to its 2nd-best character when the 1st-best
  one is illegal there (``OWT 9605`` -> ``JWT 9605``: no plate starts with O).

Not supported: diplomatic plates and dealer/trade plates.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Union

import numpy as np

# What an OCR engine typically returns when the true character is a letter / digit.
DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}
LETTER_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
                   "A": "4", "S": "5", "G": "6", "B": "8", "T": "7"}

SUFFIX_COST = 0.25        # prior: plates without a suffix letter are more common
LEADING_ZERO_COST = 0.5   # registration numbers do not start with 0
TRIM_COST = 1.0           # dropping one edge char (border/emblem read as a letter) is one edit, the
                          # same price as one in-slot correction; ties keep the untrimmed reading

# Special series that do not fit the standard layout (longer than 3 letters, or containing digits).
# Short all-letter series (VIP, GTR, US ...) already parse as standard plates. Extend as needed.
SPECIAL_PREFIXES = ("PUTRAJAYA", "MALAYSIA", "PATRIOT", "XIIINAM", "PERFECT", "BAMBEE", "MADANI",
                    "SUKOM", "RIMAU", "KRISS", "NAAM", "XOIC", "UITM", "GOLD", "VIPS", "LIMO",
                    "1M4U", "G1M")

# First letter of a standard plate -> registration state / category.
STATE_BY_PREFIX = {
    "A": "Perak", "B": "Selangor", "C": "Pahang", "D": "Kelantan", "F": "Putrajaya", "J": "Johor",
    "K": "Kedah", "L": "Labuan", "M": "Melaka", "N": "Negeri Sembilan", "P": "Pulau Pinang",
    "Q": "Sarawak", "R": "Perlis", "S": "Sabah", "T": "Terengganu", "V": "Kuala Lumpur",
    "W": "Kuala Lumpur", "H": "Taxi", "Z": "Military",
}
# Slot decoding: a standard plate whose first letter is not a known prefix (E, G, I, O, U, X, Y)
# is possible but rare. This prior only decides near-ties, it does not forbid such plates.
UNKNOWN_FIRST_LETTER_PRIOR = 0.05
# Slot decoding: accept the best legal plate only if it is at least this likely relative to the
# unconstrained top-1 reading; otherwise the plate is reported as non-standard (``valid=False``).
MIN_LEGAL_RATIO = 1e-3


@dataclass(frozen=True)
class PlateParse:
    prefix: str
    number: str
    suffix: str
    corrections: int      # characters changed by position-aware correction
    trimmed: int = 0      # edge characters dropped as OCR artefacts (0 or 1)
    special: bool = False  # special/vanity series (prefix is one of SPECIAL_PREFIXES)

    @property
    def compact(self) -> str:
        return f"{self.prefix}{self.number}{self.suffix}"

    @property
    def text(self) -> str:
        parts = [self.prefix, self.number] + ([self.suffix] if self.suffix else [])
        return " ".join(parts)

    @property
    def region(self) -> str:
        return plate_region(self)


def plate_region(parse: PlateParse) -> str:
    """Registration state/category of a parsed plate ("Johor", "Taxi", "Special series" ...)."""
    if parse.special:
        return "Special series"
    if parse.prefix.startswith("KV"):
        return "Langkawi (Kedah)"
    return STATE_BY_PREFIX.get(parse.prefix[:1], "Unknown")


def normalize(raw: Union[str, Iterable[str]]) -> str:
    """Join OCR fragments, upper-case, keep only A-Z0-9."""
    if not isinstance(raw, str):
        raw = "".join(raw)
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def _fit(text: str, n_prefix: int, n_digits: int, n_suffix: int):
    """Correct `text` into the layout; return (corrected, n_corrections) or None if impossible."""
    kinds = "L" * n_prefix + "D" * n_digits + "L" * n_suffix
    out, changes = [], 0
    for ch, kind in zip(text, kinds):
        if kind == "L":
            if ch.isalpha():
                out.append(ch)
            elif ch in DIGIT_TO_LETTER:
                out.append(DIGIT_TO_LETTER[ch]); changes += 1
            else:
                return None
        else:
            if ch.isdigit():
                out.append(ch)
            elif ch in LETTER_TO_DIGIT:
                out.append(LETTER_TO_DIGIT[ch]); changes += 1
            else:
                return None
    return "".join(out), changes


def _lookalike(ch: str, target: str) -> bool:
    return DIGIT_TO_LETTER.get(ch) == target or LETTER_TO_DIGIT.get(ch) == target


def _layout_cost(extra_cost: float, changes: int, number: str, n_suffix: int) -> float:
    return (extra_cost + changes + (SUFFIX_COST if n_suffix else 0.0)
            + (LEADING_ZERO_COST if number[0] == "0" else 0.0))


def _best_layout(text: str, max_corrections: int, extra_cost: float, trimmed: int):
    """Cheapest legal layout of `text` as (cost, PlateParse), or None."""
    n = len(text)
    best = None
    for n_prefix in (1, 2, 3):
        for n_suffix in (0, 1):
            n_digits = n - n_prefix - n_suffix
            if not 1 <= n_digits <= 4:
                continue
            fitted = _fit(text, n_prefix, n_digits, n_suffix)
            if fitted is None:
                continue
            fixed, changes = fitted
            if changes > max_corrections:
                continue
            # A plate must show at least one real digit and one real letter; otherwise letter-only
            # words ("AIA", "LUMPUR") or digit-only strings ("1234") could be "rescued" by swapping
            # look-alike characters.
            if not any(c.isdigit() for c in text[n_prefix:n_prefix + n_digits]):
                continue
            if not any(c.isalpha() for c in text[:n_prefix]):
                continue
            number = fixed[n_prefix:n_prefix + n_digits]
            cost = _layout_cost(extra_cost, changes, number, n_suffix)
            parse = PlateParse(fixed[:n_prefix], number, fixed[n_prefix + n_digits:], changes, trimmed)
            if best is None or cost < best[0]:
                best = (cost, parse)
    found = _best_special(text, max_corrections, extra_cost, trimmed)
    if found and (best is None or found[0] < best[0]):
        best = found
    return best


def _best_special(text: str, max_corrections: int, extra_cost: float, trimmed: int):
    """Cheapest special-series reading (e.g. ``GOLD1460``, ``IM4U555`` -> ``1M4U 555``), or None."""
    best = None
    for word in SPECIAL_PREFIXES:
        k = len(word)
        if len(text) <= k or not all(c == t or _lookalike(c, t) for c, t in zip(text, word)):
            continue
        prefix_changes = sum(c != t for c, t in zip(text, word))
        rest = text[k:]
        for n_suffix in (0, 1):
            n_digits = len(rest) - n_suffix
            if not 1 <= n_digits <= 4 or not any(c.isdigit() for c in rest[:n_digits]):
                continue
            fitted = _fit(rest, 0, n_digits, n_suffix)
            if fitted is None or prefix_changes + fitted[1] > max_corrections:
                continue
            fixed, changes = fitted[0], prefix_changes + fitted[1]
            cost = _layout_cost(extra_cost, changes, fixed[:n_digits], n_suffix)
            if best is None or cost < best[0]:
                best = (cost, PlateParse(word, fixed[:n_digits], fixed[n_digits:], changes, trimmed, True))
    return best


def parse_plate(raw: Union[str, Iterable[str]], max_corrections: int = 1) -> Optional[PlateParse]:
    """Best Malaysian-format interpretation of `raw`, or None if it is not a plausible plate.

    Besides the full string, the string with its first or last character dropped is also tried
    (cost TRIM_COST): OCR often reads a plate border, bolt or emblem as an extra edge character.
    """
    text = normalize(raw)
    candidates = [(text, 0.0, 0)]
    if len(text) >= 5:
        candidates += [(text[1:], TRIM_COST, 1), (text[:-1], TRIM_COST, 1)]
    best = None
    for t, extra, trimmed in candidates:
        found = _best_layout(t, max_corrections, extra, trimmed)
        if found and (best is None or found[0] < best[0]):
            best = found
    return best[1] if best else None


# ---------------------------------------------------------------------------- slot decoding
@dataclass(frozen=True)
class SlotDecode:
    raw: str                      # unconstrained top-1 text (padding removed)
    raw_conf: float               # geometric-mean probability of the top-1 path
    parse: Optional[PlateParse]   # most probable legal plate (None if nothing legal is plausible)
    conf: float                   # geometric-mean probability of that plate's path (0 if parse is None)

    @property
    def valid(self) -> bool:
        return self.parse is not None


# Characters a special-series word may show in the plate font ("IM4U" is how 1M4U is often styled).
_SPECIAL_EQUIV = {"1": "1I", "I": "I1", "0": "0O", "O": "O0"}


def decode_slots(probs: np.ndarray, alphabet: str, pad_char: str = "_",
                 min_legal_ratio: float = MIN_LEGAL_RATIO) -> SlotDecode:
    """Most probable legal Malaysian plate under a fixed-slot OCR output.

    probs: (slots, len(alphabet)) per-slot probabilities, text left-aligned, padded with `pad_char`.
    The search is exact: for every layout the best character per slot is independent given its
    type (letter / digit / pad), so each layout costs O(slots) and there are only a few dozen.
    """
    logp = np.log(np.clip(np.asarray(probs, dtype=np.float64), 1e-12, 1.0))
    n_slots = logp.shape[0]
    index = {c: i for i, c in enumerate(alphabet)}

    top = logp.argmax(1)
    raw = "".join(alphabet[i] for i in top).replace(pad_char, "")
    raw_score = float(logp.max(1).sum())

    def best_of(chars: str, prior: Optional[np.ndarray] = None):
        cols = [index[c] for c in chars if c in index]
        sub = logp[:, cols] + (0.0 if prior is None else prior)
        return sub.max(1), [chars[j] for j in sub.argmax(1)]

    letters = "".join(c for c in alphabet if c.isalpha())
    first_prior = np.array([0.0 if c in STATE_BY_PREFIX else math.log(UNKNOWN_FIRST_LETTER_PRIOR)
                            for c in letters])
    L, L_ch = best_of(letters)
    L0, L0_ch = best_of(letters, first_prior)
    D, D_ch = best_of("0123456789")
    DNZ, DNZ_ch = best_of("123456789")
    pad = logp[:, index[pad_char]]
    pad_tail = np.concatenate([np.cumsum(pad[::-1])[::-1], [0.0]])   # pad_tail[n] = sum(pad[n:])

    def tail(start: int, n_digits: int, n_suffix: int):
        """Score + text of NUMBER [SUFFIX] + padding starting at slot `start`."""
        end = start + n_digits + n_suffix
        if end > n_slots:
            return None
        s = DNZ[start] + D[start + 1:start + n_digits].sum() + pad_tail[end]
        t = DNZ_ch[start] + "".join(D_ch[start + 1:start + n_digits])
        suffix = ""
        if n_suffix:
            s += L[end - 1]
            suffix = L_ch[end - 1]
        return s, t, suffix

    best = None   # (score, PlateParse)
    for n_prefix in (1, 2, 3):
        p_score = L0[0] + L[1:n_prefix].sum()
        prefix = L0_ch[0] + "".join(L_ch[1:n_prefix])
        for n_digits in (1, 2, 3, 4):
            for n_suffix in (0, 1):
                t = tail(n_prefix, n_digits, n_suffix)
                if t and (best is None or p_score + t[0] > best[0]):
                    best = (p_score + t[0], PlateParse(prefix, t[1], t[2], 0))
    for word in SPECIAL_PREFIXES:
        if len(word) >= n_slots:
            continue
        w_score = sum(max(logp[i, index[c]] for c in _SPECIAL_EQUIV.get(ch, ch) if c in index)
                      for i, ch in enumerate(word))
        for n_digits in (1, 2, 3, 4):
            for n_suffix in (0, 1):
                t = tail(len(word), n_digits, n_suffix)
                if t and (best is None or w_score + t[0] > best[0]):
                    best = (w_score + t[0], PlateParse(word, t[1], t[2], 0, special=True))

    raw_conf = math.exp(raw_score / n_slots)
    if best is None or best[0] - raw_score < math.log(min_legal_ratio):
        return SlotDecode(raw, raw_conf, None, 0.0)
    return SlotDecode(raw, raw_conf, best[1], math.exp(best[0] / n_slots))

