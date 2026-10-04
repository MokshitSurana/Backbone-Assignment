"""Interval algebra for patient-present time.

All durations in the abstraction come from here. Nothing subtracts a flat
"break length" -- a break only reduces a session by its *overlap* with the
interval the patient was actually present.
"""
from __future__ import annotations

import datetime as dt
import re

Interval = tuple[int, int]  # minutes from midnight, [start, end)

# hh:mm or a bare hour, each with an optional meridiem.
_TIME = re.compile(r"\b([0-2]?\d)(?::([0-5]\d))?\s*([APap]\.?[Mm]\.?)?")


def to_min(hhmm: str) -> int:
    """Minutes from midnight. Understands "10:45", "11", "1:15 PM", "12:30am"."""
    m = _TIME.search(hhmm.strip())
    if not m:
        raise ValueError(f"not a time: {hhmm!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    merid = (m.group(3) or "").lower().replace(".", "")
    if merid.startswith("p") and hour < 12:
        hour += 12
    elif merid.startswith("a") and hour == 12:
        hour = 0                      # 12:30 AM is 00:30
    if hour > 23:
        raise ValueError(f"not a time: {hhmm!r}")
    return hour * 60 + minute


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


#: An outpatient contact longer than this is not credible; an end time that
#: appears to precede its start is far more likely a 12-hour clock written
#: without a meridiem ("12:30-1:15") than a session running past midnight.
MAX_PLAUSIBLE_MINUTES = 8 * 60


def mk(a: str, b: str) -> Interval:
    """Build an interval from two clock times.

    An end before its start is ambiguous. Treating it as crossing midnight --
    which an earlier version did unconditionally -- turns "12:30-1:15" into 765
    minutes instead of 45. So the 12-hour reading is tried first, and midnight
    is only assumed when no 12-hour reading gives a plausible session length.
    """
    s, e = to_min(a), to_min(b)
    if e >= s:
        return (s, e)
    for bump in (12 * 60, 24 * 60):          # PM reading first, then midnight
        cand = e + bump
        if cand > s and (cand - s) <= MAX_PLAUSIBLE_MINUTES:
            return (s, cand)
    return (s, e + 24 * 60)


def normalize(ivs: list[Interval]) -> list[Interval]:
    """Sort and merge overlapping/abutting intervals."""
    out: list[Interval] = []
    for s, e in sorted(i for i in ivs if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(base: list[Interval], cut: list[Interval]) -> list[Interval]:
    """base minus cut, by overlap."""
    cur = normalize(base)
    for cs, ce in normalize(cut):
        nxt: list[Interval] = []
        for s, e in cur:
            if ce <= s or cs >= e:          # no overlap
                nxt.append((s, e))
                continue
            if s < cs:
                nxt.append((s, cs))
            if ce < e:
                nxt.append((ce, e))
        cur = nxt
    return normalize(cur)


def total(ivs: list[Interval]) -> int:
    return sum(e - s for s, e in normalize(ivs))


def overlaps(a: list[Interval], b: list[Interval]) -> int:
    """Total overlapping minutes between two interval sets."""
    tot = 0
    for s1, e1 in normalize(a):
        for s2, e2 in normalize(b):
            tot += max(0, min(e1, e2) - max(s1, s2))
    return tot


def present_minutes(presence: list[Interval], breaks: list[Interval]) -> int:
    """Patient-present therapeutic minutes = presence minus break overlap."""
    return total(subtract(presence, breaks))


def dumps(ivs: list[Interval]) -> list[list[str]]:
    return [[to_hhmm(s), to_hhmm(e)] for s, e in normalize(ivs)]


def loads(raw) -> list[Interval]:
    if not raw:
        return []
    return normalize([mk(a, b) for a, b in raw])


# --------------------------------------------------------------------------
# Week bucketing
# --------------------------------------------------------------------------
def week_key(date_iso: str, scheme: str = "week_mon_sun") -> str:
    d = dt.date.fromisoformat(date_iso)
    if scheme == "week_mon_sun":
        start = d - dt.timedelta(days=d.weekday())
    elif scheme == "week_sun_sat":
        start = d - dt.timedelta(days=(d.weekday() + 1) % 7)
    else:
        raise ValueError(f"unknown week scheme {scheme}")
    return start.isoformat()


def week_span(week_start_iso: str) -> tuple[str, str]:
    s = dt.date.fromisoformat(week_start_iso)
    return s.isoformat(), (s + dt.timedelta(days=6)).isoformat()
