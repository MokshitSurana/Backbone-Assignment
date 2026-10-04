# Answers

Source: `questions.json`  ·  abstraction: `out/clinical.db`  ·  all figures computed in `backbone.queries` from the reconciled event table.


## DEV-01

> For January 5–30, 2026, how many therapy sessions did Rowan attend, by service type and in total, and on how many distinct days? Provide a reviewable abstraction with source support and explain records that could lead to duplicate or ineligible counts.

*Resolved to* `session_counts(mrn='HG-M042', start='2026-01-05', end='2026-01-30')` *in 3 ms (router: rules).*

**12 therapy sessions** on **11 distinct days** (2026-01-05 to 2026-01-30).

| service type | sessions |
|---|---:|
| family therapy | 2 |
| group therapy | 5 |
| individual therapy | 5 |
| **total** | **12** |

Service types the plan counts: family_therapy, group_therapy, individual_therapy.

**Per-session ledger**

| date | encounter | service | minutes | certain |
|---|---|---|---:|---|
| 2026-01-05 | HG-E101 | individual therapy | 50 | yes |
| 2026-01-06 | HG-E102 | group therapy | 45 | yes |
| 2026-01-09 | HG-E104 | family therapy | 45 | yes |
| 2026-01-12 | HG-E105 | group therapy | 75 | yes |
| 2026-01-14 | HG-E107 | individual therapy | 45 | yes |
| 2026-01-19 | HG-E110 | group therapy | 60 | yes |
| 2026-01-19 | HG-E111 | individual therapy | 30 | yes |
| 2026-01-21 | HG-E112 | individual therapy | 45 | yes |
| 2026-01-22 | HG-E113 | group therapy | 45 | yes |
| 2026-01-26 | HG-E115 | individual therapy | 40-50 | yes |
| 2026-01-29 | HG-E118 | group therapy | 75 | yes |
| 2026-01-30 | HG-E119 | family therapy | 30 | yes |

**Records that could produce a duplicate or ineligible count**

- `HG-E103` 2026-01-08 -- contact did not occur (occurred=no)
    - BH-D015 [372:445] "There was no arrival call or cancellation message on the scheduling line."
    - BH-D006 [493:562] "HG-E103   | Jan08 | Individual therapy     | 11:00–11:45    | No show"
- `HG-E106` 2026-01-13 -- medication_management is named in the plan as not contributing
    - BH-D006 [707:778] "HG-E106   | Jan13 | Medication management  | 09:00–09:25    | Completed"
    - BH-D010 [257:278] "completed, 25 minutes"
- `HG-E108` 2026-01-15 -- contact did not occur (occurred=no)
    - BH-D016 [284:405] "The coping skills group scheduled for January 15 from 10:00 to 11:30 is cancelled by the clinic because of staff illness."
    - BH-D006 [851:929] "HG-E108   | Jan15 | Coping skills group    | 10:00–11:30    | Clinic cancelled"
- `HG-E109` 2026-01-16 -- collateral_contact is named in the plan as not contributing
    - BH-D012 [318:358] "Rowan was absent for the entire contact."
    - BH-D006 [930:1001] "HG-E109   | Jan16 | Family collateral      | 14:00–14:40    | Completed"
- `HG-E114` 2026-01-23 -- care_coordination is named in the plan as not contributing
    - BH-D109 [238:266] "Patient participation: None."
- `HG-E116` 2026-01-27 -- contact did not occur (occurred=no)
    - BH-D108 [535:626] "January 27 | HG-E116 | Skills group | 10:00–11:30 | — | — | No show; patient did not attend"
    - BH-D112 [900:1139] "Charge ID: CH-116 | Encounter: HG-E116 Service date: January 27, 2026 | Posting date: January 27, 2026, 18:06 Description: Group psychotherapy Quantity charged: 1 group session ..."
- `HG-E117` 2026-01-28 -- contact did not occur (occurred=no)
    - BH-D108 [627:721] "January 28 | HG-E117 | Individual | 14:00–14:45 | — | — | Patient cancelled before appointment"
    - BH-D108 [1242:1532] "January 28 scheduling entry: Cancellation received from patient January 28, 08:12. Rowan reported a personal scheduling conflict. Appointment HG-E117 was cancelled before the sc..."
- `HG-E120` 2026-01-30 -- medication_management is named in the plan as not contributing
    - BH-D114 [140:194] "January 30, 2026 | 15:00–15:20 | Completed, 20 minutes"

**Claims rejected or left unmatched during reconciliation**

- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] schedule_export cannot establish that care was delivered (BH-D006)
- [RULE-PRES-02] retransmission cannot establish that care was delivered (BH-D104)
- [RULE-DUR-05] retransmission contributes no duration (BH-D104)
- [RULE-PRES-02] platform_log cannot establish that care was delivered (BH-D106)
- [RULE-PRES-02] platform_log cannot establish that care was delivered (BH-D106)
- [RULE-DUR-05] platform_log contributes no duration (BH-D106)
- [RULE-DUR-05] platform_log contributes no duration (BH-D106)
- [RULE-PRES-02] draft_note cannot establish that care was delivered (BH-D112)

## DEV-02

> How many therapy minutes and hours did Rowan actually receive during the review period, overall and for each Monday–Sunday week? Show calculations or supporting detail, and report any conclusion the available documents do not settle.

*Resolved to* `minutes(mrn='HG-M042', start='2026-01-05', end='2026-01-30')` *in 2 ms (router: rules).*

**Total patient-present therapy: 585-595 minutes (9.75-9.92 hours).**
Weeks are mon–sun.

| week | therapy days | minutes | hours | calculation |
|---|---:|---:|---:|---|
| 2026-01-05 -> 2026-01-11 | 3 | 140 | 2.33 | 50 + 45 + 45 = 140 |
| 2026-01-12 -> 2026-01-18 | 2 | 120 | 2 | 75 + 45 = 120 |
| 2026-01-19 -> 2026-01-25 | 3 | 180 | 3 | 60 + 30 + 45 + 45 = 180 |
| 2026-01-26 -> 2026-02-01 | 3 | 145-155 | 2.42-2.58 | (40-50) + 75 + 30 = 145-155 |

**Session detail with sources**

*week of 2026-01-05*
- 2026-01-05 `HG-E101` individual therapy: 50 min
    - BH-D002 [323:345] "completed, 50 minutes."
- 2026-01-06 `HG-E102` group therapy: 45 min, breaks [['10:45', '11:00']]
    - BH-D005 [429:521] "2026-01-06 | HG-E102   | 10:00–11:30    | 10:15           | 11:15            | Attended part"
- 2026-01-09 `HG-E104` family therapy: 45 min
    - BH-D007 [343:394] "Patient-present family therapy duration: 45 minutes"
    - BH-D008 [348:384] "both present for the full 45 minutes"

*week of 2026-01-12*
- 2026-01-12 `HG-E105` group therapy: 75 min, breaks [['10:40', '10:55']]
    - BH-D005 [522:614] "2026-01-12 | HG-E105   | 10:00–11:30    | 10:00           | 11:30            | Attended full"
- 2026-01-14 `HG-E107` individual therapy: 45 min
    - BH-D011 [276:297] "completed, 45 minutes"

*week of 2026-01-19*
- 2026-01-19 `HG-E110` group therapy: 60 min, breaks [['10:45', '11:00']]
    - BH-D103 [223:320] "Correction: Patient departure for HG-E110 is 11:15, replacing the original roster value of 11:30."
    - BH-D102 [296:364] "Patient arrival: 10:00 | Patient departure: 11:30 | Status: Attended"
- 2026-01-19 `HG-E111` individual therapy: 30 min
    - BH-D105 [172:224] "Patient contact: 11:15–11:45 | Completed: 30 minutes"
- 2026-01-21 `HG-E112` individual therapy: 45 min, breaks [['13:20', '13:30']]
    - BH-D106 [368:416] "Total patient psychotherapy contact: 45 minutes."
- 2026-01-22 `HG-E113` group therapy: 45 min, breaks [['10:45', '11:00']]
    - BH-D108 [444:534] "January 22 | HG-E113 | Skills group | 10:00–11:30 | 10:30 | 11:30 | Attended, late arrival"

*week of 2026-01-26*
- 2026-01-26 `HG-E115` individual therapy: 40-50 min
    - BH-D111 [260:322] "Actual patient psychotherapy contact: 09:10–09:50, 40 minutes."
    - BH-D110 [241:303] "Actual patient psychotherapy contact: 09:00–09:50, 50 minutes."
- 2026-01-29 `HG-E118` group therapy: 75 min, breaks [['10:45', '11:00']]
    - BH-D108 [722:798] "January 29 | HG-E118 | Skills group | 10:00–11:30 | 10:00 | 11:30 | Attended"
- 2026-01-30 `HG-E119` family therapy: 30 min
    - BH-D113 [278:330] "Rowan present with partner: 13:15–13:45, 30 minutes."

**Not settled by the available documents**

- `HG-E115` 2026-01-26: bounds 40-50 min [RULE-DUR-03]
    - records of equal authority disagree and neither is a correction: kept as bounds (BH-D111=40min; BH-D110=50min)
    - would be settled by: an addendum or correction from either author that names the other record's value, or a check-in/room-transfer timestamp for the start of the contact
    - BH-D111 [260:322] "Actual patient psychotherapy contact: 09:10–09:50, 40 minutes."
    - BH-D110 [241:303] "Actual patient psychotherapy contact: 09:00–09:50, 50 minutes."

## DEV-03

> For each week, did the delivered therapy meet the goal documented in Rowan’s treatment plan? State the goal, the relevant therapy-day and minute totals, and whether it was met, not met, or cannot be determined from the current record.

*Resolved to* `compliance(mrn='HG-M042', start='2026-01-05', end='2026-01-30')` *in 1 ms (router: rules).*

**Goal as documented in the treatment plan**

- therapy days >= 3 per mon_sun week, effective 2026-01-05 to 2026-01-30
    - counts: individual_therapy, group_therapy, family_therapy
    - BH-D003 [779:918] "Local treatment participation goal: at least 3 therapy days and at least 150 minutes of patient-present therapy in each Monday–Sunday week."
- therapy minutes >= 150 per mon_sun week, effective 2026-01-05 to 2026-01-30
    - counts: individual_therapy, group_therapy, family_therapy
    - BH-D003 [779:918] "Local treatment participation goal: at least 3 therapy days and at least 150 minutes of patient-present therapy in each Monday–Sunday week."

| week | therapy days (goal) | therapy minutes (goal) | result |
|---|---|---|---|
| 2026-01-05 -> 2026-01-11 | 3 (>=3) MET | 140 (>=150) NOT_MET | **NOT_MET** |
| 2026-01-12 -> 2026-01-18 | 2 (>=3) NOT_MET | 120 (>=150) NOT_MET | **NOT_MET** |
| 2026-01-19 -> 2026-01-25 | 3 (>=3) MET | 180 (>=150) MET | **MET** |
| 2026-01-26 -> 2026-02-01 *(partial)* | 3 (>=3) MET | 145-155 (>=150) CANNOT_DETERMINE | **CANNOT_DETERMINE** |

**Arithmetic**

- week 2026-01-05..2026-01-11 therapy_days: 3 >= 3 -> MET
- week 2026-01-05..2026-01-11 therapy_minutes: 140 >= 150 -> NOT_MET
- week 2026-01-12..2026-01-18 therapy_days: 2 >= 3 -> NOT_MET
- week 2026-01-12..2026-01-18 therapy_minutes: 120 >= 150 -> NOT_MET
- week 2026-01-19..2026-01-25 therapy_days: 3 >= 3 -> MET
- week 2026-01-19..2026-01-25 therapy_minutes: 180 >= 150 -> MET
- week 2026-01-26..2026-02-01 therapy_days: 3 >= 3 -> MET
- week 2026-01-26..2026-02-01 therapy_minutes: 145-155 >= 150 -> CANNOT_DETERMINE

**Why a week can be undetermined**

- `HG-E115` 2026-01-26: 40-50 min -- records of equal authority disagree and neither is a correction: kept as bounds (BH-D111=40min; BH-D110=50min)
    - would be settled by: an addendum or correction from either author that names the other record's value, or a check-in/room-transfer timestamp for the start of the contact

## DEV-04

> Reconstruct the care on January 19 and January 21. How many therapy contacts and patient therapy minutes occurred on each date, and how do the attendance records, clinical notes, later documents, and telehealth records affect your answer?

*Resolved to* `day_detail(mrn='HG-M042', dates=['2026-01-19', '2026-01-21'])` *in 1 ms (router: rules).*

> **Routing warning:** the question does not name a patient; answered for the only patient in the collection (HG-M042)

**2026-01-19** -- 2 therapy contact(s), 90 patient therapy minutes (2 contact record(s) of all kinds).

- `HG-E110` group therapy -- occurred=yes, patient present=yes, 60 min (counted)
    - resolved patient-present interval(s): [['10:00', '10:45'], ['11:00', '11:15']], minus break(s) [['10:45', '11:00']]
    - [RULE-PRES-02] rejected_presence = present -- retransmission cannot establish that care was delivered (BH-D104)
    - [RULE-PRES-02] occurred = yes -- attested or clinically documented patient contact: BH-D102
    - [RULE-DUR-04] breaks = [["10:45", "11:00"]] -- nontherapeutic intervals subtracted by overlap with the patient's own presence interval, not as a flat amount
    - [RULE-DUR-05] rejected_duration = [['10:00', '11:30']] -- retransmission contributes no duration (BH-D104)
    - [RULE-DUR-01] minutes = 60 -- attendance_register BH-D102 is the authority for duration on a group_therapy event; intervals+correction; correction 6853e674a5cd-C01 applied
    - BH-D103 [223:320] "Correction: Patient departure for HG-E110 is 11:15, replacing the original roster value of 11:30."
    - BH-D103 [156:221] "Applies to group encounter HG-E110, service date January 19, 2026"
    - BH-D101 [114:169] "Group encounter: HG-E110 | Facilitator: Leah Chen, LCSW"
    - BH-D102 [296:364] "Patient arrival: 10:00 | Patient departure: 11:30 | Status: Attended"
    - BH-D104 [789:857] "Patient arrival: 10:00 | Patient departure: 11:30 | Status: Attended"
- `HG-E111` individual therapy -- occurred=yes, patient present=yes, 30 min (counted)
    - resolved patient-present interval(s): [['11:15', '11:45']]
    - [RULE-PRES-02] occurred = yes -- attested or clinically documented patient contact: BH-D105
    - [RULE-DUR-02] minutes = 30 -- clinical_note BH-D105 is the authority for duration on a individual_therapy event; intervals
    - BH-D105 [172:224] "Patient contact: 11:15–11:45 | Completed: 30 minutes"

Wall-clock check: no two counted contacts overlap in time.

**2026-01-21** -- 1 therapy contact(s), 45 patient therapy minutes (1 contact record(s) of all kinds).

- `HG-E112` individual therapy -- occurred=yes, patient present=yes, 45 min (counted)
    - resolved patient-present interval(s): [['13:00', '13:20'], ['13:30', '13:55']], minus break(s) [['13:20', '13:30']]
    - [RULE-PRES-02] rejected_presence = present -- platform_log cannot establish that care was delivered (BH-D106)
    - [RULE-PRES-02] rejected_presence = present -- platform_log cannot establish that care was delivered (BH-D106)
    - [RULE-PRES-02] occurred = yes -- attested or clinically documented patient contact: BH-D106
    - [RULE-DUR-04] breaks = [["13:20", "13:30"]] -- nontherapeutic intervals subtracted by overlap with the patient's own presence interval, not as a flat amount
    - [RULE-DUR-05] rejected_duration = [['13:00', '13:20']] -- platform_log contributes no duration (BH-D106)
    - [RULE-DUR-05] rejected_duration = [['13:30', '13:55']] -- platform_log contributes no duration (BH-D106)
    - [RULE-DUR-02] minutes = 45 -- clinical_note BH-D106 is the authority for duration on a individual_therapy event; intervals
    - BH-D106 [1462:1517] "HG-A112 | VC-112A | January 21 13:00 | January 21 13:20"
    - BH-D106 [1518:1573] "HG-A112 | VC-112B | January 21 13:30 | January 21 13:55"
    - BH-D106 [368:416] "Total patient psychotherapy contact: 45 minutes."

Wall-clock check: no two counted contacts overlap in time.

**Arithmetic**

- 2026-01-19: HG-E110 60 + HG-E111 30 = 90 min
- 2026-01-21: HG-E112 45 = 45 min

## DEV-05

> Summarize the documented symptom course during the episode and the reason for the additional individual contact on January 19. Which symptom assessments are distinct, and what conclusions about progress can and cannot be supported?

*Resolved to* `progress(mrn='HG-M042', start='2026-01-05', end='2026-01-30')` *in 2 ms (router: rules).*

> **Routing warning:** the question does not name a patient; answered for the only patient in the collection (HG-M042)

**3 distinct symptom assessment(s) in the record**

| instrument | completed | total | items | form id | source records |
|---|---|---:|---|---|---|
| PHQ-9 | 2026-01-05 | 18 |  | *not stated* | 1 |
| PHQ-9 | 2026-01-16 | 14 |  | HG-Q116 | 2 |
| PHQ-9 | 2026-01-30 | 10 | {'item9': 0} | *not stated* | 1 |

**Measure provenance** (a re-import collapses onto the original administration, RULE-MEAS-01)

- PHQ-9 2026-01-05 = 18
    - BH-D002 [1394:1449] "PHQ-9 completed by Rowan on 2026-01-05: total score 18."
- PHQ-9 2026-01-16 = 14
    - BH-D013 [191:238] "Instrument: PHQ-9 | Patient portal form HG-Q116"
    - BH-D014 [572:615] "PHQ-9   | 14     | 2026-01-16     | HG-Q116"
- PHQ-9 2026-01-30 = 10
    - BH-D115 [185:201] "PHQ-9 total: 10."

**Contacts the record describes as added or unscheduled**

- 2026-01-19 `HG-E110` group therapy, 60 min (counted as therapy)
    - stated reason: "The facilitator offered grounding and arranged a same-day individual meeting with the treating clinician."
    - BH-D101 [1108:1214] "The facilitator offered grounding and arranged a same-day individual meeting with the treating clinician."
- 2026-01-19 `HG-E111` individual therapy, 30 min (counted as therapy)
    - stated reason: "This visit was added because Rowan became anxious during group and needed individual grounding and review of coping strategies."
    - BH-D105 [254:381] "This visit was added because Rowan became anxious during group and needed individual grounding and review of coping strategies."

**What the record supports**

- PHQ-9 total fell from 18 to 10 between 2026-01-05 and 2026-01-30 (3 distinct patient-completed questionnaire(s): 2026-01-05=18, 2026-01-16=14, 2026-01-30=10)
    - BH-D002 [1394:1449] "PHQ-9 completed by Rowan on 2026-01-05: total score 18."
    - BH-D115 [185:201] "PHQ-9 total: 10."
- avoidance of work communication: 6 persistence, 4 mixed, 3 improvement, 3 worsening observation(s) in the record (dated spans from clinician, informant, patient record(s), 2026-01-12 to 2026-01-30)
    - BH-D009 [973:1071] "They identified looking at one message as a lower step than replying to every outstanding message."
    - BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
    - BH-D011 [509:615] "They have not yet replied to the message and continue to imagine being asked questions they cannot answer."
    - BH-D011 [1157:1269] "They said the message seemed less overwhelming when it did not need to solve the entire return-to-work question."
- daily activity and task initiation: 5 persistence, 3 improvement, 2 mixed, 1 worsening observation(s) in the record (dated spans from clinician, informant record(s), 2026-01-05 to 2026-01-30)
    - BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
    - BH-D007 [1228:1355] "They became more animated when describing a shared evening walk and identified this as support that did not feel like pressure."
    - BH-D009 [1072:1200] "They also described taking a walk with Casey over the weekend and noted that it helped the evening feel less dominated by worry."
    - BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
- anxiety: 4 improvement, 4 persistence, 2 worsening, 1 mixed observation(s) in the record (dated spans from clinician, patient record(s), 2026-01-12 to 2026-01-30)
    - BH-D009 [1072:1200] "They also described taking a walk with Casey over the weekend and noted that it helped the evening feel less dominated by worry."
    - BH-D011 [1067:1156] "Rowan noticed physical tension during the rehearsal but was able to remain with the task."
    - BH-D011 [1519:1612] "Affect was more varied than at intake, although worry was evident when discussing employment."
    - BH-D105 [423:584] "The patient described feeling overwhelmed when other members discussed workplace demands and worried that returning to work would expose difficulties keeping up."
- sleep: 5 persistence, 2 mixed, 1 worsening observation(s) in the record (dated spans from clinician, patient record(s), 2026-01-05 to 2026-01-30)
    - BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
    - BH-D015 [840:997] "They said the morning had gotten away from them after a poor night of sleep and confirmed that they still intended to attend the next day's visit with Casey."
    - BH-D011 [734:824] "Sleep remains interrupted, and getting started in the morning continues to require effort."
    - BH-D013 [612:655] "Rowan also described sleep as inconsistent."
- mood: 3 mixed, 1 worsening observation(s) in the record (dated spans from clinician record(s), 2026-01-05 to 2026-01-30)
    - BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
    - BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
    - BH-D115 [282:435] "The patient continued to endorse sleep difficulty and trouble sustaining usual activities, with fewer days of pervasive low mood than reported at intake."
    - BH-D114 [420:552] "Mood felt less persistently low than earlier in the month, although anxiety remained noticeable when anticipating contact with work."
- support at home: 2 improvement observation(s) in the record (dated spans from clinician, patient record(s), 2026-01-09 to 2026-01-14)
    - BH-D007 [1228:1355] "They became more animated when describing a shared evening walk and identified this as support that did not feel like pressure."
    - BH-D011 [616:733] "Rowan described the family appointment as helpful because the planned check-in with Casey reduced repeated reminders."
- participation in treatment: 1 persistence observation(s) in the record (dated spans from clinician record(s), 2026-01-09 to 2026-01-09)
    - BH-D007 [1171:1227] "Rowan remained present and engaged throughout the visit."
- safety: 1 persistence observation(s) in the record (dated spans from patient record(s), 2026-01-19 to 2026-01-19)
    - BH-D105 [1180:1283] "Rowan denied current suicidal thoughts and remained future oriented in discussing the next appointment."

**What the record does not support**

- a severity category or diagnostic threshold for any questionnaire score -- no document in the record states a severity band for these scores; the abstraction carries only the totals the documents state
- that improvement was caused by any particular service -- the record documents co-occurring therapy, medication management and family support; no document attributes change to one of them
- that every questionnaire in the chart is a separate administration -- at least one measure carries no form identifier, so re-imports can only be collapsed on instrument and completion date (RULE-MEAS-01)

**Dated observations by domain**

*activity*
- 2026-01-05 [worsening, clinician] BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
- 2026-01-09 [improvement, clinician] BH-D007 [1228:1355] "They became more animated when describing a shared evening walk and identified this as support that did not feel like pressure."
- 2026-01-12 [improvement, clinician] BH-D009 [1072:1200] "They also described taking a walk with Casey over the weekend and noted that it helped the evening feel less dominated by worry."
- 2026-01-13 [mixed, clinician] BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
- 2026-01-14 [persistence, clinician] BH-D011 [1067:1156] "Rowan noticed physical tension during the rehearsal but was able to remain with the task."
- 2026-01-16 [improvement, informant] BH-D012 [683:797] "Casey reported that Rowan had been getting out for short walks and seemed more willing to discuss the coming week."
- 2026-01-16 [persistence, clinician] BH-D013 [946:1057] "Rowan has attempted small activities and communication practice but has not yet established a reliable routine."
- 2026-01-26 [persistence, clinician] BH-D110 [1154:1267] "The patient remained attentive and collaborative, although hesitant about completing the task outside the office."
- 2026-01-30 [mixed, clinician] BH-D115 [282:435] "The patient continued to endorse sleep difficulty and trouble sustaining usual activities, with fewer days of pervasive low mood than reported at intake."
- 2026-01-30 [persistence, clinician] BH-D115 [688:878] "The patient has taken some initial steps, including drafting and sending a message, but continues to delay follow-up and becomes anxious when a task expands beyond a narrowly de..."
- 2026-01-30 [persistence, clinician] BH-D115 [879:971] "Sleep disruption remains an intermittent barrier to establishing a steadier daytime routine."

*anxiety*
- 2026-01-12 [improvement, clinician] BH-D009 [1072:1200] "They also described taking a walk with Casey over the weekend and noted that it helped the evening feel less dominated by worry."
- 2026-01-14 [persistence, clinician] BH-D011 [1067:1156] "Rowan noticed physical tension during the rehearsal but was able to remain with the task."
- 2026-01-14 [improvement, clinician] BH-D011 [1519:1612] "Affect was more varied than at intake, although worry was evident when discussing employment."
- 2026-01-19 [worsening, clinician] BH-D105 [423:584] "The patient described feeling overwhelmed when other members discussed workplace demands and worried that returning to work would expose difficulties keeping up."
- 2026-01-19 [improvement, clinician] BH-D105 [793:935] "Rowan participated throughout the individual contact and reported that the immediate intensity of anxiety eased enough to discuss a next step."
- 2026-01-19 [worsening, patient] BH-D101 [968:1108] "When discussion turned to returning to the workplace, Rowan became visibly tense and said the amount of discussion felt difficult to manage."
- 2026-01-26 [persistence, clinician] BH-D111 [1138:1254] "The patient could use the cue during the rehearsal but remained uncertain about using it independently when anxious."
- 2026-01-26 [improvement, clinician] BH-D110 [419:551] "The reply reduced one uncertainty but also brought up worry about being asked for commitments the patient might not be able to meet."
- 2026-01-30 [persistence, clinician] BH-D115 [688:878] "The patient has taken some initial steps, including drafting and sending a message, but continues to delay follow-up and becomes anxious when a task expands beyond a narrowly de..."
- 2026-01-30 [persistence, clinician] BH-D113 [1238:1331] "Rowan remained anxious about the work conversation but could explain the intended first step."
- 2026-01-30 [mixed, clinician] BH-D114 [420:552] "Mood felt less persistently low than earlier in the month, although anxiety remained noticeable when anticipating contact with work."

*avoidance_work*
- 2026-01-12 [improvement, clinician] BH-D009 [973:1071] "They identified looking at one message as a lower step than replying to every outstanding message."
- 2026-01-13 [mixed, clinician] BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
- 2026-01-14 [persistence, clinician] BH-D011 [509:615] "They have not yet replied to the message and continue to imagine being asked questions they cannot answer."
- 2026-01-14 [improvement, clinician] BH-D011 [1157:1269] "They said the message seemed less overwhelming when it did not need to solve the entire return-to-work question."
- 2026-01-14 [improvement, clinician] BH-D011 [1519:1612] "Affect was more varied than at intake, although worry was evident when discussing employment."
- 2026-01-16 [persistence, informant] BH-D012 [798:902] "Mornings remain difficult, and Casey described Rowan becoming quiet when the conversation turns to work."
- 2026-01-16 [mixed, patient] BH-D013 [435:611] "In the optional update field, Rowan wrote that getting out of the apartment had become a little easier, while thinking about work continued to make them want to put things off."
- 2026-01-19 [worsening, clinician] BH-D105 [423:584] "The patient described feeling overwhelmed when other members discussed workplace demands and worried that returning to work would expose difficulties keeping up."
- 2026-01-19 [worsening, patient] BH-D101 [968:1108] "When discussion turned to returning to the workplace, Rowan became visibly tense and said the amount of discussion felt difficult to manage."
- 2026-01-26 [worsening, clinician] BH-D111 [678:766] "The patient anticipated becoming overwhelmed if several work issues were raised at once."
- 2026-01-26 [persistence, clinician] BH-D110 [552:617] "Rowan continued to postpone choosing a time for the conversation."
- 2026-01-26 [persistence, clinician] BH-D110 [618:721] "Sleep remained uneven, with difficulty settling on nights when work-related thoughts became repetitive."
- 2026-01-30 [mixed, clinician] BH-D115 [540:687] "Clinician review, January 30: Rowan shows partial improvement, with persistent avoidance and meaningful functional impact around returning to work."
- 2026-01-30 [persistence, clinician] BH-D115 [688:878] "The patient has taken some initial steps, including drafting and sending a message, but continues to delay follow-up and becomes anxious when a task expands beyond a narrowly de..."
- 2026-01-30 [persistence, clinician] BH-D113 [1238:1331] "Rowan remained anxious about the work conversation but could explain the intended first step."
- 2026-01-30 [mixed, clinician] BH-D114 [420:552] "Mood felt less persistently low than earlier in the month, although anxiety remained noticeable when anticipating contact with work."

*mood*
- 2026-01-05 [worsening, clinician] BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
- 2026-01-13 [mixed, clinician] BH-D010 [575:698] "They described mood as somewhat less heavy on days with a planned activity but remained concerned about work communication."
- 2026-01-30 [mixed, clinician] BH-D115 [282:435] "The patient continued to endorse sleep difficulty and trouble sustaining usual activities, with fewer days of pervasive low mood than reported at intake."
- 2026-01-30 [mixed, clinician] BH-D114 [420:552] "Mood felt less persistently low than earlier in the month, although anxiety remained noticeable when anticipating contact with work."

*participation*
- 2026-01-09 [persistence, clinician] BH-D007 [1171:1227] "Rowan remained present and engaged throughout the visit."

*safety*
- 2026-01-19 [persistence, patient] BH-D105 [1180:1283] "Rowan denied current suicidal thoughts and remained future oriented in discussing the next appointment."

*sleep*
- 2026-01-05 [worsening, clinician] BH-D002 [378:534] "Reason for care: Rowan describes several weeks of low mood, reduced interest in usual activities, fragmented sleep, and difficulty beginning ordinary tasks."
- 2026-01-08 [persistence, clinician] BH-D015 [840:997] "They said the morning had gotten away from them after a poor night of sleep and confirmed that they still intended to attend the next day's visit with Casey."
- 2026-01-14 [persistence, clinician] BH-D011 [734:824] "Sleep remains interrupted, and getting started in the morning continues to require effort."
- 2026-01-16 [persistence, patient] BH-D013 [612:655] "Rowan also described sleep as inconsistent."
- 2026-01-21 [mixed, patient] BH-D106 [1011:1100] "Rowan described one night of improved sleep followed by a night of prolonged wakefulness."
- 2026-01-26 [persistence, clinician] BH-D110 [618:721] "Sleep remained uneven, with difficulty settling on nights when work-related thoughts became repetitive."
- 2026-01-30 [mixed, clinician] BH-D115 [282:435] "The patient continued to endorse sleep difficulty and trouble sustaining usual activities, with fewer days of pervasive low mood than reported at intake."
- 2026-01-30 [persistence, clinician] BH-D115 [879:971] "Sleep disruption remains an intermittent barrier to establishing a steadier daytime routine."

*social_support*
- 2026-01-09 [improvement, clinician] BH-D007 [1228:1355] "They became more animated when describing a shared evening walk and identified this as support that did not feel like pressure."
- 2026-01-14 [improvement, patient] BH-D011 [616:733] "Rowan described the family appointment as helpful because the planned check-in with Casey reduced repeated reminders."

