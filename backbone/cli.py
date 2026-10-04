"""Command line interface.

    python -m backbone build documents/           ingest + reconcile (first run)
    python -m backbone answer questions.json      answer from the saved abstraction
    python -m backbone ask "<question>"           one new question, no re-processing
    python -m backbone trace '<MRN>|<ENCOUNTER>'    follow one event to its sources
    python -m backbone export                     dump the abstraction as JSON
    python -m backbone bench                      measured performance report
    python -m backbone experiment                 the conflict-policy A/B
    python -m backbone verify                     integrity + regression checks
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import queries as Q
from . import reconcile, render, router
from .ingest import ingest_dir, ingest_path
from .store import Store

DEFAULT_DB = "out/clinical.db"


def _store(args) -> Store:
    return Store(args.db)


# ---------------------------------------------------------------------------
def cmd_ingest(args) -> int:
    st = _store(args)
    t0 = time.perf_counter()
    rep = ingest_dir(st, pathlib.Path(args.path), extractor=args.extractor,
                     force=args.force)
    el = time.perf_counter() - t0
    print(f"ingested {rep['ingested']} / {rep['files']} files "
          f"({rep['duplicates']} duplicate, {rep['unchanged']} unchanged) "
          f"-> {rep['claims']} claims in {el:.2f}s")
    if rep["unverified_quotes"]:
        print(f"WARNING: {rep['unverified_quotes']} claim quote(s) did not verify")
    st.close()
    return 0


def cmd_reconcile(args) -> int:
    st = _store(args)
    t0 = time.perf_counter()
    stats = reconcile.reconcile_all(st, policy=args.policy)
    el = time.perf_counter() - t0
    for mrn, s in stats.items():
        print(f"{mrn}: {s['events']} events, {s['therapy_events']} counted as therapy, "
              f"{s['disputed']} disputed, {s['unmatched']} claims unmatched "
              f"[policy={s['policy']}]")
    print(f"reconciled in {el:.2f}s")
    st.close()
    return 0


def cmd_build(args) -> int:
    st = _store(args)
    t0 = time.perf_counter()
    rep = ingest_dir(st, pathlib.Path(args.path), extractor=args.extractor,
                     force=args.force)
    t1 = time.perf_counter()
    stats = reconcile.reconcile_all(st, policy=args.policy)
    t2 = time.perf_counter()
    print(f"ingest    {rep['ingested']}/{rep['files']} files, {rep['claims']} claims, "
          f"{t1 - t0:.2f}s")
    for mrn, s in stats.items():
        print(f"reconcile {mrn}: {s['events']} events "
              f"({s['therapy_events']} therapy, {s['disputed']} disputed), "
              f"{t2 - t1:.2f}s")
    integ = Q.integrity(st)["value"]
    print("integrity", "ok" if integ["ok"] else integ["issues"])
    st.close()      # folds the write-ahead log into the main file
    size = sum(pathlib.Path(args.db + s).stat().st_size for s in ("", "-wal", "-shm")
               if pathlib.Path(args.db + s).exists())
    print(f"abstraction {args.db}  {size / 1024:.1f} KiB")
    return 0


def cmd_add(args) -> int:
    """Incremental path: one new document, reconcile only its patient."""
    st = _store(args)
    t0 = time.perf_counter()
    r = ingest_path(st, pathlib.Path(args.file), extractor=args.extractor,
                    force=args.force)
    print(json.dumps(r, indent=1))
    if r["status"] == "ingested" and r.get("mrn"):
        s = reconcile.reconcile_patient(st, r["mrn"], policy=args.policy)
        print(f"reconciled {r['mrn']}: {json.dumps(s)}")
    print(f"{time.perf_counter() - t0:.3f}s")
    st.close()
    return 0


# ---------------------------------------------------------------------------
def cmd_answer(args) -> int:
    st = _store(args)
    qs = json.loads(pathlib.Path(args.questions).read_text(encoding="utf-8"))
    out = [f"# Answers\n",
           f"Source: `{args.questions}`  ·  abstraction: `{args.db}`  ·  "
           f"all figures computed in `backbone.queries` from the reconciled "
           f"event table.\n"]
    machine = []
    for item in qs:
        qid = item.get("id", "?")
        text = item["question"]
        t0 = time.perf_counter()
        res = router.answer(st, text, router=args.router)
        el = time.perf_counter() - t0
        out.append(f"\n## {qid}\n\n> {text}\n")
        if "error" in res:
            out.append(f"ERROR: {res['error']}")
            continue
        plan = res["route"]
        out.append(f"*Resolved to* `{plan['function']}("
                   f"{', '.join(f'{k}={v!r}' for k, v in plan['params'].items())})`"
                   f" *in {el * 1000:.0f} ms (router: {plan['router']}).*\n")
        for w in plan.get("warnings", []):
            out.append(f"> **Routing warning:** {w}\n")
        out.append(render.render(plan["function"], res["result"]))
        machine.append({"id": qid, "question": text, "route": plan,
                        "latency_ms": round(el * 1000, 1),
                        "value": res["result"]["value"],
                        "calculation": res["result"]["calculation"]})
    md = "\n".join(out) + "\n"
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(md, encoding="utf-8")
    pathlib.Path(args.out).with_suffix(".json").write_text(
        json.dumps(machine, indent=1, default=str), encoding="utf-8")
    print(f"wrote {args.out} and {pathlib.Path(args.out).with_suffix('.json')}")
    st.log_run("answer", args.questions)
    st.close()
    return 0


def cmd_ask(args) -> int:
    st = _store(args)
    t0 = time.perf_counter()
    res = router.answer(st, args.question, router=args.router)
    el = time.perf_counter() - t0
    if "error" in res:
        print("ERROR:", res["error"])
        st.close()
        return 1
    plan = res["route"]
    print(f"[{plan['function']}({', '.join(f'{k}={v!r}' for k, v in plan['params'].items())})"
          f"  {el * 1000:.0f} ms  router={plan['router']}]\n")
    for w in plan.get("warnings", []):
        print(f"WARNING: {w}\n")
    print(render.render(plan["function"], res["result"]))
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(res, indent=1, default=str), encoding="utf-8")
        print(f"\n(full result with evidence written to {args.json})")
    st.close()
    return 0


def cmd_trace(args) -> int:
    st = _store(args)
    print(render.render("trace", Q.trace(st, args.event_id)))
    st.close()
    return 0


def cmd_export(args) -> int:
    st = _store(args)
    doc = {"patients": [], "documents": [], "decisions": [], "duplicates":
           Q.duplicates(st)["value"]}
    for r in st.q("SELECT doc_pk, doc_id, filename, doc_class, sha256_raw,"
                  " sha256_norm, char_len, received_at, authored_at, extractor,"
                  " extractor_ver FROM documents ORDER BY filename"):
        doc["documents"].append(dict(r))
    for mrn in st.patients():
        p = dict(st.q1("SELECT * FROM patients WHERE mrn=?", (mrn,)))
        p["episode"] = dict(st.episode(mrn)) if st.episode(mrn) else None
        p["requirements"] = [dict(r) for r in st.requirements(mrn)]
        p["measures"] = [dict(r) for r in st.measures(mrn)]
        p["events"] = []
        for e in st.events(mrn):
            ev = dict(e)
            ev["resolved"] = json.loads(ev["resolved"])
            ev["decisions"] = [{"field": d["field"], "rule": d["rule_id"],
                                "chosen": d["chosen"], "why": d["rationale"]}
                               for d in st.event_decisions(e["event_id"])]
            ev["evidence"] = Q.all_evidence(st, e["event_id"])
            p["events"].append(ev)
        p["observations"] = [dict(o) for o in st.observations(mrn)]
        p["totals"] = Q.session_counts(st, mrn, *Q.episode_window(st, mrn))["value"]
        doc["patients"].append(p)
    for d in st.q("SELECT * FROM decisions WHERE event_id IS NULL ORDER BY decision_id"):
        doc["decisions"].append(dict(d))
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(doc, indent=1, default=str),
                                      encoding="utf-8")
    print(f"wrote {args.out} ({pathlib.Path(args.out).stat().st_size / 1024:.1f} KiB)")
    st.close()
    return 0


def cmd_bench(args) -> int:
    from .bench import run
    run(args)
    return 0


def cmd_experiment(args) -> int:
    from .experiment import run
    run(args)
    return 0


def cmd_models(args) -> int:
    from . import provider
    ok, why = provider.available()
    if not ok:
        print(why)
        print("\nPut one key in a .env file at the repository root "
              "(see .env.example):\n    GROQ_API_KEY=gsk_...")
        return 1
    print(f"provider: {provider.provider()}   default model: {provider.model()}")
    try:
        models = provider.list_models()
    except provider.ModelError as e:
        print("")
        print(f"could not list models: {e}")
        print("")
        print("A 401 or 403 here means the key itself was rejected. Rewrite "
              ".env with the current key and no stray characters:")
        print("    $k = Read-Host 'Groq API key'; "
              "Set-Content -Path .env -Value \"GROQ_API_KEY=$k\" -Encoding ascii")
        return 1
    for m in models:
        ctx = f"ctx {m['context']}" if m.get("context") else ""
        price = "priced locally" if m["priced"] else "no local price"
        print(f"  {str(m['id']):<52} {str(m['owned_by'] or ''):<18} "
              f"{ctx:<12} {price}")
    return 0


def cmd_compare(args) -> int:
    from .compare import run
    return run(args)


def cmd_verify(args) -> int:
    import unittest
    st = _store(args)
    integ = Q.integrity(st)
    print(render.render("integrity", integ))
    st.close()
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() and integ["value"]["ok"] else 1


# ---------------------------------------------------------------------------
def _global_flags(ap) -> None:
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"abstraction file (default {DEFAULT_DB})")
    ap.add_argument("--policy", default="explicit_correction",
                    choices=reconcile.POLICIES,
                    help="conflict-resolution policy "
                         "(see docs/experiment-conflict-policy.md)")
    ap.add_argument("--extractor", default="rules",
                    choices=("rules", "llm", "llm_full"))
    ap.add_argument("--router", default="rules", choices=("rules", "llm"))
    ap.add_argument("--force", action="store_true",
                    help="re-extract known documents")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backbone", description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    _global_flags(p)
    # The same flags are attached to every subcommand through a parent parser,
    # so `build documents --extractor llm` works as well as
    # `--extractor llm build documents`. Requiring one order is a trap when
    # somebody else is driving.
    common = argparse.ArgumentParser(add_help=False)
    _global_flags(common)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", parents=[common], help="read documents into claims")
    s.add_argument("path")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("reconcile", parents=[common], help="claims -> events")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("build", parents=[common], help="ingest + reconcile")
    s.add_argument("path", nargs="?", default="documents")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("add", parents=[common], help="ingest one new document and reconcile its patient")
    s.add_argument("file")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("answer", parents=[common], help="answer a questions.json from the abstraction")
    s.add_argument("questions", nargs="?", default="questions.json")
    s.add_argument("--out", default="out/answers.md")
    s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("ask", parents=[common], help="answer one new question")
    s.add_argument("question")
    s.add_argument("--json", help="also write the full result with evidence here")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("trace", parents=[common], help="follow one event to its sources")
    s.add_argument("event_id")
    s.set_defaults(fn=cmd_trace)

    s = sub.add_parser("export", parents=[common], help="dump the abstraction")
    s.add_argument("--out", default="out/abstraction.json")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("bench", parents=[common], help="measured performance report")
    s.add_argument("--path", default="documents")
    s.add_argument("--out", default="out/benchmark.json")
    s.add_argument("--repeat", type=int, default=3)
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("experiment", parents=[common], help="conflict-policy A/B")
    s.add_argument("--path", default="documents")
    s.add_argument("--out", default="docs/experiment-conflict-policy.md")
    s.set_defaults(fn=cmd_experiment)

    s = sub.add_parser("verify", parents=[common], help="integrity checks + regression tests")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("models", parents=[common], help="list models this API key can use")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("compare", parents=[common], help="diff the rules and model extractors")
    s.add_argument("path", nargs="?", default="documents")
    s.add_argument("--out", default="docs/experiment-extractor-comparison.md")
    s.add_argument("--extractor-b", default="llm_full", choices=("llm", "llm_full"))
    s.set_defaults(fn=cmd_compare)

    # argparse lets the subcommand's defaults overwrite a value given before
    # it, so resolve each global flag from whichever side supplied a non-default.
    pre, _ = p.parse_known_args(argv)
    args = p.parse_args(argv)
    for flag, default in (("db", DEFAULT_DB), ("policy", "explicit_correction"),
                          ("extractor", "rules"), ("router", "rules"),
                          ("force", False)):
        if getattr(args, flag, default) == default:
            setattr(args, flag, getattr(pre, flag, default))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
