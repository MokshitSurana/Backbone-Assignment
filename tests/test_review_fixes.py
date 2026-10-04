"""Regressions for the second review round.

Each test names the defect it pins. Several use small synthetic corpora, because
the supplied documents are structurally incapable of exhibiting the bug -- which
is exactly why these survived the first pass.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

from backbone import intervals as iv
from backbone import queries as Q
from backbone import reconcile, render, router
from backbone.ingest import ingest_dir, ingest_path
from backbone.store import Store

HDR = "Harbor Grove Behavioral Health"


def _corpus(docs: dict[str, str], db: str):
    """Build an abstraction from synthetic documents."""
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(db + s)
        if p.exists():
            p.unlink()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bb_rev_"))
    for name, text in docs.items():
        (tmp / name).write_text(text, encoding="utf-8")
    st = Store(db)
    ingest_dir(st, tmp)
    reconcile.reconcile_all(st)
    return st, tmp


class DisputedAttendanceBounds(unittest.TestCase):
    """#2 An unresolved attendance must raise the upper bound, not store 0-0.

    Without this, a week hinging on a disputed attendance is reported NOT_MET
    instead of CANNOT_DETERMINE, and the patient can never appear in
    "inclusion depends on unresolved documentation".
    """

    DOCS = {
        "plan.txt": (
            f"Document ID: RV-P1\n{HDR} | Outpatient treatment plan\n"
            "Wren Calloway | DOB 1990-01-01 | MRN HG-M601\n"
            "Episode dates: 2026-04-06 through 2026-04-19\n"
            "signed 2026-04-06\n"
            # 60 minutes is deliberately below the disputed session's 90, so
            # the week genuinely turns on whether that session happened. (With
            # a threshold above 90 the week fails either way and NOT_MET would
            # be the correct answer.)
            "Local treatment participation goal: at least 1 therapy days and at "
            "least 60 minutes of patient-present therapy in each Monday-Sunday "
            "week. Patient-present individual, group, and family therapy "
            "contribute to the minute goal. Medication management and care "
            "coordination do not contribute.\n"),
        # week 1: a signed note says the session happened...
        "note.txt": (
            f"Document ID: RV-N1\n{HDR} | Individual psychotherapy\n"
            "Wren Calloway | DOB 1990-01-01 | MRN HG-M601 | Encounter HG-E601\n"
            "Service date 2026-04-07\n"
            "Patient contact: 09:00-10:30 | Completed: 90 minutes\n"),
        # ...and the signed register says it did not
        "register.txt": (
            f"Document ID: RV-R1\n{HDR} | Outpatient attendance and appointment "
            "disposition extract\n"
            "Wren Calloway | DOB 1990-01-01 | MRN HG-M601\n\n"
            "Service date | Encounter | Service | Scheduled | Actual arrival | "
            "Actual departure | Final disposition\n"
            "2026-04-07 | HG-E601 | Individual | 09:00-10:30 | - | - | "
            "No show; patient did not attend\n"),
        # week 2 clears the goal outright
        "note2.txt": (
            f"Document ID: RV-N2\n{HDR} | Individual psychotherapy\n"
            "Wren Calloway | DOB 1990-01-01 | MRN HG-M601 | Encounter HG-E602\n"
            "Service date 2026-04-14\n"
            "Patient contact: 09:00-11:00 | Completed: 120 minutes\n"),
    }

    @classmethod
    def setUpClass(cls):
        cls.st, cls.tmp = _corpus(cls.DOCS, "out/test_disputed.db")

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_attendance_is_recorded_as_unresolved(self):
        row = self.st.q1("SELECT * FROM events WHERE encounter_ref='HG-E601'")
        self.assertEqual(row["occurred"], "unknown")
        self.assertEqual(row["disputed"], 1)

    def test_upper_bound_carries_the_minutes_the_note_describes(self):
        row = self.st.q1("SELECT * FROM events WHERE encounter_ref='HG-E601'")
        self.assertEqual(row["min_minutes"], 0)
        self.assertEqual(row["max_minutes"], 90,
                         "an unresolved attendance must still bound the maximum")

    def test_week_is_undetermined_not_failed(self):
        comp = Q.compliance(self.st, "HG-M601", "2026-04-06", "2026-04-19")
        got = {w["week_start"]: w["status"] for w in comp["value"]["weeks"]}
        self.assertEqual(got["2026-04-06"], "CANNOT_DETERMINE")
        self.assertEqual(got["2026-04-13"], "MET")

    def test_patient_lands_in_depends_on_documentation(self):
        run = Q.consecutive_below(self.st, "HG-M601", 1)["value"]
        self.assertTrue(run["depends_on_documentation"],
                        "a week turning on a disputed attendance must put the "
                        "patient in the contingent group")
        self.assertFalse(run["included"])


class SingleMetricPlan(unittest.TestCase):
    """#3 The compliance renderer must not assume two metrics."""

    DOCS = {
        "plan.txt": (
            f"Document ID: RV-P2\n{HDR} | Outpatient treatment plan\n"
            "Ira Nkemdirim | DOB 1985-05-05 | MRN HG-M602\n"
            "Episode dates: 2026-05-04 through 2026-05-10\n"
            "signed 2026-05-04\n"
            "Participation goal: at least 2.5 hours of patient-present therapy "
            "in each Monday-Sunday week. Patient-present individual and group "
            "therapy contribute to the goal.\n"),
        "note.txt": (
            f"Document ID: RV-N3\n{HDR} | Individual psychotherapy\n"
            "Ira Nkemdirim | DOB 1985-05-05 | MRN HG-M602 | Encounter HG-E610\n"
            "Service date 2026-05-05\n"
            "Patient contact: 09:00-10:00 | Completed: 60 minutes\n"),
    }

    @classmethod
    def setUpClass(cls):
        cls.st, cls.tmp = _corpus(cls.DOCS, "out/test_onemetric.db")

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_only_the_minute_goal_is_in_force(self):
        reqs = [r["metric"] for r in self.st.requirements("HG-M602")]
        self.assertEqual(reqs, ["therapy_minutes"])
        self.assertEqual(self.st.requirements("HG-M602")[0]["threshold"], 150)

    def test_renderer_does_not_crash_and_omits_the_missing_column(self):
        comp = Q.compliance(self.st, "HG-M602", "2026-05-04", "2026-05-10")
        md = render.render("compliance", comp)      # used to raise TypeError
        self.assertIn("therapy minutes (goal)", md)
        self.assertNotIn("therapy days (goal)", md)
        self.assertIn("NOT_MET", md)               # 60 < 150


class TimeFormats(unittest.TestCase):
    """#7 Clock formats, and the 12-hour range that looked like midnight."""

    def test_twelve_hour_range_is_not_read_as_crossing_midnight(self):
        # the old behaviour made this 765 minutes
        self.assertEqual(iv.total([iv.mk("12:30", "1:15")]), 45)
        self.assertEqual(iv.total([iv.mk("11:45", "12:30")]), 45)

    def test_meridiem(self):
        self.assertEqual(iv.total([iv.mk("10:00 AM", "11:30 AM")]), 90)
        self.assertEqual(iv.total([iv.mk("1:00 PM", "1:45 PM")]), 45)
        self.assertEqual(iv.to_min("12:30 AM"), 30)
        self.assertEqual(iv.to_min("12:30 PM"), 12 * 60 + 30)

    def test_bare_hour_end(self):
        self.assertEqual(iv.total([iv.mk("10:45", "11")]), 15)

    def test_a_genuine_overnight_span_still_works(self):
        self.assertEqual(iv.total([iv.mk("23:30", "00:15")]), 45)

    def test_extractor_reads_the_new_phrasings(self):
        from backbone.extract import rules
        doc = (f"Document ID: RV-T1\n{HDR} | Coping skills group\n"
               "Ada Sorensen | DOB 1991-02-02 | MRN HG-M603 | Encounter HG-E620\n"
               "Service date 2026-06-01 | Group scheduled 10:00 AM-11:30 AM\n"
               "The group took a break from 10:45 until 11:00.\n")
        facts, claims = rules.extract(doc, "pk-rv-t1")
        enc = [c for c in claims if c.claim_type == "encounter"][0]
        self.assertEqual(enc.breaks, [["10:45", "11:00"]])
        self.assertEqual(enc.fields["interval_classes"].get("scheduled"),
                         [["10:00 AM", "11:30 AM"]])


class RouterPrecision(unittest.TestCase):
    """#8 "week of <date>" and #11 whole-word keywords."""

    @classmethod
    def setUpClass(cls):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_routing.db" + s)
            if p.exists():
                p.unlink()
        cls.st = Store("out/test_routing.db")
        ingest_dir(cls.st, pathlib.Path("documents"))
        reconcile.reconcile_all(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def test_week_of_a_date_selects_that_week_only(self):
        plan = router.route(self.st, "Did Rowan meet the goal in the week of "
                                     "January 12, 2026?")
        self.assertEqual(plan["function"], "compliance")
        self.assertEqual((plan["params"]["start"], plan["params"]["end"]),
                         ("2026-01-12", "2026-01-18"))

    def test_week_beginning_also_works(self):
        plan = router.route(self.st, "How many sessions in the week beginning "
                                     "2026-01-19?")
        self.assertEqual((plan["params"]["start"], plan["params"]["end"]),
                         ("2026-01-19", "2026-01-25"))

    def test_keywords_match_whole_words_only(self):
        self.assertTrue(router._kw("meet").search("did they meet the goal"))
        self.assertFalse(router._kw("meet").search("the team meeting ran late"))
        self.assertTrue(router._kw("count").search("a session count"))
        self.assertFalse(router._kw("count").search("give me an account"))

    def test_meeting_does_not_route_to_compliance(self):
        intent, hits = router.classify("What happened at the team meeting?")
        self.assertEqual(hits, 0, "no keyword should match; it must warn instead")


class SmallerIssues(unittest.TestCase):
    """#12 the remaining items."""

    def test_match_rule_recorded_per_claim_not_always_encounter_id(self):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_prov.db" + s)
            if p.exists():
                p.unlink()
        st = Store("out/test_prov.db")
        ingest_dir(st, pathlib.Path("documents"))
        reconcile.reconcile_all(st)
        rules_used = {r["match_rule"] for r in
                      st.q("SELECT DISTINCT match_rule FROM event_claims")}
        self.assertIn("RULE-MATCH-01", rules_used)
        self.assertIn("RULE-MATCH-02", rules_used,
                      "the telehealth call rows match by appointment id")
        # the two platform rows specifically
        for r in st.q("SELECT ec.match_rule FROM event_claims ec JOIN claims c "
                      "ON c.claim_id=ec.claim_id WHERE c.call_ref IS NOT NULL"):
            self.assertEqual(r["match_rule"], "RULE-MATCH-02")
        st.close()

    def test_correction_without_an_old_value_is_not_applied_to_every_interval(self):
        from backbone.reconcile import _apply_corrections
        claim = {"intervals": [["13:00", "13:20"], ["13:30", "13:55"]]}
        corr = [{"claim_id": "c1",
                 "fields": {"target_field": "departure", "new_value": "14:00",
                            "old_value": None}}]
        log: list = []
        ivs, used = _apply_corrections(claim, corr, log)
        self.assertEqual(ivs, [["13:00", "13:20"], ["13:30", "13:55"]],
                         "an unqualified correction must not rewrite both legs")
        self.assertEqual(used, [])
        self.assertTrue(log and "ambiguous" in log[0][1])

    def test_correction_with_an_old_value_targets_only_that_interval(self):
        from backbone.reconcile import _apply_corrections
        claim = {"intervals": [["13:00", "13:20"], ["13:30", "13:55"]]}
        corr = [{"claim_id": "c1",
                 "fields": {"target_field": "departure", "new_value": "14:00",
                            "old_value": "13:55"}}]
        ivs, used = _apply_corrections(claim, corr, [])
        self.assertEqual(ivs, [["13:00", "13:20"], ["13:30", "14:00"]])
        self.assertEqual(used, ["c1"])

    def test_stated_minutes_with_a_documented_break_becomes_a_band(self):
        docs = {
            "plan.txt": (
                f"Document ID: RV-P3\n{HDR} | Outpatient treatment plan\n"
                "Bo Lindqvist | DOB 1988-08-08 | MRN HG-M604\n"
                "Episode dates: 2026-07-06 through 2026-07-12\n"
                "signed 2026-07-06\n"
                "Participation goal: at least 1 therapy days and at least 30 "
                "minutes of patient-present therapy in each Monday-Sunday week. "
                "Patient-present group therapy contributes to the goal.\n"),
            # a stated figure, a documented break, and no patient interval: the
            # note never says whether the 90 already excludes the break
            "note.txt": (
                f"Document ID: RV-N4\n{HDR} | Coping skills group\n"
                "Bo Lindqvist | DOB 1988-08-08 | MRN HG-M604 | Encounter HG-E630\n"
                "Service date 2026-07-07\n"
                "Patient-present group therapy duration: 90 minutes\n"
                "The whole group took a break from 10:45 to 11:00.\n"),
        }
        st, tmp = _corpus(docs, "out/test_statedbreak.db")
        row = st.q1("SELECT * FROM events WHERE encounter_ref='HG-E630'")
        self.assertEqual((row["min_minutes"], row["max_minutes"]), (75, 90))
        self.assertEqual(row["disputed"], 1)
        why = [d["rationale"] for d in st.event_decisions(row["event_id"])
               if d["rule_id"] == "RULE-DUR-04"]
        self.assertTrue(any("already excludes" in w for w in why))
        st.close()
        shutil.rmtree(tmp, ignore_errors=True)

    def test_reimport_without_a_form_id_still_collapses(self):
        docs = {
            "review.txt": (
                f"Document ID: RV-M1\n{HDR} | Measurement review\n"
                "Juno Abara | DOB 1992-09-09 | MRN HG-M605\n"
                "Instrument: PHQ-9 | Patient portal form HG-Q900\n"
                "Completed by patient: 2026-08-03, 08:00 local\n"
                "Total score: 12\n"),
            # the batch summary omits the form id entirely
            "import.txt": (
                f"Document ID: RV-M2\n{HDR} | Administrative import receipt\n"
                "Juno Abara | DOB 1992-09-09 | MRN HG-M605\n"
                "Received into chart: 2026-08-14, 07:00 local\n\n"
                "Measure | Result | Date completed\n"
                "PHQ-9   | 12     | 2026-08-03\n"
                "Import detail: copied result from the August 3 portal form. No "
                "newly completed patient questionnaire is included in this batch. "
                "This receipt does not document a visit.\n"),
        }
        st, tmp = _corpus(docs, "out/test_reimport.db")
        ms = st.measures("HG-M605")
        self.assertEqual(len(ms), 1,
                         "a re-import missing the form id must collapse onto the "
                         f"original, got {[dict(m) for m in ms]}")
        self.assertGreaterEqual(len(json.loads(ms[0]["claim_ids"])), 2)
        self.assertEqual(ms[0]["form_ref"], "HG-Q900")
        st.close()
        shutil.rmtree(tmp, ignore_errors=True)

    def test_narrative_date_prefers_the_encounter_header_line(self):
        from backbone.extract import rules
        # the signature date comes first in the text, but the encounter line
        # carries the service date
        doc = (f"Document ID: RV-D1\n{HDR} | Individual psychotherapy\n"
               "Signed 2026-09-30, 17:00 local\n"
               "Tam Oyelaran | DOB 1993-03-03 | MRN HG-M606\n"
               "Encounter HG-E640 | 2026-09-14 | In person\n"
               "Patient contact: 09:00-09:45 | Completed: 45 minutes\n")
        facts, claims = rules.extract(doc, "pk-rv-d1")
        enc = [c for c in claims if c.claim_type == "encounter"][0]
        self.assertEqual(enc.service_date, "2026-09-14")
        self.assertEqual(enc.fields["service_date_basis"], "encounter_header_line")

    def test_days_arithmetic_is_not_always_zero(self):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_days.db" + s)
            if p.exists():
                p.unlink()
        st = Store("out/test_days.db")
        ingest_dir(st, pathlib.Path("documents"))
        reconcile.reconcile_all(st)
        comp = Q.compliance(st, "HG-M042", "2026-01-05", "2026-01-30")
        week1 = comp["value"]["weeks"][0]
        days = [c for c in week1["checks"] if c["metric"] == "therapy_days"][0]
        self.assertNotEqual(days["arithmetic"], "0")
        self.assertIn("2026-01-05", days["arithmetic"])
        self.assertTrue(days["arithmetic"].endswith("= 3"), days["arithmetic"])
        st.close()

    def test_progress_statements_name_only_observed_domains(self):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_prog.db" + s)
            if p.exists():
                p.unlink()
        st = Store("out/test_prog.db")
        ingest_dir(st, pathlib.Path("documents"))
        reconcile.reconcile_all(st)
        v = Q.progress(st, "HG-M042")["value"]
        observed = set(v["observations_by_domain"])
        for s_ in v["supported"]:
            # every domain statement must correspond to a domain with sources
            self.assertTrue(s_["sources"], s_["statement"])
        # and the canned sentence is gone
        joined = " ".join(s_["statement"] for s_ in v["supported"])
        self.assertNotIn("partial improvement in mood, activity and "
                         "work-approach behaviour", joined)
        self.assertTrue(observed)
        st.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
