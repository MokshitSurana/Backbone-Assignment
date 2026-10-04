"""Unit tests for the parts where a quiet mistake would be expensive."""
from __future__ import annotations

import pathlib
import unittest

from backbone import intervals as iv
from backbone import normalize as nz
from backbone.extract import rules
from backbone.taxonomy import canonical_service, classify_document


class Intervals(unittest.TestCase):
    def test_break_is_subtracted_by_overlap_not_by_length(self):
        # the whole break falls inside the presence window: lose all 15
        self.assertEqual(
            iv.present_minutes([iv.mk("10:30", "11:30")], [iv.mk("10:45", "11:00")]), 45)
        # arrival after the break starts: lose only the overlapping 10
        self.assertEqual(
            iv.present_minutes([iv.mk("10:50", "11:30")], [iv.mk("10:45", "11:00")]), 30)
        # arrival after the break ends: lose nothing
        self.assertEqual(
            iv.present_minutes([iv.mk("11:00", "11:30")], [iv.mk("10:45", "11:00")]), 30)
        # departure before the break starts: lose nothing
        self.assertEqual(
            iv.present_minutes([iv.mk("10:00", "10:40")], [iv.mk("10:45", "11:00")]), 40)

    def test_split_session_sums_only_connected_time(self):
        self.assertEqual(
            iv.present_minutes([iv.mk("13:00", "13:20"), iv.mk("13:30", "13:55")],
                               [iv.mk("13:20", "13:30")]), 45)

    def test_duplicate_intervals_do_not_double_count(self):
        self.assertEqual(iv.total([iv.mk("10:00", "11:00"), iv.mk("10:00", "11:00")]), 60)

    def test_overlap_detection(self):
        self.assertEqual(iv.overlaps([iv.mk("10:00", "11:30")],
                                     [iv.mk("11:15", "11:45")]), 15)
        self.assertEqual(iv.overlaps([iv.mk("10:00", "11:15")],
                                     [iv.mk("11:15", "11:45")]), 0)

    def test_week_keys(self):
        self.assertEqual(iv.week_key("2026-01-05"), "2026-01-05")   # Monday
        self.assertEqual(iv.week_key("2026-01-11"), "2026-01-05")   # Sunday
        self.assertEqual(iv.week_key("2026-01-12"), "2026-01-12")
        self.assertEqual(iv.week_key("2026-01-11", "week_sun_sat"), "2026-01-11")


class Dates(unittest.TestCase):
    def test_month_abbreviations(self):
        self.assertEqual(nz.parse_date("Jan05", default_year=2026), "2026-01-05")
        self.assertEqual(nz.parse_date("Jan14", default_year=2026), "2026-01-14")
        self.assertEqual(nz.parse_date("Feb14", default_year=2026), "2026-02-14")
        self.assertEqual(nz.parse_date("January 22, 2026"), "2026-01-22")
        self.assertEqual(nz.parse_date("2026-01-30"), "2026-01-30")


class Normalisation(unittest.TestCase):
    def test_receipt_furniture_does_not_change_identity(self):
        a = "Harbor Grove | Roster\nPatient arrival: 10:00\n"
        b = ("Received into chart: 2026-02-02, 09:00\n"
             "Harbor Grove  |  Roster\nPatient arrival: 10:00\n")
        self.assertNotEqual(nz.hashes(a)[0], nz.hashes(b)[0])      # raw differs
        self.assertEqual(nz.hashes(a)[1], nz.hashes(b)[1])         # identity matches


class Taxonomy(unittest.TestCase):
    def test_service_synonyms(self):
        for text, expect in [
            ("Coping skills group", "group_therapy"),
            ("Group psychotherapy", "group_therapy"),
            ("process group", "group_therapy"),
            ("Individual psychotherapy", "individual_therapy"),
            ("Family psychotherapy", "family_therapy"),
            ("Prescriber visit", "medication_management"),
            ("Care coordination", "care_coordination"),
            ("Family collateral", "collateral_contact"),
        ]:
            self.assertEqual(canonical_service(text), expect, text)

    def test_class_comes_from_the_masthead_not_a_passing_mention(self):
        note = ("Harbor Grove Behavioral Health | Family collateral\n"
                "Patient: X | MRN: HG-M001\n"
                "We discussed ways to support the treatment plan.\n")
        self.assertEqual(classify_document(note), "clinical_note")
        plan = ("Harbor Grove Behavioral Health | Outpatient treatment plan\n"
                "Patient: X | MRN: HG-M001\nindividual psychotherapy weekly\n")
        self.assertEqual(classify_document(plan), "treatment_plan")


class ExtractorGeneralisation(unittest.TestCase):
    """Synthetic documents that are not in the supplied corpus."""

    def test_unseen_patient_and_hour_based_requirement(self):
        plan = ("Document ID: SYN-1\n"
                "Harbor Grove Behavioral Health | Outpatient treatment plan\n"
                "Dana Okonjo | DOB 1988-02-03 | MRN HG-M777\n"
                "Episode dates: 2026-03-02 through 2026-03-29\n"
                "signed 2026-03-02\n"
                "Participation goal: at least 2 therapy days and at least 2.5 hours "
                "of patient-present therapy in each Monday-Sunday week. "
                "Patient-present individual and group therapy contribute to the "
                "goal. Medication management and care coordination do not "
                "contribute.\n")
        facts, claims = rules.extract(plan, "pk-syn1")
        self.assertEqual(facts.patient_mrn, "HG-M777")
        self.assertEqual(facts.given_name, "Dana")
        reqs = {c.fields["metric"]: c.fields for c in claims
                if c.claim_type == "plan_requirement"}
        self.assertEqual(reqs["therapy_days"]["threshold"], 2)
        self.assertEqual(reqs["therapy_minutes"]["threshold"], 150)   # 2.5 h
        self.assertEqual(sorted(reqs["therapy_minutes"]["service_types"]),
                         ["group_therapy", "individual_therapy"])
        self.assertIn("medication_management",
                      reqs["therapy_minutes"]["excluded_types"])

    def test_unseen_group_note_with_a_break_and_a_desk_roster(self):
        note = ("Document ID: SYN-2\n"
                "Harbor Grove Behavioral Health | Coping skills group\n"
                "Dana Okonjo | DOB 1988-02-03 | MRN HG-M777 | Encounter HG-E900\n"
                "Service date 2026-03-03 | Group scheduled 14:00-15:30 local\n"
                "The group took a break from 14:50 to 15:05.\n"
                "Dana contributed to the discussion.\n")
        facts, claims = rules.extract(note, "pk-syn2")
        enc = [c for c in claims if c.claim_type == "encounter"][0]
        self.assertEqual(enc.encounter_ref, "HG-E900")
        self.assertEqual(enc.service_type, "group_therapy")
        self.assertEqual(enc.breaks, [["14:50", "15:05"]])
        # a facilitator note must NOT claim the scheduled slot as patient time
        self.assertEqual(enc.intervals, [])

    def test_unseen_correction_targets_the_right_field(self):
        corr = ("Document ID: SYN-3\n"
                "Harbor Grove Behavioral Health | Attendance correction\n"
                "Dana Okonjo | DOB 1988-02-03 | MRN HG-M777\n"
                "Applies to group encounter HG-E900, service date March 3, 2026\n"
                "Correction: Patient arrival for HG-E900 is 14:20, replacing the "
                "original roster value of 14:00.\n")
        facts, claims = rules.extract(corr, "pk-syn3")
        c = [x for x in claims if x.claim_type == "correction"][0]
        self.assertEqual(c.encounter_ref, "HG-E900")
        self.assertEqual(c.fields["target_field"], "arrival")
        self.assertEqual(c.fields["new_value"], "14:20")
        self.assertEqual(c.fields["old_value"], "14:00")

    def test_every_claim_quote_verifies_on_unseen_text(self):
        for doc in ("Document ID: SYN-4\nHarbor Grove | Individual psychotherapy\n"
                    "Dana Okonjo | MRN HG-M777 | Encounter HG-E901\n"
                    "2026-03-05 | Patient contact: 09:00-09:45 | Completed: 45 minutes\n",):
            facts, claims = rules.extract(doc, "pk-syn4")
            for c in claims:
                self.assertTrue(c.verify(doc), c)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RouterSafety(unittest.TestCase):
    """Regressions for four bugs found in review."""

    @classmethod
    def setUpClass(cls):
        from backbone.ingest import ingest_dir
        from backbone import reconcile
        from backbone.store import Store
        for s in ("", "-wal", "-shm"):
            q = pathlib.Path("out/test_router.db" + s)
            if q.exists():
                q.unlink()
        cls.st = Store("out/test_router.db")
        ingest_dir(cls.st, pathlib.Path("documents"))
        reconcile.reconcile_all(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def test_no_patient_name_is_hardcoded_anywhere_in_the_package(self):
        # The brief forbids encoding patient facts. The only place a name from
        # the corpus may appear is the test suite.
        import backbone
        root = pathlib.Path(backbone.__file__).parent
        for f in sorted(root.rglob("*.py")) + [root / "schema.sql"]:
            text = f.read_text(encoding="utf-8").lower()
            for name in ("rowan", "mercer", "casey", "hg-m042", "hg-e1"):
                self.assertNotIn(name, text, f"{f.name} contains {name!r}")

    def test_unknown_patient_is_refused_not_silently_redirected(self):
        from backbone import router
        res = router.answer(self.st, "How many sessions did Dana attend?")
        self.assertIn("error", res)
        self.assertIn("Dana", res["error"])
        res = router.answer(self.st, "Tell me about HG-M999")
        self.assertIn("error", res)

    def test_unmatched_question_warns_instead_of_guessing_silently(self):
        from backbone import router
        res = router.answer(self.st, "wibble wobble")
        self.assertTrue(any("no question type matched" in w
                            for w in res["route"]["warnings"]))

    def test_collection_question_is_not_scoped_to_one_patient(self):
        from backbone import router
        plan = router.route(self.st,
                            "Which patients had two consecutive weeks below?")
        self.assertEqual(plan["function"], "consecutive_below")
        self.assertIsNone(plan["params"]["mrn"])
        # ...but naming a patient does scope it
        plan = router.route(self.st,
                            "Did Rowan have two consecutive weeks below?")
        self.assertEqual(plan["params"]["mrn"], "HG-M042")

    def test_all_five_dev_questions_route_without_a_patient_error(self):
        import json
        from backbone import router
        qs = json.loads(pathlib.Path("questions.json").read_text(encoding="utf-8"))
        for item in qs:
            res = router.answer(self.st, item["question"])
            self.assertNotIn("error", res, f"{item['id']}: {res.get('error')}")


class WeeklyCoverage(unittest.TestCase):
    """A week with no attendance is a week below the goal, not a missing week."""

    PLAN = ("Document ID: SYN-W1\n"
            "Harbor Grove Behavioral Health | Outpatient treatment plan\n"
            "Wren Calloway | DOB 1990-01-01 | MRN HG-M555\n"
            "Episode dates: 2026-03-02 through 2026-03-29\n"
            "signed 2026-03-02\n"
            "Local treatment participation goal: at least 1 therapy days and at "
            "least 150 minutes of patient-present therapy in each Monday-Sunday "
            "week. Patient-present individual, group, and family therapy "
            "contribute to the minute goal. Medication management and care "
            "coordination do not contribute.\n")
    # week 1 has one short session, week 2 and week 3 have nothing at all,
    # week 4 clears the goal
    NOTE_W1 = ("Document ID: SYN-W2\n"
               "Harbor Grove Behavioral Health | Individual psychotherapy\n"
               "Wren Calloway | DOB 1990-01-01 | MRN HG-M555 | Encounter HG-E801\n"
               "Service date 2026-03-03\n"
               "Patient contact: 09:00-09:40 | Completed: 40 minutes\n")
    NOTE_W4 = ("Document ID: SYN-W3\n"
               "Harbor Grove Behavioral Health | Individual psychotherapy\n"
               "Wren Calloway | DOB 1990-01-01 | MRN HG-M555 | Encounter HG-E802\n"
               "Service date 2026-03-24\n"
               "Patient contact: 09:00-11:40 | Completed: 160 minutes\n")

    @classmethod
    def setUpClass(cls):
        from backbone.ingest import ingest_path
        from backbone import reconcile
        from backbone.store import Store
        import tempfile
        for s in ("", "-wal", "-shm"):
            q = pathlib.Path("out/test_weeks.db" + s)
            if q.exists():
                q.unlink()
        cls.st = Store("out/test_weeks.db")
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bb_weeks_"))
        for i, text in enumerate((cls.PLAN, cls.NOTE_W1, cls.NOTE_W4)):
            f = cls.tmp / f"syn_{i}.txt"
            f.write_text(text, encoding="utf-8")
            ingest_path(cls.st, f)
        reconcile.reconcile_all(cls.st)

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.st.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_week_in_the_episode_appears(self):
        from backbone import queries as Q
        v = Q.minutes(self.st, "HG-M555", "2026-03-02", "2026-03-29")["value"]
        self.assertEqual([w["week_start"] for w in v["weeks"]],
                         ["2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23"])
        by = {w["week_start"]: w for w in v["weeks"]}
        self.assertEqual(by["2026-03-09"]["minutes"], [0, 0])
        self.assertEqual(by["2026-03-09"]["therapy_days"], [0, 0])

    def test_empty_weeks_are_not_met_and_count_as_consecutive(self):
        from backbone import queries as Q
        comp = Q.compliance(self.st, "HG-M555", "2026-03-02", "2026-03-29")["value"]
        got = {w["week_start"]: w["status"] for w in comp["weeks"]}
        self.assertEqual(got["2026-03-02"], "NOT_MET")   # 40 min < 150
        self.assertEqual(got["2026-03-09"], "NOT_MET")   # no attendance at all
        self.assertEqual(got["2026-03-16"], "NOT_MET")   # no attendance at all
        self.assertEqual(got["2026-03-23"], "MET")       # 160 min >= 150
        run = Q.consecutive_below(self.st, "HG-M555", 2)["value"]
        self.assertTrue(run["included"], "three empty/short weeks in a row "
                                         "must be found")
        self.assertEqual(run["included"][0]["longest_run_optimistic"], 3)


class VerdictPrecedence(unittest.TestCase):
    """A definite failure on one required metric settles the week."""

    def test_not_met_outranks_cannot_determine(self):
        from backbone import queries as Q
        # the goal is a conjunction: days AND minutes
        checks = [{"metric": "therapy_days", "status": Q.BELOW},
                  {"metric": "therapy_minutes", "status": Q.UNKNOWN}]
        overall = (Q.BELOW if any(c["status"] == Q.BELOW for c in checks) else
                   Q.UNKNOWN if any(c["status"] == Q.UNKNOWN for c in checks)
                   else Q.MET)
        self.assertEqual(overall, Q.BELOW)

    def test_rowan_week_four_is_still_undetermined(self):
        # the precedence change must not alter the dev answers: week 4 has no
        # definite failure, so it stays CANNOT_DETERMINE
        from backbone.ingest import ingest_dir
        from backbone import queries as Q, reconcile
        from backbone.store import Store
        for s in ("", "-wal", "-shm"):
            q = pathlib.Path("out/test_verdict.db" + s)
            if q.exists():
                q.unlink()
        st = Store("out/test_verdict.db")
        ingest_dir(st, pathlib.Path("documents"))
        reconcile.reconcile_all(st)
        got = {w["week_start"]: w["status"] for w in
               Q.compliance(st, "HG-M042", "2026-01-05", "2026-01-30")["value"]["weeks"]}
        self.assertEqual(got["2026-01-05"], "NOT_MET")
        self.assertEqual(got["2026-01-26"], "CANNOT_DETERMINE")
        st.close()


class SourceHygiene(unittest.TestCase):
    """Guards for two kinds of damage that are invisible on screen."""

    def _sources(self):
        import backbone
        root = pathlib.Path(backbone.__file__).parent
        return sorted(root.rglob("*.py")) + sorted(
            pathlib.Path("tests").rglob("*.py"))

    def test_no_raw_control_characters(self):
        # A shell heredoc can turn the two characters backslash-b into a
        # literal backspace, which renders as nothing and silently
        # disables a regex anchor. That happened three times in router.py
        # during this build, so it is now a test.
        names = {'\x07': "a", '\x08': "b", '\x0b': "v",
                 '\x0c': "f", '\x00': "0"}
        for f in self._sources():
            text = f.read_text(encoding="utf-8")
            for ch, esc in names.items():
                self.assertNotIn(
                    ch, text,
                    f"{f.name} holds a raw control character where the "
                    f"escape sequence {chr(92)}{esc} was intended")

    def test_every_module_parses(self):
        import ast
        for f in self._sources():
            try:
                ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as e:
                self.fail(f"{f.name} does not parse: {e}")
