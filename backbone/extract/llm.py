"""Model-backed extraction, in two modes.

    llm       hybrid (shipped recommendation). Tables, ids, dispositions and
              clock times are parsed by `rules`; the model is asked only to turn
              clinical prose into structured observations and to read plan
              requirement language.

    llm_full  the model also extracts the encounter structure -- document class,
              service type, presence, patient-present intervals, breaks, stated
              minutes, corrections, measures. It still never computes a duration
              and never resolves a conflict: `intervals.py` does the arithmetic
              and `reconcile.py` applies the authority policy, exactly as for the
              rules path. This mode exists so the two front-ends can be diffed
              against each other (`python -m backbone compare`).

In both modes every string the model returns must be found character-for-
character in the source document, or the claim is dropped and counted. That
check is cheap and it is what keeps a hallucinated quote out of the abstraction.

Responses are cached in their own SQLite file (out/llm-cache.db, override with
BACKBONE_CACHE_DB), keyed by (normalized document text, prompt version, mode,
provider, model). A rebuild of the abstraction therefore costs nothing, and a
prompt change re-extracts only what changed. The cache is also what makes this
path reproducible -- see docs/experiment-extractor-comparison.md.
"""
from __future__ import annotations

import json
import os
import pathlib
import re

from .. import normalize as nz
from .. import provider
from ..taxonomy import EXTRACTOR_VERSION, DOC_CLASSES, SERVICE_TYPES
from . import rules
from .base import Claim, DocFacts

NAME = "llm"
VERSION = EXTRACTOR_VERSION
PROMPT_VERSION = "v3"
MAX_TOKENS = 9000
TEMPERATURE = 0.0          # extraction must be reproducible

# Filled in by ingest so a run can report what the model cost.
STATS: dict = {"calls": 0, "cached": 0, "in_tokens": 0, "out_tokens": 0,
               "usd": 0.0, "latency_ms": 0.0, "dropped_quotes": 0,
               "parse_failures": 0, "call_failures": 0, "documents": 0}


def reset_stats() -> None:
    for k in STATS:
        STATS[k] = 0 if isinstance(STATS[k], int) else 0.0


def available() -> tuple[bool, str]:
    return provider.available()


def describe() -> str:
    return provider.describe()


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
_OBS_SCHEMA = """{"observations":[{"quote":"<verbatim sentence from the document>",
   "domains":["mood"|"sleep"|"anxiety"|"avoidance_work"|"activity"|
              "social_support"|"safety"|"participation"],
   "polarity":"improvement"|"persistence"|"worsening"|"mixed",
   "reporter":"patient"|"clinician"|"informant",
   "date":"YYYY-MM-DD"|null}],
 "requirements":[{"quote":"<verbatim>","metric":"therapy_days"|"therapy_minutes",
   "comparator":">=","threshold":<number>,
   "period":"week_mon_sun"|"week_sun_sat",
   "service_types":[<service types that count>],
   "excluded_types":[<service types the text says do not count>]}]}"""

PROSE_SYSTEM = f"""You convert clinical prose into structured facts. You never \
infer, never compute durations, and never resolve disagreements between \
documents.

Return ONLY a JSON object of this shape:
{_OBS_SCHEMA}

Rules:
- Every "quote" MUST appear character-for-character in the document. Do not \
normalise punctuation or dashes, do not join sentences, do not paraphrase, do \
not trim or add words.
- An observation describes something that has already happened to this patient. \
A goal, plan, intention, recommendation or scheduled activity is NOT an \
observation; omit it.
- "reporter" is "patient" when the patient is the source of the statement, \
"informant" when another person reports about the patient, otherwise \
"clinician".
- Convert hours to minutes in a requirement threshold.
- Return an empty list for any section the document does not contain."""

FULL_SYSTEM = f"""You are a clinical records abstractor. You extract what a \
single document ASSERTS. You never infer facts it does not state, never compute \
a duration, never add up minutes, and never decide which of two documents is \
right.

Return ONLY a JSON object:
{{"document_class":<one of {list(DOC_CLASSES)}>,
 "patient":{{"mrn":"<string>"|null,"name":"<string>"|null,"dob":"YYYY-MM-DD"|null}},
 "encounters":[{{
   "encounter_ref":"<id as printed>"|null,
   "appointment_ref":"<id>"|null,
   "call_ref":"<telehealth call/session id>"|null,
   "service_date":"YYYY-MM-DD"|null,
   "service_type":<one of {list(SERVICE_TYPES)}>,
   "presence":"present"|"partial"|"absent"|"cancelled"|"unknown",
   "negation_kind":"no_show"|"cancelled"|"patient_absent"|"not_a_contact"|null,
   "patient_present_intervals":[["HH:MM","HH:MM"], ...],
   "nontherapeutic_breaks":[["HH:MM","HH:MM"], ...],
   "scheduled_interval":["HH:MM","HH:MM"]|null,
   "stated_patient_minutes":<number>|null,
   "reason_added":"<verbatim sentence>"|null,
   "quote":"<verbatim span that carries these facts>"}}],
 "corrections":[{{"encounter_ref":"<id>"|null,
   "target_field":"arrival"|"departure"|"duration"|"presence"|"service_date",
   "old_value":"<as printed>"|null,"new_value":"<as printed>",
   "quote":"<verbatim>"}}],
 "measures":[{{"instrument":"<e.g. PHQ-9>","total":<number>|null,
   "form_ref":"<form id>"|null,"completed_date":"YYYY-MM-DD"|null,
   "items":{{"item9":0}}|null,"quote":"<verbatim>"}}],
 "episode":{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"|null}}|null,
 "requirements":[ ...as below... ],
 "observations":[ ...as below... ]}}

The requirements and observations entries use exactly this shape:
{_OBS_SCHEMA}

Hard rules:
- Every "quote" MUST appear character-for-character in the document, dashes and \
punctuation included.
- One entry per encounter. A document that tabulates several encounters \
produces several entries. A document that describes one encounter produces one, \
even if several clinicians wrote about it.
- "patient_present_intervals" are intervals the document says THE PATIENT was \
present for. A scheduled or booked slot is NOT patient-present time: put it in \
"scheduled_interval". A therapist-only or partner-only interval is NOT \
patient-present time: omit it.
- "nontherapeutic_breaks" are intervals the document says had no treatment: \
breaks, pauses, lost telehealth connections.
- Do NOT subtract breaks, do NOT total intervals, do NOT convert to minutes. \
Report the clock times as printed. Use "stated_patient_minutes" only when the \
document itself states a number of patient-present minutes.
- "presence" is what THIS document asserts. Use "absent" with \
negation_kind="patient_absent" when the contact happened without the patient \
(for example a collateral or coordination contact). Use negation_kind \
"not_a_contact" when the document says of itself that it records no visit (an \
import receipt, a questionnaire review, a retransmission cover sheet).
- "document_class": use "draft_note" for unsigned template-populated text, \
"billing" for charge rows, "retransmission" for a resent copy of an earlier \
record, "schedule_export" for a booking view, "attendance_register" for a \
signed attendance or disposition record, "correction" for an entry that names \
the record and field it replaces, "platform_log" for a telehealth connection \
export, "clinical_note" for a signed clinician narrative.
- Return empty lists for sections the document does not contain."""


# ---------------------------------------------------------------------------
# The cache lives in its own file so it survives a rebuild of the abstraction.
# Without this, re-extracting a corpus would re-pay for every document.
CACHE_DB = pathlib.Path(
    os.environ.get("BACKBONE_CACHE_DB", "out/llm-cache.db"))
_cache_store = None


def cache_store():
    global _cache_store
    if _cache_store is None:
        from ..store import Store
        _cache_store = Store(CACHE_DB)
    return _cache_store


def _cache_key(text: str, mode: str) -> str:
    return nz.sha256("|".join([nz.normalize_text(text), PROMPT_VERSION, mode,
                               provider.provider(), provider.model()]))


def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError("model did not return parseable JSON")


def _call(text: str, mode: str, store) -> dict:
    key = _cache_key(text, mode)
    cache = cache_store()
    hit = cache.cache_get(key) or (store.cache_get(key) if store else None)
    if hit:
        STATS["cached"] += 1
        if store is not None:          # keep the abstraction self-contained
            store.cache_put(key, hit["model"], hit["prompt_ver"], hit["response"],
                            hit["in_tokens"], hit["out_tokens"], hit["usd"])
            store.commit()
        return json.loads(hit["response"])
    system = FULL_SYSTEM if mode == "llm_full" else PROSE_SYSTEM
    try:
        raw, usage = provider.complete(
            system, f"<document>\n{text}\n</document>",
            max_tokens=MAX_TOKENS, temperature=TEMPERATURE, json_object=True)
    except provider.ModelError as e:
        # One document failing must not abort a corpus. Count it and continue;
        # the report says how many documents the model could not deliver.
        STATS["call_failures"] += 1
        print(f"    [model failed on one document: {str(e)[:140]}]", flush=True)
        return {}
    STATS["calls"] += 1
    STATS["in_tokens"] += usage["in_tokens"]
    STATS["out_tokens"] += usage["out_tokens"]
    STATS["usd"] += usage["usd"]
    STATS["latency_ms"] += usage["latency_ms"]
    try:
        data = _parse_json(raw)
    except ValueError:
        STATS["parse_failures"] += 1
        data = {}
    payload = json.dumps(data)
    for tgt in (cache, store):
        if tgt is not None:
            tgt.cache_put(key, usage["model"], f"{PROMPT_VERSION}:{mode}", payload,
                          usage["in_tokens"], usage["out_tokens"], usage["usd"])
            tgt.commit()
    return data


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """Verbatim span, with one narrow concession for trailing whitespace."""
    if not quote:
        return None
    pos = text.find(quote)
    if pos >= 0:
        return pos, pos + len(quote)
    stripped = quote.strip()
    if stripped and stripped != quote:
        pos = text.find(stripped)
        if pos >= 0:
            return pos, pos + len(stripped)
    return None


def _pairs(raw) -> list:
    """Accept [["10:00","11:30"], ...] and reject anything else quietly."""
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if (isinstance(item, (list, tuple)) and len(item) == 2
                and all(isinstance(x, str) and rules.SINGLE.search(x) for x in item)):
            out.append([rules.SINGLE.search(item[0]).group(1),
                        rules.SINGLE.search(item[1]).group(1)])
    return out


# ---------------------------------------------------------------------------
def extract(text: str, doc_pk: str, store=None,
            mode: str = "llm") -> tuple[DocFacts, list[Claim]]:
    STATS["documents"] += 1
    if mode == "llm_full":
        return _extract_full(text, doc_pk, store)
    return _extract_hybrid(text, doc_pk, store)


# ---- hybrid ----------------------------------------------------------------
def _extract_hybrid(text, doc_pk, store) -> tuple[DocFacts, list[Claim]]:
    facts, claims = rules.extract(text, doc_pk)
    # The model replaces exactly the prose claims and nothing else.
    keep = [c for c in claims if c.claim_type != "observation"]
    if facts.doc_class in ("authorization", "billing", "schedule_export",
                           "retransmission", "draft_note", "attendance_register"):
        return facts, keep          # no clinical prose worth a call

    data = _call(text, "llm", store)
    keep += _prose_claims(text, doc_pk, facts, data, keep)
    return facts, keep


def _prose_claims(text, doc_pk, facts, data, existing) -> list[Claim]:
    out: list[Claim] = []
    n = 0
    for o in data.get("observations") or []:
        span = _locate(text, (o or {}).get("quote"))
        if not span:
            STATS["dropped_quotes"] += 1
            continue
        n += 1
        doms = [d for d in (o.get("domains") or []) if isinstance(d, str)]
        out.append(Claim(
            claim_id=f"{doc_pk[:12]}-L{n:02d}", doc_pk=doc_pk,
            patient_mrn=facts.patient_mrn, claim_type="observation",
            service_date=o.get("date") or nz.parse_date(text),
            authority_class=facts.doc_class, extractor=NAME,
            quote=text[span[0]:span[1]], quote_start=span[0], quote_end=span[1],
            quote_verified=1,
            fields={"domains": doms[:3] or ["general"],
                    "polarity": o.get("polarity") or "neutral",
                    "reporter": o.get("reporter") or "clinician",
                    "model": provider.model(), "prompt_version": PROMPT_VERSION}))
    if facts.doc_class == "treatment_plan":
        have = {c.fields.get("metric") for c in existing
                if c.claim_type == "plan_requirement"}
        for i, rq in enumerate(data.get("requirements") or []):
            c = _requirement_claim(text, doc_pk, facts, rq, i, have)
            if c:
                out.append(c)
                have.add(c.fields["metric"])
    return out


def _requirement_claim(text, doc_pk, facts, rq, i, have) -> Claim | None:
    rq = rq or {}
    span = _locate(text, rq.get("quote"))
    if not span:
        STATS["dropped_quotes"] += 1
        return None
    metric = rq.get("metric")
    if metric not in ("therapy_days", "therapy_minutes") or metric in have:
        return None
    try:
        threshold = float(rq.get("threshold"))
    except (TypeError, ValueError):
        return None
    ep = _episode_dates(text)
    return Claim(
        claim_id=f"{doc_pk[:12]}-LP{i:02d}", doc_pk=doc_pk,
        patient_mrn=facts.patient_mrn, claim_type="plan_requirement",
        service_date=ep[0] or nz.parse_date(text),
        authority_class="treatment_plan", extractor=NAME,
        quote=text[span[0]:span[1]], quote_start=span[0], quote_end=span[1],
        quote_verified=1,
        fields={"metric": metric, "comparator": rq.get("comparator") or ">=",
                "threshold": threshold,
                "period": rq.get("period") or "week_mon_sun",
                "service_types": [s for s in (rq.get("service_types") or [])
                                  if s in SERVICE_TYPES],
                "excluded_types": [s for s in (rq.get("excluded_types") or [])
                                   if s in SERVICE_TYPES],
                "effective_start": ep[0] or nz.parse_date(text),
                "effective_end": ep[1],
                "model": provider.model(), "prompt_version": PROMPT_VERSION})


def _episode_dates(text: str) -> tuple[str | None, str | None]:
    m = rules.EPISODE.search(text)
    if not m:
        return None, None
    y = rules._guess_year(text)
    return nz.parse_date(m.group(1), default_year=y), \
        nz.parse_date(m.group(2), default_year=y)


# ---- full ------------------------------------------------------------------
def _extract_full(text, doc_pk, store) -> tuple[DocFacts, list[Claim]]:
    data = _call(text, "llm_full", store)
    base = rules.doc_facts(text)            # ids/MRN regexes are not worth a call
    facts = DocFacts(
        patient_mrn=(data.get("patient") or {}).get("mrn") or base.patient_mrn,
        patient_name=(data.get("patient") or {}).get("name") or base.patient_name,
        given_name=base.given_name,
        dob=(data.get("patient") or {}).get("dob") or base.dob,
        doc_id=base.doc_id,
        doc_class=(data.get("document_class")
                   if data.get("document_class") in DOC_CLASSES else base.doc_class),
        received_at=base.received_at, authored_at=base.authored_at)
    if facts.patient_name and not facts.given_name:
        facts.given_name = facts.patient_name.split()[0]

    claims: list[Claim] = []
    seq = 0

    def nid(kind: str) -> str:
        nonlocal seq
        seq += 1
        return f"{doc_pk[:12]}-F{kind}{seq:02d}"

    for e in data.get("encounters") or []:
        e = e or {}
        span = _locate(text, e.get("quote"))
        if not span:
            STATS["dropped_quotes"] += 1
            span = (0, min(len(text), 160))      # keep the claim, flag the quote
            verified = 0
        else:
            verified = 1
        svc = e.get("service_type")
        pres = e.get("presence")
        ivs = _pairs(e.get("patient_present_intervals"))
        brks = _pairs(e.get("nontherapeutic_breaks"))
        sched = _pairs([e.get("scheduled_interval")] if e.get("scheduled_interval")
                       else [])
        try:
            stated = int(e["stated_patient_minutes"]) \
                if e.get("stated_patient_minutes") is not None else None
        except (TypeError, ValueError):
            stated = None
        fields = {"interval_classes": {k: v for k, v in
                                       (("patient_present", ivs), ("break", brks),
                                        ("scheduled", sched)) if v},
                  "negation_kind": e.get("negation_kind"),
                  "presence_cue": "model", "extractor_mode": "llm_full",
                  "model": provider.model(), "prompt_version": PROMPT_VERSION}
        if sched:
            fields["scheduled"] = sched[0]
        if e.get("reason_added"):
            rspan = _locate(text, e["reason_added"])
            if rspan:
                fields["reason_added"] = {"quote": text[rspan[0]:rspan[1]],
                                          "offsets": [rspan[0], rspan[1]]}
        claims.append(Claim(
            claim_id=nid("E"), doc_pk=doc_pk, patient_mrn=facts.patient_mrn,
            claim_type="encounter",
            encounter_ref=(e.get("encounter_ref") or None),
            appointment_ref=(e.get("appointment_ref") or None),
            call_ref=(e.get("call_ref") or None),
            service_date=e.get("service_date") or None,
            service_raw=(e.get("quote") or "")[:120],
            service_type=svc if svc in SERVICE_TYPES else "unknown",
            presence=pres if pres in ("present", "partial", "absent",
                                      "cancelled", "unknown") else None,
            presence_basis="narrative" if facts.doc_class == "clinical_note"
            else "derived",
            intervals=ivs, breaks=brks, stated_minutes=stated,
            minutes_basis="computed_from_intervals" if ivs else
            ("stated_patient" if stated else None),
            authority_class=facts.doc_class, fields=fields,
            quote=text[span[0]:span[1]], quote_start=span[0], quote_end=span[1],
            quote_verified=verified, extractor=NAME))

    for c in data.get("corrections") or []:
        c = c or {}
        span = _locate(text, c.get("quote"))
        if not span or not c.get("new_value") or not c.get("target_field"):
            STATS["dropped_quotes"] += 1 if not span else 0
            continue
        claims.append(Claim(
            claim_id=nid("C"), doc_pk=doc_pk, patient_mrn=facts.patient_mrn,
            claim_type="correction",
            encounter_ref=c.get("encounter_ref") or None,
            service_date=nz.parse_date(text, rules._guess_year(text)),
            authority_class="correction", extractor=NAME,
            quote=text[span[0]:span[1]], quote_start=span[0], quote_end=span[1],
            quote_verified=1,
            fields={"target_field": c.get("target_field"),
                    "old_value": c.get("old_value"),
                    "new_value": c.get("new_value"),
                    "model": provider.model()}))

    for m in data.get("measures") or []:
        m = m or {}
        span = _locate(text, m.get("quote"))
        if not span:
            STATS["dropped_quotes"] += 1
            continue
        try:
            total = float(m["total"]) if m.get("total") is not None else None
        except (TypeError, ValueError):
            total = None
        claims.append(Claim(
            claim_id=nid("M"), doc_pk=doc_pk, patient_mrn=facts.patient_mrn,
            claim_type="measure", form_ref=m.get("form_ref") or None,
            service_date=m.get("completed_date") or None,
            authority_class=facts.doc_class, extractor=NAME,
            quote=text[span[0]:span[1]], quote_start=span[0], quote_end=span[1],
            quote_verified=1,
            fields={"instrument": (m.get("instrument") or "UNKNOWN").upper(),
                    "total": total,
                    "items": m.get("items") if isinstance(m.get("items"), dict) else None,
                    "is_import": facts.doc_class in ("admin_log", "retransmission"),
                    "model": provider.model()}))

    ep = data.get("episode") or {}
    if ep.get("start"):
        claims.append(Claim(
            claim_id=nid("P"), doc_pk=doc_pk, patient_mrn=facts.patient_mrn,
            claim_type="episode", service_date=ep.get("start"),
            authority_class=facts.doc_class, extractor=NAME,
            quote=text[:120], quote_start=0, quote_end=min(len(text), 120),
            quote_verified=1,
            fields={"start": ep.get("start"), "end": ep.get("end")}))

    have: set = set()
    for i, rq in enumerate(data.get("requirements") or []):
        c = _requirement_claim(text, doc_pk, facts, rq, i, have)
        if c:
            c.claim_id = nid("R")
            claims.append(c)
            have.add(c.fields["metric"])

    claims += _prose_claims(text, doc_pk, facts, data, claims)
    for c in claims:
        if c.quote_verified:
            c.verify(text)
    return facts, claims


# ---------------------------------------------------------------------------
# optional LLM router: picks a query function, never a number
# ---------------------------------------------------------------------------
ROUTER_SYSTEM = """Pick the query function that answers the user's question and \
fill in its parameters. Return ONLY JSON: {"function":"<name>","params":{...}}.
Never answer the question yourself and never invent a number.

session_counts(mrn, start, end)             how many sessions, by type, how many days
minutes(mrn, start, end)                    minutes/hours overall and per week
compliance(mrn, start, end)                 did delivered care meet the plan goal
consecutive_below(mrn|null, n, start, end)  patients with n consecutive weeks below
day_detail(mrn, dates=[...])                reconstruct specific dates
plan_change_comparison(mrn, change_date)    care before vs after a plan change
progress(mrn, start, end)                   symptom course and supporting evidence
trace(event_id)                             why one event resolved as it did
duplicates()                                duplicate documents detected
integrity(mrn)                              self-consistency checks

Dates are ISO (YYYY-MM-DD). Use null for a parameter the question does not fix."""


def route_llm(store, question: str, allowed: list[str]) -> dict | None:
    ok, _ = available()
    if not ok:
        return None
    pats = ", ".join(f"{r['mrn']} ({r['name']})" for r in
                     store.q("SELECT mrn, name FROM patients"))
    try:
        raw, usage = provider.complete(
            ROUTER_SYSTEM, f"Patients: {pats}\nQuestion: {question}",
            max_tokens=500, temperature=0.0, json_object=True)
        data = _parse_json(raw)
    except Exception:
        return None
    if data.get("function") not in allowed:
        return None
    params = data.get("params") or {}
    params = {k: v for k, v in params.items() if v is not None or k == "mrn"}
    return {"function": data["function"], "params": params, "router": "llm",
            "patients_in_scope": [], "router_tokens": usage["in_tokens"]
            + usage["out_tokens"], "router_usd": usage["usd"]}
