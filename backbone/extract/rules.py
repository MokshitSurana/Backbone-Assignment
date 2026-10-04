"""Deterministic claim extraction.

Design note: no rule here knows anything about a specific patient, date or
encounter. Time ranges are found anywhere in the document and then *classified
by the words around them*, which is what lets the same code read a facilitator
narrative, a desk roster and a billing extract.

Interval classes, in precedence order:
    break           nontherapeutic / break / connection lost
    scheduled       the booked slot (never proof of patient-present time)
    other_present   therapist-only or partner-only interval
    patient_present explicit patient contact / arrival+departure
    service         an unlabelled interval on an encounter header line
                    (fallback only, and never for group services)
"""
from __future__ import annotations

import re

from .. import normalize as nz
from ..taxonomy import (EXTRACTOR_VERSION, canonical_service, classify_document,
                        ESTABLISHES_DELIVERY, PRESENCE_WORDS,
                        NEGATIVE_PRESENCE)
from .base import Claim, DocFacts

NAME = "rules"
VERSION = EXTRACTOR_VERSION

DASH = r"[-–—]"
# A clock time with an optional meridiem, plus an optional-minute variant for
# the end of a range: "10:45-11", "from 10:45 until 11:00", "10:00 AM-11:30 AM".
MERID = r"(?:\s*[APap]\.?[Mm]\.?)?"
T = rf"([0-2]?\d:[0-5]\d{MERID})"
T_LOOSE = rf"([0-2]?\d(?::[0-5]\d)?{MERID})"
SEP = rf"(?:\s*(?:{DASH}|\bto\b|\bthrough\b|\buntil\b|\bthru\b)\s*)"
RANGE = re.compile(rf"{T}{SEP}{T_LOOSE}")
SINGLE = re.compile(rf"\b{T}")

MRN = re.compile(r"\bMRN:?\s*([A-Z]{1,5}-?[A-Z]?\d{2,8})\b")
DOB = re.compile(r"\bDOB:?\s*(\d{4}-\d{2}-\d{2})\b")
DOCID = re.compile(r"\bDocument ID:?\s*([A-Za-z0-9\-]+)")
ENC = re.compile(r"\b(?:encounter\s*)?((?:HG|BH|[A-Z]{2,4})-E\d{2,6})\b", re.I)
APPT = re.compile(r"\b((?:HG|BH|[A-Z]{2,4})-A\d{2,8}(?:-\d+)?)\b")
FORM = re.compile(r"\b((?:HG|BH|[A-Z]{2,4})-Q\d{2,6})\b")
CALL = re.compile(r"\b(VC-[A-Z0-9]+)\b")
CHARGE = re.compile(r"\b(CH-\d+)\b")
MINUTES = re.compile(r"\b(\d{1,3})\s*minutes?\b", re.I)
HOURS = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*hours?\b", re.I)

BREAK_CUES = ("break", "nontherapeutic", "non-therapeutic", "no therapy was conducted",
              "connection was lost", "disconnect", "no therapeutic contact",
              "no facilitated discussion", "unstructured time")
SCHED_CUES = ("scheduled", "schedule", "booked", "slot", "room schedule",
              "scheduled opening", "scheduled closing", "scheduled service")
OTHER_CUES = ("therapist session interval", "partner only", "partner-only",
              "clinician only", "staff only")
PRESENT_CUES = ("patient-present", "patient present", "patient contact",
                "patient arrival", "patient departure", "patient arrived",
                "patient departed", "actual", "attended", "completed",
                "present", "joined", "arrived", "departed", "visit",
                "contact occurred", "psychotherapy contact", "session interval")

# Statements that the patient was not present for the *whole* contact. A
# sentence about one portion of a session ("<patient> was not present for that
# portion") must not land here -- see _narrative_encounters, where an explicit
# patient-present interval overrides a document-level cue.
NO_PATIENT_CUES = (
    ("absent for the entire", "patient_absent"),
    ("patient participation: none", "patient_absent"),
    ("no patient contact occurred", "patient_absent"),
    ("did not join", "patient_absent"),
    ("no patient-present psychotherapy", "patient_absent"),
    ("no patient treatment contact", "patient_absent"),
    ("patient was absent", "patient_absent"),
    ("was absent for the entire", "patient_absent"),
    ("no patient-contact interval", "not_a_contact"),
    ("does not document a visit", "not_a_contact"),
    ("is not a separate treatment appointment", "not_a_contact"),
    ("no additional visit", "not_a_contact"),
    ("no service attendance record", "not_a_contact"),
    ("records no additional visit", "not_a_contact"),
)

SENT_SPLIT = re.compile(r"(?<=[.;])\s+|\n")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _span_of_context(text: str, pos: int) -> tuple[int, int]:
    """The sentence-ish span containing `pos` (bounded by newline or . ; )."""
    start = max(text.rfind("\n", 0, pos), text.rfind(". ", 0, pos),
                text.rfind("; ", 0, pos)) + 1
    ends = [x for x in (text.find("\n", pos), text.find(". ", pos),
                        text.find("; ", pos)) if x != -1]
    end = min(ends) + 1 if ends else len(text)
    return max(start, 0), min(end, len(text))


def _classify_interval(ctx: str) -> str:
    low = ctx.lower()
    if any(c in low for c in BREAK_CUES):
        return "break"
    if any(c in low for c in SCHED_CUES):
        return "scheduled"
    if any(c in low for c in OTHER_CUES):
        return "other_present"
    if any(c in low for c in PRESENT_CUES):
        return "patient_present"
    return "service"


NEG_KIND = {"no show": "no_show", "no-show": "no_show",
            "did not attend": "no_show", "was absent": "patient_absent",
            "patient cancelled": "cancelled", "cancelled by the clinic": "cancelled",
            "clinic cancelled": "cancelled", "cancellation": "cancelled"}



def _presence_detail(text: str, given: str = ""):
    """(presence, cue, negation_kind, ambiguity) for one span of text.

    Each cue occurrence is scored by the authority of its context:

        3  inside a status/disposition/attendance field
        2  in a sentence that names the patient
        1  anywhere else (a passing mention)

    The highest-scoring cue wins, longer phrase breaking a tie. If the best
    positive and the best negative cue score equally, the document does not
    settle the question: presence is returned as None and the competition is
    reported, so reconciliation falls back to other records rather than
    believing whichever phrase happened to be longer or listed first.
    """
    low = text.lower()
    for c, k in NO_PATIENT_CUES:
        if c in low:
            return "absent", c, k, None

    name = (given or "").lower()
    scored: list[tuple[int, int, str, str]] = []
    for word, val in PRESENCE_WORDS:
        start = 0
        while True:
            at = low.find(word, start)
            if at < 0:
                break
            start = at + 1
            cs, ce = _span_of_context(low, at)
            ctx = low[cs:ce]
            field = low[max(0, cs):at]
            if re.search(r"(?:status|disposition|attendance|roster)\s*:?\s*$",
                         field) or re.search(
                             r"\b(?:status|final disposition|desk status)\b", ctx):
                score = 3
            elif "patient" in ctx or (name and name in ctx):
                score = 2
            else:
                score = 1
            scored.append((score, len(word), word, val))
    if not scored:
        return None, None, None, None

    scored.sort(reverse=True)
    best = scored[0]
    pos = next((x for x in scored if x[3] not in NEGATIVE_PRESENCE), None)
    neg = next((x for x in scored if x[3] in NEGATIVE_PRESENCE), None)
    if pos and neg and pos[0] == neg[0]:
        return None, None, None, {
            "competing": [pos[2], neg[2]],
            "competing_presence": [pos[3], neg[3]],
            "score": pos[0],
            "note": "both a positive and a negative attendance cue appear with "
                    "equal contextual authority; this document does not settle "
                    "attendance"}
    return best[3], best[2], NEG_KIND.get(best[2]), None


def _mk_claim(doc_pk: str, facts: DocFacts, text: str, span: tuple[int, int],
              **kw) -> Claim:
    s, e = span
    kw.setdefault("authority_class", facts.doc_class)
    kw.setdefault("patient_mrn", facts.patient_mrn)
    c = Claim(doc_pk=doc_pk, extractor=NAME,
              quote=text[s:e].strip(), quote_start=s, quote_end=e, **kw)
    # keep the stored quote byte-identical to the stored offsets
    c.quote_start = s + (len(text[s:e]) - len(text[s:e].lstrip()))
    c.quote_end = c.quote_start + len(c.quote)
    c.verify(text)
    return c


# ---------------------------------------------------------------------------
# header facts
# ---------------------------------------------------------------------------
def doc_facts(text: str) -> DocFacts:
    f = DocFacts()
    f.doc_class = classify_document(text)
    m = DOCID.search(text)
    if m:
        f.doc_id = m.group(1)
    m = MRN.search(text)
    if m:
        f.patient_mrn = m.group(1)
    m = DOB.search(text)
    if m:
        f.dob = m.group(1)
    # patient name: the token run immediately before a DOB or MRN marker
    nm = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z'\-]+){0,3})\s*\|?\s*(?:DOB|MRN)", text)
    if nm:
        f.patient_name = nm.group(1).strip()
        f.given_name = f.patient_name.split()[0]
    for pat, attr in (
        (r"(?:Received(?: into chart)?|Receipt date|Received)\s*:?\s*([A-Za-z0-9,\- ]{6,30})", "received_at"),
        (r"(?:correspondence received|Notice entered|Entered(?: by [A-Za-z. ]+,)?)\s*([A-Za-z0-9,\- ]{6,30})", "received_at"),
        (r"(?:Electronically signed|signed|Signed)\s*:?[^|\n]{0,40}?((?:\d{4}-\d{2}-\d{2})|(?:[A-Z][a-z]+ \d{1,2}, 20\d\d))", "authored_at"),
    ):
        m = re.search(pat, text)
        if m and not getattr(f, attr):
            iso = nz.parse_date(m.group(1), default_year=_guess_year(text))
            if iso:
                setattr(f, attr, iso)
    return f


def _guess_year(text: str) -> int | None:
    m = re.search(r"\b(20\d\d)\b", text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# table parsing
# ---------------------------------------------------------------------------
COLMAP = [
    ("encounter", ("encounter",)),
    ("date", ("service date", "date completed", "date")),
    ("service", ("appointment type", "service", "type")),
    ("scheduled", ("scheduled time", "scheduled slot", "scheduled")),
    ("arrival", ("patient arrived", "actual arrival", "arrival", "arrived", "connected")),
    ("departure", ("patient departed", "actual departure", "departure", "departed", "ended")),
    ("status", ("desk status", "final disposition", "disposition", "status")),
    ("appointment", ("appointment id",)),
    ("call", ("call id",)),
    ("measure", ("measure", "instrument")),
    ("result", ("result", "total score", "score")),
    ("form", ("source form", "form")),
]


def _header_map(cells: list[str]) -> dict[int, str] | None:
    """A header row names columns and carries no values: reject cells with data."""
    out: dict[int, str] = {}
    for i, cell in enumerate(cells):
        low = cell.strip().lower()
        if SINGLE.search(low) or re.search(r"\d{3,}", low):
            return None                      # e.g. "Patient arrival: 10:00"
        for key, needles in COLMAP:
            if any(low == n or low.startswith(n) for n in needles):
                out[i] = key
                break
    return out if len(out) >= 3 else None


def _tables(text: str):
    """Yield (header_map, [(line_text, line_start)]) for pipe tables."""
    lines, pos = [], 0
    for line in text.split("\n"):
        lines.append((line, pos))
        pos += len(line) + 1
    i = 0
    while i < len(lines):
        line, start = lines[i]
        cells = [c for c in line.split("|")]
        if len(cells) >= 3:
            hm = _header_map(cells)
            if hm:
                rows = []
                j = i + 1
                while j < len(lines):
                    l2, s2 = lines[j]
                    if l2.count("|") < len(cells) - 2 or not l2.strip():
                        break
                    rows.append((l2, s2))
                    j += 1
                if rows:
                    yield hm, rows
                    i = j
                    continue
        i += 1


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out spans while preserving every offset."""
    if not spans:
        return text
    buf = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(buf), e)):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def _dob_spans(text: str) -> list[tuple[int, int]]:
    """A date of birth must never be read as a service date."""
    return [(m.start(1), m.end(1)) for m in DOB.finditer(text)]


def extract(text: str, doc_pk: str, store=None) -> tuple[DocFacts, list[Claim]]:
    # `store` is accepted so that an extractor needing state (a cache, a
    # lookup) can be dropped in behind the same signature. This one is
    # stateless, which is what makes it byte-reproducible across runs.
    facts = doc_facts(text)
    year = _guess_year(text)
    claims: list[Claim] = []
    seq = [0]

    def nxt(kind: str) -> str:
        seq[0] += 1
        return f"{doc_pk[:12]}-{kind}{seq[0]:02d}"

    consumed: list[tuple[int, int]] = []   # spans owned by table rows
    row_encs: set[str] = set()

    # ---- 1. tables -------------------------------------------------------
    for hm, rows in _tables(text):
        for line, lstart in rows:
            cells = line.split("|")
            get = lambda k: next((cells[i].strip() for i, kk in hm.items()
                                  if kk == k and i < len(cells)), "")
            span = (lstart, lstart + len(line))
            if get("measure") or get("result"):
                claims.append(_measure_row(doc_pk, facts, text, span, get, year))
                consumed.append(span)
                continue
            if get("call"):
                claims.append(_call_row(doc_pk, facts, text, span, get, year))
                consumed.append(span)
                continue
            c = _encounter_row(doc_pk, facts, text, span, get, year, nxt)
            if c:
                claims.append(c)
                consumed.append(span)
                if c.encounter_ref:
                    row_encs.add(c.encounter_ref)

    # Scanning text: table rows and the DOB are blanked so prose rules cannot
    # re-read a row as narrative or mistake a birth date for a service date.
    scan = _mask(text, consumed + _dob_spans(text))

    # ---- 2. corrections --------------------------------------------------
    claims += _corrections(doc_pk, facts, text, scan, nxt)

    # ---- 3. narrative encounter sections ---------------------------------
    # A document can be both a table and a narrative (e.g. a telehealth note
    # with an attached connection export). Rows own the encounters they name;
    # the prose is read for everything else.
    claims += _narrative_encounters(doc_pk, facts, text, scan, year, nxt,
                                    skip_encs=row_encs)
    if consumed:
        claims += _attestations(doc_pk, facts, text, scan, year, row_encs, nxt)

    # ---- 4. measures stated in prose -------------------------------------
    claims += _prose_measures(doc_pk, facts, text, scan, year, consumed, nxt)

    # ---- 5. plan requirements / episode ----------------------------------
    claims += _plan(doc_pk, facts, text, scan, year, nxt)

    # ---- 6. billing + authorization --------------------------------------
    claims += _admin(doc_pk, facts, text, scan, year, nxt)

    # ---- 7. observations -------------------------------------------------
    claims += _observations(doc_pk, facts, text, scan, year, nxt)

    for i, c in enumerate(claims):
        if not c.claim_id:
            c.claim_id = f"{doc_pk[:12]}-X{i:02d}"
        c.verify(text)
    return facts, claims


# ---------------------------------------------------------------------------
# table row -> claim
# ---------------------------------------------------------------------------
def _encounter_row(doc_pk, facts, text, span, get, year, nxt) -> Claim | None:
    enc = get("encounter")
    date_s = get("date")
    if not enc and not date_s:
        return None
    m = ENC.search(enc) if enc else None
    enc_ref = m.group(1).upper() if m else None
    date = nz.parse_date(date_s, default_year=year) if date_s else None
    if not date and enc_ref is None:
        return None
    svc_raw = get("service")
    status_raw = get("status")
    presence, basis_word, neg, _ = (
        _presence_detail(f"Final disposition: {status_raw}")
        if status_raw else (None, None, None, None))
    sched = RANGE.search(get("scheduled") or "")
    arr = SINGLE.search(get("arrival") or "")
    dep = SINGLE.search(get("departure") or "")
    intervals, arrival, departure = [], None, None
    if arr and dep:
        arrival, departure = arr.group(1), dep.group(1)
        intervals = [[arrival, departure]]
    fields = {"status_raw": status_raw, "row": True, "presence_cue": basis_word,
              "negation_kind": neg}
    if sched:
        fields["scheduled"] = [sched.group(1), sched.group(2)]
    return _mk_claim(
        doc_pk, facts, text, span,
        claim_id=nxt("R"), claim_type="encounter", encounter_ref=enc_ref,
        service_date=date, service_raw=svc_raw or None,
        service_type=canonical_service(svc_raw) if svc_raw else None,
        presence=presence,
        presence_basis=("attested" if facts.doc_class == "attendance_register"
                        else "scheduled" if facts.doc_class == "schedule_export"
                        else "derived"),
        arrival=arrival, departure=departure, intervals=intervals,
        minutes_basis="computed_from_intervals" if intervals else None,
        fields=fields)


def _measure_row(doc_pk, facts, text, span, get, year) -> Claim:
    res = get("result")
    total = None
    m = re.search(r"-?\d+(?:\.\d+)?", res)
    if m:
        total = float(m.group(0))
    form = FORM.search(get("form") or "")
    return _mk_claim(
        doc_pk, facts, text, span,
        claim_type="measure",
        form_ref=form.group(1) if form else None,
        service_date=nz.parse_date(get("date"), default_year=year),
        fields={"instrument": (get("measure") or "").strip().upper() or "UNKNOWN",
                "total": total})


def _call_row(doc_pk, facts, text, span, get, year) -> Claim:
    appt = APPT.search(get("appointment") or "")
    call = CALL.search(get("call") or "")
    a, d = SINGLE.search(get("arrival") or ""), SINGLE.search(get("departure") or "")
    return _mk_claim(
        doc_pk, facts, text, span,
        claim_type="encounter", appointment_ref=appt.group(1) if appt else None,
        call_ref=call.group(1) if call else None,
        service_date=nz.parse_date(get("arrival") or "", default_year=year),
        intervals=[[a.group(1), d.group(1)]] if a and d else [],
        presence="present" if a and d else None, presence_basis="platform_log",
        authority_class="platform_log",
        minutes_basis="computed_from_intervals",
        fields={"platform_row": True})


# ---------------------------------------------------------------------------
# corrections
# ---------------------------------------------------------------------------
FIELD_WORDS = {
    "departure": "departure", "departed": "departure",
    "arrival": "arrival", "arrived": "arrival",
    "duration": "duration", "minutes": "duration",
    "status": "presence", "attendance": "presence",
    "service date": "service_date", "date": "service_date",
}
CORR_SENT = re.compile(r"[^.\n]*\bcorrection\b[^.]*\.", re.I)


def _corrections(doc_pk, facts, text, scan, nxt) -> list[Claim]:
    out: list[Claim] = []
    if "correction" not in scan.lower():
        return out
    target_doc_enc = None
    m = re.search(rf"applies to[^.\n]*?((?:HG|BH|[A-Z]{{2,4}})-E\d+)", scan, re.I)
    if m:
        target_doc_enc = m.group(1).upper()
    for sm in CORR_SENT.finditer(scan):
        sent = sm.group(0)
        enc = ENC.search(sent)
        enc_ref = (enc.group(1).upper() if enc else target_doc_enc)
        newv = re.search(rf"\b(?:is|should be|becomes|=)\s*{T}", sent)
        if not newv:
            nm = re.search(r"\b(?:is|should be|becomes)\s+([A-Za-z0-9 ]{2,30})", sent)
            if not nm:
                continue
            new_val, old_val, assert_at = nm.group(1).strip(), None, nm.start()
        else:
            oldv = re.search(rf"replacing[^.]*?(?:value of|of)\s*{T}", sent)
            new_val = newv.group(1)
            old_val = oldv.group(1) if oldv else None
            assert_at = newv.start()
        # The corrected field is the field word closest *before* the assertion,
        # so "service date January 19" in a preamble cannot hijack a departure fix.
        low = sent.lower()
        fw, best = None, -1
        for k in FIELD_WORDS:
            pos = low.rfind(k, 0, assert_at)
            if pos > best or (pos == best and len(k) > len(fw or "")):
                if pos != -1:
                    fw, best = FIELD_WORDS[k], pos
        if not fw:
            continue
        date = nz.parse_date(sent, default_year=_guess_year(scan)) or \
            nz.parse_date(scan, default_year=_guess_year(scan))
        # Quote the correction statement itself, not everything the sentence
        # scanner swallowed from the masthead onwards.
        qs, qe = sm.start(), sm.end()
        marker = sent.lower().rfind("correction:", 0, assert_at)
        if marker > 0:
            qs = sm.start() + marker
        c = _mk_claim(
            doc_pk, facts, text, (qs, qe),
            claim_id=nxt("C"), claim_type="correction", encounter_ref=enc_ref,
            service_date=date, authority_class="correction",
            fields={"target_field": fw, "old_value": old_val, "new_value": new_val,
                    "scope_note": _correction_scope(scan)})
        out.append(c)
    return out


def _correction_scope(text: str) -> str:
    m = re.search(r"(?:This correction|It does not change)[^.]*\.(?:[^.]*\.)?", text)
    return m.group(0).strip() if m else ""


# ---------------------------------------------------------------------------
# attestation paragraphs in table documents
# ---------------------------------------------------------------------------
ATTEST_CUES = ("attestation", "desk note", "comment", "arrived", "remained",
               "absent", "no show", "cancel", "present", "attended", "roster",
               "outreach", "unarrived", "retained")


def _attestations(doc_pk, facts, text, scan, year, row_encs, nxt) -> list[Claim]:
    """Evidence-only claims for the prose that accompanies a table.

    These never carry intervals or minutes: the rows are the numeric record.
    Their job is to give a reviewer the sentence a disposition rests on.
    """
    out: list[Claim] = []
    for para in re.finditer(r"[^\n]{40,}", scan):
        seg = para.group(0)
        low = seg.lower()
        if not any(c in low for c in ATTEST_CUES):
            continue
        enc = ENC.search(seg)
        date = nz.parse_date(seg, default_year=year)
        if not enc and not date:
            continue
        presence, pword, neg, pambig = _presence_detail(
            seg, facts.given_name or "")
        svc = canonical_service(seg)
        out.append(_mk_claim(
            doc_pk, facts, text, (para.start(), para.end()),
            claim_id=nxt("T"), claim_type="encounter",
            encounter_ref=enc.group(1).upper() if enc else None,
            service_date=date, service_type=svc if svc != "unknown" else None,
            presence=presence,
            presence_basis="attested" if facts.doc_class == "attendance_register" else "derived",
            fields={"evidence_only": True, "presence_cue": pword,
                    "negation_kind": neg, "presence_ambiguous": pambig}))
    return out


# ---------------------------------------------------------------------------
# narrative encounters
# ---------------------------------------------------------------------------
def _sections(text: str) -> list[tuple[int, int, str | None]]:
    """Split a document into per-encounter sections when it names several."""
    hits = [(m.start(), m.group(1).upper()) for m in ENC.finditer(text)]
    order: list[tuple[int, str]] = []
    for pos, ref in hits:
        if not order or order[-1][1] != ref:
            order.append((pos, ref))
    distinct = {r for _, r in order}
    if len(distinct) <= 1:
        return [(0, len(text), next(iter(distinct)) if distinct else None)]
    bounds: list[tuple[int, int, str | None]] = []
    starts: list[tuple[int, str]] = []
    for pos, ref in order:
        if not starts or starts[-1][1] != ref:
            starts.append((pos, ref))
    for i, (pos, ref) in enumerate(starts):
        line_start = text.rfind("\n", 0, pos) + 1
        end = text.rfind("\n", 0, starts[i + 1][0]) + 1 if i + 1 < len(starts) else len(text)
        bounds.append((line_start if i else 0, end, ref))
    return bounds


def _narrative_encounters(doc_pk, facts, text, scan, year, nxt,
                          skip_encs: set | None = None) -> list[Claim]:
    if facts.doc_class in ("treatment_plan", "authorization"):
        return []
    skip_encs = skip_encs or set()
    out: list[Claim] = []
    for s, e, enc_ref in _sections(scan):
        if enc_ref and enc_ref in skip_encs:
            continue
        seg = scan[s:e]
        svc_type = canonical_service(seg[:600]) or "unknown"
        if svc_type == "unknown":
            svc_type = canonical_service(seg)
        # Service date, in order of how strongly the document asserts it:
        #   1. a line that labels it as the service date
        #   2. a date on the same line as this encounter's own reference
        #   3. the first date anywhere in the section (weakest, and the reason
        #      the two stronger signals exist: a section can open with a
        #      signature or export date that is not the service date)
        date = None
        date_basis = None
        dm = re.search(r"(?:service date|date of service)\s*:?\s*"
                       r"([A-Za-z0-9,\- ]{6,24})", seg, re.I)
        if dm:
            date = nz.parse_date(dm.group(1), default_year=year)
            date_basis = "labelled_service_date"
        if not date and enc_ref:
            for line in seg.splitlines():
                if enc_ref.lower() in line.lower():
                    date = nz.parse_date(line, default_year=year)
                    if date:
                        date_basis = "encounter_header_line"
                        break
        if not date:
            dm = re.search(r"\bDate\s*:?\s*([A-Za-z0-9,\- ]{6,24})", seg)
            if dm:
                date = nz.parse_date(dm.group(1), default_year=year)
                date_basis = "labelled_date" if date else None
        if not date:
            date = nz.parse_date(seg, default_year=year)
            date_basis = "first_date_in_section"
        buckets = _intervals(seg, s, facts)
        presence, pword, neg, pambig = _presence_detail(
            seg, facts.given_name or "")
        stated, sbasis, sspan = _stated_minutes(seg, s, facts)
        appt = APPT.search(seg)
        is_group = svc_type == "group_therapy"
        pres_iv = buckets["patient_present"]
        if pres_iv and presence != "present":
            # An explicit patient-present interval IS a statement of attendance,
            # and it outranks a document-level cue about one portion of the
            # session: "<patient> was not present for that portion" can sit in
            # the same note as "<patient> present with partner: 13:15-13:45".
            presence, pword, neg = "present", "explicit patient-present interval", None
        if not pres_iv and not is_group and presence == "present":
            pres_iv = buckets["service"]
            iv_basis = "service_interval_fallback"
        else:
            iv_basis = "patient_present_label"
        # A document can hold an unsigned draft inside an administrative
        # wrapper. The draft section cannot establish delivery whatever the
        # wrapper is.
        authority = facts.doc_class
        seg_low = seg.lower()
        if ("unsigned" in seg_low and "draft" in seg_low) or \
                "autogenerated progress note" in seg_low:
            authority = "draft_note"
        span = _best_span(seg, s, e, sspan, pres_iv, pword, enc_ref)
        reason = _added_reason(seg, s)
        fields = {
            "interval_classes": {k: v for k, v in buckets.items() if v},
            "interval_basis": iv_basis,
            "presence_cue": pword, "negation_kind": neg,
            "presence_ambiguous": pambig,
            "service_date_basis": date_basis,
            "establishes_delivery": ESTABLISHES_DELIVERY.get(authority, False),
        }
        if reason:
            fields["reason_added"] = reason
        if buckets["other_present"]:
            fields["other_present"] = buckets["other_present"]
        c = _mk_claim(
            doc_pk, facts, text, span,
            claim_id=nxt("N"), claim_type="encounter", encounter_ref=enc_ref,
            appointment_ref=appt.group(1) if appt else None,
            service_date=date, service_raw=seg[:120].replace("\n", " ").strip(),
            service_type=svc_type, presence=presence, authority_class=authority,
            presence_basis="narrative" if authority == "clinical_note" else "derived",
            intervals=pres_iv, breaks=buckets["break"],
            stated_minutes=stated, minutes_basis=sbasis,
            fields=fields)
        if pres_iv or stated or presence or enc_ref:
            out.append(c)
    return out


# A contact the record describes as added, unscheduled or arranged same-day.
ADDED_CUES = re.compile(
    r"[^.\n]*?\b(?:was added|added because|arranged a same-day"
    r"|same-day (?:individual|appointment|meeting)|unscheduled"
    r"|additional (?:individual )?(?:meeting|contact|visit|appointment)"
    r"|retained as)\b[^.]*\.", re.I)


ADDED_NEGATIONS = ("no additional", "records no", "not an additional",
                   "no new", "without an additional")


def _added_reason(seg: str, offset: int) -> dict | None:
    for m in ADDED_CUES.finditer(seg):
        low = m.group(0).lower()
        if any(n in low for n in ADDED_NEGATIONS):
            continue        # "records no additional visit" is the opposite claim
        return {"quote": m.group(0).strip(),
                "offsets": [offset + m.start(), offset + m.end()]}
    return None



def _best_span(seg: str, s: int, e: int, sspan, pres_iv, pword,
               enc_ref) -> tuple[int, int]:
    """Pick the most informative quotable span for a narrative claim.

    A reviewer asking "where does 45 minutes come from?" should land on the
    sentence that says 45 minutes, not on the document masthead.
    """
    if sspan:
        return sspan
    if pres_iv:
        # The same clock time can appear on a scheduled-slot line and on the
        # patient-arrival line. Prefer the context that is about the patient.
        best = None
        arrive, depart = pres_iv[0][0], pres_iv[0][-1]
        for m in re.finditer(re.escape(arrive), seg):
            cs, ce = _span_of_context(seg, m.start())
            if ce - cs <= 15:
                continue
            ctx = seg[cs:ce]
            score = (_classify_interval(ctx) == "patient_present") * 2 \
                + (depart in ctx)
            if best is None or score > best[0]:
                best = (score, cs, ce)
        if best:
            return (s + best[1], s + best[2])
    for needle in [n for n in (pword, enc_ref) if n]:
        pos = seg.lower().find(str(needle).lower())
        if pos >= 0:
            cs, ce = _span_of_context(seg, pos)
            if ce - cs > 15:
                return (s + cs, s + ce)
    nl = seg.find("\n", 60)
    return (s, min(e, s + nl)) if nl > 0 else (s, min(e, s + 160))


def _intervals(seg: str, offset: int, facts: DocFacts) -> dict[str, list]:
    out: dict[str, list] = {"break": [], "scheduled": [], "patient_present": [],
                            "other_present": [], "service": []}
    for m in RANGE.finditer(seg):
        cs, ce = _span_of_context(seg, m.start())
        cls = _classify_interval(seg[cs:ce])
        out[cls].append([m.group(1), m.group(2)])
    # arrival / departure field pairs
    arr = re.search(rf"(?:patient\s+)?arriv\w*\s*:?\s*{T}", seg, re.I)
    dep = re.search(rf"(?:patient\s+)?depart\w*\s*:?\s*{T}", seg, re.I)
    if arr and dep:
        out["patient_present"].append([arr.group(1), dep.group(1)])
    # "<Name> joined at 13:15" + an enclosing session interval
    jm = re.search(rf"joined at {T}", seg, re.I)
    if jm and not out["patient_present"]:
        ends = [iv[1] for iv in out["scheduled"] + out["other_present"] + out["service"]]
        if ends:
            out["patient_present"].append([jm.group(1), max(ends)])
    for k in out:
        seen, dedup = set(), []
        for iv in out[k]:
            t = tuple(iv)
            if t not in seen:
                seen.add(t)
                dedup.append(iv)
        out[k] = dedup
    return out


def _stated_minutes(seg: str, offset: int, facts: DocFacts):
    """Minutes explicitly stated as patient time, with its source span."""
    best = None
    for m in MINUTES.finditer(seg):
        cs, ce = _span_of_context(seg, m.start())
        ctx = seg[cs:ce]
        cls = _classify_interval(ctx)
        if cls in ("break", "scheduled", "other_present"):
            continue
        low = ctx.lower()
        rank = 3 if ("patient-present" in low or "patient psychotherapy contact" in low
                     or "total patient" in low) else \
               2 if ("present" in low or "completed" in low or "contact" in low) else 1
        cand = (rank, int(m.group(1)),
                "stated_patient" if rank >= 2 else "stated_total",
                (offset + cs, offset + ce))
        if best is None or cand[0] > best[0]:
            best = cand
    if not best:
        return None, None, None
    return best[1], best[2], best[3]


# ---------------------------------------------------------------------------
# measures stated in prose
# ---------------------------------------------------------------------------
INSTRUMENTS = ("PHQ-9", "PHQ9", "GAD-7", "GAD7", "PCL-5", "AUDIT", "MDQ", "WHODAS")


def _prose_measures(doc_pk, facts, text, scan, year, consumed, nxt) -> list[Claim]:
    out: list[Claim] = []
    for inst in INSTRUMENTS:
        for m in re.finditer(re.escape(inst), scan, re.I):
            cs, ce = _span_of_context(scan, m.start())
            if any(cs >= a and ce <= b for a, b in consumed):
                continue
            ctx = scan[cs:ce]
            # The score usually sits on the line after the instrument name, so
            # search a window around the mention, not only its own sentence.
            win = scan[max(0, cs - 300):min(len(scan), ce + 400)]
            tm = re.search(r"(?:total(?: score)?|score)\s*:?\s*(\d{1,3})\b", win, re.I) or \
                 re.search(r"total[^\d]{0,12}(\d{1,3})\b", win, re.I)
            if not tm:
                continue
            dm = re.search(r"(?:completed(?: by [a-z]+)?(?: on)?|date completed)\s*:?\s*"
                           r"([A-Za-z0-9,\- ]{6,30})", win, re.I)
            date = nz.parse_date(dm.group(1), default_year=year) if dm else None
            date = date or nz.parse_date(ctx, default_year=year) or \
                nz.parse_date(text, default_year=year)
            form = FORM.search(win)
            items = {}
            im = re.search(r"item\s*(\d{1,2})\s*:?\s*(\d{1,2})", win, re.I)
            if im:
                items[f"item{im.group(1)}"] = int(im.group(2))
            out.append(_mk_claim(
                doc_pk, facts, text, (cs, ce),
                claim_id=nxt("M"), claim_type="measure",
                form_ref=form.group(1) if form else None, service_date=date,
                fields={"instrument": inst.upper().replace("PHQ9", "PHQ-9")
                        .replace("GAD7", "GAD-7"),
                        "total": float(tm.group(1)), "items": items or None,
                        "is_import": facts.doc_class in ("admin_log", "retransmission")}))
            break   # one claim per instrument per document
    return out


# ---------------------------------------------------------------------------
# plan requirements
# ---------------------------------------------------------------------------
REQ_DAYS = re.compile(
    r"(?:at least|no fewer than|minimum of|a minimum of)\s+(\d+)\s+"
    r"(?:therapy\s+)?days?", re.I)
REQ_MIN = re.compile(
    r"(?:at least|no fewer than|minimum of|a minimum of)\s+(\d+)\s+minutes", re.I)
REQ_HRS = re.compile(
    r"(?:at least|no fewer than|minimum of|a minimum of)\s+(\d+(?:\.\d+)?)\s+hours", re.I)
PERIOD = re.compile(r"each\s+(Monday" + DASH + r"Sunday|Sunday" + DASH + r"Saturday|calendar)?\s*week", re.I)
EPISODE = re.compile(
    r"(?:episode dates?|episode is planned for|review period)\s*:?\s*"
    r"([A-Za-z0-9,\- ]{6,30}?)\s*(?:through|to|" + DASH + r")\s*([A-Za-z0-9,\- ]{6,30}?)\s*[.,\n]", re.I)
CONTRIB = re.compile(r"([^.\n]*?\bcontribute[^.\n]*\.)", re.I)
NOCONTRIB = re.compile(r"([^.\n]*?\bdo not contribute[^.\n]*\.)", re.I)


def _plan(doc_pk, facts, text, scan, year, nxt) -> list[Claim]:
    out: list[Claim] = []
    ep_start = ep_end = None
    m = EPISODE.search(text)
    if m:
        ep_start = nz.parse_date(m.group(1), default_year=year)
        ep_end = nz.parse_date(m.group(2), default_year=year)
        out.append(_mk_claim(
            doc_pk, facts, text, (m.start(), m.end()),
            claim_id=nxt("E"), claim_type="episode",
            service_date=ep_start,
            fields={"start": ep_start, "end": ep_end}))
    if facts.doc_class != "treatment_plan":
        return out

    pm = PERIOD.search(text)
    period = "week_mon_sun"
    if pm and pm.group(1) and "sunday-saturday" in pm.group(1).lower().replace("–", "-"):
        period = "week_sun_sat"
    include, exclude = _contrib_sets(text)
    signed = nz.parse_date(
        (re.search(r"signed\s*([A-Za-z0-9,\- ]{6,24})", text, re.I) or
         re.match(r"", text)).group(1) if re.search(r"signed\s*([A-Za-z0-9,\- ]{6,24})", text, re.I)
        else "", default_year=year)
    eff_start = ep_start or signed
    for rx, metric, mult in ((REQ_DAYS, "therapy_days", 1),
                             (REQ_MIN, "therapy_minutes", 1),
                             (REQ_HRS, "therapy_minutes", 60)):
        m = rx.search(text)
        if not m:
            continue
        if metric == "therapy_minutes" and any(
                c.fields.get("metric") == "therapy_minutes" for c in out):
            continue
        cs, ce = _span_of_context(text, m.start())
        out.append(_mk_claim(
            doc_pk, facts, text, (cs, ce),
            claim_id=nxt("P"), claim_type="plan_requirement",
            service_date=eff_start,
            fields={"metric": metric, "comparator": ">=",
                    "threshold": float(m.group(1)) * mult, "period": period,
                    "service_types": include, "excluded_types": exclude,
                    "effective_start": eff_start, "effective_end": ep_end}))
    return out


def _contrib_sets(text: str) -> tuple[list[str], list[str]]:
    include: list[str] = []
    exclude: list[str] = []
    nm = NOCONTRIB.search(text)
    if nm:
        exclude = _types_in(nm.group(1))
    for m in CONTRIB.finditer(text):
        s = m.group(1)
        if nm and s.strip() == nm.group(1).strip():
            continue
        include = _types_in(s)
        if include:
            break
    if not include:
        include = ["individual_therapy", "group_therapy", "family_therapy"]
    return include, exclude


def _types_in(sentence: str) -> list[str]:
    """Which canonical services a sentence enumerates (handles 'individual, group, and family therapy')."""
    low = sentence.lower()
    found: list[str] = []
    pairs = [("individual", "individual_therapy"), ("group", "group_therapy"),
             ("family", "family_therapy"), ("medication", "medication_management"),
             ("collateral", "collateral_contact"), ("care coordination", "care_coordination"),
             ("coordination", "care_coordination")]
    for word, canon in pairs:
        if re.search(rf"\b{re.escape(word)}\b", low) and canon not in found:
            found.append(canon)
    return found


# ---------------------------------------------------------------------------
# billing / authorization
# ---------------------------------------------------------------------------
def _admin(doc_pk, facts, text, scan, year, nxt) -> list[Claim]:
    out: list[Claim] = []
    cm = CHARGE.search(text)
    if cm:
        cs, ce = _span_of_context(text, cm.start())
        enc = ENC.search(text[max(0, cs - 200):ce + 400])
        dm = re.search(r"service date\s*:?\s*([A-Za-z0-9,\- ]{6,24})", text, re.I)
        out.append(_mk_claim(
            doc_pk, facts, text, (cs, min(len(text), ce + 200)),
            claim_id=nxt("B"), claim_type="charge",
            encounter_ref=enc.group(1).upper() if enc else None,
            service_date=nz.parse_date(dm.group(1), default_year=year) if dm else None,
            authority_class="billing", presence=None,
            fields={"charge_id": cm.group(1)}))
    am = re.search(r"Authorization:?\s*([A-Z0-9\-]{6,24})", text)
    if am and facts.doc_class == "authorization":
        qm = re.search(r"Authorized quantity:?\s*(\d+)\s*([a-z ]+)", text, re.I)
        cs, ce = _span_of_context(text, am.start())
        out.append(_mk_claim(
            doc_pk, facts, text, (cs, ce),
            claim_id=nxt("A"), claim_type="authorization",
            authority_class="authorization",
            fields={"authorization_id": am.group(1),
                    "quantity": int(qm.group(1)) if qm else None,
                    "unit": qm.group(2).strip() if qm else None}))
    return out


# ---------------------------------------------------------------------------
# observations (progress evidence)
# ---------------------------------------------------------------------------
DOMAINS = {
    "mood": ("mood", "low mood", "depress", "heavy", "sad", "pervasive low"),
    "sleep": ("sleep", "waking", "wakeful", "night", "settling", "settle", "insomnia",
              "wind-down", "bedtime"),
    "anxiety": ("anxiet", "anxious", "worry", "worried", "tense", "tension", "panic",
                "grounding", "paced breathing", "activation"),
    "avoidance_work": ("avoid", "postpon", "delay", "supervisor", "work", "employment",
                       "email", "message", "inbox", "return to work", "draft"),
    "activity": ("activity", "activities", "task", "initiat", "walk", "routine",
                 "follow-through", "follow through", "behavioral activation"),
    # No proper noun belongs here: the patient's own given name is read from
    # the document header at runtime, and a relative is recognised by role.
    "social_support": ("partner", "spouse", "husband", "wife", "family member",
                       "support", "check-in", "reminder", "together",
                       "informant", "caregiver"),
    "safety": ("suicid", "self-harm", "safety", "item 9"),
    "participation": ("participat", "engaged", "attend", "contributed", "receptive"),
}
# "improve" on its own is aspirational ("Goal 1: improve daily activity"), so
# only completed-change wording counts as improvement.
IMPROVE = ("less ", "lower", "easier", "improved", "improvement", "more willing",
           "reduced", "fewer", "more varied", "eased", "better", "no longer",
           "more animated", "less overwhelming", "less heavy", "somewhat less")
# Wording that looks like improvement but describes a symptom. "reduced
# interest in usual activities" is a presenting complaint, not progress.
NEGATED_IMPROVE = ("reduced interest", "less interest", "less able", "less willing",
                   "fewer activities", "lower mood", "less motivated",
                   "reduced appetite", "reduced activity", "less energy")
PERSIST = ("remains", "remain", "continues", "continue", "continued", "still",
           "persistent", "unchanged", "ongoing", "has not yet", "inconsistent",
           "variable", "uneven", "intermittent")
WORSE = ("worse", "increased", "more difficult", "harder", "deteriorat",
         "became tense", "visibly tense", "overwhelmed", "prolonged wakefulness")
PATIENT_REPORT = ("reported", "described", "said", "endorse", "wrote", "denied",
                  "noted")
# A sentence stating an intention, goal or plan describes care that has not
# happened yet; it is not evidence of symptom course.
FORWARD_LOOKING = re.compile(
    r"^(?:goal\s*\d|plan\b|plan:|review:|recommend|continue\b|continued treatment|"
    r"interventions?:|presenting needs|local treatment participation goal|"
    r"discussed\b|break details)", re.I)
FORWARD_PHRASES = ("wishes to", "will practice", "will select", "will address",
                   "will provide", "will continue", "intends to", "agreed to",
                   "may address", "may help", "is appropriate", "recommend",
                   "will review", "encouraged to", "was directed to",
                   "i will continue", "plan is to", "members reviewed",
                   "attendance attestation")


def _observations(doc_pk, facts, text, scan, year, nxt) -> list[Claim]:
    """Narrative evidence about symptom course, with its exact source span.

    Deliberately conservative: a sentence is kept only when it refers to the
    patient, names a clinical domain, and carries a change or persistence cue.
    Goal and plan language is excluded, because a plan is not an observation.
    """
    if facts.doc_class in ("authorization", "billing", "schedule_export",
                           "retransmission", "draft_note", "treatment_plan",
                           "attendance_register"):
        return []
    given = (facts.given_name or "").lower()
    base_date = nz.parse_date(scan, default_year=year)
    dm = re.search(r"(?:service date|date of service|questionnaire completed|"
                   r"completed(?: by [a-z]+)?(?: on)?)\s*:?\s*([A-Za-z0-9,\- ]{6,24})",
                   scan, re.I)
    if dm:
        base_date = nz.parse_date(dm.group(1), default_year=year) or base_date
    is_collateral = "collateral" in scan[:400].lower()
    out: list[Claim] = []
    pos = 0
    for raw in SENT_SPLIT.split(scan):
        start = scan.find(raw, pos)
        if start < 0:
            start = pos
        pos = start + len(raw)
        sent = raw.strip()
        if len(sent) < 40:
            continue
        low = sent.lower()
        if FORWARD_LOOKING.match(low) or any(ph in low for ph in FORWARD_PHRASES):
            continue
        if not ((given and given in low) or "the patient" in low
                or low.startswith("patient ") or " patient " in low
                or low.startswith("they ") or low.startswith("sleep ")
                or low.startswith("mood ") or low.startswith("affect ")):
            continue
        doms = [d for d, cues in DOMAINS.items() if any(c in low for c in cues)]
        doms = [d for d in doms if d != "participation"] or doms
        if not doms:
            continue
        up = any(c in low for c in IMPROVE) and             not any(c in low for c in NEGATED_IMPROVE)
        down = any(c in low for c in WORSE) or             any(c in low for c in NEGATED_IMPROVE)
        flat = any(c in low for c in PERSIST)
        if not (up or down or flat):
            continue
        polarity = ("mixed" if up and (down or flat) else
                    "improvement" if up else
                    "worsening" if down else "persistence")
        reporter = "clinician"
        if is_collateral:
            reporter = "informant"
        elif given and re.search(
                rf"\b{re.escape(given)}\b[^.]{{0,40}}(?:{'|'.join(PATIENT_REPORT)})", low):
            reporter = "patient"
        out.append(_mk_claim(
            doc_pk, facts, text, (start, start + len(raw)),
            claim_id=nxt("O"), claim_type="observation", service_date=base_date,
            fields={"domains": doms[:3], "polarity": polarity, "reporter": reporter}))
    return out
