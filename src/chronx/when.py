"""Parsing human time specs for `chronx diff <time>`.

Accepted forms:
    10m / 10m ago / -10m / 2h30s     relative to now (s, m, h, d)
    14:32 / 14:32:05                 today at that wall time (or yesterday
                                     if that instant hasn't happened yet)
    2026-07-13 / 2026-07-13T14:32    ISO date or datetime
    1752345678 / 1752345678.5        unix epoch seconds
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_RELATIVE = re.compile(
    r"^-?\s*((?:\d+(?:\.\d+)?\s*[smhd]\s*)+)(?:ago)?$", re.IGNORECASE
)
_RELATIVE_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class WhenParseError(ValueError):
    pass


def parse_when(spec: str, *, now: float | None = None) -> float:
    """Parse a time spec into unix epoch seconds. Raises WhenParseError."""
    now = time.time() if now is None else now
    spec = spec.strip()
    if not spec:
        raise WhenParseError("empty time spec")
    if spec.lower() == "now":
        return now

    m = _RELATIVE.match(spec)
    if m:
        seconds = sum(
            float(qty) * _UNIT_SECONDS[unit.lower()]
            for qty, unit in _RELATIVE_PART.findall(m.group(1))
        )
        return now - seconds

    m = _CLOCK.match(spec)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if hh > 23 or mm > 59 or ss > 59:
            raise WhenParseError(f"invalid clock time: {spec!r}")
        today = date.fromtimestamp(now)
        candidate = datetime.combine(today, datetime.min.time()) + timedelta(
            hours=hh, minutes=mm, seconds=ss
        )
        ts = candidate.timestamp()
        if ts > now:  # "14:32" said at 09:00 means yesterday's 14:32
            ts -= 86400.0
        return ts

    # Bare number: epoch seconds (anything >= ~2001 to avoid ambiguity).
    try:
        as_float = float(spec)
    except ValueError:
        pass
    else:
        if as_float >= 1e9:
            return as_float
        raise WhenParseError(
            f"{spec!r} looks like a number but not an epoch timestamp; "
            "pass an event id without quotes to `diff` instead"
        )

    try:
        dt = datetime.fromisoformat(spec)
    except ValueError:
        raise WhenParseError(
            f"could not parse {spec!r}; try '10m', '14:32', an ISO date, "
            "or an event id"
        ) from None
    return dt.timestamp()


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
