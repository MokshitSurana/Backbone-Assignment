"""Measured scaling curve.

Clones the supplied corpus into N synthetic patients -- new MRN, new name, new
encounter/appointment/form ids, dates shifted by whole weeks so Monday-Sunday
bucketing still lines up -- then builds the abstraction over each size and
records ingest time, reconcile time, storage and query latency.

This replaces guesswork about 500K documents with a measured curve over three
orders of magnitude of what we actually have. The README labels which figures
are measured here and which are extrapolated from the fitted slope.

    python scripts/scale_test.py --patients 1 10 100 --out out/scaling.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import shutil
import statistics
import sys
import time

# run as `python scripts/scale_test.py` from the repository root
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backbone import queries as Q
from backbone import reconcile
from backbone.ingest import ingest_dir
from backbone.store import Store

DOCS = pathlib.Path("documents")
NAMES = ["Rowan Mercer", "Dana Okonjo", "Pilar Vance", "Noor Halabi",
         "Theo Brandt", "Imani Sealy", "Kaito Mori", "Lena Fischer",
         "Omar Haddad", "Sibyl Arnesen"]
ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
LONG = re.compile(r"\b(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December) (\d{1,2})\b")
SHORT = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{1,2})\b")
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _shift(iso: str, weeks: int) -> dt.date:
    return dt.date.fromisoformat(iso) + dt.timedelta(weeks=weeks)


def clone(text: str, i: int) -> str:
    """One synthetic patient: new identifiers, dates shifted by whole weeks."""
    weeks = 4 * i
    name = NAMES[i % len(NAMES)] if i < len(NAMES) else f"Patient Case{i:04d}"
    given = name.split()[0]
    out = text.replace("Rowan Mercer", name).replace("Rowan", given)
    out = out.replace("HG-M042", f"HG-M{1000 + i:04d}")
    out = re.sub(r"\bHG-E(\d+)\b", lambda m: f"HG-E{i}{m.group(1)}", out)
    out = re.sub(r"\bHG-A(\d+)\b", lambda m: f"HG-A{i}{m.group(1)}", out)
    out = re.sub(r"\bHG-Q(\d+)\b", lambda m: f"HG-Q{i}{m.group(1)}", out)
    out = re.sub(r"\bBH-D(\d+)\b", lambda m: f"BH-D{i}{m.group(1)}", out)
    out = re.sub(r"\bCH-(\d+)\b", lambda m: f"CH-{i}{m.group(1)}", out)
    # do not shift the date of birth
    out = out.replace("1991-04-12", "@@DOB@@")

    def iso_sub(m):
        d = _shift(m.group(0), weeks)
        return d.isoformat()

    def long_sub(m):
        mo = MONTHS.index(m.group(1)) + 1
        try:
            d = _shift(f"2026-{mo:02d}-{int(m.group(2)):02d}", weeks)
        except ValueError:
            return m.group(0)
        return f"{MONTHS[d.month - 1]} {d.day}"

    def short_sub(m):
        mo = [x[:3] for x in MONTHS].index(m.group(1)) + 1
        try:
            d = _shift(f"2026-{mo:02d}-{int(m.group(2)):02d}", weeks)
        except ValueError:
            return m.group(0)
        return f"{MONTHS[d.month - 1][:3]}{d.day:02d}"

    out = ISO.sub(iso_sub, out)
    out = LONG.sub(long_sub, out)
    out = SHORT.sub(short_sub, out)
    return out.replace("@@DOB@@", "1991-04-12")


def materialise(n: int, root: pathlib.Path) -> pathlib.Path:
    d = root / f"patients_{n}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    src = sorted(DOCS.glob("*.txt"))
    for i in range(n):
        for f in src:
            text = f.read_text(encoding="utf-8") if i == 0 else \
                clone(f.read_text(encoding="utf-8"), i)
            (d / f"p{i:04d}_{f.name}").write_text(text, encoding="utf-8")
    return d


def measure(n: int, root: pathlib.Path, repeat: int = 3) -> dict:
    corpus = materialise(n, root)
    db = root / f"scale_{n}.db"
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db) + s)
        if p.exists():
            p.unlink()
    st = Store(db)
    t0 = time.perf_counter()
    rep = ingest_dir(st, corpus)
    t1 = time.perf_counter()
    stats = reconcile.reconcile_all(st)
    t2 = time.perf_counter()
    mrns = st.patients()
    one = mrns[0]
    start, end = Q.episode_window(st, one)

    def t(fn):
        runs = []
        for _ in range(repeat):
            a = time.perf_counter()
            fn()
            runs.append((time.perf_counter() - a) * 1000)
        return round(statistics.median(runs), 2)

    out = {
        "patients": n,
        "documents": rep["files"],
        "claims": rep["claims"],
        "events": sum(s["events"] for s in stats.values()),
        "ingest_s": round(t1 - t0, 3),
        "reconcile_s": round(t2 - t1, 3),
        "ms_per_document_ingest": round((t1 - t0) * 1000 / max(1, rep["ingested"]), 3),
        "ms_per_patient_reconcile": round((t2 - t1) * 1000 / max(1, len(mrns)), 3),
        "one_patient_compliance_ms": t(lambda: Q.compliance(st, one, start, end)),
        "one_patient_minutes_ms": t(lambda: Q.minutes(st, one, start, end)),
        "collection_consecutive_below_ms": t(lambda: Q.consecutive_below(st, None, 2)),
        "integrity_all_ms": t(lambda: Q.integrity(st)),
    }
    st.close()
    out["sqlite_bytes"] = sum(
        pathlib.Path(str(db) + s).stat().st_size for s in ("", "-wal", "-shm")
        if pathlib.Path(str(db) + s).exists())
    out["bytes_per_document"] = round(out["sqlite_bytes"] / max(1, out["documents"]))
    shutil.rmtree(corpus)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients", type=int, nargs="+", default=[1, 10, 50, 100])
    ap.add_argument("--out", default="out/scaling.json")
    ap.add_argument("--root", default="out/scale")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    root.mkdir(parents=True, exist_ok=True)
    rows = [measure(n, root, a.repeat) for n in a.patients]
    pathlib.Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    hdr = ("patients", "documents", "claims", "events", "ingest_s", "reconcile_s",
           "ms_per_document_ingest", "ms_per_patient_reconcile",
           "one_patient_compliance_ms", "collection_consecutive_below_ms",
           "integrity_all_ms", "bytes_per_document")
    print(" | ".join(h.replace("_", " ") for h in hdr))
    print("-|-".join("-" * len(h) for h in hdr))
    for r in rows:
        print(" | ".join(str(r[h]) for h in hdr))
    print(f"\nwrote {a.out}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
