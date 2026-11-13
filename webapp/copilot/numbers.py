"""The number check: every number, time and plate in a generated text must be found in the data it was written from.

The data (the tool results of a chat answer, or the FACTS of a document) give two sets: the numeric VALUES in them
(JSON numbers, and numbers written inside their strings) and their TEXT (for times such as 07:13, dates and plates).
A number of the answer matches a value when it is that value rounded to the answer's precision (113.56 -> "114",
"113.6"), or the value as a percentage (0.62 -> "62 %"). Numbers in the user's question count as known, and so do
list markers and the digits of record citations ([[S:12]]). Whatever is left is flagged for the reader.

It is a safety net, not a proof: a fabricated number that happens to equal some other number in the data passes,
and numbers written as words (e.g. Chinese numerals) are not read.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Set, Tuple

from alpr.plates import SPECIAL_PREFIXES

_SPECIAL_PREFIX_RE = "|".join(sorted(SPECIAL_PREFIXES, key=len, reverse=True))
CITE = re.compile(r"\[\[[A-Z]:\d+\]\]")
PLATE = re.compile(
    rf"(?<![A-Za-z0-9])({_SPECIAL_PREFIX_RE}|[A-Z]{{1,3}}) ?(\d{{1,4}})(?: ?([A-Z]{{1,2}}))?(?![A-Za-z0-9*])"
)
MASKED = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{1,3} ?)?\*{4}")
TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")
DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
LIST_MARKER = re.compile(r"(?m)^\s*(?:\d+[.)]|#+ \d+[.)]?)\s")
NUMBER = re.compile(r"(?<![A-Za-z0-9_.:])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
PERCENT = re.compile(r"^\s*(?:%|％|percent|per cent|percentage point|peratus|mata peratus|个百分点|百分点)", re.I)


def _strings_and_values(obj: Any, strings: List[str], values: List[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            values.append(float(obj))
    elif isinstance(obj, str):
        strings.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            strings.append(str(k).replace("_", " "))             # "lower_95_kmh" -> "lower 95 kmh"
            _strings_and_values(v, strings, values)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings_and_values(v, strings, values)


class Corpus:
    """What the answer may draw on."""

    def __init__(self, sources: Iterable[Any]):
        strings: List[str] = []
        values: List[float] = []
        for src in sources:
            if isinstance(src, str):
                try:
                    src = json.loads(src)
                except (ValueError, TypeError):
                    pass
            _strings_and_values(src, strings, values)
        self.text = "\n".join(strings)
        self.upper = re.sub(r"[^A-Z0-9*]", "", self.text.upper())
        for s in strings:                                    # numbers written inside strings ("114 ± 15 km/h")
            for m in NUMBER.finditer(CITE.sub(" ", s)):
                v = _to_float(m.group(0))
                if v is not None:
                    values.append(v)
        self.values = values
        self.times: Set[str] = {_hhmm(m) for m in TIME.finditer(self.text)}
        self.dates: Set[str] = {m.group(0) for m in DATE.finditer(self.text)}

    def has_number(self, value: float, decimals: int, percent: bool) -> bool:
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        for y in self.values:
            if abs(abs(y) - abs(value)) <= tol:
                return True
            if percent and abs(y) <= 1.5 and abs(abs(y) * 100 - abs(value)) <= tol:
                return True                                   # a share written as a percentage
        return False


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "").replace("−", "-"))
    except ValueError:
        return None


def _hhmm(m: re.Match) -> str:
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def check(answer: str, sources: Iterable[Any], question: str = "") -> Dict[str, Any]:
    """{ok, checked, unmatched: [{text, context}], plates_unmatched: [...]} for one answer."""
    corpus = Corpus(list(sources))
    asked = Corpus([question]) if question else None
    text = CITE.sub(" ", answer or "")
    unmatched: List[Dict[str, str]] = []
    plates_bad: List[str] = []
    checked = 0
    spans: List[Tuple[int, int]] = []

    def taken(a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in spans)

    def ctx(a: int, b: int) -> str:
        return text[max(0, a - 30):b + 30].replace("\n", " ").strip()

    for m in MASKED.finditer(text):
        spans.append(m.span())
    for m in PLATE.finditer(text):
        compact = "".join(g for g in m.groups() if g)
        if len(m.group(2)) < 2 and not m.group(3):          # "TC1", "A1" ... are names, not plates
            if compact in corpus.upper or (asked and compact in asked.upper):
                spans.append(m.span())
            continue
        spans.append(m.span())
        checked += 1
        if compact not in corpus.upper and not (asked and compact in asked.upper):
            plates_bad.append(m.group(0))
    for m in DATE.finditer(text):
        spans.append(m.span())
        checked += 1
        if m.group(0) not in corpus.dates and not (asked and m.group(0) in asked.dates):
            unmatched.append({"text": m.group(0), "context": ctx(*m.span())})
    for m in TIME.finditer(text):
        if taken(*m.span()):
            continue
        spans.append(m.span())
        checked += 1
        if _hhmm(m) not in corpus.times and not (asked and _hhmm(m) in asked.times):
            unmatched.append({"text": m.group(0), "context": ctx(*m.span())})
    for m in LIST_MARKER.finditer(text):
        spans.append(m.span())
    for m in NUMBER.finditer(text):
        if taken(*m.span()):
            continue
        tok = m.group(0)
        value = _to_float(tok)
        if value is None:
            continue
        decimals = len(tok.split(".")[1]) if "." in tok else 0
        percent = bool(PERCENT.match(text[m.end():m.end() + 9]))
        checked += 1
        if corpus.has_number(value, decimals, percent) or (asked and asked.has_number(value, decimals, percent)):
            continue
        unmatched.append({"text": tok + ("%" if percent else ""), "context": ctx(*m.span())})
    return {"ok": not unmatched and not plates_bad, "checked": checked, "unmatched": unmatched,
            "plates_unmatched": plates_bad}
