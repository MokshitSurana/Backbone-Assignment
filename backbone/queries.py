"""The query library.

Every number a reviewer sees is computed here, in Python, from the reconciled
event table -- never by a model. Each function returns

    {"value": ..., "calculation": [...], "evidence": [...], "caveats": [...]}

`calculation` is the arithmetic written out so it can be checked by hand.
`evidence` is claim-level: document id, character offsets, and the quoted span.

Uncertainty is carried as bounds, not as a label. A disputed duration widens
[min, max]; an unresolved occurrence contributes to max only. A requirement is
MET when the minimum clears it, BELOW when even the maximum misses, and
CANNOT_DETERMINE when the threshold falls inside the band.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from . import intervals as iv
from .store import Store
from .taxonomy import THERAPY_CANDIDATES

MET, BELOW, UNKNOWN = "MET", "NOT_MET", "CANNOT_DETERMINE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def included_types(st: Store, mrn: str) -> list[str]:
    """Service types the patient's own plan counts toward the therapy goal."""
    types: set[str] = set()
    for r in st.requirements(mrn):
        types |= set(json.loads(r["service_types"]))
    return sorted(types or set(THERAPY_CANDIDATES))


def episode_window(st: Store, mrn: str) -> tuple[str | None, str | None]:
    row = st.episode(mrn)
    return (row["start_date"], row["end_date"]) if row else (None, None)


def _events(st: Store, mrn: str, start=None, end=None) -> list[dict]:
    out = []
    for r in st.events(mrn, start, end):
        d = dict(r)
        d["resolved"] = json.loads(d["resolved"])
        out.append(d)
    return out


def _band(ev: dict, include: list[str]) -> tuple[int, int]:
    """(minutes certainly delivered, minutes possibly delivered)."""
    if not ev["service_date"]:
        return 0, 0          # cannot be placed in a week; surfaced as a caveat
    if ev["service_type"] not in include:
        return 0, 0
    if ev["patient_present"] == "no":
        return 0, 0
    if ev["occurred"] == "yes":
        return ev["min_minutes"], ev["max_minutes"]
    if ev["occurred"] == "unknown":
        return 0, max(ev["max_minutes"], 0)
    return 0, 0


def _counts(ev: dict, include: list[str]) -> tuple[int, int]:
    """(session certainly delivered, session possibly delivered)."""
    if not ev["service_date"]:
        return 0, 0
    if ev["service_type"] not in include or ev["patient_present"] == "no":
        return 0, 0
    if ev["occurred"] == "yes":
        return 1, 1
    if ev["occurred"] == "unknown":
        return 0, 1
    return 0, 0


def evidence_for(st: Store, event_id: str, roles=("primary", "correction")) -> list[dict]:
    out = []
    for r in st.event_evidence(event_id):
        if roles and r["role"] not in roles:
            continue
        out.append({
            "event_id": event_id, "role": r["role"], "claim_id": r["claim_id"],
            "document": r["doc_id"] or r["filename"], "file": r["filename"],
            "authority": r["authority_class"],
            "offsets": [r["quote_start"], r["quote_end"]],
            "quote": r["quote"], "verified": bool(r["quote_verified"]),
        })
    return out


def all_evidence(st: Store, event_id: str) -> list[dict]:
    return evidence_for(st, event_id, roles=None)


# ---------------------------------------------------------------------------
# Running a per-patient query over several patients
# ---------------------------------------------------------------------------
def per_patient(st: Store, fn, mrns: list[str], **params) -> dict:
    """Run a per-patient query for each patient and keep the results apart.

    The brief asks these questions "for each patient", so a question that names
    no single patient must not collapse to whichever MRN happened to sort
    first. Each patient's value, calculation and evidence stay under their own
    MRN; the top level carries the union so a reviewer can still follow any
    figure to a passage.
    """
    out: dict[str, dict] = {}
    calc: list[str] = []
    evidence: list[dict] = []
    caveats: list[dict] = []
    for mrn in mrns:
        local = dict(params)
        if "start" in local and local.get("start") is None \
                and local.get("end") is None:
            # no window was fixed by the question: use this patient's own
            # episode rather than another patient's dates
            local["start"], local["end"] = episode_window(st, mrn)
        r = fn(st, mrn, **local)
        out[mrn] = r
        calc += [f"[{mrn}] {c}" for c in r.get("calculation", [])]
        for e in r.get("evidence", []):
            evidence.append({**e, "patient": mrn})
        for c in r.get("caveats", []):
            caveats.append({**c, "patient": mrn})
    return {"value": {"per_patient": {m: r["value"] for m, r in out.items()},
                      "patients": list(mrns)},
            "results": out, "calculation": calc, "evidence": evidence,
            "caveats": caveats}


# ---------------------------------------------------------------------------
# Q: session counts
# ---------------------------------------------------------------------------
def session_counts(st: Store, mrn: str, start=None, end=None) -> dict:
    include = included_types(st, mrn)
    evs = _events(st, mrn, start, end)
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    days_min, days_max = set(), set()
    lo = hi = 0
    rows, calc = [], []
    for e in evs:
        cmin, cmax = _counts(e, include)
        if not cmax:
            continue
        by_type[e["service_type"]][0] += cmin
        by_type[e["service_type"]][1] += cmax
        lo += cmin
        hi += cmax
        if cmin:
            days_min.add(e["service_date"])
        days_max.add(e["service_date"])
        rows.append({"event_id": e["event_id"], "encounter": e["encounter_ref"],
                     "date": e["service_date"], "service": e["service_type"],
                     "certain": bool(cmin), "minutes": [e["min_minutes"], e["max_minutes"]]})
        calc.append(f"{e['service_date']} {e['encounter_ref'] or e['event_id']} "
                    f"{e['service_type']} -> {'1 session' if cmin else '0-1 session'}")
    value = {
        "sessions": lo if lo == hi else [lo, hi],
        "by_service_type": {k: (v[0] if v[0] == v[1] else v) for k, v in sorted(by_type.items())},
        "distinct_days": len(days_min) if days_min == days_max
        else [len(days_min), len(days_max)],
        "days": sorted(days_max),
    }
    ev = [x for r in rows for x in evidence_for(st, r["event_id"])]
    return {"value": value, "calculation": calc, "rows": rows, "evidence": ev,
            "caveats": _count_caveats(st, mrn, evs, include),
            "window": [start, end], "counted_service_types": include}


def _count_caveats(st: Store, mrn: str, evs: list[dict], include: list[str]) -> list[dict]:
    """Records that could inflate a count, and why they do not."""
    out = []
    for e in evs:
        if e["service_type"] in include and e["occurred"] != "yes":
            out.append({"kind": "not_counted", "event": e["encounter_ref"] or e["event_id"],
                        "date": e["service_date"], "reason": e["exclusion_reason"],
                        "evidence": all_evidence(st, e["event_id"])})
        elif e["service_type"] not in include:
            out.append({"kind": "ineligible_service", "event": e["encounter_ref"] or e["event_id"],
                        "date": e["service_date"], "reason": e["exclusion_reason"],
                        "evidence": evidence_for(st, e["event_id"], roles=None)[:2]})
        elif e["patient_present"] == "no":
            out.append({"kind": "patient_not_present",
                        "event": e["encounter_ref"] or e["event_id"],
                        "date": e["service_date"], "reason": e["exclusion_reason"],
                        "evidence": evidence_for(st, e["event_id"], roles=None)[:2]})
    # Filter to this patient. Reading the whole table would list another
    # patient's rejected claims inside this patient's answer. A dedupe decision
    # has no patient (identity is decided before extraction), so it is included
    # with its own rule id and read as corpus-level.
    for d in st.q("SELECT * FROM decisions WHERE rule_id IN "
                  "('RULE-DEDUPE-01','RULE-PRES-02','RULE-DUR-05','RULE-MATCH-03',"
                  "'RULE-MATCH-05','RULE-MEAS-01','RULE-MEAS-02')"
                  " AND (patient_mrn=? OR (patient_mrn IS NULL AND"
                  " rule_id='RULE-DEDUPE-01')) ORDER BY decision_id", (mrn,)):
        if d["field"] in ("rejected_presence", "rejected_duration", "duplicate",
                          "unmatched"):
            out.append({"kind": d["field"], "rule": d["rule_id"],
                        "reason": d["rationale"], "evidence": []})
    return out


# ---------------------------------------------------------------------------
# Q: minutes and hours, overall and per week
# ---------------------------------------------------------------------------
def _week_keys(st: Store, mrn: str, evs: list[dict], start, end,
               scheme: str) -> list[str]:
    """Every week in the review window, including weeks with no therapy at all.

    A week with no attendance is a week below the requirement, not a week that
    does not exist. Bucketing only the weeks that happen to contain an event
    would silently drop it from compliance and from any consecutive-weeks
    question.
    """
    lo, hi = start, end
    if not (lo and hi):
        ep_start, ep_end = episode_window(st, mrn)
        lo = lo or ep_start
        hi = hi or ep_end
    dates = sorted(e["service_date"] for e in evs if e["service_date"])
    if not lo and dates:
        lo = dates[0]
    if not hi and dates:
        hi = dates[-1]
    if not (lo and hi):
        return []
    cur = dt.date.fromisoformat(iv.week_key(lo, scheme))
    last = dt.date.fromisoformat(iv.week_key(hi, scheme))
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += dt.timedelta(days=7)
    return out


def _empty_week(wk: str, scheme: str) -> dict:
    return {"week_start": wk, "week_end": iv.week_span(wk)[1], "min": 0, "max": 0,
            "days_min": set(), "days_max": set(), "items": [], "disputed": []}


def minutes(st: Store, mrn: str, start=None, end=None,
            scheme: str = "week_mon_sun") -> dict:
    include = included_types(st, mrn)
    evs = _events(st, mrn, start, end)
    weeks: dict[str, dict] = {wk: _empty_week(wk, scheme) for wk in
                              _week_keys(st, mrn, evs, start, end, scheme)}
    tot_lo = tot_hi = 0
    calc: list[str] = []
    for e in sorted(evs, key=lambda x: (x["service_date"], x["event_id"])):
        lo, hi = _band(e, include)
        if not hi:
            continue
        wk = iv.week_key(e["service_date"], scheme)
        w = weeks.setdefault(wk, _empty_week(wk, scheme))
        w["min"] += lo
        w["max"] += hi
        cmin, cmax = _counts(e, include)
        if cmin:
            w["days_min"].add(e["service_date"])
        if cmax:
            w["days_max"].add(e["service_date"])
        tot_lo += lo
        tot_hi += hi
        label = f"{lo}" if lo == hi else f"{lo}-{hi}"
        w["items"].append({"date": e["service_date"],
                           "encounter": e["encounter_ref"] or e["event_id"],
                           "service": e["service_type"], "minutes": [lo, hi],
                           "breaks": e["resolved"].get("breaks"),
                           "evidence": evidence_for(st, e["event_id"])})
        if e["disputed"]:
            w["disputed"].append(e["encounter_ref"] or e["event_id"])
        calc.append(f"{e['service_date']} {e['encounter_ref'] or e['event_id']:<10}"
                    f" {e['service_type']:<18} {label} min")
    out_weeks = []
    for wk in sorted(weeks):
        w = weeks[wk]
        parts = [f"{i['minutes'][0]}" if i["minutes"][0] == i["minutes"][1]
                 else f"({i['minutes'][0]}-{i['minutes'][1]})" for i in w["items"]]
        out_weeks.append({
            "week_start": w["week_start"], "week_end": w["week_end"],
            "minutes": [w["min"], w["max"]],
            "hours": [round(w["min"] / 60, 2), round(w["max"] / 60, 2)],
            "therapy_days": [len(w["days_min"]), len(w["days_max"])],
            "sum": ((" + ".join(parts) + f" = {w['min']}"
                     + (f"-{w['max']}" if w["max"] != w["min"] else ""))
                    if parts else "no therapy recorded in this week = 0"),
            "items": w["items"], "disputed_events": w["disputed"],
        })
    return {
        "value": {"total_minutes": [tot_lo, tot_hi],
                  "total_hours": [round(tot_lo / 60, 2), round(tot_hi / 60, 2)],
                  "weeks": out_weeks},
        "calculation": calc,
        "evidence": [x for w in out_weeks for i in w["items"] for x in i["evidence"]],
        "caveats": _dispute_caveats(st, mrn, evs),
        "week_scheme": scheme, "window": [start, end],
    }


def _dispute_caveats(st: Store, mrn: str, evs: list[dict]) -> list[dict]:
    out = []
    for e in evs:
        if not e["service_date"]:
            out.append({
                "event": e["encounter_ref"] or e["event_id"], "date": None,
                "bounds": [e["min_minutes"], e["max_minutes"]], "rules": ["RULE-DATE-01"],
                "reason": "no record states a usable service date for this "
                          "encounter, so it cannot be placed in a week and is "
                          "excluded from every weekly total",
                "resolvable_by": "any record stating the service date for this "
                                 "encounter",
                "evidence": all_evidence(st, e["event_id"])})
            continue
        if not e["disputed"]:
            continue
        why = [dict(d) for d in st.event_decisions(e["event_id"])
               if d["rule_id"] in ("RULE-DUR-03", "RULE-PRES-04", "RULE-DUR-05")]
        out.append({
            "event": e["encounter_ref"] or e["event_id"], "date": e["service_date"],
            "bounds": [e["min_minutes"], e["max_minutes"]],
            "rules": [w["rule_id"] for w in why],
            "reason": "; ".join(w["rationale"] for w in why),
            "resolvable_by": _what_would_settle_it(why),
            "evidence": all_evidence(st, e["event_id"]),
        })
    return out


def _what_would_settle_it(why: list[dict]) -> str:
    for w in why:
        if w["rule_id"] == "RULE-DUR-03":
            return ("an addendum or correction from either author that names the "
                    "other record's value, or a check-in/room-transfer timestamp "
                    "for the start of the contact")
        if w["rule_id"] == "RULE-PRES-04":
            return ("a signed attendance entry or clinician attestation for the "
                    "encounter")
    return "an explicit statement of the patient-present interval"


# ---------------------------------------------------------------------------
# Q: did delivered care meet the plan?
# ---------------------------------------------------------------------------
def compliance(st: Store, mrn: str, start=None, end=None,
               scheme: str | None = None) -> dict:
    reqs = [dict(r) for r in st.requirements(mrn)]
    if not reqs:
        return {"value": None, "calculation": [],
                "evidence": [], "caveats": [{"kind": "no_plan",
                "reason": "no treatment-plan requirement found in the record"}]}
    scheme = scheme or reqs[0]["period"]
    m = minutes(st, mrn, start, end, scheme)
    ep_start, ep_end = episode_window(st, mrn)
    out, calc = [], []
    for w in m["value"]["weeks"]:
        in_force = [r for r in reqs
                    if r["effective_start"] <= w["week_end"]
                    and (not r["effective_end"] or r["effective_end"] >= w["week_start"])]
        checks = []
        for r in in_force:
            lo, hi = ((w["minutes"] if r["metric"] == "therapy_minutes"
                       else w["therapy_days"]))
            status = (MET if lo >= r["threshold"] else
                      BELOW if hi < r["threshold"] else UNKNOWN)
            checks.append({
                "metric": r["metric"], "comparator": r["comparator"],
                "threshold": r["threshold"], "actual": [lo, hi], "status": status,
                "requirement_source": _req_evidence(st, r),
                "arithmetic": (w["sum"] if r["metric"] == "therapy_minutes"
                               else _days_arithmetic(w)),
            })
            calc.append(f"week {w['week_start']}..{w['week_end']} {r['metric']}: "
                        f"{lo}" + (f"-{hi}" if hi != lo else "")
                        + f" {r['comparator']} {r['threshold']:g} -> {status}")
        # The plan requires every metric ("at least 3 therapy days AND at least
        # 150 minutes"), so one definite failure settles the week however
        # uncertain the other metric is. NOT_MET therefore outranks
        # CANNOT_DETERMINE; reversing them would report a week that provably
        # missed a required threshold as merely undetermined.
        overall = (BELOW if any(c["status"] == BELOW for c in checks) else
                   UNKNOWN if any(c["status"] == UNKNOWN for c in checks) else MET)
        partial = bool(ep_start and ep_end and
                       (w["week_start"] < ep_start or w["week_end"] > ep_end))
        out.append({"week_start": w["week_start"], "week_end": w["week_end"],
                    "checks": checks, "status": overall,
                    "minutes": w["minutes"], "therapy_days": w["therapy_days"],
                    "sum": w["sum"], "partial_week": partial,
                    "episode_window": [ep_start, ep_end],
                    "items": w["items"], "disputed_events": w["disputed_events"]})
    return {"value": {"weeks": out, "week_scheme": scheme,
                      "requirements": [{"metric": r["metric"],
                                        "comparator": r["comparator"],
                                        "threshold": r["threshold"],
                                        "period": r["period"],
                                        "service_types": json.loads(r["service_types"]),
                                        "effective": [r["effective_start"],
                                                      r["effective_end"]],
                                        "source": _req_evidence(st, r)}
                                       for r in reqs]},
            "calculation": calc, "evidence": m["evidence"],
            "caveats": m["caveats"]}


def _days_arithmetic(w: dict) -> str:
    """The therapy-day count written out, e.g. "2026-01-05, 2026-01-06 = 2"."""
    days = sorted({i["date"] for i in w["items"] if i["minutes"][1]})
    if not days:
        return "no therapy day in this week = 0"
    lo, hi = w["therapy_days"]
    return ", ".join(days) + f" = {lo}" + (f"-{hi}" if hi != lo else "")


def _req_evidence(st: Store, r: dict) -> dict:
    c = st.q1("SELECT c.*, d.doc_id, d.filename FROM claims c "
              "JOIN documents d ON d.doc_pk=c.doc_pk WHERE c.claim_id=?",
              (r["claim_id"],))
    if not c:
        return {}
    return {"document": c["doc_id"] or c["filename"], "file": c["filename"],
            "offsets": [c["quote_start"], c["quote_end"]], "quote": c["quote"]}


# ---------------------------------------------------------------------------
# Q: consecutive weeks below requirement (collection-wide)
# ---------------------------------------------------------------------------
def consecutive_below(st: Store, mrn: str | None = None, n: int = 2,
                      start=None, end=None) -> dict:
    """Patients with >= n consecutive weeks below the plan requirement.

    Reported twice: under the minimum-minutes reading of the record and under
    the maximum reading. A patient whose answer differs between the two is a
    patient whose inclusion depends on documentation that is not settled.
    """
    mrns = [mrn] if mrn else st.patients()
    definite, contingent, clear = [], [], []
    calc: list[str] = []
    evidence: list[dict] = []
    for m in mrns:
        comp = compliance(st, m, start, end)
        if not comp["value"]:
            continue
        weeks = comp["value"]["weeks"]
        # pessimistic: UNKNOWN counts as below. optimistic: UNKNOWN counts as met.
        pess = [w["status"] in (BELOW, UNKNOWN) for w in weeks]
        opt = [w["status"] == BELOW for w in weeks]
        rp = _max_run(pess)
        ro = _max_run(opt)
        # Item 9: a collection-wide finding must be traceable to the same
        # passages as a per-patient one. Every week that is below or
        # undetermined carries its sessions and their quotes.
        short_weeks = []
        for w in weeks:
            if w["status"] == MET:
                continue
            ev = [x for i in w["items"] for x in i["evidence"]]
            short_weeks.append({
                "week_start": w["week_start"], "week_end": w["week_end"],
                "status": w["status"], "minutes": w["minutes"],
                "therapy_days": w["therapy_days"], "sum": w["sum"],
                "sessions": [{"date": i["date"], "encounter": i["encounter"],
                              "service": i["service"], "minutes": i["minutes"]}
                             for i in w["items"]],
                "evidence": ev})
            evidence += [{**x, "patient": m, "week": w["week_start"]} for x in ev]
            for req in comp["value"]["requirements"]:
                if req.get("source"):
                    evidence.append({**req["source"], "patient": m,
                                     "week": w["week_start"],
                                     "role": "plan_requirement"})
        entry = {"patient": m, "weeks": [{"week_start": w["week_start"],
                                          "week_end": w["week_end"],
                                          "status": w["status"],
                                          "minutes": w["minutes"],
                                          "therapy_days": w["therapy_days"]}
                                         for w in weeks],
                 "longest_run_pessimistic": rp, "longest_run_optimistic": ro,
                 "runs": _runs(weeks, n),
                 "weeks_below_with_sources": short_weeks,
                 "requirements": comp["value"]["requirements"]}
        calc.append(f"{m}: statuses={[w['status'] for w in weeks]} "
                    f"run(min-reading)={rp} run(max-reading)={ro} threshold={n}")
        if ro >= n:
            definite.append(entry)
        elif rp >= n:
            entry["depends_on"] = [
                {"week": w["week_start"], "disputed_events": w["disputed_events"],
                 "resolvable_by": [c["resolvable_by"] for c in comp["caveats"]
                                   if c.get("event") in w["disputed_events"]]}
                for w in weeks if w["status"] == UNKNOWN]
            contingent.append(entry)
        else:
            clear.append(m)
    return {"value": {"n": n, "included": definite,
                      "depends_on_documentation": contingent,
                      "not_included": clear},
            "calculation": calc, "evidence": evidence, "caveats": []}


def _max_run(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _runs(weeks: list[dict], n: int) -> list[dict]:
    out, cur = [], []
    for w in weeks:
        if w["status"] in (BELOW, UNKNOWN):
            cur.append(w)
        else:
            if len(cur) >= n:
                out.append({"from": cur[0]["week_start"], "to": cur[-1]["week_end"],
                            "statuses": [x["status"] for x in cur]})
            cur = []
    if len(cur) >= n:
        out.append({"from": cur[0]["week_start"], "to": cur[-1]["week_end"],
                    "statuses": [x["status"] for x in cur]})
    return out


# ---------------------------------------------------------------------------
# Q: reconstruct one or more dates
# ---------------------------------------------------------------------------
def day_detail(st: Store, mrn: str, dates: list[str]) -> dict:
    include = included_types(st, mrn)
    out, calc = [], []
    for d in dates:
        if not d:
            continue
        evs = _events(st, mrn, d, d)
        contacts, lo, hi = [], 0, 0
        for e in evs:
            b = _band(e, include)
            c = _counts(e, include)
            contacts.append({
                "encounter": e["encounter_ref"] or e["event_id"],
                "service": e["service_type"], "occurred": e["occurred"],
                "patient_present": e["patient_present"],
                "counts_as_therapy": bool(e["counts_as_therapy"]),
                "minutes": list(b), "resolved": e["resolved"],
                "decisions": [{"field": x["field"], "rule": x["rule_id"],
                               "chosen": x["chosen"], "why": x["rationale"]}
                              for x in st.event_decisions(e["event_id"])],
                "evidence": all_evidence(st, e["event_id"]),
            })
            lo += b[0]
            hi += b[1]
        therapy = [c for c in contacts if c["counts_as_therapy"]]
        calc.append(f"{d}: " + " + ".join(
            f"{c['encounter']} {c['minutes'][0]}" for c in therapy)
            + f" = {lo}" + (f"-{hi}" if hi != lo else "") + " min")
        out.append({"date": d, "therapy_contacts": len(therapy),
                    "all_contacts": len(contacts),
                    "patient_therapy_minutes": [lo, hi], "contacts": contacts,
                    "wall_clock_check": _wall_clock(contacts)})
    return {"value": out, "calculation": calc,
            "evidence": [x for d in out for c in d["contacts"] for x in c["evidence"]],
            "caveats": []}


def _wall_clock(contacts: list[dict]) -> dict:
    """Do the resolved presence intervals of one day overlap each other?

    A patient cannot be in two therapy rooms at once, so an overlap means the
    reconciliation is wrong somewhere. This is the check that caught the
    latest-document policy (see docs/experiment-conflict-policy.md).
    """
    spans = []
    for c in contacts:
        if not c["counts_as_therapy"]:
            continue
        for a, b in c["resolved"].get("presence_intervals") or []:
            spans.append((iv.mk(a, b), c["encounter"]))
    bad = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            ov = iv.overlaps([spans[i][0]], [spans[j][0]])
            if ov > 0:
                bad.append({"a": spans[i][1], "b": spans[j][1], "overlap_minutes": ov})
    return {"overlapping_contacts": bad, "ok": not bad}


# ---------------------------------------------------------------------------
# Q: before/after a plan change
# ---------------------------------------------------------------------------
def plan_change_comparison(st: Store, mrn: str, change_date: str | None = None) -> dict:
    reqs = [dict(r) for r in st.requirements(mrn)]
    starts = sorted({r["effective_start"] for r in reqs if r["effective_start"]})
    changes = starts[1:]
    if change_date:
        changes = [change_date]
    if not changes:
        return {"value": {"plan_changes": [], "note":
                "the record contains one plan period only; no mid-episode plan "
                "change was documented"},
                "calculation": [], "evidence": [
                    _req_evidence(st, r) for r in reqs], "caveats": []}
    include = included_types(st, mrn)
    out, calc = [], []
    ep_start, ep_end = episode_window(st, mrn)
    for cd in changes:
        before = _slice_stats(st, mrn, ep_start, _day_before(cd), include)
        after = _slice_stats(st, mrn, cd, ep_end, include)
        # The week containing the change has both plans in force for part of
        # it, so it is reported rather than silently assigned to one side.
        split_week = iv.week_key(cd)
        split = {"week_start": split_week,
                 "week_end": iv.week_span(split_week)[1],
                 "change_falls_on": cd,
                 "note": "both plans are in force during this week; it appears "
                         "in both slices below and should not be read as "
                         "belonging to either period alone"
                 } if split_week != cd else None
        out.append({
            "change_date": cd, "before": before, "after": after,
            "split_week": split,
            "delta_total": {k: [after[k][i] - before[k][i] for i in (0, 1)]
                            for k in ("minutes", "sessions")},
            "delta_per_week": {
                k: [round(after[f"{k}_per_week"][i]
                          - before[f"{k}_per_week"][i], 2) for i in (0, 1)]
                for k in ("minutes", "sessions")},
            "mix_before": before["by_type_per_week"],
            "mix_after": after["by_type_per_week"],
            "requirements_before": [r for r in reqs
                                    if r["effective_start"] < cd],
            "requirements_after": [r for r in reqs
                                   if r["effective_start"] >= cd]})
        calc.append(
            f"plan change {cd}: before {before['weeks']} week(s) at "
            f"{before['minutes_per_week']} min/week, after {after['weeks']} "
            f"week(s) at {after['minutes_per_week']} min/week "
            f"(raw totals {before['minutes']} -> {after['minutes']}, which are "
            f"not comparable when the periods differ in length)")
    return {"value": {"plan_changes": out}, "calculation": calc,
            "evidence": [_req_evidence(st, r) for r in reqs], "caveats": []}


def _slice_stats(st: Store, mrn: str, start, end, include,
                 scheme: str = "week_mon_sun") -> dict:
    """Totals for a slice, and the per-week rates that make slices comparable.

    Raw totals mislead whenever the before and after periods are different
    lengths -- which they almost always are -- so the per-week figures are what
    the report should lead with.
    """
    evs = _events(st, mrn, start, end)
    lo = hi = s_lo = s_hi = 0
    by_type: dict[str, int] = defaultdict(int)
    days = set()
    for e in evs:
        b = _band(e, include)
        c = _counts(e, include)
        lo += b[0]
        hi += b[1]
        s_lo += c[0]
        s_hi += c[1]
        if c[1]:
            by_type[e["service_type"]] += 1
            days.add(e["service_date"])
    weeks = _week_keys(st, mrn, evs, start, end, scheme)
    n = len(weeks) or 1
    return {"window": [start, end], "minutes": [lo, hi],
            "sessions": [s_lo, s_hi], "therapy_days": len(days),
            "weeks": len(weeks), "week_starts": weeks,
            "minutes_per_week": [round(lo / n, 1), round(hi / n, 1)],
            "sessions_per_week": [round(s_lo / n, 2), round(s_hi / n, 2)],
            "days_per_week": round(len(days) / n, 2),
            "by_type": dict(sorted(by_type.items())),
            "by_type_per_week": {k: round(v / n, 2)
                                 for k, v in sorted(by_type.items())}}


def _day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Q: documented symptom course
# ---------------------------------------------------------------------------
def progress(st: Store, mrn: str, start=None, end=None) -> dict:
    ms = [dict(m) for m in st.measures(mrn)]
    for m in ms:
        m["claim_ids"] = json.loads(m["claim_ids"])
        m["sources"] = []
        for cid in m["claim_ids"]:
            c = st.q1("SELECT c.*, d.doc_id, d.filename FROM claims c "
                      "JOIN documents d ON d.doc_pk=c.doc_pk WHERE claim_id=?", (cid,))
            if c:
                m["sources"].append({"document": c["doc_id"] or c["filename"],
                                     "file": c["filename"],
                                     "offsets": [c["quote_start"], c["quote_end"]],
                                     "quote": c["quote"]})
        m["items"] = json.loads(m["items"]) if m["items"] else None
        m["conflicting_totals"] = (json.loads(m["total_conflicts"])
                                   if m.get("total_conflicts") else None)
        m["disputed"] = bool(m.get("disputed"))
    obs = []
    for o in st.observations(mrn, start, end):
        obs.append({"date": o["obs_date"], "domain": o["domain"],
                    "polarity": o["polarity"], "reporter": o["reporter"],
                    "service_type": o["service_type"],
                    "document": o["doc_id"] or o["filename"], "file": o["filename"],
                    "offsets": [o["quote_start"], o["quote_end"]], "quote": o["quote"]})
    by_domain: dict[str, list] = defaultdict(list)
    for o in obs:
        by_domain[o["domain"]].append(o)
    supported, unsupported = _progress_claims(ms, obs)
    added = added_contacts(st, mrn, start, end)
    return {"value": {"distinct_measures": ms, "added_contacts": added,
                      "measure_count": len(ms),
                      "observations_by_domain": {k: v for k, v in sorted(by_domain.items())},
                      "supported": supported, "not_supported": unsupported},
            "calculation": [f"{m['instrument']} {m['completed_at']} = {m['total']:g}"
                            f" (form {m['form_ref'] or 'not stated'}, "
                            f"{len(m['claim_ids'])} source record(s))" for m in ms],
            "evidence": [s for m in ms for s in m["sources"]] + obs,
            "caveats": []}


def added_contacts(st: Store, mrn: str, start=None, end=None) -> list[dict]:
    """Contacts the record itself describes as added or unscheduled, with the
    sentence that gives the reason."""
    out = []
    for e in _events(st, mrn, start, end):
        for ev in st.event_evidence(e["event_id"]):
            f = json.loads(ev["fields"] or "{}")
            r = f.get("reason_added")
            if not r:
                continue
            out.append({"event": e["encounter_ref"] or e["event_id"],
                        "date": e["service_date"], "service": e["service_type"],
                        "minutes": [e["min_minutes"], e["max_minutes"]],
                        "counts_as_therapy": bool(e["counts_as_therapy"]),
                        "reason": r["quote"],
                        "evidence": [{"document": ev["doc_id"] or ev["filename"],
                                      "file": ev["filename"],
                                      "offsets": r["offsets"], "quote": r["quote"],
                                      "verified": True}]})
    return out


def _progress_claims(ms: list[dict], obs: list[dict]) -> tuple[list, list]:
    """What the record supports about progress, and what it does not."""
    supported, unsupported = [], []
    # Per instrument, state only the direction the scores actually show.
    by_inst: dict[str, list[dict]] = defaultdict(list)
    for m in ms:
        if m["total"] is not None:
            by_inst[m["instrument"]].append(m)
    for inst, series in sorted(by_inst.items()):
        series = sorted(series, key=lambda x: x["completed_at"])
        if len(series) == 1:
            one = series[0]
            supported.append({
                "statement": f"{inst} total {one['total']:g} recorded on "
                             f"{one['completed_at']}",
                "basis": "one patient-completed questionnaire; no second "
                         "administration to compare against",
                "sources": one["sources"]})
            continue
        a, b = series[0], series[-1]
        if b["total"] < a["total"]:
            move = f"fell from {a['total']:g} to {b['total']:g}"
        elif b["total"] > a["total"]:
            move = f"rose from {a['total']:g} to {b['total']:g}"
        else:
            move = f"was unchanged at {a['total']:g}"
        supported.append({
            "statement": f"{inst} total {move} between {a['completed_at']} and "
                         f"{b['completed_at']}",
            "basis": f"{len(series)} distinct patient-completed "
                     f"questionnaire(s): "
                     + ", ".join(f"{x['completed_at']}={x['total']:g}"
                                 for x in series),
            "sources": a["sources"] + b["sources"]})

    # One statement per domain actually observed, counted rather than narrated.
    # The previous version asserted a fixed sentence naming mood, activity and
    # work-approach behaviour whenever any improvement existed, which would
    # overstate for a patient whose record says something else.
    LABEL = {"mood": "mood", "sleep": "sleep", "anxiety": "anxiety",
             "avoidance_work": "avoidance of work communication",
             "activity": "daily activity and task initiation",
             "social_support": "support at home", "safety": "safety",
             "participation": "participation in treatment"}
    by_dom: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        by_dom[o["domain"]].append(o)
    for dom, items in sorted(by_dom.items(), key=lambda kv: -len(kv[1])):
        tally = defaultdict(int)
        for o in items:
            tally[o["polarity"]] += 1
        parts = [f"{n} {pol}" for pol, n in
                 sorted(tally.items(), key=lambda kv: -kv[1])]
        reporters = sorted({o["reporter"] for o in items})
        supported.append({
            "statement": f"{LABEL.get(dom, dom)}: " + ", ".join(parts)
                         + " observation(s) in the record",
            "basis": f"dated spans from {', '.join(reporters)} record(s), "
                     f"{items[0]['date']} to {items[-1]['date']}",
            "sources": items[:6]})
    for m in ms:
        if m.get("disputed"):
            unsupported.append({
                "statement": f"a single score for the {m['instrument']} "
                             f"administration completed {m['completed_at']}",
                "why": "two records of that one administration state different "
                       "scores ("
                       + ", ".join(f"{c['total']:g}"
                                   for c in (m.get("conflicting_totals") or []))
                       + ") and nothing in the record says which is right "
                         "(RULE-MEAS-02)"})
    unsupported.append({
        "statement": "a severity category or diagnostic threshold for any questionnaire "
                     "score",
        "why": "no document in the record states a severity band for these scores; "
               "the abstraction carries only the totals the documents state"})
    unsupported.append({
        "statement": "that improvement was caused by any particular service",
        "why": "the record documents co-occurring therapy, medication management and "
               "family support; no document attributes change to one of them"})
    if any(m["form_ref"] is None for m in ms):
        unsupported.append({
            "statement": "that every questionnaire in the chart is a separate "
                         "administration",
            "why": "at least one measure carries no form identifier, so re-imports "
                   "can only be collapsed on instrument and completion date "
                   "(RULE-MEAS-01)"})
    return supported, unsupported


# ---------------------------------------------------------------------------
# cross-cutting: audit helpers
# ---------------------------------------------------------------------------
def trace(st: Store, event_id: str) -> dict:
    e = st.q1("SELECT * FROM events WHERE event_id=?", (event_id,))
    if not e:
        return {"value": None, "calculation": [], "evidence": [],
                "caveats": [{"reason": f"no event {event_id}"}]}
    return {"value": {"event": dict(e), "resolved": json.loads(e["resolved"]),
                      "decisions": [dict(d) for d in st.event_decisions(event_id)]},
            "calculation": [], "evidence": all_evidence(st, event_id), "caveats": []}


def duplicates(st: Store) -> dict:
    rows = st.q("SELECT a.path, a.kind, d.filename, d.doc_id FROM document_aliases a "
                "JOIN documents d ON d.doc_pk=a.doc_pk ORDER BY a.path")
    return {"value": [dict(r) for r in rows], "calculation": [],
            "evidence": [], "caveats": []}


def integrity(st: Store, mrn: str | None = None) -> dict:
    """Checks that must hold if the abstraction is sound."""
    issues = []
    bad = st.q1("SELECT COUNT(*) n FROM claims WHERE quote_verified=0")["n"]
    if bad:
        issues.append({"check": "quote_offsets", "failures": bad,
                       "detail": "claims whose quote does not match stored offsets"})
    for m in ([mrn] if mrn else st.patients()):
        for d in day_detail(st, m, sorted({e["service_date"]
                                           for e in _events(st, m)
                                           if e["service_date"]}))["value"]:
            if not d["wall_clock_check"]["ok"]:
                issues.append({"check": "wall_clock", "patient": m, "date": d["date"],
                               "detail": d["wall_clock_check"]["overlapping_contacts"]})
    return {"value": {"ok": not issues, "issues": issues}, "calculation": [],
            "evidence": [], "caveats": []}
