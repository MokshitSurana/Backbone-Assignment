"""Render query results as reviewable Markdown.

The renderer only formats. It never computes a total, and it never states a
figure that is not present in the query result it was handed.
"""
from __future__ import annotations

import json


def _rng(v) -> str:
    if isinstance(v, (list, tuple)):
        a, b = v[0], v[-1]
        return f"{a:g}" if a == b else f"{a:g}-{b:g}"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _src(e: dict) -> str:
    q = (e.get("quote") or "").replace("\n", " ").strip()
    if len(q) > 180:
        q = q[:177] + "..."
    off = e.get("offsets") or [0, 0]
    flag = "" if e.get("verified", True) else "  [QUOTE UNVERIFIED]"
    return f"{e.get('document', '?')} [{off[0]}:{off[1]}] \"{q}\"{flag}"


def evidence_block(evs: list[dict], limit: int = 6, indent: str = "  ") -> str:
    seen, out = set(), []
    for e in evs:
        k = (e.get("document"), tuple(e.get("offsets") or []))
        if k in seen:
            continue
        seen.add(k)
        out.append(f"{indent}- {_src(e)}")
        if len(out) >= limit:
            break
    return "\n".join(out)


# ---------------------------------------------------------------------------
def session_counts(r: dict) -> str:
    v = r["value"]
    L = [f"**{_rng(v['sessions'])} therapy sessions** on "
         f"**{_rng(v['distinct_days'])} distinct days**"
         f" ({r['window'][0]} to {r['window'][1]}).", ""]
    L.append("| service type | sessions |")
    L.append("|---|---:|")
    for k, n in v["by_service_type"].items():
        L.append(f"| {k.replace('_', ' ')} | {_rng(n)} |")
    L.append(f"| **total** | **{_rng(v['sessions'])}** |")
    L += ["", f"Service types the plan counts: {', '.join(r['counted_service_types'])}.",
          "", "**Per-session ledger**", "",
          "| date | encounter | service | minutes | certain |", "|---|---|---|---:|---|"]
    for row in r["rows"]:
        L.append(f"| {row['date']} | {row['encounter'] or row['event_id']} | "
                 f"{row['service'].replace('_', ' ')} | {_rng(row['minutes'])} | "
                 f"{'yes' if row['certain'] else 'no'} |")
    excl = [c for c in r["caveats"] if c["kind"] in
            ("ineligible_service", "patient_not_present", "not_counted")]
    if excl:
        L += ["", "**Records that could produce a duplicate or ineligible count**", ""]
        for c in excl:
            L.append(f"- `{c.get('event')}` {c.get('date')} -- {c.get('reason')}")
            if c.get("evidence"):
                L.append(evidence_block(c["evidence"], 2, "    "))
    rej = [c for c in r["caveats"] if c["kind"] in
           ("rejected_presence", "rejected_duration", "duplicate", "unmatched")]
    if rej:
        L += ["", "**Claims rejected or left unmatched during reconciliation**", ""]
        for c in rej[:14]:
            L.append(f"- [{c.get('rule')}] {c['reason']}")
    return "\n".join(L)


def minutes(r: dict) -> str:
    v = r["value"]
    L = [f"**Total patient-present therapy: {_rng(v['total_minutes'])} minutes "
         f"({_rng(v['total_hours'])} hours).**",
         f"Weeks are {r['week_scheme'].replace('week_', '').replace('_', '–')}.", "",
         "| week | therapy days | minutes | hours | calculation |",
         "|---|---:|---:|---:|---|"]
    for w in v["weeks"]:
        L.append(f"| {w['week_start']} -> {w['week_end']} | {_rng(w['therapy_days'])} "
                 f"| {_rng(w['minutes'])} | {_rng(w['hours'])} | {w['sum']} |")
    L += ["", "**Session detail with sources**", ""]
    for w in v["weeks"]:
        L.append(f"*week of {w['week_start']}*")
        for i in w["items"]:
            brk = f", breaks {i['breaks']}" if i.get("breaks") else ""
            L.append(f"- {i['date']} `{i['encounter']}` {i['service'].replace('_', ' ')}: "
                     f"{_rng(i['minutes'])} min{brk}")
            L.append(evidence_block(i["evidence"], 3, "    "))
        L.append("")
    if r["caveats"]:
        L += ["**Not settled by the available documents**", ""]
        for c in r["caveats"]:
            L.append(f"- `{c['event']}` {c['date']}: bounds {_rng(c['bounds'])} min "
                     f"[{', '.join(c['rules'])}]")
            L.append(f"    - {c['reason']}")
            L.append(f"    - would be settled by: {c['resolvable_by']}")
            L.append(evidence_block(c["evidence"], 4, "    "))
    return "\n".join(L)


def compliance(r: dict) -> str:
    if not r["value"]:
        return "No treatment-plan requirement is present in the record."
    v = r["value"]
    L = ["**Goal as documented in the treatment plan**", ""]
    for req in v["requirements"]:
        L.append(f"- {req['metric'].replace('_', ' ')} {req['comparator']} "
                 f"{req['threshold']:g} per {req['period'].replace('week_', '')} "
                 f"week, effective {req['effective'][0]} to "
                 f"{req['effective'][1] or 'open'}")
        L.append(f"    - counts: {', '.join(req['service_types'])}")
        if req.get("source"):
            L.append(f"    - {_src(req['source'])}")
    # One column per metric the plan actually states, so a plan with only a
    # days goal, only an hours goal, or a metric this code has never seen all
    # render without a missing-key crash.
    metrics: list[str] = []
    for req in v["requirements"]:
        if req["metric"] not in metrics:
            metrics.append(req["metric"])
    for w in v["weeks"]:
        for c in w["checks"]:
            if c["metric"] not in metrics:
                metrics.append(c["metric"])
    L += ["", "| week | " + " | ".join(f"{m.replace('_', ' ')} (goal)"
                                       for m in metrics) + " | result |",
          "|---|" + "---|" * (len(metrics) + 1)]
    for w in v["weeks"]:
        cells = {c["metric"]: c for c in w["checks"]}
        row = [f"{w['week_start']} -> {w['week_end']}"
               f"{' *(partial)*' if w['partial_week'] else ''}"]
        for metric in metrics:
            c = cells.get(metric)
            if not c:
                row.append("not in force")
                continue
            row.append(f"{_rng(c['actual'])} "
                       f"({c['comparator']}{c['threshold']:g}) {c['status']}")
        row.append(f"**{w['status']}**")
        L.append("| " + " | ".join(row) + " |")
    L += ["", "**Arithmetic**", ""] + [f"- {c}" for c in r["calculation"]]
    if r["caveats"]:
        L += ["", "**Why a week can be undetermined**", ""]
        for c in r["caveats"]:
            L.append(f"- `{c['event']}` {c['date']}: {_rng(c['bounds'])} min -- "
                     f"{c['reason']}")
            L.append(f"    - would be settled by: {c['resolvable_by']}")
    return "\n".join(L)


def consecutive_below(r: dict) -> str:
    v = r["value"]
    L = [f"**Patients with {v['n']} or more consecutive weeks below the plan "
         f"requirement**", ""]
    if v["included"]:
        L.append("*Included on the record as it stands:*")
        for p in v["included"]:
            runs = "; ".join(f"{x['from']}->{x['to']}" for x in p["runs"])
            L.append(f"- `{p['patient']}` runs: {runs or 'n/a'} "
                     f"(longest run {p['longest_run_optimistic']} weeks even under the "
                     f"most favourable reading)")
    else:
        L.append("*Included on the record as it stands:* none")
    L.append("")
    if v["depends_on_documentation"]:
        L.append("*Inclusion depends on documentation that is not settled:*")
        for p in v["depends_on_documentation"]:
            L.append(f"- `{p['patient']}`: {p['longest_run_optimistic']} consecutive "
                     f"weeks below if every undetermined week is read favourably, "
                     f"{p['longest_run_pessimistic']} if read unfavourably")
            for d in p.get("depends_on", []):
                L.append(f"    - week {d['week']} turns on {d['disputed_events']}; "
                         f"{'; '.join(x for x in d['resolvable_by'])}")
    else:
        L.append("*Inclusion depends on unresolved documentation:* none")
    if v["not_included"]:
        L += ["", f"*Not included:* {', '.join(v['not_included'])}"]
    L += ["", "**Below-goal weeks, with the sessions and passages behind them**", ""]
    for p_ in v["included"] + v["depends_on_documentation"]:
        for w in p_.get("weeks_below_with_sources", []):
            L.append(f"- `{p_['patient']}` week {w['week_start']} -> "
                     f"{w['week_end']}: **{w['status']}**, "
                     f"{_rng(w['minutes'])} min across "
                     f"{_rng(w['therapy_days'])} day(s) ({w['sum']})")
            for sess in w["sessions"]:
                L.append(f"    - {sess['date']} `{sess['encounter']}` "
                         f"{sess['service'].replace('_', ' ')}: "
                         f"{_rng(sess['minutes'])} min")
            L.append(evidence_block(w["evidence"], 4, "    "))
    L += ["", "**Per-patient week status**", ""]
    for p in v["included"] + v["depends_on_documentation"]:
        L.append(f"- `{p['patient']}`: " + ", ".join(
            f"{w['week_start']}={w['status']}({_rng(w['minutes'])}min,"
            f"{_rng(w['therapy_days'])}d)" for w in p["weeks"]))
    return "\n".join(L)


def day_detail(r: dict) -> str:
    L = []
    for d in r["value"]:
        L += [f"**{d['date']}** -- {d['therapy_contacts']} therapy contact(s), "
              f"{_rng(d['patient_therapy_minutes'])} patient therapy minutes "
              f"({d['all_contacts']} contact record(s) of all kinds).", ""]
        for c in d["contacts"]:
            tag = ("counted" if c["counts_as_therapy"] else "not counted")
            L.append(f"- `{c['encounter']}` {c['service'].replace('_', ' ')} "
                     f"-- occurred={c['occurred']}, patient present="
                     f"{c['patient_present']}, {_rng(c['minutes'])} min ({tag})")
            pres = c["resolved"].get("presence_intervals")
            if pres:
                L.append(f"    - resolved patient-present interval(s): {pres}"
                         + (f", minus break(s) {c['resolved'].get('breaks')}"
                            if c["resolved"].get("breaks") else ""))
            for dec in c["decisions"]:
                if dec["field"] in ("occurred", "minutes", "breaks",
                                    "rejected_presence", "rejected_duration"):
                    L.append(f"    - [{dec['rule']}] {dec['field']} = {dec['chosen']}"
                             f" -- {dec['why']}")
            L.append(evidence_block(c["evidence"], 6, "    "))
        wc = d["wall_clock_check"]
        L += ["", f"Wall-clock check: "
              + ("no two counted contacts overlap in time."
                 if wc["ok"] else f"OVERLAP DETECTED {wc['overlapping_contacts']}"), ""]
    L += ["**Arithmetic**", ""] + [f"- {c}" for c in r["calculation"]]
    return "\n".join(L)


def plan_change_comparison(r: dict) -> str:
    v = r["value"]
    if not v["plan_changes"]:
        return "\n".join([
            f"**No mid-episode plan change is documented.** "
            f"{v.get('note', '')}", "",
            "The comparison function is available for any date, so a plan "
            "amendment in a later drop is handled by the same code path; "
            "with a single plan period in force there is nothing to "
            "compare across."])
    L = []
    for c in v["plan_changes"]:
        b, a = c["before"], c["after"]
        L += [f"**Plan change effective {c[chr(39)+chr(39)]}**" if False else
              f"**Plan change effective {c['change_date']}**", "",
              f"Before: {b['weeks']} week(s), {b['window'][0]} to "
              f"{b['window'][1]}.  After: {a['weeks']} week(s), "
              f"{a['window'][0]} to {a['window'][1]}.", "",
              "Per-week rates are the comparable figures. Raw totals are "
              "shown too, but they mislead whenever the two periods differ "
              "in length -- which they usually do.", "",
              "| | before | after | change |", "|---|---:|---:|---:|",
              f"| **minutes per week** | {_rng(b['minutes_per_week'])} | "
              f"{_rng(a['minutes_per_week'])} | "
              f"{_rng(c['delta_per_week']['minutes'])} |",
              f"| **sessions per week** | {_rng(b['sessions_per_week'])} | "
              f"{_rng(a['sessions_per_week'])} | "
              f"{_rng(c['delta_per_week']['sessions'])} |",
              f"| therapy days per week | {b['days_per_week']} | "
              f"{a['days_per_week']} | "
              f"{round(a['days_per_week'] - b['days_per_week'], 2)} |",
              f"| minutes (raw total) | {_rng(b['minutes'])} | "
              f"{_rng(a['minutes'])} | {_rng(c['delta_total']['minutes'])} |",
              f"| sessions (raw total) | {_rng(b['sessions'])} | "
              f"{_rng(a['sessions'])} | "
              f"{_rng(c['delta_total']['sessions'])} |", "",
              f"Service mix per week before: {c['mix_before']}",
              f"Service mix per week after: {c['mix_after']}", ""]
        if c.get("split_week"):
            sw = c["split_week"]
            L += [f"*Week {sw['week_start']} to {sw['week_end']} contains "
                  f"the change date {sw['change_falls_on']}: "
                  f"{sw['note']}.*", ""]
        for label, reqs in (("in force before", c["requirements_before"]),
                            ("in force after", c["requirements_after"])):
            if reqs:
                L.append(f"Requirements {label}: " + "; ".join(
                    f"{x['metric']} {x['comparator']} {x['threshold']:g}"
                    for x in reqs))
        L.append("")
    return "\n".join(L)


def progress(r: dict) -> str:
    v = r["value"]
    L = [f"**{v['measure_count']} distinct symptom assessment(s) in the record**", "",
         "| instrument | completed | total | items | form id | source records |",
         "|---|---|---:|---|---|---|"]
    for m in v["distinct_measures"]:
        L.append(f"| {m['instrument']} | {m['completed_at']} | "
                 f"{m['total']:g} | {m['items'] or ''} | "
                 f"{m['form_ref'] or '*not stated*'} | {len(m['claim_ids'])} |")
    L += ["", "**Measure provenance** (a re-import collapses onto the original "
          "administration, RULE-MEAS-01)", ""]
    for m in v["distinct_measures"]:
        L.append(f"- {m['instrument']} {m['completed_at']} = {m['total']:g}")
        L.append(evidence_block(m["sources"], 4, "    "))
    if v.get("added_contacts"):
        L += ["", "**Contacts the record describes as added or unscheduled**", ""]
        for a in v["added_contacts"]:
            L.append(f"- {a['date']} `{a['event']}` "
                     f"{a['service'].replace('_', ' ')}, {_rng(a['minutes'])} min"
                     f"{' (counted as therapy)' if a['counts_as_therapy'] else ''}")
            L.append(f"    - stated reason: \"{a['reason']}\"")
            L.append(evidence_block(a["evidence"], 2, "    "))
    L += ["", "**What the record supports**", ""]
    for s in v["supported"]:
        L.append(f"- {s['statement']} ({s['basis']})")
        L.append(evidence_block(s["sources"], 4, "    "))
    L += ["", "**What the record does not support**", ""]
    for s in v["not_supported"]:
        L.append(f"- {s['statement']} -- {s['why']}")
    L += ["", "**Dated observations by domain**", ""]
    for dom, items in v["observations_by_domain"].items():
        L.append(f"*{dom}*")
        for o in items:
            L.append(f"- {o['date']} [{o['polarity']}, {o['reporter']}] {_src(o)}")
        L.append("")
    return "\n".join(L)


def trace(r: dict) -> str:
    if not r["value"]:
        return "No such event."
    v = r["value"]
    e = v["event"]
    L = [f"**{e['event_id']}** -- {e['service_date']} {e['service_type']}",
         f"- occurred: {e['occurred']}; patient present: {e['patient_present']}",
         f"- minutes: {_rng([e['min_minutes'], e['max_minutes']])}"
         f"{'  (disputed)' if e['disputed'] else ''}",
         f"- counts as therapy: {'yes' if e['counts_as_therapy'] else 'no'}"
         + (f" -- {e['exclusion_reason']}" if e["exclusion_reason"] else ""),
         f"- resolved: {json.dumps(v['resolved'], sort_keys=True)}", "",
         "**Decision log**", ""]
    for d in v["decisions"]:
        L.append(f"- [{d['rule_id']}] {d['field']} = {d['chosen']} -- {d['rationale']}")
    L += ["", "**Every claim attached to this event**", ""]
    for ev in r["evidence"]:
        L.append(f"- ({ev['role']}, {ev['authority']}) {_src(ev)}")
    return "\n".join(L)


def duplicates(r: dict) -> str:
    if not r["value"]:
        return "No duplicate files detected in this corpus."
    L = ["| file | resolved to | kind |", "|---|---|---|"]
    for d in r["value"]:
        L.append(f"| {d['path']} | {d['doc_id'] or d['filename']} | {d['kind']} |")
    return "\n".join(L)


def integrity(r: dict) -> str:
    v = r["value"]
    if v["ok"]:
        return "All integrity checks pass (quote offsets verified; no counted " \
               "contacts overlap in wall-clock time)."
    return "Issues:\n" + "\n".join(f"- {i}" for i in v["issues"])


RENDERERS = {
    "session_counts": session_counts, "minutes": minutes, "compliance": compliance,
    "consecutive_below": consecutive_below, "day_detail": day_detail,
    "plan_change_comparison": plan_change_comparison, "progress": progress,
    "trace": trace, "duplicates": duplicates, "integrity": integrity,
}


def per_patient(fn_name: str, result: dict) -> str:
    """One section per patient, each rendered by that function's own renderer."""
    inner = RENDERERS.get(fn_name, lambda r: json.dumps(r, indent=1))
    pats = result["value"]["patients"]
    L = [f"**{len(pats)} patients in scope:** " + ", ".join(f"`{p}`" for p in pats),
         ""]
    for mrn in pats:
        L += [f"### `{mrn}`", "", inner(result["results"][mrn]), ""]
    return "\n".join(L)


def render(fn_name: str, result: dict) -> str:
    if isinstance(result.get("value"), dict) and "per_patient" in result["value"]:
        return per_patient(fn_name, result)
    return RENDERERS.get(fn_name, lambda r: json.dumps(r, indent=1))(result)
