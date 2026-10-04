# `extract/rules.py` — function-by-function walkthrough

The deterministic extractor. Its job is to turn one document into a list of
`Claim` objects: assertions, each with a source span. It decides nothing about
truth — no conflict resolution, no arithmetic, no "which document wins". That
all happens later in `reconcile.py` and `queries.py`.

The organising idea: **find every clock time in the document, then classify each
one by the words around it.** That is what lets one code path read a facilitator
narrative, a front-desk roster, a telehealth export and a billing extract
without a per-document-type parser.

---

## Contents

- [The four ranking systems](#the-four-ranking-systems)
- [Module-level patterns and cue lists](#module-level-patterns-and-cue-lists)
- [Small helpers](#small-helpers)
- [Header facts](#header-facts)
- [Table parsing](#table-parsing)
- [`extract` — the seven stages](#extract--the-seven-stages)
- [Row → claim](#row--claim)
- [Corrections](#corrections)
- [Attestations](#attestations)
- [Narrative encounters](#narrative-encounters)
- [Span selection](#span-selection)
- [Intervals and stated minutes](#intervals-and-stated-minutes)
- [Measures in prose](#measures-in-prose)
- [Plan requirements](#plan-requirements)
- [Billing and authorization](#billing-and-authorization)
- [Observations](#observations)
- [What this file deliberately does not do](#what-this-file-deliberately-does-not-do)

---

## The four ranking systems

Four places in this file choose between candidates. They are easy to confuse
because three of them use the numbers 3/2/1, so they are collected here.

### 1. `_classify_interval` — ordered precedence, not numeric

An `if/elif` chain. The **first** matching class wins, so the order encodes the
policy:

```
break            BREAK_CUES   "break", "nontherapeutic", "connection was lost", ...
scheduled        SCHED_CUES   "scheduled", "slot", "room schedule", ...
other_present    OTHER_CUES   "therapist session interval", "partner only", ...
patient_present  PRESENT_CUES "patient contact", "patient arrival", "attended", ...
service          (nothing matched)
```

Why `break` is tested first: *"Connection was lost from 13:20–13:30; there was
no therapeutic contact during that interval"* contains the word **contact**,
which is a `PRESENT_CUES` member. Tested in the other order, a ten-minute
dropout would be counted as ten minutes of therapy. The precedence is the
safety property.

Why `scheduled` outranks `patient_present`: a booked slot is never evidence the
patient was there. `"Group scheduled 10:00–11:30"` must not become 90 minutes
of attendance just because the facilitator later writes that the patient
attended.

`service` is the fallback for an unlabelled interval on an encounter header line
(`"Encounter HG-E104 | 2026-01-09, 14:00–14:45"`). It is used as patient time
only in one narrow case, described under
[narrative encounters](#narrative-encounters).

### 2. `_presence_detail` — contextual authority, 3 / 2 / 1

This answers "does this document say the patient attended?" Each **occurrence**
of each presence cue is scored by *where it sits*:

| score | condition | reading |
|---:|---|---|
| **3** | the cue is the value of a labelled field — the text between the start of its sentence and the cue itself ends with `status`/`disposition`/`attendance`/`roster` and an optional colon — or its sentence contains `status`, `final disposition` or `desk status` | a disposition field: the record's formal answer |
| **2** | the cue's sentence contains the word `patient`, or the patient's given name (read from the header at runtime) | an attestation about this patient |
| **1** | anything else | a passing mention |

The highest score wins; a longer cue phrase breaks a tie.

**Why score rather than order or length.** The original version returned the
first cue in list order, so a note mentioning a "cancellation" anywhere was read
as cancelled even when it also attested attendance. The obvious fix —
longest-phrase-wins — *changed nothing*, because `"cancellation"` (12
characters) is longer than `"attended"` (8). Length is not the discriminator.
Position in a labelled field is.

```
"Status: Attended. A cancellation notice was sent to other members."
   attended      -> 3   (field label "Status:" immediately precedes it)
   cancellation  -> 1   (passing mention in a later sentence)
   => present
```

**The tie rule, which matters more than the scoring.** After sorting, `pos` is
the best-scoring positive cue and `neg` the best-scoring negative one. If their
scores are **equal**, the function returns `presence = None` and reports the
competition in an `ambiguity` dict. It deliberately does not pick:

```
"The patient attended. The patient cancellation was recorded."
   attended      -> 2   ("patient" in its sentence)
   cancellation  -> 2   ("patient" in its sentence)
   => None, plus {"competing": ["attended", "cancellation"], ...}
```

A document that says both things does not settle attendance. Returning `None`
lets `reconcile` fall back to other records instead of believing whichever
phrase happened to be longer or listed first. That is the honest answer, and
`RULE-PRES-05` logs it.

One short-circuit sits above the scoring entirely: `NO_PATIENT_CUES` are
phrases that assert the patient was absent for the *whole* contact
(`"absent for the entire"`, `"patient participation: none"`). If one is present
the function returns immediately with `absent` and a `negation_kind`, because
those phrases are unambiguous by construction.

### 3. `_stated_minutes` — rank 3 / 2 / 1 over "N minutes" phrases

A document may state several durations. This picks the one that is stated as
*the patient's* time:

| rank | condition on the enclosing sentence | example |
|---:|---|---|
| **3** | contains `patient-present`, `patient psychotherapy contact`, or `total patient` | `"Total patient psychotherapy contact: 45 minutes"` |
| **2** | contains `present`, `completed`, or `contact` | `"completed, 50 minutes"` |
| **1** | neither | a bare `"45 minutes"` |

Before scoring, any occurrence whose sentence classifies as `break`,
`scheduled` or `other_present` is **discarded outright**. That is what keeps
`"Therapist session interval: 13:00–13:45, 45 minutes"` from being read as the
patient's 45 minutes when the patient was present for only 30 of them.

The highest rank wins, with a strict `>` comparison, so the first occurrence
survives a tie. Rank also sets `minutes_basis`: `stated_patient` at rank ≥ 2,
`stated_total` at rank 1 — which tells `reconcile` how much to trust it.

### 4. `_best_span` — evidence-quality score, 0–3

Not about facts at all; about which sentence to quote. When a reviewer asks
"where does 60 minutes come from?", they should land on the sentence carrying
the times, not the document title. The same clock time often appears twice:

```
Scheduled opening: 10:00 | Scheduled closing: 11:30     <- not this one
Patient arrival: 10:00 | Patient departure: 11:30       <- this one
```

So every occurrence of the arrival time is scored:

```
score = 2 * (the sentence classifies as patient_present)
      + 1 * (the departure time also appears in that sentence)
```

giving 0–3, highest wins. In the example above the scheduled line scores **1**
(it classifies as `scheduled`, so no 2, but it does contain 11:30, so +1) and
the patient-arrival line scores **3** (patient_present, and it holds 11:30 too).
The margin is what matters, not the absolute values.

Note the weights are not interchangeable: being *about the patient* is worth 2
and holding the paired time is worth 1, so a patient-present sentence missing
the departure time (score 2) still beats a scheduled line that has both
(score 1). Attribution outranks completeness.

1. the stated-minutes span, if there is one — the most direct evidence
2. the best-scoring patient-present interval sentence
3. the sentence containing the presence cue, then the one containing the
   encounter reference
4. the document's first line, as a last resort

---

## Module-level patterns and cue lists

**Time patterns.** `MERID` is an optional meridiem (`AM`, `p.m.`). `T` is a
full `hh:mm` with optional meridiem; `T_LOOSE` additionally allows a bare hour,
used only for the *end* of a range so `"10:45-11"` parses. `SEP` accepts a dash
(hyphen, en dash, em dash) or the words `to`, `through`, `until`, `thru`.
`RANGE` is `T SEP T_LOOSE`; `SINGLE` is a lone time.

Note `RANGE` requires a full `hh:mm` on the **left**. That is deliberate: it
stops `"8 to 10 members"` and `"Goal 2 to 3 sessions"` from parsing as time
ranges.

**Identifier patterns.** `ENC`, `APPT`, `FORM`, `CALL`, `CHARGE` are kept
separate because the three identifier systems mean different things — only an
encounter id identifies a real-world contact. A call id (`VC-112A`) is evidence
attached to an encounter and must never key one, or the January 21 telehealth
session with its reconnect becomes two sessions.

`DOB` exists mainly so its span can be **masked** before date parsing: a date of
birth is the first ISO date in several of these documents and would otherwise be
read as a service date.

**Cue lists.** `BREAK_CUES`, `SCHED_CUES`, `OTHER_CUES`, `PRESENT_CUES` feed
`_classify_interval`. `NO_PATIENT_CUES` are total-absence phrases paired with a
`negation_kind` (`patient_absent` = the contact happened without the patient;
`not_a_contact` = this document records no visit at all, e.g. an import
receipt). That distinction is load-bearing: the first still produces an event,
the second must never be matched to one.

`NEG_KIND` maps a cue to a finer negation kind so `reconcile` can tell a no-show
(nothing happened) from a cancellation from a patient-absent contact.

---

## Small helpers

### `_span_of_context(text, pos) -> (start, end)`
The sentence-ish window containing `pos`, bounded by the nearest newline, `". "`
or `"; "` on each side. Nearly every classification decision in the file is made
on this window rather than the whole document, which is what keeps a cue in
paragraph 5 from affecting a time in paragraph 1.

### `_mk_claim(doc_pk, facts, text, span, **kw) -> Claim`
Builds a `Claim`, defaulting `authority_class` to the document class and
`patient_mrn` to the header MRN (either can be overridden — a posted charge
inside an administrative wrapper passes `authority_class="billing"`).

The three lines after construction matter: the quote is `.strip()`ed, so the
stored offsets are then corrected forward by the amount of leading whitespace
removed and the end recomputed from the quote's length. Without that, stored
offsets would not reproduce the stored quote, and `Claim.verify` would fail.
`verify` is called immediately, so a bad span is caught at creation.

---

## Header facts

### `doc_facts(text) -> DocFacts`
Everything true of the document as a whole: `doc_class` (from
`taxonomy.classify_document`, which reads the masthead), publisher document id,
MRN, DOB, patient name, received and authored dates.

The name pattern takes the capitalised token run immediately **before** a `DOB`
or `MRN` marker — `"Rowan Mercer | DOB 1991-04-12"`. That is a layout
regularity, not a name list, so no patient name is encoded. `given_name` is the
first token, and is used later to recognise `"Rowan present with partner:
13:15–13:45"` as patient-present time.

### `_guess_year(text) -> int | None`
First four-digit year in the document, used as the default year when a document
writes `"Jan05"` or `"January 22"` without one.

---

## Table parsing

### `COLMAP`
Maps header-cell wording to a canonical column key. Several spellings map to one
key (`patient arrived`, `actual arrival`, `arrival`, `arrived`, `connected` all
→ `arrival`), so a roster, a disposition register and a telehealth export are all
read by the same code.

### `_header_map(cells) -> dict | None`
Decides whether a pipe-delimited line is a **header**. Requires at least three
recognised columns, and — the important guard — **rejects the line outright if
any cell contains a time or a run of three or more digits.** A header names
columns and carries no values. Without that guard,
`"Patient arrival: 10:00 | Patient departure: 11:30 | Status: Attended"` maps to
three known columns and would be treated as a header, making the following lines
its data rows.

### `_tables(text)`
Yields `(header_map, rows)` for each table. Tracks byte offsets per line so row
claims get real spans. A row block ends at a blank line or a line with too few
pipes. After yielding, the scan resumes **after** the block, so a table's rows
are never re-examined as a second header.

---

## `extract` — the seven stages

```python
def extract(text, doc_pk, store=None) -> (DocFacts, list[Claim])
```

`store` is accepted only for interface parity with the LLM extractor, which
caches responses there. The rules path is stateless, which is what makes it
byte-reproducible.

1. **Tables.** Every row becomes its own claim — a measure row, a platform call
   row, or an encounter row. Each row's span is recorded in `consumed`, and the
   encounter ids it covered in `row_encs`.

2. **Masking.** `scan = _mask(text, consumed + _dob_spans(text))`. Table rows
   and dates of birth are overwritten with spaces, **preserving every offset**
   (newlines are kept so line structure survives). All prose rules then run on
   `scan`, while quotes are still taken from the original `text`. This is what
   stops a row being re-read as narrative, and a DOB being read as a service
   date. Offsets stay valid because masking is length-preserving.

3. **Corrections.**
4. **Narrative encounters**, skipping any encounter a table row already owns.
5. **Attestations**, only for documents that had tables.
6. **Prose measures**, **plan requirements**, **billing/authorization**.
7. **Observations.**

Finally every claim gets an id if it lacks one, and `verify` runs again on all
of them against the raw text.

---

## Row → claim

### `_encounter_row`
One table row to one encounter claim. Presence comes from the status cell, and
note the trick: the cell is wrapped as `f"Final disposition: {status_raw}"`
before being handed to `_presence_detail`, so the cue scores **3** — a status
cell *is* a disposition field. Arrival and departure produce a patient-present
interval; the scheduled cell is stored under `fields["scheduled"]` and never
treated as patient time.

### `_measure_row`
A questionnaire row: instrument, numeric result, completion date, form id.

### `_call_row`
A telehealth connection row. Forces `authority_class="platform_log"` regardless
of the document's own class, because a connection log corroborates intervals but
cannot establish that therapy happened. Keeps `call_ref` separate from
`appointment_ref` so `reconcile` can attach it by appointment and never key an
event on the call id.

---

## Corrections

### `_corrections`
`CORR_SENT` finds a sentence containing the word "correction". Because `[^.]*`
crosses newlines, the match often swallows the masthead too, which creates the
problem this function then has to solve.

The new value is `(?:is|should be|becomes|=) <time>`; the old value is
`replacing ... (?:value of|of) <time>`.

**Which field is being corrected** is the subtle part. The field word is the one
**closest before the assertion** (`low.rfind(k, 0, assert_at)`, keeping the
largest position). The naive version took the longest matching field word over
the whole block, so in:

```
Applies to group encounter HG-E110, service date January 19, 2026
Correction: Patient departure for HG-E110 is 11:15, replacing ... 11:30.
```

`"service date"` (12 chars) beat `"departure"` (9) and the correction was
recorded as changing the *service date*. Closest-before fixes it.

The stored quote is then narrowed: if `"correction:"` appears before the
assertion, the span starts there, so the evidence is the correction statement
rather than everything from the masthead down.

### `_correction_scope`
Captures the document's own statement of what the correction does **not** change
("This correction applies only to…"), kept as context for a reviewer.

---

## Attestations

### `_attestations`
For documents that contain tables, the surrounding prose is also worth keeping —
`"January 22 attendance attestation: Rowan arrived at 10:30 and remained until
the group closed."` These claims are marked `evidence_only: True` and carry **no
intervals and no minutes**: the rows are the numeric record. Their only job is to
give a reviewer the sentence a disposition rests on. That separation is why a
prose paragraph can never silently compete with a signed row.

---

## Narrative encounters

### `_sections(text)`
Splits a document into per-encounter sections when it names more than one, by
locating each distinct encounter reference and cutting at line boundaries. This
is what makes `BH-D107`'s January 22 break attach to `HG-E113` and its January
29 break to `HG-E118`, rather than both landing on one event.

### `_narrative_encounters`
The largest function. Per section:

**Service type** from `canonical_service` on the first 600 characters (the
masthead and header block), falling back to the whole section.

**Service date**, by a three-rung ladder, with the rung recorded in
`fields["service_date_basis"]` so the choice is auditable:

1. `labelled_service_date` — a line labelling it (`"Service date: …"`)
2. `encounter_header_line` — a date on the same line as this encounter's own
   reference
3. `labelled_date`, then `first_date_in_section` — weakest, and the reason the
   stronger rungs exist: a section can open with a signature or export date that
   is not the service date

**Intervals** from `_intervals`, then presence from `_presence_detail`.

**Two overrides, in order:**

- If a labelled patient-present interval exists but presence is not `present`,
  presence is forced to `present`. An explicit patient-present interval *is* a
  statement of attendance, and it outranks a document-level cue about one
  portion of the session — `"Rowan was not present for that portion"` sits in
  the same note as `"Rowan present with partner: 13:15–13:45"`.
- If there is no labelled patient-present interval, the service is **not** a
  group, and presence is `present`, the unlabelled `service` interval is
  promoted to patient time (`iv_basis = "service_interval_fallback"`). The
  group exclusion is deliberate: for a group the front desk owns arrival and
  departure, so a facilitator's header interval must never stand in for them.

**Authority override.** If the section contains "unsigned" and "draft", or
"autogenerated progress note", its `authority_class` becomes `draft_note`
whatever the document wrapper says — which is how one administrative extract can
hold a non-establishing draft *and* a billing charge with different authorities.

A claim is emitted only if it has an interval, stated minutes, a presence value
or an encounter reference. Otherwise there is nothing to assert.

---

## Span selection

### `_added_reason`
Finds a sentence describing a contact as added, unscheduled or same-day, with
`ADDED_NEGATIONS` rejecting the inverse (`"records no additional visit"` is the
opposite claim). This is what answers "why was there an extra contact?" with the
record's own words rather than an inference.

### `_best_span`
Scoring described [above](#4-_best_span--evidence-quality-score-03).

---

## Intervals and stated minutes

### `_intervals(seg, offset, facts) -> dict`
Returns the five interval buckets. Three sources:

1. every `RANGE` match, bucketed by `_classify_interval` on its sentence
2. an **arrival/departure field pair** (`arriv\w*: <time>` … `depart\w*: <time>`)
   → patient-present. Note the pattern requires the time to follow the label
   closely, so `"Rowan arrived at 10:30"` in prose does **not** match — that is
   intentional, since prose like this is attestation, not a timing field.
3. `"joined at <time>"` combined with the latest end among the scheduled,
   other-present and service intervals — how `"Rowan joined at 13:15"` plus a
   `13:00–13:45` therapist interval yields `13:15–13:45`. Only used when no
   patient-present interval was found directly.

Each bucket is de-duplicated preserving order.

### `_stated_minutes`
Scoring described [above](#3-_stated_minutes--rank-321-over-n-minutes-phrases).
Returns `(minutes, basis, span)`; the span becomes the claim's quote, which is
why a minutes claim cites the sentence stating the minutes.

---

## Measures in prose

### `_prose_measures`
For each known instrument, finds its mentions in `scan`, skips any inside a
consumed table row, then looks for a total in a **window** of roughly −300/+400
characters rather than only the mention's own sentence — because the score
usually sits on the following line (`"Instrument: PHQ-9 | form HG-Q116"` then
`"Total score: 14"`).

Completion date prefers a nearby `"completed … <date>"` over any date in the
document, which is what keeps a January 26 *receipt* date from overwriting a
January 16 *completion* date. Also extracts a form id and an `item N: V` pair
(PHQ-9 item 9 matters clinically). `is_import` is set for administrative
wrappers, so `reconcile` can collapse a re-import onto the original
administration rather than counting a second questionnaire.

One claim per instrument per document (`break` after the first hit).

---

## Plan requirements

### `_plan`
Extracts the episode window (`EPISODE`) from any document, then — only for a
treatment plan — the requirements:

- `REQ_DAYS` → `therapy_days`
- `REQ_MIN` → `therapy_minutes`
- `REQ_HRS` → `therapy_minutes`, **multiplied by 60**, and skipped if a minutes
  requirement was already found, so a plan stating both does not double up

All three accept `at least`, `no fewer than`, `minimum of`, `a minimum of`.
`PERIOD` reads the week convention from the plan's own wording. Effective start
is the episode start, else the signature date — which is what lets a later plan
close an earlier one in `reconcile._plan_rows` and gives each week the
requirement in force *for that week*.

### `_contrib_sets` / `_types_in`
Reads which services the plan says **do** and **do not** contribute, from its own
sentences. `_types_in` enumerates canonical types in a sentence, handling
`"individual, group, and family therapy"`. This is why the inclusion rule is not
hardcoded: the plan states it, and an unseen plan with different wording is read
the same way.

---

## Billing and authorization

### `_admin`
A posted charge becomes a `charge` claim with `authority_class="billing"` —
structurally incapable of establishing delivery, which is what makes the January
27 no-show survive a posted charge for that date. An authorization letter
becomes an `authorization` claim with its quantity and unit, recorded as payer
approval and never as delivered care.

---

## Observations

### `_observations`
Narrative evidence of symptom course. Deliberately conservative, and the most
lossy part of this file (measured: it keeps 35 spans where the hybrid LLM mode
finds 120).

Skips whole document classes with no clinical prose. Then per sentence, all
filters must pass:

1. at least 40 characters
2. not forward-looking — `FORWARD_LOOKING` matches a sentence *starting* with
   goal/plan/review/recommend language; `FORWARD_PHRASES` catches intentions
   anywhere (`"wishes to"`, `"will practice"`, `"agreed to"`). A plan is not an
   observation, and without this the treatment plan's `"Goal 1: improve daily
   activity"` was being recorded as evidence of improvement.
3. refers to the patient — given name, `"the patient"`, or a sentence opening
   with `they`/`sleep`/`mood`/`affect`
4. names a clinical domain from `DOMAINS`. `participation` is dropped when any
   other domain matched, since it is the least clinically informative.
5. carries a change cue

**Polarity.**

```
up   = any IMPROVE  and not any NEGATED_IMPROVE
down = any WORSE    or      any NEGATED_IMPROVE
flat = any PERSIST

mixed       if up and (down or flat)
improvement if up
worsening   if down
persistence otherwise
```

`NEGATED_IMPROVE` is the interesting list: `"reduced interest"`, `"lower mood"`,
`"less energy"` all contain improvement words but describe **symptoms**. Without
it, `"reduced interest in usual activities"` — a presenting complaint — was
scored as improvement. Note it both cancels `up` and sets `down`.

`IMPROVE` contains `"improved"`/`"improvement"` but deliberately **not**
`"improv"`, because the bare stem matches the aspirational `"improve"` in goal
text.

**Reporter** is `informant` if the document is collateral, `patient` if the
given name appears within 40 characters before a reporting verb
(`reported`, `described`, `denied`, …), else `clinician`. That distinction is
what lets a partner's account be used as progress evidence while being labelled
as an informant report rather than the patient's own.

At most three domains per sentence are kept.

---

## What this file deliberately does not do

- **No arithmetic.** It never sums intervals, subtracts a break, or converts to
  minutes. It reports clock times as printed. `intervals.py` does the maths.
- **No conflict resolution.** Two documents disagreeing produce two claims.
  `reconcile.py` applies the authority policy.
- **No truth.** `presence` is what *this document asserts*, not what happened.
- **No patient facts.** No name, MRN, date or encounter id appears anywhere in
  it; `tests/test_units.py::SourceHygiene` and the no-patient-facts test enforce
  that across the whole package.

That boundary is the design: this file is allowed to be wrong about a document,
because every claim it makes is attributed, spans-verified, and subject to a
stated authority policy downstream.
