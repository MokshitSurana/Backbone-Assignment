"""Document ingestion.

Idempotent by construction: identity is the hash of the *normalized* text, so a
second copy of a record -- re-sent, re-stamped, re-scanned, differently named --
lands on the same doc_pk and contributes the same single set of claims.

Content-identical duplicates are the cheap half of the problem. The real
safeguard is that two *near* copies both produce claims that reconcile into one
event (see reconcile.RULE-MATCH-01).
"""
from __future__ import annotations

import json
import pathlib
import time

from . import normalize as nz
from .extract import rules
from .store import Store, now
from .taxonomy import EXTRACTOR_VERSION


EXTRACTORS = ("rules",)


def _extractor(name: str):
    if name == "rules":
        return rules
    raise SystemExit(f"unknown extractor {name!r} (use: {', '.join(EXTRACTORS)})")


def ingest_path(st: Store, path: pathlib.Path, extractor: str = "rules",
                force: bool = False) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    sha_raw, sha_norm = nz.hashes(raw)
    doc_pk = sha_norm

    # A file edited in place hashes differently, so it would otherwise be
    # ingested as a second document while the old row and its claims remained.
    prior = st.doc_by_path(str(path))
    if prior and prior["doc_pk"] != doc_pk:
        st.drop_document(prior["doc_pk"])
        st.decide(None, "dedupe", "replaced", str(path), "RULE-DEDUPE-03",
                  f"{path.name} changed on disk (normalized sha256 "
                  f"{prior['sha256_norm'][:12]} -> {sha_norm[:12]}); the "
                  f"previous version's claims were removed",
                  [], prior["patient_mrn"] if "patient_mrn" in prior.keys()
                  else None)

    existing = st.doc_by_norm(sha_norm)
    if existing and not force:
        same_file = existing["path"] == str(path)
        if not same_file:
            kind = ("exact_duplicate" if existing["sha256_raw"] == sha_raw
                    else "normalized_duplicate")
            st.add_alias(str(path), doc_pk, kind)
            st.decide(None, "dedupe", "duplicate", str(path), "RULE-DEDUPE-01",
                      f"{kind} of {existing['filename']} "
                      f"(normalized sha256 {sha_norm[:12]}); no new claims created",
                      [])
            st.commit()
            return {"status": kind, "doc_pk": doc_pk, "claims": 0,
                    "duplicate_of": existing["filename"]}
        if existing["extractor"] == extractor and \
                existing["extractor_ver"] == EXTRACTOR_VERSION:
            return {"status": "unchanged", "doc_pk": doc_pk, "claims": 0}

    mod = _extractor(extractor)
    t0 = time.perf_counter()
    facts, claims = mod.extract(raw, doc_pk, st)
    elapsed = time.perf_counter() - t0

    st.drop_doc_derivations(doc_pk)
    st.upsert_document({
        "doc_pk": doc_pk, "doc_id": facts.doc_id, "path": str(path),
        "filename": path.name, "doc_class": facts.doc_class,
        "authority_class": facts.doc_class, "sha256_raw": sha_raw,
        "sha256_norm": sha_norm, "char_len": len(raw),
        "received_at": facts.received_at, "authored_at": facts.authored_at,
        "ingested_at": now(), "extractor": extractor,
        "extractor_ver": EXTRACTOR_VERSION,
    })
    if facts.patient_mrn:
        st.upsert_patient(facts.patient_mrn, facts.patient_name,
                          facts.given_name, facts.dob)
    st.set_patient_context(facts.patient_mrn)
    bad = 0
    for c in claims:
        c.doc_pk = doc_pk
        if not c.patient_mrn:
            c.patient_mrn = facts.patient_mrn
        if not c.verify(raw):
            bad += 1
        st.insert_claim(c.row())
    if bad:
        st.decide(None, "dedupe", "quote_verification", str(bad), "RULE-QUOTE-01",
                  f"{bad} claim(s) in {path.name} carry a quote that does not match "
                  f"the stored offsets; they are kept but flagged", [])
    st.set_patient_context(None)
    st.commit()
    return {"status": "ingested", "doc_pk": doc_pk, "claims": len(claims),
            "replaced": bool(prior and prior["doc_pk"] != doc_pk),
            "unverified_quotes": bad, "seconds": round(elapsed, 4),
            "doc_class": facts.doc_class, "mrn": facts.patient_mrn}


#: Text-bearing extensions picked up by a directory ingest. A records drop is
#: not reliably all-.txt, and silently ignoring a file is worse than failing on
#: it, so the default is a set rather than one glob.
TEXT_SUFFIXES = (".txt", ".text", ".md", ".rtf", ".csv", ".tsv", ".dat", ".log",
                 ".note", ".asc", "")


def ingest_dir(st: Store, directory: pathlib.Path, extractor: str = "rules",
               force: bool = False, pattern: str | None = None) -> dict:
    if pattern:
        files, skipped = sorted(directory.rglob(pattern)), []
    else:
        # One walk, partitioned: a second rglob plus an O(n^2) membership test
        # would be the wrong shape at corpus scale.
        files, skip = [], []
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            (files if f.suffix.lower() in TEXT_SUFFIXES else skip).append(f)
        files.sort()
        skipped = sorted(f.name for f in skip)
    report = {"files": len(files), "ingested": 0, "duplicates": 0,
              "unchanged": 0, "replaced": 0, "claims": 0, "per_doc": [],
              "unverified_quotes": 0, "skipped_files": skipped}
    for f in files:
        r = ingest_path(st, f, extractor=extractor, force=force)
        r["file"] = f.name
        report["per_doc"].append(r)
        if r["status"] == "ingested":
            report["ingested"] += 1
            report["claims"] += r["claims"]
            report["unverified_quotes"] += r.get("unverified_quotes", 0)
        elif r["status"] == "unchanged":
            report["unchanged"] += 1
        else:
            report["duplicates"] += 1
        if r.get("replaced"):
            report["replaced"] += 1
    st.log_run("ingest", json.dumps({k: v for k, v in report.items()
                                     if k != "per_doc"}))
    st.commit()
    return report
