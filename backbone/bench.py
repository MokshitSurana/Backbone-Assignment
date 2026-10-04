"""Measured performance. Nothing here is an estimate.

Estimates for a 500K-document corpus are derived from these numbers in the
README and are labelled as estimates there.
"""
from __future__ import annotations

import json
import pathlib
import statistics
import time

from . import queries as Q
from . import reconcile, router
from .ingest import ingest_dir
from .store import Store


def _timeit(fn, repeat: int) -> dict:
    runs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t0) * 1000)
    return {"ms_median": round(statistics.median(runs), 2),
            "ms_min": round(min(runs), 2), "ms_max": round(max(runs), 2),
            "runs": repeat}


def run(args) -> dict:
    src = pathlib.Path(args.path)
    db = pathlib.Path("out/bench.db")
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db) + suffix)
        if p.exists():
            p.unlink()

    out: dict = {"corpus": {"path": str(src)}, "cold": {}, "warm": {},
                 "queries": {}, "storage": {}, "model": {}}

    files = sorted(src.rglob("*.txt"))
    out["corpus"] = {"path": str(src), "documents": len(files),
                     "bytes": sum(f.stat().st_size for f in files)}

    # ---- cold build: empty database, every document read ------------------
    st = Store(db)
    t0 = time.perf_counter()
    rep = ingest_dir(st, src)
    t1 = time.perf_counter()
    stats = reconcile.reconcile_all(st)
    t2 = time.perf_counter()
    out["cold"] = {
        "ingest_s": round(t1 - t0, 3), "reconcile_s": round(t2 - t1, 3),
        "total_s": round(t2 - t0, 3),
        "ms_per_document": round((t1 - t0) * 1000 / max(1, rep["ingested"]), 2),
        "claims": rep["claims"], "documents_ingested": rep["ingested"],
        "events": sum(s["events"] for s in stats.values()),
        "therapy_events": sum(s["therapy_events"] for s in stats.values()),
        "disputed_events": sum(s["disputed"] for s in stats.values()),
    }

    # ---- warm build: same corpus again, nothing should be re-extracted ----
    t0 = time.perf_counter()
    rep2 = ingest_dir(st, src)
    t1 = time.perf_counter()
    out["warm"] = {"ingest_s": round(t1 - t0, 3),
                   "documents_reextracted": rep2["ingested"],
                   "documents_unchanged": rep2["unchanged"]}

    # ---- incremental: one document re-extracted and its patient redone ----
    one = files[0]
    from .ingest import ingest_path
    t0 = time.perf_counter()
    r = ingest_path(st, one, force=True)
    if r.get("mrn"):
        reconcile.reconcile_patient(st, r["mrn"])
    out["warm"]["one_document_update_s"] = round(time.perf_counter() - t0, 3)

    # ---- query latency ----------------------------------------------------
    mrn = st.patients()[0]
    start, end = Q.episode_window(st, mrn)
    # Derive the benchmark's own inputs from the corpus. Hardcoding a name or an
    # encounter id here would put a patient fact in the package, which the brief
    # forbids and tests/test_units.py enforces.
    who = st.q1("SELECT given_name, name FROM patients WHERE mrn=?", (mrn,))
    given = (who["given_name"] or who["name"] or mrn) if who else mrn
    ev = st.q1("SELECT event_id FROM events WHERE patient_mrn=? AND disputed=0"
               " AND counts_as_therapy=1 ORDER BY service_date LIMIT 1", (mrn,))
    sample_event = ev["event_id"] if ev else f"{mrn}|none"
    cases = {
        "session_counts (one patient)": lambda: Q.session_counts(st, mrn, start, end),
        "minutes by week (one patient)": lambda: Q.minutes(st, mrn, start, end),
        "compliance (one patient)": lambda: Q.compliance(st, mrn, start, end),
        "day_detail (two dates)": lambda: Q.day_detail(st, mrn, [start, end]),
        "progress (one patient)": lambda: Q.progress(st, mrn, start, end),
        "consecutive_below (all patients)": lambda: Q.consecutive_below(st, None, 2),
        "trace (one event)": lambda: Q.trace(st, sample_event),
        "integrity (all patients)": lambda: Q.integrity(st),
    }
    for name, fn in cases.items():
        out["queries"][name] = _timeit(fn, args.repeat)

    # ---- natural-language routing end to end ------------------------------
    first_day = st.q1("SELECT service_date d FROM events WHERE patient_mrn=?"
                      " AND counts_as_therapy=1 ORDER BY service_date LIMIT 1",
                      (mrn,))
    day = first_day["d"] if first_day else (start or "")
    nlq = [f"How many therapy sessions did {given} attend?",
           f"How many therapy minutes did {given} receive each Monday-Sunday week?",
           f"Did the delivered therapy meet the plan goal each week for {given}?",
           "Which patients had two consecutive weeks below the requirement?",
           f"Reconstruct the care on {day}.",
           f"Summarise the documented symptom course for {given}."]
    rt = []
    for q in nlq:
        t0 = time.perf_counter()
        router.answer(st, q)
        rt.append((time.perf_counter() - t0) * 1000)
    out["queries"]["end_to_end_nl_question"] = {
        "ms_median": round(statistics.median(rt), 2),
        "ms_min": round(min(rt), 2), "ms_max": round(max(rt), 2),
        "runs": len(rt)}

    # ---- storage ----------------------------------------------------------
    st.commit()
    sizes = {t: st.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in
             ("documents", "claims", "events", "event_claims", "decisions",
              "observations", "measures", "plan_requirements",
              "corrections")}
    st.close()
    total = sum(pathlib.Path(str(db) + s).stat().st_size
                for s in ("", "-wal", "-shm")
                if pathlib.Path(str(db) + s).exists())
    out["storage"] = {
        "sqlite_bytes": total, "sqlite_kib": round(total / 1024, 1),
        "bytes_per_document": round(total / max(1, out["corpus"]["documents"])),
        "rows": sizes,
        "source_bytes": out["corpus"]["bytes"],
        "abstraction_to_source_ratio": round(total / max(1, out["corpus"]["bytes"]), 2),
    }

    # ---- model usage ------------------------------------------------------
    # The default and only extractor is deterministic, so a build makes no
    # model calls and costs nothing. (An optional model-backed extractor and a
    # rules-vs-model comparison live on the post-timebox branch.)
    out["model"] = {
        "extractor": "rules",
        "model_calls": 0,
        "tokens": 0,
        "usd": 0.0,
        "comment": "the pipeline is fully deterministic: no model is called at "
                   "any point in ingest, reconcile or query.",
    }

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    _print(out)
    print(f"\nwrote {args.out}")
    return out


def _print(o: dict) -> None:
    c, w, s = o["cold"], o["warm"], o["storage"]
    print(f"corpus              {o['corpus']['documents']} documents, "
          f"{o['corpus']['bytes'] / 1024:.1f} KiB")
    print(f"cold build          {c['total_s']}s  "
          f"(ingest {c['ingest_s']}s = {c['ms_per_document']} ms/doc, "
          f"reconcile {c['reconcile_s']}s)")
    print(f"                    {c['claims']} claims -> {c['events']} events "
          f"({c['therapy_events']} therapy, {c['disputed_events']} disputed)")
    print(f"warm re-ingest      {w['ingest_s']}s, {w['documents_reextracted']} "
          f"re-extracted / {w['documents_unchanged']} unchanged")
    print(f"one new document    {w['one_document_update_s']}s "
          f"(extract + reconcile that patient only)")
    print(f"abstraction size    {s['sqlite_kib']} KiB "
          f"({s['bytes_per_document']} B/document, "
          f"{s['abstraction_to_source_ratio']}x the source text)")
    print("query latency (median of repeats)")
    for k, v in o["queries"].items():
        print(f"  {k:<34} {v['ms_median']:>8.2f} ms   "
              f"[{v['ms_min']:.2f}-{v['ms_max']:.2f}]")
    print(f"model usage         {o['model']['comment']}")
