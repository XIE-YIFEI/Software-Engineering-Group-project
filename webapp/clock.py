"""Time in one place: Malaysia time (MYT, UTC+8, no daylight saving), stored as naive ISO 8601 text.

Every timestamp in the database is local Malaysia time written by `iso()`, e.g. "2026-10-14T08:00:23.500", so
text comparison in SQL orders times correctly. Tests freeze the clock with `set_now()`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

MYT = timezone(timedelta(hours=8), "MYT")
_frozen: Optional[datetime] = None


def now() -> datetime:
    """The current Malaysia time (naive)."""
    return _frozen if _frozen is not None else datetime.now(MYT).replace(tzinfo=None)


def set_now(when: Optional[datetime]) -> None:
    """Freeze the clock at `when` (None = real time again). For tests and demos."""
    global _frozen
    _frozen = when


def iso(dt: datetime) -> str:
    """Fixed-width text form (milliseconds) so that string order is time order."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def now_iso() -> str:
    return iso(now())


def parse(text: str) -> datetime:
    """ISO text (date, minutes, seconds or milliseconds; a trailing offset is converted to MYT) -> naive MYT."""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(MYT).replace(tzinfo=None)
    return dt
