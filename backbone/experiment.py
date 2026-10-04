"""The design decision I tested: how to resolve conflicting records.

Two policies, same corpus, same code path:

  explicit_correction  only an entry that names the record and field it replaces
                       supersedes that field; a later document that does not
                       reference the earlier one does not
  latest_document      the most recently received document wins

The harness is corpus-agnostic: it does not know which encounter the policies
will disagree about. It builds the abstraction twice, finds the events whose
resolved fields differ, and reports those with both decision logs side by side.
A narrative reading of what it found on the supplied corpus is in DESIGN.md.
"""
from __future__ import annotations

import json
import pathlib

from . import queries as Q
from . import reconcile
from .ingest import ingest_dir
from .store import Store


def _run_policy(src: pathlib.Path, policy: str) -> dict:
    db = pathlib.Path(f"out/exp-{policy}.db")
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db) + s)
        if p.exists():
            p.unlink()
    st = Store(db)
    ingest_dir(st, src)
    reconcile.reconcile_all(st, policy=policy)
    out: dict = {"policy": policy, "patients": {}}
    for mrn in st.patients():
        start, end = Q.episode_window(st, mrn)
        comp = Q.compliance(st, mrn, start, end)
        integ = Q.integrity(st, mrn)
        events = {}
        for e in st.events(mrn):
            events[e["encounter_ref"] or e["event_id"]] = {
                "date": e["service_date"], "service_type": e["service_type"],
                "occurred": e["occurred"], "patient_present": e["patient_present"],
                "minutes": [e["min_minutes"], e["max_minutes"]],
                "therapy": e["counts_as_therapy"], "disputed": e["disputed"],
                "decisions": [
                    {"rule": d["rule_id"], "field": d["field"],
                     "chosen": d["chosen"], "why": d["rationale"]}
                    for d in st.event_decisions(e["event_id"])
                    if d["field"] in ("minutes", "occurred", "rejected_duration",
                                      "rejected_presence")],
            }
        out["patients"][mrn] = {
            "sessions": Q.session_counts(st, mrn, start, end)["value"]["sessions"],
            "total_minutes": Q.minutes(st, mrn, start, end)["value"]["total_minutes"],
            "weeks": [{"week": w["week_start"], "minutes": w["minutes"],
                       "days": w["therapy_days"], "status": w["status"]}
                      for w in (comp["value"]["weeks"] if comp["value"] else [])],
            "integrity_ok": integ["value"]["ok"],
            "integrity_issues": integ["value"]["issues"],
            "events": events,
            "days": sorted({e["date"] for e in events.values() if e["date"]}),
        }
    st.close()
    return out


def _diverging_events(a: dict, b: dict) -> list[dict]:
    """Events whose resolved fields differ between the two policies."""
    out = []
    for mrn, pa in a["patients"].items():
        pb = b["patients"].get(mrn, {"events": {}})
        for key in sorted(set(pa["events"]) | set(pb["events"])):
            ea = pa["events"].get(key)
            eb = pb["events"].get(key)
            if not ea or not eb:
                out.append({"patient": mrn, "event": key, "field": "exists",
                            "a": bool(ea), "b": bool(eb), "a_log": [], "b_log": []})
                continue
            for f in ("occurred", "patient_present", "minutes", "therapy",
                      "disputed"):
                if ea[f] != eb[f]:
                    out.append({"patient": mrn, "event": key, "date": ea["date"],
                                "field": f, "a": ea[f], "b": eb[f],
                                "a_log": ea["decisions"], "b_log": eb["decisions"]})
    return out


def _diverging_weeks(a: dict, b: dict) -> list[dict]:
    out = []
    for mrn, pa in a["patients"].items():
        pb = b["patients"].get(mrn, {"weeks": []})
        wb = {w["week"]: w for w in pb["weeks"]}
        for w in pa["weeks"]:
            o = wb.get(w["week"])
            if o and (o["status"] != w["status"] or o["minutes"] != w["minutes"]):
                out.append({"patient": mrn, "week": w["week"],
                            "a_status": w["status"], "b_status": o["status"],
                            "a_minutes": w["minutes"], "b_minutes": o["minutes"]})
    return out


def run(args) -> dict:
    src = pathlib.Path(args.path)
    a = _run_policy(src, "explicit_correction")
    b = _run_policy(src, "latest_document")
    ev = _diverging_events(a, b)
    wk = _diverging_weeks(a, b)
    md = _report(a, b, ev, wk)
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(md, encoding="utf-8")
    pathlib.Path(args.out).with_suffix(".json").write_text(
        json.dumps({"explicit_correction": a, "latest_document": b,
                    "diverging_events": ev, "diverging_weeks": wk}, indent=1),
        encoding="utf-8")
    print(md)
    print(f"\nwrote {args.out}")
    return {"explicit_correction": a, "latest_document": b}


def _rng(v) -> str:
    if isinstance(v, (list, tuple)):
        return f"{v[0]}" if v[0] == v[-1] else f"{v[0]}-{v[-1]}"
    return str(v)


def _report(a: dict, b: dict, ev: list[dict], wk: list[dict]) -> str:
    L = ["# Tested design decision: conflict-resolution policy", "",
         "Same documents, same extractor, same query code. Only the policy in",
         "`reconcile.py` changes. Reproduce with `python -m backbone experiment`.",
         "",
         "- **explicit-correction (shipped)**: only an entry naming the record "
         "and field it replaces supersedes that field.",
         "- **latest-document**: the most recently received document wins.", "",
         "## Totals", "",
         "| patient | | explicit-correction (shipped) | latest-document |",
         "|---|---|---|---|"]
    for mrn, pa in a["patients"].items():
        pb = b["patients"][mrn]
        L += [f"| `{mrn}` | sessions | **{_rng(pa['sessions'])}** | "
              f"{_rng(pb['sessions'])} |",
              f"| | episode minutes | **{_rng(pa['total_minutes'])}** | "
              f"{_rng(pb['total_minutes'])} |",
              f"| | wall-clock check | "
              f"{'passes' if pa['integrity_ok'] else 'FAILS'} | "
              f"{'passes' if pb['integrity_ok'] else 'FAILS'} |"]
    if wk:
        L += ["", "## Weeks that change", "",
              "| patient | week | shipped | latest-document |",
              "|---|---|---|---|"]
        for w in wk:
            L.append(f"| `{w['patient']}` | {w['week']} | "
                     f"{_rng(w['a_minutes'])} min, {w['a_status']} | "
                     f"{_rng(w['b_minutes'])} min, {w['b_status']} |")
    else:
        L += ["", "No weekly total or verdict changed between the policies.", ""]

    L += ["", "## Events that resolve differently", ""]
    if not ev:
        L += ["None. On this corpus the two policies cannot be distinguished, "
              "which means the comparison proves nothing -- check that the "
              "baseline is not silently inheriting the shipped rules.", ""]
    else:
        L += ["| patient | event | date | field | shipped | latest-document |",
              "|---|---|---|---|---|---|"]
        for d in ev:
            L.append(f"| `{d['patient']}` | `{d['event']}` | "
                     f"{d.get('date', '')} | {d['field']} | "
                     f"{_rng(d['a'])} | {_rng(d['b'])} |")
        L += ["", "### Decision logs side by side", ""]
        seen = set()
        for d in ev:
            if d["event"] in seen:
                continue
            seen.add(d["event"])
            L.append(f"**`{d['event']}`** — shipped policy")
            L.append("")
            for x in d["a_log"]:
                L.append(f"- `[{x['rule']}]` {x['field']} = {x['chosen']} "
                         f"-- {x['why']}")
            L += ["", f"**`{d['event']}`** — latest-document policy", ""]
            for x in d["b_log"]:
                L.append(f"- `[{x['rule']}]` {x['field']} = {x['chosen']} "
                         f"-- {x['why']}")
            L.append("")

    failed = [(mrn, p) for mrn, p in b["patients"].items() if not p["integrity_ok"]]
    L += ["## What the wall-clock check found", ""]
    if failed:
        L += ["The naive policy is not merely a different judgement call: it "
              "makes the record internally impossible, and the system detects "
              "that *without being told the right answer*. A patient cannot be "
              "in two therapy rooms at once, so an overlap between two counted "
              "contacts on one day means the reconciliation is wrong somewhere.",
              ""]
        for mrn, p in failed:
            for i in p["integrity_issues"]:
                L.append(f"    {json.dumps(i)}")
        L += ["",
              "That check (`queries.integrity`) runs on every build. It is the "
              "cheapest answer-independent signal I found that a reconciliation "
              "policy is wrong, and it is why recency is only ever a tiebreak in "
              "the shipped policy, never authority.", ""]
    else:
        L += ["Both policies pass. Compare the tables above directly.", ""]

    flips = [w for w in wk if w["a_status"] != w["b_status"]]
    if flips:
        L += ["## The quieter effect", "",
              "Recency also *resolves* conflicts the documents do not resolve. "
              "Where the shipped policy keeps bounds and reports "
              "`CANNOT_DETERMINE`, picking the later signature produces a "
              "confident verdict from a record that does not support one:", ""]
        for w in flips:
            L.append(f"- `{w['patient']}` week of {w['week']}: "
                     f"`{w['a_status']}` ({_rng(w['a_minutes'])} min) becomes "
                     f"`{w['b_status']}` ({_rng(w['b_minutes'])} min)")
        L += ["",
              "Unlike the overlap, this one leaves no trace in the output at "
              "all. It is the argument for carrying bounds rather than a chosen "
              "value.", ""]
    return "\n".join(L)
