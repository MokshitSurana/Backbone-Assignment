"""Regressions for submission-readiness items 1, 9 and 10.

1  a per-patient question with no single patient named must answer for every
   patient, not for whichever MRN sorted first
9  the collection-wide answer must carry source passages
10 unequal before/after periods must be compared per week
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

from backbone import intervals as iv
from backbone import queries as Q
from backbone import reconcile, render, router
from backbone.ingest import ingest_dir
from backbone.store import Store

HDR = "Harbor Grove Behavioral Health"

PLAN_B = (f"Document ID: FO-P1\n{HDR} | Outpatient treatment plan\n"
          "Marek Dvorak | DOB 1979-11-11 | MRN HG-M700\n"
          "Episode dates: 2026-02-02 through 2026-02-15\n"
          "signed 2026-02-02\n"
          "Local treatment participation goal: at least 2 therapy days and at "
          "least 90 minutes of patient-present therapy in each Monday-Sunday "
          "week. Patient-present individual, group, and family therapy "
          "contribute to the minute goal.\n")
NOTE_B = (f"Document ID: FO-N1\n{HDR} | Individual psychotherapy\n"
          "Marek Dvorak | DOB 1979-11-11 | MRN HG-M700 | Encounter HG-E700\n"
          "Service date 2026-02-03\n"
          "Patient contact: 09:00-09:45 | Completed: 45 minutes\n")


def _two_patients(db: str):
    for s in ("", "-wal", "-shm"):
        p = pathlib.Path(db + s)
        if p.exists():
            p.unlink()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bb_fo_"))
    for f in sorted(pathlib.Path("documents").glob("*.txt")):
        shutil.copy(f, tmp / f.name)
    (tmp / "fo_plan.txt").write_text(PLAN_B, encoding="utf-8")
    (tmp / "fo_note.txt").write_text(NOTE_B, encoding="utf-8")
    st = Store(db)
    ingest_dir(st, tmp)
    reconcile.reconcile_all(st)
    return st, tmp


class PerPatientAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st, cls.tmp = _two_patients("out/test_fanout.db")

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_both_patients_were_discovered(self):
        self.assertEqual(sorted(self.st.patients()), ["HG-M042", "HG-M700"])

    def test_each_patient_question_fans_out(self):
        res = router.answer(
            self.st, "How many therapy sessions did each patient attend?")
        self.assertTrue(res["route"]["per_patient"])
        per = res["result"]["value"]["per_patient"]
        self.assertEqual(sorted(per), ["HG-M042", "HG-M700"])
        self.assertEqual(per["HG-M042"]["sessions"], 12)
        self.assertEqual(per["HG-M700"]["sessions"], 1)

    def test_naming_one_patient_still_answers_only_for_them(self):
        res = router.answer(self.st, "How many sessions did Rowan attend?")
        self.assertFalse(res["route"]["per_patient"])
        self.assertEqual(res["result"]["value"]["sessions"], 12)

    def test_each_patient_is_measured_over_their_own_episode(self):
        res = router.answer(
            self.st, "How many therapy minutes per week for each patient?")
        per = res["result"]["value"]["per_patient"]
        weeks_a = {w["week_start"] for w in per["HG-M042"]["weeks"]}
        weeks_b = {w["week_start"] for w in per["HG-M700"]["weeks"]}
        self.assertTrue(weeks_a and weeks_b)
        self.assertFalse(weeks_a & weeks_b,
                         "one patient's window was applied to the other")
        self.assertEqual(per["HG-M042"]["total_minutes"], [585, 595])
        self.assertEqual(per["HG-M700"]["total_minutes"], [45, 45])

    def test_an_explicit_window_is_shared_by_all_patients(self):
        plan = router.route(self.st, "How many sessions did each patient attend "
                                     "between January 5 and January 30, 2026?")
        self.assertTrue(plan["per_patient"])
        self.assertEqual(plan["params"]["start"], "2026-01-05")
        self.assertEqual(plan["params"]["end"], "2026-01-30")

    def test_compliance_fans_out(self):
        res = router.answer(self.st, "Did each patient meet the plan goal "
                                     "every week?")
        self.assertIn("per_patient", res["result"]["value"])
        per = res["result"]["value"]["per_patient"]
        self.assertEqual(len(per), 2)

    def test_progress_fans_out(self):
        res = router.answer(self.st,
                            "Summarise the symptom course for each patient")
        self.assertIn("per_patient", res["result"]["value"])

    def test_collection_wide_questions_do_not_fan_out(self):
        plan = router.route(self.st, "Which patients had two consecutive weeks "
                                     "below the requirement?")
        self.assertFalse(plan["per_patient"])
        self.assertIsNone(plan["params"]["mrn"])

    def test_the_fan_out_is_rendered_per_mrn(self):
        res = router.answer(self.st,
                            "How many sessions did each patient attend?")
        md = render.render(res["route"]["function"], res["result"])
        self.assertIn("### `HG-M042`", md)
        self.assertIn("### `HG-M700`", md)
        self.assertIn("2 patients in scope", md)

    def test_routing_reports_that_it_fanned_out(self):
        plan = router.route(self.st, "sessions for each patient")
        self.assertTrue(any("patients" in w for w in plan["warnings"]),
                        plan["warnings"])

    def test_evidence_is_attributed_to_the_right_patient(self):
        res = router.answer(self.st,
                            "How many sessions did each patient attend?")
        for e in res["result"]["evidence"]:
            self.assertIn("patient", e)
        pats = {e["patient"] for e in res["result"]["evidence"]}
        self.assertEqual(pats, {"HG-M042", "HG-M700"})


class CollectionEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_collev.db" + s)
            if p.exists():
                p.unlink()
        cls.st = Store("out/test_collev.db")
        ingest_dir(cls.st, pathlib.Path("documents"))
        reconcile.reconcile_all(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def test_evidence_is_not_empty(self):
        r = Q.consecutive_below(self.st, None, 2)
        self.assertTrue(r["evidence"],
                        "a collection-wide finding with no source passages "
                        "cannot be traced back to the documents")

    def test_every_evidence_item_names_a_patient_and_a_week(self):
        for e in Q.consecutive_below(self.st, None, 2)["evidence"]:
            self.assertIn("patient", e)
            self.assertIn("week", e)
            self.assertTrue(e.get("quote"))

    def test_below_weeks_carry_their_sessions_and_quotes(self):
        v = Q.consecutive_below(self.st, None, 2)["value"]
        inc = v["included"] + v["depends_on_documentation"]
        self.assertTrue(inc)
        weeks = inc[0]["weeks_below_with_sources"]
        self.assertTrue(weeks)
        self.assertTrue(weeks[0]["sessions"])
        self.assertTrue(weeks[0]["evidence"])
        self.assertIn("=", weeks[0]["sum"])

    def test_the_plan_requirement_is_cited_too(self):
        roles = {e.get("role")
                 for e in Q.consecutive_below(self.st, None, 2)["evidence"]}
        self.assertIn("plan_requirement", roles)

    def test_quotes_resolve_against_the_source_files(self):
        checked = 0
        for e in Q.consecutive_below(self.st, None, 2)["evidence"]:
            if not e.get("file"):
                continue
            raw = (pathlib.Path("documents") / e["file"]).read_text(
                encoding="utf-8")
            s0, s1 = e["offsets"]
            self.assertEqual(raw[s0:s1], e["quote"])
            checked += 1
        self.assertGreater(checked, 0)

    def test_the_renderer_shows_the_passages(self):
        md = render.render("consecutive_below",
                           Q.consecutive_below(self.st, None, 2))
        self.assertIn("Below-goal weeks", md)
        self.assertIn("[", md)      # an offset span


class PlanChangePerWeek(unittest.TestCase):
    """A real amendment: the goal rises three weeks into a four-week episode."""

    DOCS = {
        "plan1.txt": (f"Document ID: PC-P1\n{HDR} | Outpatient treatment plan\n"
                      "Esme Thorvald | DOB 1987-07-07 | MRN HG-M800\n"
                      "Episode dates: 2026-06-01 through 2026-06-28\n"
                      "signed 2026-06-01\n"
                      "Local treatment participation goal: at least 1 therapy "
                      "days and at least 60 minutes of patient-present therapy "
                      "in each Monday-Sunday week. Patient-present individual "
                      "therapy contributes to the minute goal.\n"),
        "plan2.txt": (f"Document ID: PC-P2\n{HDR} | Outpatient treatment plan\n"
                      "Esme Thorvald | DOB 1987-07-07 | MRN HG-M800\n"
                      "Episode dates: 2026-06-22 through 2026-06-28\n"
                      "signed 2026-06-22\n"
                      "Local treatment participation goal: at least 2 therapy "
                      "days and at least 120 minutes of patient-present therapy "
                      "in each Monday-Sunday week. Patient-present individual "
                      "therapy contributes to the minute goal.\n"),
        "n1.txt": (f"Document ID: PC-N1\n{HDR} | Individual psychotherapy\n"
                   "Esme Thorvald | MRN HG-M800 | Encounter HG-E801\n"
                   "Service date 2026-06-02\n"
                   "Patient contact: 09:00-10:00 | Completed: 60 minutes\n"),
        "n2.txt": (f"Document ID: PC-N2\n{HDR} | Individual psychotherapy\n"
                   "Esme Thorvald | MRN HG-M800 | Encounter HG-E802\n"
                   "Service date 2026-06-09\n"
                   "Patient contact: 09:00-10:00 | Completed: 60 minutes\n"),
        "n3.txt": (f"Document ID: PC-N3\n{HDR} | Individual psychotherapy\n"
                   "Esme Thorvald | MRN HG-M800 | Encounter HG-E803\n"
                   "Service date 2026-06-23\n"
                   "Patient contact: 09:00-11:00 | Completed: 120 minutes\n"),
    }

    @classmethod
    def setUpClass(cls):
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path("out/test_planchg.db" + s)
            if p.exists():
                p.unlink()
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bb_pc_"))
        for n, t in cls.DOCS.items():
            (cls.tmp / n).write_text(t, encoding="utf-8")
        cls.st = Store("out/test_planchg.db")
        ingest_dir(cls.st, cls.tmp)
        reconcile.reconcile_all(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_amendment_is_detected(self):
        v = Q.plan_change_comparison(self.st, "HG-M800")["value"]
        self.assertTrue(v["plan_changes"], "a second dated plan is a change")
        self.assertEqual(v["plan_changes"][0]["change_date"], "2026-06-22")

    def test_each_week_uses_the_requirement_in_force_that_week(self):
        comp = Q.compliance(self.st, "HG-M800", "2026-06-01", "2026-06-28")
        weeks = {w["week_start"]: w for w in comp["value"]["weeks"]}
        early = {c["metric"]: c["threshold"]
                 for c in weeks["2026-06-01"]["checks"]}
        late = {c["metric"]: c["threshold"]
                for c in weeks["2026-06-22"]["checks"]}
        self.assertEqual(early["therapy_minutes"], 60)
        self.assertEqual(late["therapy_minutes"], 120)

    def test_raw_totals_are_equal_but_per_week_rates_are_not(self):
        c = Q.plan_change_comparison(
            self.st, "HG-M800")["value"]["plan_changes"][0]
        b, a = c["before"], c["after"]
        # three weeks before carrying 120 minutes, one week after carrying 120
        self.assertEqual(b["minutes"][0], a["minutes"][0])
        self.assertEqual(a["minutes_per_week"][0], 120.0)
        self.assertLess(b["minutes_per_week"][0], 60.0)
        self.assertGreater(c["delta_per_week"]["minutes"][0], 0,
                           "the per-week comparison must show the increase the "
                           "raw totals hide")

    def test_both_slices_report_their_week_count(self):
        c = Q.plan_change_comparison(
            self.st, "HG-M800")["value"]["plan_changes"][0]
        self.assertEqual(c["before"]["weeks"], 3)
        self.assertEqual(c["after"]["weeks"], 1)

    def test_a_midweek_change_flags_its_split_week(self):
        self.assertEqual(iv.week_key("2026-06-24"), "2026-06-22")
        c = Q.plan_change_comparison(
            self.st, "HG-M800", change_date="2026-06-24")["value"]
        sw = c["plan_changes"][0]["split_week"]
        self.assertIsNotNone(sw)
        self.assertEqual(sw["week_start"], "2026-06-22")
        self.assertIn("both plans", sw["note"])

    def test_the_renderer_leads_with_per_week_figures(self):
        md = render.render("plan_change_comparison",
                           Q.plan_change_comparison(self.st, "HG-M800"))
        self.assertIn("minutes per week", md)
        self.assertIn("raw total", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
