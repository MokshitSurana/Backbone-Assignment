"""SQLite access layer. Thin on purpose: all policy lives in reconcile/queries."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import datetime as dt

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        # Order matters: tables, then column migrations, then indexes. An index
        # on a column added after the first release cannot be created on an
        # older database until the column exists, and `executescript` would
        # abort the whole script on that error.
        script = SCHEMA.read_text(encoding="utf-8")
        statements = [x.strip() for x in script.split(";") if x.strip()]
        indexes = [x for x in statements if x.upper().startswith("CREATE INDEX")]
        for stmt in statements:
            if stmt in indexes:
                continue
            self.db.execute(stmt)
        self._migrate()
        for stmt in indexes:
            self.db.execute(stmt)
        self.db.commit()
        # Decisions are stamped with the patient being reconciled, so that a
        # decision which is not tied to one session (an unmatched claim, a
        # measure merge, a plan row) can still be filtered and cleared per
        # patient. Set by reconcile_patient and by ingest.
        self._patient_ctx: str | None = None

    #: columns added after the first release; CREATE TABLE IF NOT EXISTS will
    #: not add them to a database that already exists.
    MIGRATIONS = (
        ("decisions", "patient_mrn", "TEXT"),
        ("measures", "disputed", "INTEGER NOT NULL DEFAULT 0"),
        ("measures", "total_conflicts", "TEXT"),
    )

    def _migrate(self) -> None:
        for table, column, decl in self.MIGRATIONS:
            cols = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.db.commit()

    def set_patient_context(self, mrn: str | None) -> None:
        self._patient_ctx = mrn

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.db.commit()
        # Fold the write-ahead log back into the main file so the abstraction is
        # one portable artifact and its reported size is its real size.
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def commit(self):
        self.db.commit()

    def log_run(self, command: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO run_log(started_at, command, detail) VALUES (?,?,?)",
            (now(), command, detail))

    # -- generic helpers ---------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def ex(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    # -- documents ---------------------------------------------------------
    def doc_by_norm(self, sha_norm: str):
        return self.q1("SELECT * FROM documents WHERE sha256_norm=?", (sha_norm,))

    def doc_by_path(self, path: str):
        return self.q1("SELECT * FROM documents WHERE path=?", (path,))

    def upsert_document(self, d: dict) -> None:
        cols = ",".join(d)
        marks = ",".join("?" * len(d))
        self.db.execute(
            f"INSERT OR REPLACE INTO documents({cols}) VALUES ({marks})",
            tuple(d.values()))

    def add_alias(self, path: str, doc_pk: str, kind: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO document_aliases(path, doc_pk, kind, seen_at)"
            " VALUES (?,?,?,?)", (path, doc_pk, kind, now()))

    def drop_doc_derivations(self, doc_pk: str) -> None:
        """Remove everything derived from a document so it can be re-extracted."""
        cids = [r["claim_id"] for r in
                self.q("SELECT claim_id FROM claims WHERE doc_pk=?", (doc_pk,))]
        for cid in cids:
            self.db.execute("DELETE FROM corrections WHERE claim_id=?", (cid,))
            self.db.execute("DELETE FROM event_claims WHERE claim_id=?", (cid,))
            self.db.execute("DELETE FROM observations WHERE claim_id=?", (cid,))
            self.db.execute("DELETE FROM plan_requirements WHERE claim_id=?", (cid,))
            self.db.execute("DELETE FROM episodes WHERE claim_id=?", (cid,))
        self.db.execute("DELETE FROM claims WHERE doc_pk=?", (doc_pk,))

    def drop_document(self, doc_pk: str) -> None:
        """Forget a document entirely: its claims, derivations and own row.

        Used when a file is edited in place, so the superseded version's claims
        do not sit alongside the new ones.
        """
        self.drop_doc_derivations(doc_pk)
        self.db.execute("DELETE FROM document_aliases WHERE doc_pk=?", (doc_pk,))
        self.db.execute("DELETE FROM documents WHERE doc_pk=?", (doc_pk,))

    # -- claims ------------------------------------------------------------
    def insert_claim(self, c: dict) -> None:
        cols = ",".join(c)
        marks = ",".join("?" * len(c))
        self.db.execute(
            f"INSERT OR REPLACE INTO claims({cols}) VALUES ({marks})",
            tuple(c.values()))
        # A correction is a relationship between records, so it also gets a row
        # of its own: the encounter and field it replaces, and the values. The
        # table existed but nothing wrote to it, which made the relationship
        # invisible to anyone reading the database directly.
        if c.get("claim_type") == "correction":
            f = json.loads(c.get("fields") or "{}")
            if f.get("target_field"):
                self.db.execute(
                    "INSERT OR REPLACE INTO corrections(claim_id,target_enc,"
                    "target_field,old_value,new_value) VALUES (?,?,?,?,?)",
                    (c["claim_id"], c.get("encounter_ref"),
                     f["target_field"], f.get("old_value"),
                     str(f.get("new_value"))))

    def corrections(self, mrn: str | None = None):
        sql = ("SELECT r.*, c.patient_mrn, c.quote, c.quote_start, c.quote_end,"
               " d.doc_id, d.filename FROM corrections r"
               " JOIN claims c ON c.claim_id=r.claim_id"
               " JOIN documents d ON d.doc_pk=c.doc_pk")
        if mrn:
            return self.q(sql + " WHERE c.patient_mrn=?", (mrn,))
        return self.q(sql)

    def claims_for_patient(self, mrn: str) -> list[sqlite3.Row]:
        return self.q(
            "SELECT c.*, d.doc_id, d.filename, d.doc_class, d.received_at, d.authored_at"
            " FROM claims c JOIN documents d ON d.doc_pk=c.doc_pk"
            " WHERE c.patient_mrn=? ORDER BY c.service_date, c.claim_id", (mrn,))

    def patients(self) -> list[str]:
        return [r["mrn"] for r in self.q("SELECT mrn FROM patients ORDER BY mrn")]

    def upsert_patient(self, mrn: str, name: str | None, given: str | None, dob: str | None):
        row = self.q1("SELECT * FROM patients WHERE mrn=?", (mrn,))
        if row:
            self.db.execute(
                "UPDATE patients SET name=COALESCE(?,name), given_name=COALESCE(?,given_name),"
                " dob=COALESCE(?,dob) WHERE mrn=?", (name, given, dob, mrn))
        else:
            self.db.execute(
                "INSERT INTO patients(mrn,name,given_name,dob) VALUES (?,?,?,?)",
                (mrn, name, given, dob))

    # -- events ------------------------------------------------------------
    def clear_patient_events(self, mrn: str) -> None:
        """Remove everything reconciliation derived for one patient.

        This includes the decisions that are not tied to a session -- unmatched
        claims, measure merges, plan rows. An earlier version tried to delete
        them with `from_claims IN (SELECT '[]' WHERE 0)`, a subquery that
        returns no rows and therefore matched nothing, so every rebuild or
        `add` appended another copy and the exclusion lists in answers grew.
        """
        ids = [r["event_id"] for r in
               self.q("SELECT event_id FROM events WHERE patient_mrn=?", (mrn,))]
        for eid in ids:
            self.db.execute("DELETE FROM event_claims WHERE event_id=?", (eid,))
        self.db.execute("DELETE FROM events WHERE patient_mrn=?", (mrn,))
        # event-tied and patient-tied decisions, by either route
        self.db.execute("DELETE FROM decisions WHERE patient_mrn=?", (mrn,))
        self.db.execute("DELETE FROM decisions WHERE event_id LIKE ?",
                        (f"{mrn}|%",))
        self.db.execute("DELETE FROM measures WHERE patient_mrn=?", (mrn,))
        self.db.execute("DELETE FROM observations WHERE patient_mrn=?", (mrn,))
        self.db.execute("DELETE FROM plan_requirements WHERE patient_mrn=?",
                        (mrn,))

    def insert_event(self, e: dict) -> None:
        cols = ",".join(e)
        marks = ",".join("?" * len(e))
        self.db.execute(
            f"INSERT OR REPLACE INTO events({cols}) VALUES ({marks})",
            tuple(e.values()))

    def link_claim(self, event_id: str, claim_id: str, role: str, rule: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO event_claims(event_id,claim_id,role,match_rule)"
            " VALUES (?,?,?,?)", (event_id, claim_id, role, rule))

    def decide(self, event_id: str | None, scope: str, field: str, chosen,
               rule_id: str, rationale: str, from_claims: list[str],
               mrn: str | None = None) -> None:
        if mrn is None and event_id and "|" in event_id:
            mrn = event_id.split("|", 1)[0]
        self.db.execute(
            "INSERT INTO decisions(event_id,patient_mrn,scope,field,chosen,"
            "rule_id,rationale,from_claims) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, mrn if mrn is not None else self._patient_ctx,
             scope, field,
             json.dumps(chosen) if not isinstance(chosen, str) else chosen,
             rule_id, rationale, json.dumps(from_claims)))

    def events(self, mrn: str, start: str | None = None, end: str | None = None):
        sql = "SELECT * FROM events WHERE patient_mrn=?"
        args: list = [mrn]
        if start:
            sql += " AND service_date>=?"
            args.append(start)
        if end:
            sql += " AND service_date<=?"
            args.append(end)
        sql += " ORDER BY service_date, event_id"
        return self.q(sql, tuple(args))

    def event_evidence(self, event_id: str):
        return self.q(
            "SELECT ec.role, ec.match_rule, c.*, d.doc_id, d.filename, d.doc_class,"
            "       d.received_at, d.authored_at, d.path"
            " FROM event_claims ec JOIN claims c ON c.claim_id=ec.claim_id"
            " JOIN documents d ON d.doc_pk=c.doc_pk WHERE ec.event_id=?"
            " ORDER BY ec.role, c.claim_id", (event_id,))

    def event_decisions(self, event_id: str):
        return self.q("SELECT * FROM decisions WHERE event_id=? ORDER BY decision_id",
                      (event_id,))

    # -- plan / measures / observations ------------------------------------
    def insert_requirement(self, r: dict) -> None:
        cols = ",".join(r)
        marks = ",".join("?" * len(r))
        self.db.execute(
            f"INSERT OR REPLACE INTO plan_requirements({cols}) VALUES ({marks})",
            tuple(r.values()))

    def requirements(self, mrn: str):
        return self.q(
            "SELECT * FROM plan_requirements WHERE patient_mrn=?"
            " ORDER BY effective_start, metric", (mrn,))

    def upsert_measure(self, m: dict) -> tuple[bool, list]:
        """Merge a measure claim onto its administration.

        Returns (conflict, totals). Two records of the same administration that
        state different scores are a documentation conflict: both are kept and
        the measure is flagged, rather than the first one silently winning.
        """
        old = self.q1("SELECT * FROM measures WHERE measure_id=?",
                      (m["measure_id"],))
        if not old:
            cols = ",".join(m)
            marks = ",".join("?" * len(m))
            self.db.execute(f"INSERT INTO measures({cols}) VALUES ({marks})",
                            tuple(m.values()))
            return False, [m.get("total")] if m.get("total") is not None else []

        ids = sorted(set(json.loads(old["claim_ids"])
                         + json.loads(m["claim_ids"])))
        known = json.loads(old["total_conflicts"] or "[]") if \
            "total_conflicts" in old.keys() else []
        if old["total"] is not None and not known:
            known = [{"total": old["total"],
                      "claim_ids": json.loads(old["claim_ids"])}]
        new_total = m.get("total")
        conflict = (new_total is not None and old["total"] is not None
                    and float(new_total) != float(old["total"]))
        if conflict:
            known.append({"total": float(new_total),
                          "claim_ids": json.loads(m["claim_ids"])})
        self.db.execute(
            "UPDATE measures SET claim_ids=?, total=COALESCE(total,?),"
            " items=COALESCE(items,?), disputed=?, total_conflicts=?"
            " WHERE measure_id=?",
            (json.dumps(ids), new_total, m.get("items"),
             1 if (conflict or old["disputed"]) else 0,
             json.dumps(known) if (conflict or old["disputed"]) else None,
             m["measure_id"]))
        return conflict, [k["total"] for k in known]

    def measures(self, mrn: str):
        return self.q(
            "SELECT * FROM measures WHERE patient_mrn=? ORDER BY completed_at", (mrn,))

    def insert_observation(self, o: dict) -> None:
        cols = ",".join(o)
        marks = ",".join("?" * len(o))
        self.db.execute(
            f"INSERT OR REPLACE INTO observations({cols}) VALUES ({marks})",
            tuple(o.values()))

    def observations(self, mrn: str, start: str | None = None, end: str | None = None):
        sql = "SELECT o.*, d.doc_id, d.filename, c.service_type FROM observations o" \
              " JOIN claims c ON c.claim_id=o.claim_id" \
              " JOIN documents d ON d.doc_pk=c.doc_pk WHERE o.patient_mrn=?"
        args: list = [mrn]
        if start:
            sql += " AND o.obs_date>=?"
            args.append(start)
        if end:
            sql += " AND o.obs_date<=?"
            args.append(end)
        sql += " ORDER BY o.obs_date, o.obs_id"
        return self.q(sql, tuple(args))

    def episode(self, mrn: str):
        return self.q1("SELECT * FROM episodes WHERE patient_mrn=? ORDER BY start_date", (mrn,))
