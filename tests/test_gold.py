"""Regression tests against a hand-computed ledger.

The expected values in this file were worked out by reading all 31 documents by
hand before any code was written. They live here, in the tests, and are never
imported by the pipeline -- the pipeline has to derive them from the documents.

If a graded follow-up adds documents or a second patient, these tests should
still pass unchanged for Rowan, because nothing here is keyed to a document
count or to the order files happen to be read in.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from backbone import queries as Q
from backbone import reconcile
from backbone.ingest import ingest_dir, ingest_path
from backbone.store import Store

DOCS = pathlib.Path("documents")
MRN = "HG-M042"

# date -> (service type, minutes_min, minutes_max)
GOLD_LEDGER = {
    ("2026-01-05", "HG-E101"): ("individual_therapy", 50, 50),
    ("2026-01-06", "HG-E102"): ("group_therapy", 45, 45),     # 10:15-11:15 less 15 break
    ("2026-01-09", "HG-E104"): ("family_therapy", 45, 45),
    ("2026-01-12", "HG-E105"): ("group_therapy", 75, 75),     # 10:00-11:30 less 15 break
    ("2026-01-14", "HG-E107"): ("individual_therapy", 45, 45),
    ("2026-01-19", "HG-E110"): ("group_therapy", 60, 60),     # corrected 11:15, less break
    ("2026-01-19", "HG-E111"): ("individual_therapy", 30, 30),
    ("2026-01-21", "HG-E112"): ("individual_therapy", 45, 45),  # 20 + 25, drop lost link
    ("2026-01-22", "HG-E113"): ("group_therapy", 45, 45),     # 10:30-11:30 less 15 break
    ("2026-01-26", "HG-E115"): ("individual_therapy", 40, 50),  # unresolved conflict
    ("2026-01-29", "HG-E118"): ("group_therapy", 75, 75),
    ("2026-01-30", "HG-E119"): ("family_therapy", 30, 30),    # patient present 13:15-13:45
}

# encounters that must NOT contribute therapy, and why
GOLD_EXCLUDED = {
    "HG-E103": "no show",
    "HG-E106": "medication management",
    "HG-E108": "clinic cancelled",
    "HG-E109": "collateral, patient absent",
    "HG-E114": "care coordination, no patient",
    "HG-E116": "no show (draft note and posted charge notwithstanding)",
    "HG-E117": "patient cancelled",
    "HG-E120": "medication management",
}

GOLD_WEEKS = {
    "2026-01-05": {"days": 3, "min": 140, "max": 140, "status": "NOT_MET"},
    "2026-01-12": {"days": 2, "min": 120, "max": 120, "status": "NOT_MET"},
    "2026-01-19": {"days": 3, "min": 180, "max": 180, "status": "MET"},
    "2026-01-26": {"days": 3, "min": 145, "max": 155, "status": "CANNOT_DETERMINE"},
}


def build(tmp: str = "out/test.db") -> Store:
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(tmp + s)
        if p.exists():
            p.unlink()
    st = Store(tmp)
    ingest_dir(st, DOCS)
    reconcile.reconcile_all(st)
    return st


class GoldLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = build()

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def test_patient_discovered_from_documents(self):
        self.assertEqual(self.st.patients(), [MRN])

    def test_episode_and_plan_read_from_the_record(self):
        self.assertEqual(Q.episode_window(self.st, MRN), ("2026-01-05", "2026-01-30"))
        reqs = {r["metric"]: r for r in self.st.requirements(MRN)}
        self.assertEqual(reqs["therapy_days"]["threshold"], 3)
        self.assertEqual(reqs["therapy_minutes"]["threshold"], 150)
        self.assertEqual(reqs["therapy_minutes"]["period"], "week_mon_sun")
        self.assertEqual(sorted(json.loads(reqs["therapy_minutes"]["service_types"])),
                         ["family_therapy", "group_therapy", "individual_therapy"])

    def test_every_counted_event_matches_the_hand_ledger(self):
        got = {}
        for e in self.st.events(MRN):
            if e["counts_as_therapy"]:
                got[(e["service_date"], e["encounter_ref"])] = (
                    e["service_type"], e["min_minutes"], e["max_minutes"])
        self.assertEqual(got, GOLD_LEDGER)

    def test_excluded_encounters_are_excluded_with_a_reason(self):
        for enc, why in GOLD_EXCLUDED.items():
            row = self.st.q1("SELECT * FROM events WHERE patient_mrn=? AND "
                             "encounter_ref=?", (MRN, enc))
            self.assertIsNotNone(row, f"{enc} missing from the abstraction")
            self.assertEqual(row["counts_as_therapy"], 0, f"{enc} ({why})")
            self.assertTrue(row["exclusion_reason"], f"{enc} has no stated reason")

    def test_session_counts(self):
        v = Q.session_counts(self.st, MRN, "2026-01-05", "2026-01-30")["value"]
        self.assertEqual(v["sessions"], 12)
        self.assertEqual(v["distinct_days"], 11)
        self.assertEqual(v["by_service_type"],
                         {"family_therapy": 2, "group_therapy": 5,
                          "individual_therapy": 5})

    def test_total_minutes_and_hours(self):
        v = Q.minutes(self.st, MRN, "2026-01-05", "2026-01-30")["value"]
        self.assertEqual(v["total_minutes"], [585, 595])
        self.assertEqual(v["total_hours"], [9.75, 9.92])

    def test_weekly_minutes_days_and_compliance(self):
        comp = Q.compliance(self.st, MRN, "2026-01-05", "2026-01-30")["value"]
        got = {w["week_start"]: w for w in comp["weeks"]}
        self.assertEqual(sorted(got), sorted(GOLD_WEEKS))
        for wk, exp in GOLD_WEEKS.items():
            w = got[wk]
            self.assertEqual(w["minutes"], [exp["min"], exp["max"]], wk)
            self.assertEqual(w["therapy_days"], [exp["days"], exp["days"]], wk)
            self.assertEqual(w["status"], exp["status"], wk)

    def test_jan19_and_jan21_reconstruction(self):
        v = Q.day_detail(self.st, MRN, ["2026-01-19", "2026-01-21"])["value"]
        d19, d21 = v[0], v[1]
        self.assertEqual(d19["therapy_contacts"], 2)
        self.assertEqual(d19["patient_therapy_minutes"], [90, 90])
        self.assertTrue(d19["wall_clock_check"]["ok"])
        self.assertEqual(d21["therapy_contacts"], 1)
        self.assertEqual(d21["patient_therapy_minutes"], [45, 45])

    def test_three_distinct_phq9_administrations(self):
        ms = Q.progress(self.st, MRN)["value"]["distinct_measures"]
        self.assertEqual([(m["completed_at"], m["total"]) for m in ms],
                         [("2026-01-05", 18.0), ("2026-01-16", 14.0),
                          ("2026-01-30", 10.0)])
        jan16 = [m for m in ms if m["completed_at"] == "2026-01-16"][0]
        # the measurement review and the January 26 administrative re-import are
        # two records of ONE administration
        self.assertGreaterEqual(len(jan16["claim_ids"]), 2)

    def test_no_severity_label_is_invented(self):
        out = json.dumps(Q.progress(self.st, MRN)).lower()
        for word in ("moderately severe", "severe depression", "mild depression",
                     "moderate depression"):
            self.assertNotIn(word, out)

    def test_telehealth_rejoin_is_one_event_not_two(self):
        rows = self.st.q("SELECT * FROM events WHERE patient_mrn=? AND "
                         "service_date='2026-01-21'", (MRN,))
        self.assertEqual(len(rows), 1)
        call_claims = self.st.q(
            "SELECT c.claim_id FROM event_claims ec JOIN claims c "
            "ON c.claim_id=ec.claim_id WHERE ec.event_id=? AND c.call_ref IS NOT NULL",
            (rows[0]["event_id"],))
        self.assertEqual(len(call_claims), 2, "both call legs should attach here")

    def test_every_quote_matches_its_offsets(self):
        bad = self.st.q1("SELECT COUNT(*) n FROM claims WHERE quote_verified=0")["n"]
        self.assertEqual(bad, 0)
        for c in self.st.q("SELECT c.quote, c.quote_start, c.quote_end, d.path "
                           "FROM claims c JOIN documents d ON d.doc_pk=c.doc_pk"):
            raw = pathlib.Path(c["path"]).read_text(encoding="utf-8")
            self.assertEqual(raw[c["quote_start"]:c["quote_end"]], c["quote"])

    def test_integrity_passes(self):
        self.assertTrue(Q.integrity(self.st)["value"]["ok"])

    def test_every_counted_event_has_a_decision_trail(self):
        for e in self.st.events(MRN):
            decs = self.st.event_decisions(e["event_id"])
            self.assertTrue(decs, e["event_id"])
            fields = {d["field"] for d in decs}
            self.assertIn("occurred", fields, e["event_id"])
            self.assertIn("counts_as_therapy", fields, e["event_id"])


class Idempotence(unittest.TestCase):
    """A duplicate document must not change a single clinical number."""

    def test_duplicate_file_changes_nothing(self):
        st = build("out/test_dup.db")
        before = Q.minutes(st, MRN, "2026-01-05", "2026-01-30")["value"]
        bcount = Q.session_counts(st, MRN, "2026-01-05", "2026-01-30")["value"]
        tmp = pathlib.Path("out/dup")
        tmp.mkdir(parents=True, exist_ok=True)
        # exact copy
        src = DOCS / "BH-D102_original_attendance_2026-01-19.txt"
        copy1 = tmp / "copy_of_roster.txt"
        copy1.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        # near copy: new fax/receipt furniture and different whitespace
        copy2 = tmp / "faxed_roster.txt"
        copy2.write_text("Received into chart: 2026-02-02, 09:00\n"
                         + src.read_text(encoding="utf-8").replace("  ", " "),
                         encoding="utf-8")
        for f in (copy1, copy2):
            ingest_path(st, f)
        reconcile.reconcile_all(st)
        after = Q.minutes(st, MRN, "2026-01-05", "2026-01-30")["value"]
        acount = Q.session_counts(st, MRN, "2026-01-05", "2026-01-30")["value"]
        self.assertEqual(before["total_minutes"], after["total_minutes"])
        self.assertEqual(bcount["sessions"], acount["sessions"])
        self.assertEqual(bcount["distinct_days"], acount["distinct_days"])
        dups = Q.duplicates(st)["value"]
        self.assertEqual(len(dups), 2, dups)
        st.close()
        for f in (copy1, copy2):
            f.unlink()


class Restart(unittest.TestCase):
    """New questions must be answerable from the saved file, with no reprocessing."""

    def test_answer_after_restart_without_reingest(self):
        st = build("out/test_restart.db")
        st.close()
        st2 = Store("out/test_restart.db")
        v = Q.compliance(st2, MRN, "2026-01-05", "2026-01-30")["value"]
        self.assertEqual(len(v["weeks"]), 4)
        from backbone import router
        res = router.answer(st2, "How many group therapy sessions did Rowan attend?")
        self.assertEqual(res["result"]["value"]["by_service_type"]["group_therapy"], 5)
        st2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LedgerConsistency(unittest.TestCase):
    """The hand-computed ledger exists twice: as assertions in this module, and
    as data in tests/gold_ledger.json. They must not drift apart."""

    def test_gold_ledger_json_matches_this_module(self):
        data = json.loads(pathlib.Path("tests/gold_ledger.json")
                          .read_text(encoding="utf-8"))
        from_json = {
            k: (v["service_type"], v["min_minutes"], v["max_minutes"])
            for k, v in data["events"].items() if v["counts_as_therapy"]}
        from_py = {f"{d}|{e}": v for (d, e), v in GOLD_LEDGER.items()}
        self.assertEqual(from_json, from_py)
        self.assertEqual({k: tuple(v) for k, v in data["weeks"].items()},
                         {k: (v["min"], v["max"]) for k, v in GOLD_WEEKS.items()})

