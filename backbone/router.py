"""Question -> query-function routing.

A new question becomes a call into `queries`, never a fresh pass over the
documents and never free-form SQL. The router resolves intent and parameters;
the function computes the numbers and carries its own evidence. This is the part
that makes "related questions" cheap: the abstraction is already built.

Routing is deterministic: keyword intent matching plus patient, window and
date extraction. No model is involved in choosing a function or in producing a
number.
"""
from __future__ import annotations

import datetime as dt
import re

from . import queries as Q
from .normalize import parse_date
from .store import Store

# intent -> (keywords, function name)
INTENTS = [
    ("consecutive_below", ("consecutive", "two weeks below", "weeks below",
                           "below those requirements", "which patients had")),
    ("plan_change_comparison", ("plan change", "plan was changed", "before and after",
                                "after a treatment-plan change", "amendment")),
    ("progress", ("progress", "symptom course", "symptom", "assessment",
                  "questionnaire", "phq", "gad", "improve", "course of")),
    ("day_detail", ("reconstruct", "on january", "on february", "that date",
                    "each date", "on these dates")),
    ("compliance", ("meet", "met the goal", "requirement", "treatment plan goal",
                    "compliance", "goal documented", "cannot be determined")),
    ("minutes", ("minutes", "hours", "how much time", "per week", "each week",
                 "weekly")),
    ("session_counts", ("how many therapy sessions", "sessions", "how many days",
                        "distinct days", "by service type", "count")),
    ("trace", ("trace", "why did you", "how do you know", "evidence for")),
    ("duplicates", ("duplicate", "duplicates", "copies")),
    ("integrity", ("integrity", "self-check", "consistency")),
]

FUNCS = {
    "session_counts": Q.session_counts,
    "minutes": Q.minutes,
    "compliance": Q.compliance,
    "consecutive_below": Q.consecutive_below,
    "day_detail": Q.day_detail,
    "plan_change_comparison": Q.plan_change_comparison,
    "progress": Q.progress,
    "trace": Q.trace,
    "duplicates": Q.duplicates,
    "integrity": Q.integrity,
}

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def corpus_year(st: Store, mrn: str | None = None) -> int | None:
    """The year most of this patient's care falls in.

    Questions routinely omit the year ("the care on January 19 and January 21").
    Falling back to the corpus beats silently dropping the date.
    """
    sql = ("SELECT substr(service_date,1,4) y, COUNT(*) n FROM events"
           + (" WHERE patient_mrn=?" if mrn else "")
           + " GROUP BY y ORDER BY n DESC, y DESC LIMIT 1")
    row = st.q1(sql, (mrn,) if mrn else ())
    return int(row["y"]) if row and row["y"] else None


_KW_CACHE: dict[str, re.Pattern] = {}


def _kw(phrase: str) -> re.Pattern:
    """Whole-word matcher for a keyword phrase.

    Substring matching made "meet" fire on "meeting" and "count" on "account",
    which silently routed a question to the wrong function.
    """
    if phrase not in _KW_CACHE:
        _KW_CACHE[phrase] = re.compile(
            r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b")
    return _KW_CACHE[phrase]


def classify(question: str) -> tuple[str, int]:
    """(intent, keyword hits). Zero hits means the caller is guessing."""
    low = question.lower()
    scored = []
    for name, kws in INTENTS:
        hits = sum(1 for k in kws if _kw(k).search(low))
        if hits:
            scored.append((hits, -INTENTS.index((name, kws)), name))
    if not scored:
        return "session_counts", 0
    best = max(scored)
    return best[2], best[0]


# Capitalised words that are never a patient name, so that an unresolved name
# can be detected without guessing at one.
_NOT_A_NAME = {
    # calendar
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "week", "weeks", "weekly", "day", "days", "month", "year", "date", "dates",
    # instruments and identifiers
    "phq", "gad", "pcl", "audit", "mdq", "whodas", "mrn", "dob", "encounter",
    "encounters", "appointment", "appointments", "form", "charge",
    # clinical and reporting vocabulary
    "therapy", "therapies", "group", "individual", "family", "medication",
    "collateral", "coordination", "session", "sessions", "contact", "contacts",
    "minutes", "hours", "total", "totals", "plan", "goal", "goals", "treatment",
    "progress", "symptom", "symptoms", "assessment", "assessments", "note",
    "notes", "record", "records", "document", "documents", "patient",
    "patients", "event", "events", "report", "summary", "compliance",
    "attendance", "duplicate", "duplicates", "integrity", "trace", "evidence",
    "episode", "service", "clinic", "clinician", "roster", "register",
    "correction", "telehealth", "measure", "measures", "questionnaire",
}
# A capitalised word that is not sentence-initial and not vocabulary. The
# lookbehinds skip the start of the string and any word after . ! ?
_CAP = re.compile(r"(?<![.!?]\s)(?<!\A)\b([A-Z][a-z]{2,})\b")


def resolve_patients(st: Store, question: str) -> dict:
    """Which patients a question is about, and whether it actually said.

    Returns {"mrns", "explicit", "unknown", "warnings"}. `explicit` is the
    signal every caller needs: it distinguishes "this question names a patient"
    from "this question names nobody and we picked one". Nothing here knows any
    patient's name -- they are read from the patients table.
    """
    low = question.lower()
    rows = st.q("SELECT mrn, name, given_name FROM patients")
    found, givens = [], set()
    for r in rows:
        if r["given_name"]:
            givens.add(r["given_name"].lower())
        hit = (r["mrn"].lower() in low
               or (r["name"] and r["name"].lower() in low)
               or (r["given_name"]
                   and re.search(rf"\b{re.escape(r['given_name'].lower())}\b", low)))
        if hit:
            found.append(r["mrn"])

    warnings: list[str] = []
    unknown: list[str] = []
    if not found:
        # An MRN-shaped token that matches nobody is unambiguously a different
        # patient, not a vocabulary word.
        for m in re.finditer(r"\b([A-Z]{1,5}-[A-Z]?\d{2,8})\b", question):
            tok = m.group(1)
            if not any(tok.lower() == r["mrn"].lower() for r in rows):
                unknown.append(tok)
        for m in _CAP.finditer(question):
            w = m.group(1)
            if w.lower() not in _NOT_A_NAME and w.lower() not in givens:
                unknown.append(w)
        unknown = sorted(set(unknown))

    mrns = found or [r["mrn"] for r in rows]
    if not found and not unknown and len(rows) == 1:
        warnings.append(
            f"the question does not name a patient; answered for the only "
            f"patient in the collection ({mrns[0]})")
    return {"mrns": mrns, "explicit": bool(found), "unknown": unknown,
            "warnings": warnings}


def find_patients(st: Store, question: str) -> list[str]:
    """Back-compatible helper: just the MRNs."""
    return resolve_patients(st, question)["mrns"]


def window_in_question(question: str, year: int | None = None) -> bool:
    """Does the question itself fix a window?

    This is what decides whether a per-patient fan-out may share one window or
    must use each patient's own episode.
    """
    if re.search(r"\bweek\s+(?:of|beginning|starting|commencing)\s+", question,
                 re.I):
        return True
    if re.search(r"([A-Z][a-z]+\s+\d{1,2})\s*(?:[-–—]|to|through)\s*"
                 r"(?:([A-Z][a-z]+)\s+)?(\d{1,2})", question):
        return True
    return len(set(find_dates(question, year))) >= 1


def find_window(st: Store, question: str, mrn: str | None,
                year: int | None = None) -> tuple[str | None, str | None]:
    """An explicit date range in the question, else the documented episode."""
    ym = re.search(r"\b(20\d\d)\b", question)
    if ym:
        year = int(ym.group(1))
    # "the week of January 12" / "week beginning 2026-01-12" names one week,
    # not the whole episode.
    wm = re.search(r"\bweek\s+(?:of|beginning|starting|commencing)\s+"
                   r"([A-Za-z0-9,\- ]{3,24})", question, re.I)
    if wm:
        anchor = parse_date(wm.group(1), default_year=year)
        if anchor:
            from . import intervals as _iv
            wk = _iv.week_key(anchor)
            return _iv.week_span(wk)
    # "January 5-30, 2026" / "January 5 to January 30"
    m = re.search(r"([A-Z][a-z]+\s+\d{1,2})\s*(?:[-–—]|to|through)\s*"
                  r"(?:([A-Z][a-z]+)\s+)?(\d{1,2})", question)
    if m and m.group(1).split()[0].lower() in _MONTHS:
        a = parse_date(m.group(1), default_year=year)
        bmonth = m.group(2) or m.group(1).split()[0]
        b = parse_date(f"{bmonth} {m.group(3)}", default_year=year)
        if a and b:
            return a, b
    dates = sorted(set(find_dates(question, year)))
    if len(dates) >= 2:
        return dates[0], dates[-1]
    if mrn:
        return Q.episode_window(st, mrn)
    return None, None


def find_dates(question: str, year: int | None = None) -> list[str]:
    out = []
    for m in re.finditer(r"\b(20\d\d-\d{2}-\d{2})\b", question):
        out.append(m.group(1))
    for m in re.finditer(r"\b([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", question):
        if m.group(1).lower() in _MONTHS:
            iso = parse_date(f"{m.group(1)} {m.group(2)}", default_year=year)
            if iso:
                out.append(iso)
    return out


COLLECTION_WIDE = {"consecutive_below", "duplicates", "integrity"}

#: Questions that are asked of one patient. When such a question does not pin a
#: single patient, it is answered for every patient in scope rather than for the
#: first MRN (see queries.per_patient).
PER_PATIENT = {"session_counts", "minutes", "compliance", "progress",
               "day_detail", "plan_change_comparison"}


def route(st: Store, question: str, router: str = "rules") -> dict:
    """Return {"function": name, "params": {...}} for a natural-language question."""
    intent, hits = classify(question)
    who = resolve_patients(st, question)
    mrns, mrn = who["mrns"], (who["mrns"][0] if who["mrns"] else None)
    # A collection-wide question is supposed to name nobody.
    warnings = ([] if intent in COLLECTION_WIDE else list(who["warnings"]))
    if hits == 0:
        warnings.append(
            "no question type matched; answered as session_counts. Supported: "
            + ", ".join(sorted(FUNCS)))
    ym = re.search(r"\b(20\d\d)\b", question)
    year = int(ym.group(1)) if ym else corpus_year(st, mrn)
    start, end = find_window(st, question, mrn, year)
    params: dict = {}
    if intent in ("session_counts", "minutes", "progress"):
        params = {"mrn": mrn, "start": start, "end": end}
    elif intent == "compliance":
        params = {"mrn": mrn, "start": start, "end": end}
        m = re.search(r"(sunday\s*[-–—]\s*saturday)", question, re.I)
        if m:
            params["scheme"] = "week_sun_sat"
    elif intent == "consecutive_below":
        n = 2
        nm = re.search(r"\b(two|three|four|\d+)\s+consecutive", question, re.I)
        if nm:
            word = nm.group(1).lower()
            n = {"two": 2, "three": 3, "four": 4}.get(word, int(word)
                                                      if word.isdigit() else 2)
        # Scope follows whether the question actually named a patient, not a
        # hardcoded name: unnamed means the whole collection.
        named = who["explicit"]
        params = {"mrn": mrn if named else None, "n": n,
                  "start": start if named else None, "end": end if named else None}
    elif intent == "day_detail":
        dates = sorted(set(find_dates(question, year)))
        if not dates and start:
            dates = [start]
        params = {"mrn": mrn, "dates": dates}
    elif intent == "plan_change_comparison":
        dates = sorted(set(find_dates(question, year)))
        params = {"mrn": mrn, "change_date": dates[0] if len(dates) == 1 else None}
    elif intent == "trace":
        m = re.search(r"\b((?:HG|BH|[A-Z]{2,4})-E\d+)\b", question, re.I)
        params = {"event_id": f"{mrn}|{m.group(1).upper()}" if m and mrn else ""}
    elif intent == "integrity":
        params = {"mrn": mrn}
    fan_out = intent in PER_PATIENT and len(mrns) > 1
    if fan_out:
        # the per-patient parameter is supplied by queries.per_patient
        params = {k: v for k, v in params.items() if k != "mrn"}
        if not window_in_question(question, year) and "start" in params:
            # The window was defaulted from one patient's episode. Clear it so
            # each patient is measured over their own, instead of having the
            # first patient's dates applied to everybody.
            params["start"] = params["end"] = None
            warnings.append(
                "the question names no date range, so each patient is measured "
                "over their own documented episode")
        warnings.append(
            f"the question does not pin a single patient; answered for all "
            f"{len(mrns)} patients in the collection ("
            + ", ".join(mrns) + ")" if not who["explicit"] else
            f"answered for the {len(mrns)} patients named: " + ", ".join(mrns))
    return {"function": intent, "params": params, "router": router,
            "patients_in_scope": mrns, "patient_named": who["explicit"],
            "unknown_patients": who["unknown"], "warnings": warnings,
            "intent_keyword_hits": hits, "per_patient": fan_out}


def answer(st: Store, question: str, router: str = "rules") -> dict:
    plan = route(st, question, router)
    if plan.get("unknown_patients"):
        known = ", ".join(f"{r['mrn']}"
                          + (f" ({r['given_name']})" if r["given_name"] else "")
                          for r in st.q("SELECT mrn, given_name FROM patients"))
        return {"question": question, "route": plan,
                "error": "the question refers to "
                         + ", ".join(plan["unknown_patients"])
                         + ", which does not match any patient in the collection."
                           " Refusing to answer about a different patient. Known: "
                         + (known or "none")}
    fn = FUNCS[plan["function"]]
    try:
        if plan.get("per_patient"):
            result = Q.per_patient(st, fn, plan["patients_in_scope"],
                                   **plan["params"])
        else:
            result = fn(st, **plan["params"])
    except TypeError as e:
        return {"question": question, "route": plan, "error": f"routing mismatch: {e}"}
    return {"question": question, "route": plan, "result": result,
            "answered_at": dt.datetime.now().replace(microsecond=0).isoformat()}
