"""Text normalization, hashing and date parsing.

Normalization exists only for *identity* (dedupe + cache keys). All claim
offsets point into the raw bytes of the file as read, so a reviewer can open the
document and land on the quoted span.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re

# Lines that differ between two copies of the same record without changing what
# it asserts: receipt stamps, fax headers, export timestamps, page furniture.
_BOILERPLATE = re.compile(
    r"^(?:\s*(?:received(?: into chart)?|receipt date|export(?:ed)? (?:run|on)?|"
    r"printed|page \d+|fax|scanned|records intake|import operator|"
    r"produced|extract prepared|prepared)\b.*)$",
    re.IGNORECASE,
)

_WS = re.compile(r"[ \t]+")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-",
                         "‘": "'", "’": "'", "“": '"', "”": '"',
                         " ": " "})


def normalize_text(text: str) -> str:
    """Aggressive normalization for duplicate detection."""
    out = []
    for line in text.translate(_DASHES).splitlines():
        line = _WS.sub(" ", line).strip()
        if not line:
            continue
        if _BOILERPLATE.match(line):
            continue
        out.append(line.lower())
    return "\n".join(out)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hashes(raw: str) -> tuple[str, str]:
    return sha256(raw), sha256(normalize_text(raw))


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]
_MONTHS = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
_MONTHS.update({m[:3]: i + 1 for i, m in enumerate(_MONTH_NAMES)})

_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
_LONG = re.compile(r"\b([A-Z][a-z]{2,8})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b")
_SHORT = re.compile(r"\b([A-Z][a-z]{2})(\d{1,2})\b")          # Jan05
_MD = re.compile(r"\b([A-Z][a-z]{2,8})\s+(\d{1,2})\b")        # January 22


def _valid(y: int, m: int, d: int) -> str | None:
    """Reject impossible dates. A document can say "February 30"; a service
    date that cannot exist must not enter the abstraction."""
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(text: str, default_year: int | None = None) -> str | None:
    """First parseable date in `text`, as ISO. `default_year` fills in Jan05 forms."""
    m = _ISO.search(text)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _LONG.search(text)
    if m and m.group(1).lower() in _MONTHS:
        return _valid(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    if default_year:
        for rx in (_SHORT, _MD):
            m = rx.search(text)
            if m and m.group(1).lower() in _MONTHS:
                got = _valid(default_year, _MONTHS[m.group(1).lower()],
                             int(m.group(2)))
                if got:
                    return got
    return None


def parse_all_dates(text: str, default_year: int | None = None) -> list[str]:
    seen, out = set(), []
    for m in _ISO.finditer(text):
        iso = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso and iso not in seen:
            seen.add(iso)
            out.append(iso)
    for m in _LONG.finditer(text):
        if m.group(1).lower() in _MONTHS:
            iso = _valid(int(m.group(3)), _MONTHS[m.group(1).lower()],
                         int(m.group(2)))
            if iso and iso not in seen:
                seen.add(iso)
                out.append(iso)
    return out
