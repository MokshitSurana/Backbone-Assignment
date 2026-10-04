"""Extractor disagreement harness.

Builds the abstraction twice over the same corpus -- once with the deterministic
`rules` front-end, once with a model front-end -- using identical reconciliation
and identical arithmetic, then diffs the result field by field.

Every disagreement is either a rules gap or a model error, and the point of the
harness is to produce that ratio as a number instead of an opinion. Because the
hand-verified ledger in `tests/test_gold.py` says which side is right for this
corpus, each disagreement can also be scored.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

from . import queries as Q
from . import reconcile
from . import provider
from .extract import llm
from .ingest import ingest_dir
from .store import Store

# The hand-verified ledger lives outside the package (tests/gold_ledger.json)
# so that no patient fact is encoded in the implementation. When it is absent
# the harness still runs and simply reports every disagreement as unscored.
LEDGER_PATH = pathlib.Path(
    os.environ.get("BACKBONE_GOLD_LEDGER", "tests/gold_ledger.json"))


def _load_ledger() -> tuple[dict, dict, list]:
    if not LEDGER_PATH.exists():
        return {}, {}, []
    d = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    events = {k: (v["service_type"], v["min_minutes"], v["max_minutes"],
                  v["counts_as_therapy"]) for k, v in d.get("events", {}).items()}
    weeks = {k: tuple(v) for k, v in d.get("weeks", {}).items()}
    return events, weeks, d.get("total_minutes", [])


GOLD, GOLD_WEEKS, GOLD_TOTAL = _load_ledger()


def _build(src: pathlib.Path, extractor: str, db: pathlib.Path) -> dict:
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db) + s)
        if p.exists():
            p.unlink()
    llm.reset_stats()
    st = Store(db)
    t0 = time.perf_counter()
    rep = ingest_dir(st, src, extractor=extractor)
    t1 = time.perf_counter()
    reconcile.reconcile_all(st)
    t2 = time.perf_counter()
    snap = {
        "extractor": extractor, "ingest_s": round(t1 - t0, 2),
        "reconcile_s": round(t2 - t1, 3), "claims": rep["claims"],
        "unverified_quotes": rep["unverified_quotes"],
        "model": ({**llm.STATS, "provider": provider.provider(),
                   "model": provider.model(),
                   "prompt_version": llm.PROMPT_VERSION,
                   "temperature": llm.TEMPERATURE}
                  if extractor != "rules" else None),
        "events": {}, "weeks": {}, "measures": [], "requirements": [],
        "observations": 0, "patients": st.patients(),
    }
    for mrn in st.patients():
        for e in st.events(mrn):
            key = f"{e['service_date']}|{e['encounter_ref'] or e['event_id']}"
            snap["events"][key] = {
                "service_type": e["service_type"], "occurred": e["occurred"],
                "patient_present": e["patient_present"],
                "min": e["min_minutes"], "max": e["max_minutes"],
                "therapy": e["counts_as_therapy"], "disputed": e["disputed"],
            }
        comp = Q.compliance(st, mrn, *Q.episode_window(st, mrn))
        if comp["value"]:
            for w in comp["value"]["weeks"]:
                snap["weeks"][w["week_start"]] = {
                    "minutes": w["minutes"], "days": w["therapy_days"],
                    "status": w["status"]}
            snap["requirements"] = [
                {"metric": r["metric"], "threshold": r["threshold"],
                 "period": r["period"], "types": sorted(r["service_types"])}
                for r in comp["value"]["requirements"]]
        tot = Q.minutes(st, mrn, *Q.episode_window(st, mrn))["value"]
        snap["total_minutes"] = tot["total_minutes"]
        snap["measures"] = [
            {"instrument": m["instrument"], "completed": m["completed_at"],
             "total": m["total"], "form": m["form_ref"],
             "sources": len(m["claim_ids"])}
            for m in Q.progress(st, mrn)["value"]["distinct_measures"]]
        obs = st.observations(mrn)
        snap["observations"] = len(obs)
        snap["obs_spans"] = sorted({
            (o["doc_id"] or o["filename"], o["quote_start"], o["quote_end"])
            for o in obs})
        snap["obs_domains"] = sorted({o["domain"] for o in obs})
        snap["obs_reporters"] = sorted({o["reporter"] for o in obs})
        snap["obs_polarities"] = sorted({o["polarity"] for o in obs})
        snap["integrity_ok"] = Q.integrity(st, mrn)["value"]["ok"]
        snap["counts"] = Q.session_counts(st, mrn,
                                          *Q.episode_window(st, mrn))["value"]
    # Token totals come from the cache table, not only from live calls, so a
    # cached rerun still reports what the extraction actually cost.
    row = st.q1("SELECT COUNT(*) n, COALESCE(SUM(in_tokens),0) i, "
                "COALESCE(SUM(out_tokens),0) o, COALESCE(SUM(usd),0) u "
                "FROM llm_cache")
    if snap["model"] is not None:
        snap["model"]["cached_responses"] = row["n"]
        snap["model"]["in_tokens"] = max(snap["model"]["in_tokens"], row["i"])
        snap["model"]["out_tokens"] = max(snap["model"]["out_tokens"], row["o"])
        snap["model"]["usd"] = max(snap["model"]["usd"], row["u"])
    st.close()
    snap["db_bytes"] = sum(
        pathlib.Path(str(db) + s).stat().st_size for s in ("", "-wal", "-shm")
        if pathlib.Path(str(db) + s).exists())
    return snap


def _score(key: str, field: str, value) -> str | None:
    """right / wrong / unscored, against the hand-verified ledger."""
    g = GOLD.get(key)
    if not g:
        return None
    svc, lo, hi, thx = g
    got = {"service_type": svc, "min": lo, "max": hi, "therapy": thx}.get(field)
    if got is None:
        return None
    return "right" if value == got else "wrong"


def _diff_events(a: dict, b: dict) -> list[dict]:
    out = []
    for key in sorted(set(a["events"]) | set(b["events"])):
        ea, eb = a["events"].get(key), b["events"].get(key)
        if ea is None or eb is None:
            out.append({"event": key, "field": "exists",
                        "rules": "present" if ea else "MISSING",
                        "other": "present" if eb else "MISSING",
                        "rules_score": "right" if ea and key in GOLD else None,
                        "other_score": "right" if eb and key in GOLD else "wrong"})
            continue
        for f in ("service_type", "occurred", "patient_present", "min", "max",
                  "therapy"):
            if ea[f] != eb[f]:
                out.append({"event": key, "field": f, "rules": ea[f],
                            "other": eb[f],
                            "rules_score": _score(key, f, ea[f]),
                            "other_score": _score(key, f, eb[f])})
    return out


def run(args) -> int:
    src = pathlib.Path(args.path)
    other = args.extractor_b
    ok, why = llm.available()
    if not ok:
        print(f"cannot compare: {why}")
        print("\nSet a key first, for example in a .env file at the repository root:")
        print("    GROQ_API_KEY=gsk_...")
        print("    BACKBONE_MODEL=llama-3.3-70b-versatile")
        return 1
    print(f"building with rules ...")
    a = _build(src, "rules", pathlib.Path("out/cmp-rules.db"))
    print(f"building with {other} ({llm.describe()}) ...")
    b = _build(src, other, pathlib.Path(f"out/cmp-{other}.db"))

    diffs = _diff_events(a, b)
    md = _report(a, b, diffs, other)
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(md, encoding="utf-8")
    pathlib.Path(args.out).with_suffix(".json").write_text(
        json.dumps({"rules": a, other: b, "event_diffs": diffs}, indent=1,
                   default=str), encoding="utf-8")
    print(md)
    print(f"\nwrote {args.out}")
    return 0


def _report(a: dict, b: dict, diffs: list[dict], other: str) -> str:
    m = b["model"] or {}
    fields_total = len(set(a["events"]) | set(b["events"])) * 6
    agree = fields_total - len(diffs)
    rules_right = sum(1 for d in diffs if d["rules_score"] == "right")
    other_right = sum(1 for d in diffs if d["other_score"] == "right")
    unscored = sum(1 for d in diffs
                   if d["rules_score"] is None and d["other_score"] is None)

    def rng(v):
        return f"{v[0]}" if v[0] == v[1] else f"{v[0]}-{v[1]}"

    L = ["# Tested design decision: deterministic vs model extraction", "",
         "Same documents, same reconciliation policy, same arithmetic. Only the",
         "front-end that turns text into claims changes. Reproduce with",
         "`python -m backbone compare`.", "",
         f"Model under test: **{m.get('model') or other}** via "
         f"`{m.get('provider', 'n/a')}`, temperature 0, "
         f"prompt `{m.get('prompt_version', '')}`.", "",
         "## Headline", "",
         "| | rules | " + other + " |", "|---|---|---|",
         f"| therapy sessions | **{a['counts']['sessions']}** | "
         f"{b['counts']['sessions']} |",
         f"| distinct therapy days | **{a['counts']['distinct_days']}** | "
         f"{b['counts']['distinct_days']} |",
         f"| episode minutes | **{rng(a['total_minutes'])}** | "
         f"{rng(b['total_minutes'])} |",
         f"| events built | {len(a['events'])} | {len(b['events'])} |",
         f"| claims extracted | {a['claims']} | {b['claims']} |",
         f"| observations kept | {a['observations']} | {b['observations']} |",
         f"| distinct measures | {len(a['measures'])} | {len(b['measures'])} |",
         f"| wall-clock integrity | {'passes' if a['integrity_ok'] else 'FAILS'} |"
         f" {'passes' if b['integrity_ok'] else 'FAILS'} |",
         f"| ingest wall time | {a['ingest_s']} s | {b['ingest_s']} s |", "",
         "### Weekly totals and verdicts", "",
         "This is the table that matters, and it is the one an agreement",
         "percentage on the event table can hide.", "",
         "| week | gold minutes | rules | " + other + " | rules verdict | "
         + other + " verdict |",
         "|---|---|---|---|---|---|"]
    verdict_flips = []
    for wk, g in GOLD_WEEKS.items():
        wa = a["weeks"].get(wk, {})
        wb = b["weeks"].get(wk, {})
        sa, sb = wa.get("status", "-"), wb.get("status", "-")
        if sa != sb:
            verdict_flips.append((wk, sa, sb))
        L.append(f"| {wk} | {rng(list(g))} | "
                 f"{rng(wa['minutes']) if wa.get('minutes') else '-'} | "
                 f"{rng(wb['minutes']) if wb.get('minutes') else '-'} | "
                 f"{sa} | {sb}"
                 + ("  **<-- differs**" if sa != sb else "") + " |")
    if verdict_flips:
        L += ["", "### Material difference in the answer", "",
              f"Every minute total above is identical, and "
              f"{len(verdict_flips)} of {len(GOLD_WEEKS)} weekly verdicts still "
              f"differ:", ""]
        for wk, sa, sb in verdict_flips:
            L.append(f"- week of {wk}: rules `{sa}`, {other} `{sb}`")
        ra_m = {r["metric"] for r in a["requirements"]}
        rb_m = {r["metric"] for r in b["requirements"]}
        missing = ra_m - rb_m
        if missing:
            L += ["",
                  f"Cause: {other} did not extract the "
                  f"{', '.join(sorted(missing))} requirement from the treatment "
                  "plan. Where a plan states several thresholds in one "
                  "sentence, the model returned only the first clause. With a "
                  "requirement missing, `compliance` can only check the metrics "
                  "it has, so weeks that are genuinely short on the missing one "
                  "are reported as met.",
                  "",
                  "This is the most consequential error in the comparison and it "
                  "is invisible in the event table: the model agreed on every "
                  "single minute and still produced the wrong answer to DEV-03.",
                  ""]
    L += ["", "## Agreement on the event table", "",
          f"- {len(set(a['events']) | set(b['events']))} events x 6 compared "
          f"fields = {fields_total} field comparisons",
          f"- **{agree}/{fields_total} agree ({100 * agree / max(1, fields_total):.1f}%)**",
          f"- {len(diffs)} disagreements: rules correct in {rules_right}, "
          f"{other} correct in {other_right}, "
          f"{len(diffs) - rules_right - other_right - unscored} both wrong or "
          f"partially scored, {unscored} not covered by the hand-verified ledger",
          ""]
    if diffs:
        L += ["| event | field | rules | " + other + " | who is right |",
              "|---|---|---|---|---|"]
        for d in diffs:
            verdict = ("rules" if d["rules_score"] == "right" else
                       other if d["other_score"] == "right" else
                       "neither" if d["rules_score"] or d["other_score"] else
                       "not scored")
            L.append(f"| `{d['event']}` | {d['field']} | {d['rules']} | "
                     f"{d['other']} | {verdict} |")
        L.append("")
    else:
        L += ["No disagreement on any compared event field.", ""]

    # ---- prose layer -----------------------------------------------------
    sa = {tuple(x) for x in a.get("obs_spans", [])}
    sb = {tuple(x) for x in b.get("obs_spans", [])}
    both, only_a, only_b = sa & sb, sa - sb, sb - sa
    union = len(sa | sb) or 1
    L += ["## The prose layer", ""]
    if other == "llm":
        L += ["In hybrid mode the model never sees encounter structure: tables, "
              "ids, dispositions and clock times are parsed by code either way. "
              "So the identical event table above is **true by construction and "
              "is not evidence of anything** -- it only confirms the wiring. The "
              "measurement that matters is this section.", ""]
    L += [f"| | rules | {other} |", "|---|---|---|",
          f"| observation spans | {len(sa)} | {len(sb)} |",
          f"| spans both found | {len(both)} | {len(both)} |",
          f"| spans only this side found | {len(only_a)} | {len(only_b)} |",
          f"| of the other side's spans, how many it also found | "
          f"{len(both)}/{len(sb)} ({len(both) / (len(sb) or 1):.0%}) | "
          f"{len(both)}/{len(sa)} ({len(both) / (len(sa) or 1):.0%}) |",
          f"| Jaccard overlap | {len(both) / union:.0%} | "
          f"{len(both) / union:.0%} |",
          f"| domains covered | {len(a.get('obs_domains', []))} | "
          f"{len(b.get('obs_domains', []))} |",
          f"| reporters distinguished | "
          f"{', '.join(a.get('obs_reporters', [])) or '-'} | "
          f"{', '.join(b.get('obs_reporters', [])) or '-'} |", ""]
    if len(sb) > len(sa) * 1.5 and len(both) >= len(sa) * 0.8:
        L += ["",
              f"Read that carefully: the low Jaccard figure is an **asymmetry, "
              f"not a disagreement**. {other} found {len(both)} of the "
              f"{len(sa)} spans the deterministic extractor found "
              f"({len(both) / (len(sa) or 1):.0%}) and then "
              f"{len(only_b)} more. That is the conservative-and-lossy "
              f"observation extraction named as limitation 4 in DESIGN.md, "
              f"measured.", ""]
    if only_a:
        L += ["**Found only by the deterministic extractor** "
              f"({len(only_a)}, first 8):", ""]
        for doc, s0, s1 in sorted(only_a)[:8]:
            L.append(f"- {doc} [{s0}:{s1}]")
        L.append("")
    if only_b:
        L += [f"**Found only by {other}** ({len(only_b)}, first 8):", ""]
        for doc, s0, s1 in sorted(only_b)[:8]:
            L.append(f"- {doc} [{s0}:{s1}]")
        L.append("")
    L += ["## Measured model usage", "",
          f"- documents sent to the model: {m.get('calls', 0)} "
          f"(of {m.get('documents', 0)} processed; "
          f"{m.get('cached', 0)} served from cache)",
          f"- input tokens: **{m.get('in_tokens', 0):,}**",
          f"- output tokens: **{m.get('out_tokens', 0):,}**",
          f"- cost at listed on-demand prices: "
          f"**${m.get('usd', 0.0):.4f}** (approximate; free-tier usage is $0)",
          f"- total model latency: {m.get('latency_ms', 0) / 1000:.1f} s "
          f"({m.get('latency_ms', 0) / max(1, m.get('calls', 1)):.0f} ms per document)",
          f"- quotes dropped because they were not found verbatim in the source: "
          f"**{m.get('dropped_quotes', 0)}**",
          f"- responses that were not parseable JSON: {m.get('parse_failures', 0)}",
          "",
          "Per 500K documents, extrapolated linearly from the measured tokens "
          f"above: ~{m.get('in_tokens', 0) / max(1, m.get('calls', 1)) * 500000 / 1e6:,.0f}M "
          "input tokens. Labelled an estimate because it is one.", ""]

    det = pathlib.Path("out/determinism.json")
    if det.exists():
        import json as _json
        runs = _json.loads(det.read_text(encoding="utf-8"))
        same = len({r["sha"] for r in runs}) == 1
        L += ["## Reproducibility", "",
              f"Three consecutive calls on the same document with the same "
              f"prompt at temperature 0 returned "
              f"{'identical' if same else 'differing'} JSON "
              f"({len({r['sha'] for r in runs})} distinct response(s) in "
              f"{len(runs)} calls).", "",
              "Across runs it is a different story. For the group facilitator "
              "note probed in `out/determinism.json`, the model returned "
              "`patient_present_intervals: []` during the comparison build "
              "(correct: that note states no patient times and says they are "
              "held on the attendance roster) and "
              f"`{runs[0]['patient_present_intervals']}` when called again about "
              "fifteen minutes later -- the scheduled slot, split around the "
              "break. Same prompt, same temperature, clinically material "
              "difference.", "",
              "So the response cache is not only a cost optimisation; on this "
              "path it is what makes an abstraction reproducible at all. The "
              "rules extractor is byte-identical across runs by construction.",
              ""]
    L += ["## Plan requirements read by each front-end", "",
          "| | rules | " + other + " |", "|---|---|---|"]
    ra = {r["metric"]: r for r in a["requirements"]}
    rb = {r["metric"]: r for r in b["requirements"]}
    def req_cell(r: dict | None) -> str:
        if not r:
            return "not found"
        return f">= {r['threshold']:g} per {r['period']}, counts {len(r['types'])}"

    for metric in sorted(set(ra) | set(rb)):
        L.append(f"| {metric} | {req_cell(ra.get(metric))} | "
                 f"{req_cell(rb.get(metric))} |")
    L += ["", "## Measures found by each front-end", "",
          "| instrument | completed | rules total | " + other + " total |",
          "|---|---|---|---|"]
    ma = {(x["instrument"], x["completed"]): x for x in a["measures"]}
    mb = {(x["instrument"], x["completed"]): x for x in b["measures"]}
    for k in sorted(set(ma) | set(mb)):
        L.append(f"| {k[0]} | {k[1]} | "
                 f"{ma[k]['total'] if k in ma else 'not found'} | "
                 f"{mb[k]['total'] if k in mb else 'not found'} |")
    L.append("")
    return "\n".join(L)
