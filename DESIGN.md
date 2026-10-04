# Backbone — design document

A complete account of what was built, why each decision was made, what every
file does, and what the measurements showed. The README is the short version
for a reviewer; this is the long version for a design discussion.

---

## Contents

1. [The problem, stated precisely](#1-the-problem-stated-precisely)
2. [What the documents actually contain](#2-what-the-documents-actually-contain)
3. [Why this architecture](#3-why-this-architecture)
4. [Why SQLite](#4-why-sqlite)
5. [Why an LLM at all, and exactly where](#5-why-an-llm-at-all-and-exactly-where)
6. [The data model, table by table](#6-the-data-model-table-by-table)
7. [The pipeline, stage by stage](#7-the-pipeline-stage-by-stage)
8. [Every file, and why it exists](#8-every-file-and-why-it-exists)
9. [The rule catalogue](#9-the-rule-catalogue)
10. [How uncertainty is represented](#10-how-uncertainty-is-represented)
11. [How a question becomes an answer](#11-how-a-question-becomes-an-answer)
12. [The audit chain](#12-the-audit-chain)
13. [What was measured](#13-what-was-measured)
14. [The two experiments](#14-the-two-experiments)
15. [Build chronology, including every bug found](#15-build-chronology-including-every-bug-found)
16. [Limitations and what I would do next](#16-limitations-and-what-i-would-do-next)

---

## 1. The problem, stated precisely

The brief asks for a system that reads inpatient behavioral-health records and
answers questions about care delivered. Underneath the five sample questions
there are four things actually being tested, and they pull in different
directions:

| what is tested | what it demands |
|---|---|
| **accuracy** | a number that is right, not plausible |
| **conflict handling** | documents that disagree must not be silently resolved |
| **auditability** | any figure traceable to the sentence it came from |
| **reuse** | answer new questions without reprocessing 500K documents |

The tension is that the obvious approach to accuracy — read everything, reason
about it — destroys auditability and reuse. So the architecture is chosen to
decouple them: reading happens once and is recorded as evidence; conclusions are
derived from that record by code; questions are answered from the derived record.

Two constraints shaped almost every decision:

- **No patient facts may be encoded.** The system has to work on unseen
  documents and unseen questions. So no date, encounter id, patient name or
  expected total appears anywhere in the pipeline. The hand-computed answers
  live only in `tests/test_gold.py`, as assertions the pipeline must satisfy by
  deriving them.
- **A wrong number is worse than no number.** In a review context, a confident
  wrong duration is a liability. "Cannot be determined, and here is what would
  settle it" is a better output than a guess.

---

## 2. What the documents actually contain

Before writing any code I read all 31 documents and computed the ledger by
hand. This was the single highest-value hour of the build: almost every design
decision below exists because of something found in that pass. The corpus is one
patient (Rowan Mercer, MRN HG-M042), one episode (2026-01-05 to 2026-01-30), 20
encounters HG-E101–HG-E120.

**The traps, and what each one forces:**

| # | what the documents do | what it forces in the design |
|---|---|---|
| 1 | A signed Jan 19 group roster says departure 11:30. A Jan 20 correction names *that roster and that field* and sets 11:15. | Corrections must be first-class claims that target a field, not just "newer data". |
| 2 | On Jan 26 the records inbox receives a **copy of the original** Jan 19 roster, still 11:30, no correction attached. | Recency cannot be authority. A retransmission must carry none. |
| 3 | Jan 26 has **two signed notes** by two participating clinicians: 50 min (09:00–09:50) and 40 min (09:10–09:50). Neither references the other. | Genuine conflict must survive as bounds, not be broken by a tiebreak. |
| 4 | Group sessions have breaks: Jan 6 10:45–11:00, Jan 12 10:40–10:55, Jan 19/22/29 10:45–11:00. Rowan arrives 10:15 (Jan 6) and 10:30 (Jan 22). | Break subtraction must be interval *overlap*, not "minus 15". |
| 5 | Jan 21 telehealth: contact 13:00–13:20 **and** 13:30–13:55, connection lost between. Two call ids (VC-112A/B) under one appointment (HG-A112), one encounter (HG-E112). | A call id must never key an event. Split sessions must sum only connected time. |
| 6 | Jan 27: an **unsigned template note** says "Patient attended the full session" and a **charge** was posted for one group session. The signed register says no-show. | Draft notes and billing rows must be structurally incapable of establishing delivery. |
| 7 | Jan 16 is a **partner-only collateral** contact: "Rowan was absent for the entire contact." | "The contact happened" and "the patient was there" are different facts. |
| 8 | Jan 23 care coordination: "Patient participation: None." | Same, plus: service type alone decides eligibility. |
| 9 | Jan 30 family therapy: therapist interval 13:00–13:45, **partner only** 13:00–13:15, Rowan present 13:15–13:45. | Patient-present time ≠ session time. |
| 10 | A Jan 16 PHQ-9 (form HG-Q116, score 14) is **re-imported** on Jan 26 as a batch summary. | Measure identity must be instrument + form + completion date, not receipt date. |
| 11 | Documents name several encounters each: BH-D107 covers E113+E118, BH-D108 covers four, the appointment export covers nine. | One claim per encounter row, not per file. |
| 12 | Three id systems: encounters (HG-E112), appointments (HG-A112, HG-A115), calls (VC-112A/B). | Only the encounter id identifies an event. |
| 13 | The plan states the goal in one sentence: "at least 3 therapy days **and** at least 150 minutes of patient-present therapy in each Monday–Sunday week", plus which services count and which do not. | Requirements must be parsed into dated structured rows, and the inclusion list read from the plan rather than hardcoded. |
| 14 | Progress evidence sits in documents that contribute **no** therapy minutes (medication visits, the partner collateral). | Observations must be decoupled from billable events. |
| 15 | No two files are byte-identical. | Exact hashing is insufficient for dedupe. |

The hand-computed result, which the system must reproduce without being told:

```
12 therapy sessions (5 individual, 5 group, 2 family) on 11 distinct days
585–595 minutes total

Jan 5–11   3 days  140 min      (50 + 45 + 45)         goal NOT met
Jan 12–18  2 days  120 min      (75 + 45)              goal NOT met
Jan 19–25  3 days  180 min      (60 + 30 + 45 + 45)    goal met
Jan 26–30  3 days  145–155 min  ((40–50) + 75 + 30)    cannot determine
```

---

## 3. Why this architecture

### The shape

```
documents  ──►  claims  ──►  events  ──►  query functions  ──►  answers
           (1)         (2)          (3)                   (4)
```

1. **Read once, record what each document asserts.** A claim is one assertion by
   one document, with its authority class and the exact character offsets of the
   span that carries it. Nothing is overwritten here.
2. **Group claims into the real-world contacts they describe.** Several
   documents describe one session; one document describes several sessions.
3. **Resolve each field under a stated policy**, writing a decision row naming
   the rule for every outcome.
4. **Compute every number in Python** from the resolved events.

### Alternatives considered and rejected

**Ask a model the question with the documents in context.** Fails all four
tests at once. It cannot be audited (no spans), cannot be reused (every question
re-reads everything: at 500K documents it is not merely expensive, it does not
fit), and it miscounts — long-context arithmetic over twenty encounters with
corrections and exclusions is exactly the task models are least reliable at. It
also has no way to express "cannot be determined".

**RAG over the documents.** Good for "find notes discussing anxiety". Wrong for
"count every qualifying contact across four weeks". Retrieval is lossy by
design: retrieve four of five therapy notes and the system confidently reports
four sessions, with no signal that anything is missing. Recall failures are
silent, which is the worst possible failure mode for a count.

**Extract straight to a flat session table.** This is the tempting
simplification, and it is where most of the difficulty hides. The moment two
documents disagree you must either overwrite (losing the ability to explain) or
add a conflict column (at which point you have reinvented claims, badly). The
Jan 19 correction is the proof: with a flat table you can store 11:15 or 11:30,
but you cannot answer *why*, and you cannot tell the correction apart from the
resent copy that also says 11:30.

**Text-to-SQL for new questions.** Rejected for auditability. A generated query
is a new, unreviewed piece of logic for every question. A library of ten
parameterized functions is logic a reviewer can read once and trust thereafter.

### Why the layering earns its keep

The separation is what makes the hard requirements fall out almost for free:

- **Duplicates don't change results**, because two copies of a record produce
  two claims that reconcile into one event. This holds even for near-copies that
  defeat hashing.
- **"A later document does not automatically override an earlier one"** is
  expressible, because authority is a property of the document class and the
  correction relationship, not of time.
- **New questions are cheap**, because they are queries over a built
  abstraction: measured ~1 ms, and flat as the corpus grows 200×.
- **Uncertainty propagates**, because bounds live on the event and the query
  functions carry them through to the verdict.

---

## 4. Why SQLite

The brief requires the system to "retain its work so that it can answer new
questions after restarting". That means a persistent store, and the choice is
between a file-based database, a document store, and plain files.

**Why a relational store rather than JSON files.** The core operation is
*joining* claims to events to decisions to documents, and filtering by patient
and date. That is exactly what indexed relational storage is for. With JSON
files, `consecutive_below` would mean loading and walking everything; with
tables it is an indexed scan. The schema also acts as a contract: a claim
*cannot* be inserted without a quote and offsets, because the columns are `NOT
NULL`. That has caught real bugs during the build.

**Why SQLite specifically.**

- **Zero setup.** A reviewer clones the repo and runs one command. No server, no
  container, no connection string. For a prototype that will be run by someone
  else on a call, this matters more than any performance consideration.
- **The abstraction is a single portable file.** `out/clinical.db` is the
  deliverable. It can be emailed, diffed, inspected with any SQLite browser.
  `Store.close()` checkpoints the write-ahead log into the main file so the
  artifact is always self-contained and its reported size is its real size.
- **Transactional.** A half-ingested document cannot leave a half-built event.
- **Standard library.** `import sqlite3` keeps the project dependency-free,
  which is also why the model layer is written against `urllib` rather than a
  vendor SDK.
- **It is honest about where it breaks.** SQLite has one writer. That is a real
  constraint at scale, it is named in the README, and the mitigation (shard by
  patient, or move to Postgres) is stated. A prototype that pretends to scale is
  worse than one that measures itself and says where it stops.

**What I would change at scale.** Partition by patient. Patient is the natural
shard key because reconciliation never crosses patients — a new document only
triggers re-reconciliation for its own patient. That property is deliberate and
is what makes the ingest path parallel.

---

## 5. Why an LLM at all, and exactly where

### The division of labour

The model is used for exactly one class of work: **messy language → structured
facts**. It is used for nothing else. In particular it never:

- computes a duration or a total,
- decides which of two disagreeing documents is right,
- decides whether a service counts toward a goal,
- produces a number that reaches an answer.

The reasoning is a reliability argument, not an ideological one. Two different
kinds of task are involved:

| task | failure mode | who should do it |
|---|---|---|
| "is this sentence about sleep, and does it describe improvement or persistence?" | graceful: a missed observation is a gap | model |
| "subtract the break overlap from the presence interval and sum the week" | catastrophic: a wrong total looks identical to a right one | code |

Code is also *verifiable* on the second class: `tests/test_units.py` asserts
that a break at 10:45–11:00 removes 15 minutes from a 10:30 arrival, 10 from a
10:50 arrival, and 0 from an 11:00 arrival. There is no equivalent assertion you
can write about a model's arithmetic.

### Where it is genuinely better

Reading `"Mood felt less persistently low than earlier in the month, although
anxiety remained noticeable when anticipating contact with work"` into
`{domains: [mood, anxiety], polarity: mixed, reporter: clinician}` is the case
for a model. My deterministic version of this is a cue-list classifier: it
works, it is auditable, and it is visibly crude. It needs a `NEGATED_IMPROVE`
list because "reduced interest in usual activities" is a *symptom* that contains
an improvement word. That kind of patch accumulates; a model generalizes.

### The verification that makes it safe

Every string a model returns must be found **character-for-character** in the
source document, or the claim is dropped and counted. This is implemented in
`llm._locate()` and is cheap, and it is the single most important safety
property of the model path: it makes a fabricated quote structurally unable to
enter the abstraction. The measured drop rate was **9 and 12 spans** across two
31-document runs — roughly one in nine extracted spans was not verbatim. Without
that check, those would have become evidence.

### Two modes, and why both exist

- **`llm` (hybrid, the shipped recommendation).** Tables, ids, dispositions and
  clock times are parsed by code; the model gets only prose. This is the
  configuration the reliability argument actually endorses.
- **`llm_full`.** The model also extracts the encounter structure, so the two
  front-ends can be compared on equal terms through identical reconciliation and
  arithmetic. Without this mode, diffing the extractors would be meaningless,
  because the event tables would be trivially identical. It exists to produce a
  number, not to be deployed.

### Caching, and what it turned out to be for

Responses are cached in `out/llm-cache.db`, keyed by `(normalized document text,
prompt version, mode, provider, model)`. The intent was cost: a rebuild is free,
and a prompt change re-extracts only what changed.

It turned out to do something more important. Two full passes over the same
corpus with the same model produced materially different abstractions (§14). The
cache is therefore what makes the model path *reproducible at all*. The
deterministic path is byte-identical across runs by construction; the model path
is only reproducible because the response is pinned.

---

## 6. The data model, table by table

`backbone/schema.sql`, 14 tables. Layer 1 = documents, layer 2 = claims, layer 3
= events plus the decision log.

### `documents`
One row per distinct document. `doc_pk` is the SHA-256 of the **normalized**
text, which makes identity content-based: a resent, re-stamped or differently
named copy lands on the same row. `sha256_raw` is kept too, so the system can
distinguish an exact duplicate from a near one. `doc_class` and
`authority_class` drive the conflict policy. `extractor` and `extractor_ver`
record which front-end produced the derived rows, so a mixed database is still
explicable.

### `document_aliases`
One row per duplicate *file path* that resolved to a known `doc_pk`, tagged
`exact_duplicate` or `normalized_duplicate`. This is what lets the system answer
"which duplicates did you detect?" rather than silently skipping them.

### `patients`
MRN, name, given name, DOB — all discovered from document headers. The given
name matters: it is used to recognise patient-present intervals (`"Rowan present
with partner: 13:15–13:45"`) without hardcoding a name.

### `claims` — the heart of the design
One row per assertion by one document about one encounter, correction, measure,
requirement, episode, observation, charge or authorization. Key columns:

- `claim_type` — what kind of assertion this is.
- `encounter_ref`, `appointment_ref`, `form_ref`, `call_ref` — the three id
  systems kept separate on purpose, so that a call id can be stored as evidence
  without ever being mistaken for an event key.
- `presence` + `presence_basis` — what the document says, and *how* it knows
  (`scheduled` / `attested` / `narrative` / `platform_log` / `derived`).
- `intervals` — patient-present intervals **only**. Scheduled slots live in
  `fields.scheduled`; therapist-only and partner-only intervals are recorded
  separately and never counted.
- `breaks` — nontherapeutic intervals.
- `stated_minutes` + `minutes_basis` — a stated number and whether it was stated
  as patient time, total time, or derived.
- `authority_class` — per-claim, not per-document, because one document can
  carry claims of different authority (BH-D112 holds an unsigned draft *and* a
  posted charge; the draft is `draft_note`, the charge is `billing`).
- `quote`, `quote_start`, `quote_end`, `quote_verified` — `NOT NULL`. A claim
  cannot exist without passage-level provenance.

### `corrections`
The target encounter, field, old value and new value of a correction. Separate
from `claims` because a correction is a *relationship* between records, and
expressing it as such is what allows RULE-PRES-01.

### `events`
One row per reconciled real-world contact. `occurred` and `patient_present` are
separate tri-state fields (`yes`/`no`/`unknown`) because they are different
facts — a collateral contact occurred without the patient. `min_minutes` and
`max_minutes` carry uncertainty as bounds. `counts_as_therapy` plus
`exclusion_reason` means every exclusion has a stated reason.

### `event_claims`
The many-to-many link, with a `role`: `primary`, `corroborating`, `correction`,
`superseded`, `rejected`. The role is the audit story — BH-D104 appears as
`superseded`, which is visible in the trace rather than inferred from its
absence.

### `decisions`
Every outcome of matching and resolution: `rule_id`, `rationale`,
`from_claims`. Nothing enters layer 3 without a row here, and every row is stamped with its patient so it can be filtered and cleared per patient. 156 rows for 31
documents. This table is the answer to "why 11:15?".

### `plan_requirements`
`(metric, service_types, period, comparator, threshold, effective_start,
effective_end)`. Dated rows, not a single current value, so a plan amendment
closes the prior row rather than overwriting it, and each week is checked
against the plan in force for that week.

### `episodes`, `measures`, `observations`
`measures` identity is `mrn|instrument|form|completed_date` — the key that makes
a re-import collapse. `observations` carry domain, polarity, reporter and their
own span, decoupled from events so that a medication visit can supply progress
evidence while contributing no minutes.

### `llm_cache`, `run_log`
Response cache with token counts and cost; a log of commands run.

---

## 7. The pipeline, stage by stage

### Stage 1 — ingest (`ingest.py`)

1. Read the file; compute `sha256_raw` and `sha256_norm`.
2. If `sha256_norm` is known under a different path → record an alias, log
   RULE-DEDUPE-01, create no claims. Idempotence is structural, not checked
   after the fact.
3. Otherwise classify the document, extract claims, verify every quote against
   its offsets, insert.

Normalization for identity (`normalize.py`) strips receipt/export/fax/scan/page
lines, collapses whitespace, unifies dashes and quotes, lowercases. It is
deliberately aggressive, because its only job is identity. **All offsets point
into the raw bytes of the file as read**, so a reviewer opening the document
lands on the quoted span. Normalized text is never used for offsets.

### Stage 2 — extraction (`extract/rules.py`)

The central idea, and the thing that makes one code path read a facilitator
narrative, a desk roster and a billing extract: **find every clock time in the
document, then classify it by the words around it.**

```
break            "break", "nontherapeutic", "connection was lost", ...
scheduled        "scheduled", "slot", "room schedule", ...
other_present    "therapist session interval", "partner only", ...
patient_present  "patient contact", "patient arrival", "<Given> present", ...
service          an unlabelled interval on an encounter header line
                 (fallback only, and never for a group service)
```

Precedence matters: `break` is tested first, because *"Connection was lost from
13:20–13:30; there was no therapeutic contact during that interval"* contains
the word "contact" and would otherwise classify as patient-present.

Other extraction concerns:

- **Tables** are detected by a header row whose cells name known columns *and
  contain no values* — the guard exists because `"Patient arrival: 10:00 |
  Patient departure: 11:30 | Status: Attended"` otherwise looks like a header.
  Each row becomes its own claim.
- **Masking.** Before prose rules run, table rows and the DOB are blanked with
  spaces (preserving offsets). Without this, a table row gets re-read as
  narrative, and a date of birth gets read as a service date.
- **Sections.** A document naming several encounters is split at the encounter
  references, so BH-D107's Jan 22 break attaches to E113 and its Jan 29 break to
  E118.
- **Corrections.** The corrected field is the field word closest *before* the
  assertion, which is why `"Applies to group encounter HG-E110, service date
  January 19, 2026 … Correction: Patient departure … is 11:15"` resolves to
  `departure` and not to `service_date`.
- **Document class** comes from the masthead — the first eight non-empty lines,
  earliest hint wins — not from any passing mention. This is why a collateral
  note containing "ways to support the treatment plan" is not classified as a
  treatment plan.
- **Evidence span selection** (`_best_span`) prefers the sentence that carries
  the fact: the stated-minutes sentence, else the patient-present interval's
  sentence scored to prefer a patient context over a scheduled one, else the
  presence cue, else the header. This is why the Jan 19 evidence quotes
  `"Patient arrival: 10:00 | Patient departure: 11:30 | Status: Attended"`
  rather than the document title.

### Stage 3 — reconciliation (`reconcile.py`)

**Matching**, in order: encounter id (RULE-MATCH-01) → appointment id resolved
to its encounter (RULE-MATCH-02, with call ids explicitly excluded by
RULE-MATCH-03) → patient + date + service type, confirmed by time overlap
(RULE-MATCH-04). More than one candidate and no tiebreak → left unmatched and
reported (RULE-MATCH-05). A claim whose document says of itself that it records
no visit is never matched into a clinical event — this is what stops the Jan 26
import receipt from voting on whether the Jan 26 session happened.

**Resolution**, per field, by authority class, with a decision row for each
outcome. The full rule list is §9. The two policies (`explicit_correction`,
`latest_document`) are selectable at runtime, which is what makes the first
experiment possible.

### Stage 4 — queries (`queries.py`)

Ten functions, each returning `{value, calculation, evidence, caveats}`.
`calculation` is the arithmetic written out (`50 + 45 + 45 = 140`) so it can be
checked by hand. This is the only module that produces a total.

---

## 8. Every file, and why it exists

### `backbone/schema.sql` (187 lines)
The 14 tables of §6. `NOT NULL` on quote columns is a design constraint
expressed in the schema rather than in code review.

### `backbone/normalize.py` (105 lines)
Normalization for identity, hashing, and date parsing. `parse_date` **validates**
— a document can say "February 30", and a service date that cannot exist must
not enter the abstraction. The month-abbreviation table is built explicitly
after an early bug produced February from `Jan14`.

### `backbone/taxonomy.py` (262 lines)
All vocabularies and the authority policy as data:
`SERVICE_SYNONYMS` (longest phrase wins), `DOC_CLASS_HINTS` (earliest hint
wins), `ESTABLISHES_DELIVERY` (can this class prove care happened?),
`FIELD_AUTHORITY` (per-field precedence). Changing a value here changes how the
whole corpus reconciles, which is why rule ids are stored on every decision.

### `backbone/intervals.py` (108 lines)
Interval algebra: `normalize`, `subtract`, `overlaps`, `present_minutes`,
`week_key`. **Every duration in the system comes from here.** Nothing subtracts
a flat break length. Also the week bucketing, configurable between Monday–Sunday
and Sunday–Saturday.

### `backbone/store.py` (239 lines)
Thin SQLite layer; deliberately contains no policy. `close()` checkpoints the
WAL so the abstraction is one portable file.

### `backbone/extract/base.py` (60 lines)
The `Claim` dataclass and `DocFacts`, shared by both extractors — which is what
makes them interchangeable and therefore comparable. `Claim.verify()` is the
offset/quote check.

### `backbone/extract/rules.py` (994 lines)
The deterministic extractor of §7. The largest file, and the one most specific
to this corpus's vocabulary — which is also the limitation named in §16.

### `backbone/extract/llm.py` (552 lines)
Both model modes, the prompts, quote verification, the persistent cache, usage
accounting, and the optional query router. The prompts forbid arithmetic
explicitly ("Do NOT subtract breaks, do NOT total intervals, do NOT convert to
minutes") because the model will otherwise do it unasked — observed, §14.

### `backbone/provider.py` (299 lines)
Groq and Anthropic over `urllib`, so the model path adds no dependency. Also:
`.env` loading (read with `utf-8-sig`, because PowerShell writes a BOM), a
`User-Agent` (Cloudflare rejects `Python-urllib` with error 1010), a
**token-rate governor** that paces against a rolling one-minute budget rather
than discovering the free-tier limit by being refused, and retry that honours
the provider's own `try again in Ns` hint.

### `backbone/reconcile.py` (629 lines)
Matching and resolution, §7 and §9. The policy switch lives here.

### `backbone/queries.py` (676 lines)
The query library. Beyond the obvious counts: `consecutive_below` evaluates
under both the minimum and maximum readings of the record so that patients whose
inclusion *depends on unresolved documentation* fall out of the comparison;
`integrity` runs the wall-clock check; `added_contacts` surfaces contacts the
record itself describes as added, with the sentence giving the reason.

### `backbone/router.py` (189 lines)
Question → function + parameters. Deterministic intent keywords, patient
resolution by MRN/name/given name, date-range extraction, and a corpus-year
fallback for questions that omit the year ("the care on January 19 and January
21" — which is how the dev questions are phrased).

### `backbone/render.py` (315 lines)
Result → reviewable Markdown. Formats only; never computes, and never prints a
figure not present in the result it was handed. ASCII output, because execution
logs are captured on terminals that are not always UTF-8.

### `backbone/cli.py` (334 lines)
`build`, `add`, `ingest`, `reconcile`, `answer`, `ask`, `trace`, `export`,
`bench`, `experiment`, `compare`, `models`, `verify`.

### `backbone/bench.py` (170 lines)
Measured performance: cold build, warm re-ingest, single-document update, query
latency with repeats, storage with row counts, model usage.

### `backbone/experiment.py` (165 lines)
The conflict-policy A/B. Builds the corpus under both policies and writes the
report, including the detection of the wall-clock violation.

### `backbone/compare.py` (344 lines)
The extractor disagreement harness. Builds twice, diffs the event table across
six fields per event, and scores each disagreement against the hand-verified
ledger so the output is "rules right in N, model right in M". Also compares
weekly *verdicts*, not only minutes — added after the first version hid a
material error behind identical totals (§14).

### `tests/gold_ledger.json`
The hand-computed ledger as data. It lives outside the package deliberately:
`compare.py` scores extractor disagreements against it, and putting it in
`backbone/` would encode patient facts in the implementation. A test asserts it
matches the assertions in `test_gold.py`, so the two cannot drift.

### `tests/test_gold.py` and `tests/test_units.py`
108 tests. The hand-computed ledger; the excluded encounters with reasons;
idempotence under an exact copy and a re-stamped copy; answering after restart
with no re-ingest; a test that no severity band is ever invented; interval
algebra including the counterfactual arrival times; generalization tests on
synthetic documents with a new patient, an hours-based requirement, a new
correction and a new break phrasing; a synthetic patient with empty weeks to
prove they are evaluated and counted as consecutive; and a guard asserting that
**no name, MRN or encounter id from the corpus appears anywhere in the
package** — which found four real violations when it was added (§15).

### `scripts/scale_test.py` (177 lines)
Clones the corpus into synthetic patients (new identifiers, dates shifted by
whole weeks so week bucketing still lines up) and measures the scaling curve —
so §13 reports measurements rather than extrapolation over three orders of
magnitude.

### `scripts/run_all.sh`
Full reproduction, captured to `out/logs/execution.log`.

---

## 9. The rule catalogue

Every decision row names one of these. The reasoning is as important as the
rule.

**Matching**

| rule | policy | why |
|---|---|---|
| RULE-MATCH-01 | an encounter id is the event key | the only id that identifies a contact |
| RULE-MATCH-02 | an appointment id resolves to its encounter | the telehealth export carries appointment, not encounter |
| RULE-MATCH-03 | a call/session id never keys an event | otherwise Jan 21 becomes two sessions |
| RULE-MATCH-04 | no id: patient + date + service type, confirmed by time overlap | hidden documents may lack ids |
| RULE-MATCH-05 | ambiguous: leave unmatched and report | a wrong merge is worse than a reported gap |

**Identity**

| rule | policy | why |
|---|---|---|
| RULE-DEDUPE-01 | identity is the hash of normalized text | catches re-stamped and renamed copies |
| RULE-DEDUPE-02 | a retransmission inherits its original and gains no authority | the Jan 26 resent roster |
| RULE-QUOTE-01 | a claim whose quote does not match its offsets is flagged | catches extraction drift and fabricated spans |
| RULE-DATE-01 | an event with no usable service date is retained but excluded from weekly totals | losing it silently would be worse |

**Presence**

| rule | policy | why |
|---|---|---|
| RULE-PRES-01 | a correction to a presence field wins | it names what it replaces |
| RULE-PRES-02 | positive delivery requires a class that can establish delivery | the Jan 27 draft note and posted charge |
| RULE-PRES-03 | absence or cancellation may be established by a final disposition | a negative claim is safe from a desk record; also distinguishes no-show/cancellation (nothing happened) from patient-absent (it happened without them) |
| RULE-PRES-04 | qualified records disagree → `unknown`, disputed | do not break a real tie |

**Duration**

| rule | policy | why |
|---|---|---|
| RULE-DUR-01 | group service: the attendance desk owns arrival/departure | the facilitator writes content, the desk records badges |
| RULE-DUR-02 | otherwise the treating clinician's patient-contact interval wins | they were in the room |
| RULE-DUR-03 | equal authority, different values, no correction → bounds | the Jan 26 50-vs-40 conflict |
| RULE-DUR-04 | breaks reduce a session by interval **overlap** | Jan 6 arrival 10:15 and Jan 22 arrival 10:30 |
| RULE-DUR-05 | schedule, draft, billing, authorization, retransmission contribute no duration at all | a booking is not delivery |

**Inclusion and measures**

| rule | policy | why |
|---|---|---|
| RULE-SVC-01 | service type by weighted vote, clinical wording over administrative | the Jan 15 cancellation notice calls itself "program operations" |
| RULE-INC-01 | therapy inclusion follows the plan's own wording | the plan names what counts; hardcoding it would fail an unseen plan |
| RULE-INC-02 | minutes require the patient to have been present | collateral and coordination contacts |
| RULE-MEAS-01 | measure identity = instrument + form id + completion date | the Jan 26 re-import of the Jan 16 PHQ-9 |

---

## 10. How uncertainty is represented

Not as a label. As bounds, carried all the way to the verdict.

Every event has `[min_minutes, max_minutes]`. A confirmed 45-minute session is
`[45, 45]`. The Jan 26 conflict is `[40, 50]`. An event whose occurrence is
unresolved contributes `0` to the minimum and its maximum to the maximum.

A requirement check then has three outcomes rather than two:

```
MET                minimum >= threshold
NOT_MET            maximum <  threshold
CANNOT_DETERMINE   threshold falls inside [minimum, maximum]
```

Two subtleties that are easy to get wrong, and were:

- **The plan is a conjunction.** It requires at least 3 therapy days *and* at
  least 150 minutes. So one metric definitely failing settles the week however
  uncertain the other is: `NOT_MET` outranks `CANNOT_DETERMINE`. The reverse
  order would report a week that provably missed a required threshold as merely
  undetermined.
- **A week with no attendance still exists.** `minutes()` enumerates every week
  in the review window and zero-fills, rather than bucketing only the weeks that
  happen to contain an event. Otherwise a patient attending in week 1 and week 3
  but nothing in week 2 would have week 2 vanish, and would be wrongly excluded
  from "two consecutive weeks below".

Week 4 is `145–155` against a 150-minute goal, so the honest answer is
`CANNOT_DETERMINE` — and the system also reports what would settle it: *"an
addendum or correction from either author that names the other record's value,
or a check-in/room-transfer timestamp for the start of the contact."*

This is what makes the collection-wide question answerable properly.
`consecutive_below` evaluates every patient twice — once treating undetermined
weeks as below, once as met — and reports three groups: included on the record
as it stands, **inclusion depends on documentation that is not settled**, and
not included. A patient whose answer differs between the two readings is exactly
a patient whose inclusion turns on unresolved documentation, which is what the
question asks for.

---

## 11. How a question becomes an answer

```
"Did Rowan meet the goal in the week of January 12?"
   │
   ├─ router.classify       → intent "compliance"
   ├─ router.find_patients  → HG-M042   (by given name, from the patients table)
   ├─ router.find_window    → 2026-01-12 … (corpus year fills in the missing year)
   │
   ├─ queries.compliance(st, "HG-M042", ...)
   │     ├─ plan requirements in force for that week
   │     ├─ queries.minutes → per-week bounds from events
   │     ├─ three-way comparison per metric
   │     └─ returns {value, calculation, evidence, caveats}
   │
   └─ render.compliance     → Markdown, formats only
```

No free-form SQL, so every answer path is logic a reviewer can read once.
Measured latency ~1 ms, from the saved file, with no reprocessing. With
`--router llm` a model picks the function and fills parameters; the numbers still
come from the function.

---

## 12. The audit chain

The requirement is that a reviewer can trace a finding back to source passages.
`python -m backbone trace "HG-M042|HG-E110"`:

```
minutes: 60
[RULE-PRES-02] rejected_presence = present — retransmission cannot establish
               that care was delivered (BH-D104)
[RULE-DUR-04]  breaks = [["10:45","11:00"]] — subtracted by overlap with the
               patient's own presence interval, not as a flat amount
[RULE-DUR-05]  rejected_duration = [['10:00','11:30']] — retransmission
               contributes no duration (BH-D104)
[RULE-DUR-01]  minutes = 60 — attendance_register BH-D102 is the authority for
               duration on a group_therapy event; intervals+correction;
               correction applied

(correction,    correction)         BH-D103 [223:320] "Correction: Patient
                                    departure for HG-E110 is 11:15, replacing
                                    the original roster value of 11:30."
(primary,       attendance_register) BH-D102 [296:364] "Patient arrival: 10:00 |
                                    Patient departure: 11:30 | Status: Attended"
(superseded,    retransmission)     BH-D104 [789:857] "Patient arrival: 10:00 |
                                    Patient departure: 11:30 | Status: Attended"
```

Every link in the chain is a stored row: answer → calculation → event → decision
→ claim → document + character offsets. The answer to "why 11:15?" is a citation
and a rule id, not a recollection.

There is also an **answer-independent** check. `queries.integrity` compares the
resolved presence intervals of every counted contact within a day: a patient
cannot be in two therapy rooms at once, so an overlap means the reconciliation
is wrong somewhere, without anyone having to know the right answer. It runs on
every build, and §14 is why.

---

## 13. What was measured

Measured on this machine (Windows 11, Python 3.13.5). `python -m backbone bench`
→ `out/benchmark.json`.

| | measured |
|---|---|
| cold build, 31 documents | **0.149 s** (ingest 0.144 s = 4.6 ms/doc, reconcile 0.005 s) |
| claims → events | 99 claims → 20 events (12 therapy, 1 disputed) |
| warm re-ingest | **0.009 s**, 0 of 31 re-extracted |
| one new document + reconcile its patient | **0.010 s** |
| per-patient question | **0.6–1.1 ms** |
| collection-wide question | **0.78 ms** |
| trace one event | **0.09 ms** |
| natural-language question end to end | **0.37–1.77 ms** |
| abstraction on disk | 284 KiB (31 docs, 99 claims, 20 events, 156 decisions, 54 observations, 3 measures, 1 correction) |
| model calls on the default path | **0 / $0.00** |

**Measured scaling curve** (`scripts/scale_test.py`, → `out/scaling.json`):

| patients | documents | ingest | reconcile | per-patient query | collection-wide query | bytes/doc |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 31 | 0.55 s | 0.009 s | 1.92 ms | 1.7 ms | 9,381 |
| 10 | 310 | 5.0 s | 0.063 s | 0.96 ms | 10.9 ms | 5,298 |
| 50 | 1,550 | 24.3 s | 0.395 s | 1.05 ms | 67 ms | 4,878 |
| 200 | 6,200 | 93.6 s | 2.98 s | **0.92 ms** | **426 ms** | 4,857 |

Three things this shows rather than assumes: ingest is flat per document,
reconcile is flat per patient (because it partitions by patient), and a
per-patient question stays at ~1 ms while the corpus grows 200× — but a
collection-wide question grows linearly with the patient count.

**The first bottleneck at a million documents is therefore collection-wide
queries, not extraction.** `consecutive_below` recomputes every weekly total
from events on each call: 426 ms per 200 patients → ~34 s at 16,000 patients.
The fix is a materialized `patient_week` table written during reconcile, which
already visits exactly the affected patient. Not built, because at 31 documents
it would be unmeasurable — and I would rather ship the measurement that says
where it is needed.

Estimates at 500K documents, labelled as estimates: ~2.1 h single-process
ingest (~8 min across 16 shards), ~4 min reconcile, ~2.4 GB storage, ~1 ms
per-patient queries unchanged, ~34 s collection-wide queries as written today.

---

## 14. The two experiments

### Experiment 1 — conflict-resolution policy

`docs/experiment-conflict-policy.md`, `python -m backbone experiment`. Same
documents, same extractor, same query code; only the policy changes.

| | explicit-correction (shipped) | latest-document |
|---|---|---|
| Jan 19 group minutes | **60** | 75 |
| Jan 19 therapy minutes | **90** | 105 |
| episode total | **585–595** | 600 |
| week of Jan 26 | **145–155, CANNOT_DETERMINE** | 145, NOT_MET |
| wall-clock check | passes | **fails** |

**What it showed.** Latest-document is not a different judgement call — it makes
the record internally impossible. With the resent roster winning, the group runs
10:00–11:30 while the same-day individual session runs 11:15–11:45: the patient
is in two therapy rooms for 15 minutes. The system detects that *without being
told the right answer*, which is why the wall-clock check now runs on every
build, and why recency is only ever a tiebreak in the shipped policy.

The quieter effect is worse: recency also *resolves* the Jan 26 conflict the
documents do not resolve, turning an honest `CANNOT_DETERMINE` into a confident
`NOT_MET`. That one leaves no trace in the output at all — which is the argument
for carrying bounds rather than a chosen value.

An aside worth recording: my first version of this experiment was **invalid**.
The `latest_document` branch still applied the retransmission filter and the
correction override, so the baseline silently inherited the shipped policy and
the two arms came out identical. A comparison that cannot distinguish its own
arms looks like a null result rather than a bug, which is why I now check that
an A/B can actually separate before trusting it.

### Experiment 2 — deterministic vs model extraction

`docs/experiment-extractor-comparison.md`, `python -m backbone compare`. Model
under test: **`openai/gpt-oss-120b`** via Groq (the strongest model available on
the key), temperature 0, prompt `v3`, identical reconciliation and arithmetic on
both sides.

Two full passes were run, and they differed in configuration as well as in
outcome — stated plainly because the confound is mine:

| | run A | run B |
|---|---|---|
| reasoning budget | provider default | `reasoning_effort: low`, `max_tokens` 6000→9000 |
| event-field agreement with rules | 96.8% (122/126) | **100%** (120/120) |
| scored disagreements | 4; rules right in 1, model in 0 | 0 |
| spurious events | 1 (invented an encounter from a questionnaire review) | 0 |
| plan requirements found | **therapy_minutes missing** | both found |
| weekly verdicts correct | **1 of 4** | 4 of 4 |
| observations extracted | 37 | **0** |
| quotes dropped as not verbatim | 9 | 12 |
| input / output tokens | 48,114 / 31,072 | 48,114 / 13,851 |
| cost at listed prices | $0.0305 | $0.0176 |
| wall time | 705 s | 623 s |

**What it showed, in order of importance.**

1. **The architecture works.** In run B the model and the rules extractor
   produced *identical* answers to all five development questions — 12 sessions,
   11 days, 585–595 minutes, all four weekly verdicts — through the same
   reconciliation and the same arithmetic. That is the strongest available
   evidence for keeping the model out of the numerical loop: the front-end
   changed completely and the numbers did not move.

2. **The event table can hide the error that matters.** In run A every minute
   total was identical to the rules path and three of four weekly verdicts were
   still wrong, because the model returned only the first clause of *"at least 3
   therapy days **and** at least 150 minutes…"*. With no minute requirement in
   force, `compliance` could only check days, so weeks genuinely short on
   minutes were reported as met. A 96.8% field-agreement score looked
   reassuring and was measuring the wrong thing. The harness now compares
   weekly verdicts, and that is the fix I would keep.

3. **The authority policy absorbs model errors.** Asked about the Jan 19
   facilitator note, the model sometimes returned the *scheduled* slot as
   patient-present time, split around the break — a clinically material error.
   It did not reach the answer, because RULE-DUR-01 gives the attendance desk
   authority for duration on a group event. The policy layer is load-bearing,
   not decoration.

4. **The model does arithmetic when told not to.** It pre-subtracted the break
   itself rather than reporting presence and break separately, despite an
   explicit instruction. Code that re-derives the number is the only defence.

5. **Verbatim-quote verification earns its place.** 9 and 12 spans — roughly one
   in nine — were not found character-for-character in the source and were
   dropped. Without that check they would have become evidence.

6. **Reproducibility is not free.** Three consecutive calls on one document were
   identical, but the same document returned a different, clinically material
   answer across runs. The response cache is therefore what makes the model path
   reproducible at all; the rules path is byte-identical by construction.

7. **Cost and speed.** ~1,550 input tokens per document, ~$0.02–0.03 per 31
   documents at listed prices (free tier: $0). Wall time 623–705 s against 0.149
   s — about 4,000× slower, mostly the free tier's 8,000 tokens/minute ceiling,
   but still ~1.5–2.6 s of real model latency per document.

**Conclusion.** For structure, the deterministic path wins on every axis that
matters here: it is right at least as often, 4,000× faster, free, and
reproducible. The model's place is the prose — and run B's zero observations
shows that even there it needs its reasoning budget left alone, which is a
configuration lesson I only have because the harness measured it.

### Experiment 2b — the hybrid mode, which is the one that ships

`docs/experiment-extractor-hybrid.md`, `python -m backbone compare
--extractor-b llm`. Same model, same reconciliation, but the model sees only
prose: 25 of 31 documents, the other 6 skipped because their class carries no
clinical narrative.

| | rules | hybrid |
|---|---|---|
| five dev answers | baseline | **identical** |
| plan requirements found | both | both |
| distinct measures | 3 | 3 |
| observation spans | 35 | **121** |
| of the other's spans, also found | 33/121 (27%) | **33/35 (94%)** |
| quotes dropped as not verbatim | n/a | **1** |
| input / output tokens | 0 | 19,692 / 29,648 |
| cost at listed prices | $0.00 | $0.0252 |
| wall time | 0.17 s | 313 s |

**What it showed.** This is the configuration the reliability argument endorses,
and the measurement supports it on both halves:

1. **The numbers do not move.** Every figure in all five development answers is
   identical to the deterministic path, because the model never touches
   structure or arithmetic. The 100% event-field agreement here is true *by
   construction* and is not evidence -- it only confirms the wiring. Which is
   precisely why the prose comparison is the one worth reporting.
2. **The prose layer improves substantially.** 121 spans against 35, including
   33 of the deterministic extractor's 35 (94%) plus 88 it missed. The low
   Jaccard figure (27%) is an asymmetry, not a disagreement. This turns
   limitation 4 from an assertion into a number: the cue lists miss roughly two
   thirds of the usable narrative evidence.
3. **Faithfulness improves when the model is given only the job it is good
   at.** 1 dropped quote across 25 documents, against 9 and 12 in the two
   `llm_full` passes over 31. Asking the model to extract structure *and* prose
   in one call made it less careful about both.

So the shipped recommendation is the measured one: deterministic structure,
model prose, verbatim verification on every span, arithmetic in code.

---

## 15. Build chronology, including every bug found

Roughly five hours. Recorded because the bugs are the design rationale, and
several of them are the kind that would otherwise have been silent.

**Hour 1 — read everything by hand.** All 31 documents, the ledger computed
manually, the 15 traps of §2 catalogued. The expected numbers were written into
`tests/test_gold.py` *before* any pipeline code, so they could only ever be
assertions, never inputs.

**Hour 2 — foundation.** `schema.sql`, `normalize.py`, `taxonomy.py`,
`intervals.py`, `store.py`, then the extractor and `reconcile.py`. First end-to-end
run produced 20 events, and seven things wrong:

1. **`Jan14` parsed as February.** The month-abbreviation table was built by
   iterating over its own already-populated values, so every abbreviation was
   off by one. Fixed by building it explicitly; a regression test now pins
   `Jan05`, `Jan14`, `Feb14`.
2. **The Jan 19 correction targeted the wrong field.** The field word was chosen
   by longest match over the whole swallowed block, so `"service date"` in the
   preamble beat `"departure"`. Fixed to take the field word closest *before*
   the assertion.
3. **Date of birth read as a service date.** `1991-04-12` was the first ISO date
   in several documents. Fixed by masking DOB spans before date parsing.
4. **Table rows re-read as narrative**, producing a second, wrong set of
   intervals. Fixed by masking consumed spans with spaces, preserving offsets.
5. **A data row misdetected as a table header.** `"Patient arrival: 10:00 | …"`
   maps to three known columns. Fixed by rejecting header candidates whose cells
   contain times or long digit runs.
6. **Jan 30 family therapy came out as not occurred.** `"Rowan was not present
   for that portion"` — about the partner-only interval — was read as a
   document-level absence. Fixed by making an explicit patient-present interval
   outrank a document-level cue, and by removing the over-loose cue.
7. **Jan 21 telehealth lost its encounter.** A document with both a table and a
   narrative took the table-only path, so the note's 45 minutes was dropped and
   the two call ids became the event. Fixed by running both paths, with rows
   owning the encounters they name.

**Hour 3 — classification and evidence quality.** Document class was being
decided by any passing mention: a collateral note saying "support the treatment
plan" became a treatment plan; a medication note mentioning "the symptom
questionnaire" became a measure. Rewritten to read the masthead, earliest hint
first — all 31 then classified correctly, pinned by a test. Evidence spans were
pointing at document titles; `_best_span` now prefers the sentence carrying the
fact. Observations were catastrophically noisy at 108 rows, including *"Goal 1:
improve daily activity"* scored as improvement; rewritten with forward-looking
exclusions and a `NEGATED_IMPROVE` list (`"reduced interest"` is a symptom, not
progress), down to 59.

Also: an administrative import receipt was matching into the Jan 26 clinical
event by date and voting on whether it happened. Fixed so a document that says
of itself that it records no visit is never matched to an encounter.

**Hour 4 — queries, answers, benchmarks, experiment 1.** The query library, the
router, the renderer, the CLI. The invalid first A/B (§14). The wall-clock check,
added because the corrected A/B produced an impossible record. The scaling
harness, which required `parse_date` to validate dates (a synthetic clone
produced "February 30") and the queries to tolerate dateless events.

**Hour 5 — the model path.** `provider.py` over `urllib`, both extractor modes,
the comparison harness. Four environment problems in a row, each worth keeping a
fix for: PowerShell's `-Encoding utf8` writes a **BOM**, making the first key
`﻿GROQ_API_KEY` (fixed with `utf-8-sig`); Cloudflare rejects
`Python-urllib` with **error 1010** (fixed with a real `User-Agent`); the free
tier is **8,000 tokens/minute** against ~2,250 per document (fixed with a rolling
token governor that honours the provider's own retry hint); and strict JSON mode
**refused outright** on one long document, aborting the whole corpus (fixed by
making a single document's failure non-fatal and counted, and by falling back
from strict JSON mode on retry).

Two of my own mistakes in that hour are worth recording because they cost two
eleven-minute runs: the comparison harness deleted the working database at the
start of each build, which **wiped the response cache** and re-paid for every
document — fixed by moving the cache to its own file; and I changed
`reasoning_effort` between runs while comparing their outputs, creating the
confound reported in §14 rather than hidden.

---

**Hour 6 — review fixes.** Four bugs found in review, each now covered by a
regression test:

1. **Weeks with no therapy disappeared.** `minutes()` created a week row only
   when an event fell in it, so a patient with a blank week would have that week
   omitted from compliance entirely and dropped from any consecutive-weeks
   question. Rowan attends every week so the dev answers were unaffected, which
   is exactly why it survived: the supplied corpus cannot exhibit the bug. Fixed
   by enumerating every week in the review window and zero-filling; tested with
   a synthetic patient whose middle two weeks are empty.
2. **The weekly verdict had the precedence backwards.** `CANNOT_DETERMINE` was
   checked before `NOT_MET`, so a week that provably missed one required
   threshold while the other was uncertain was reported as undetermined. The
   plan is a conjunction, so a definite failure settles it. Fixed and tested,
   and the dev answers are unchanged (week 4 has no definite failure, so it
   stays `CANNOT_DETERMINE`).
3. **The router guessed silently.** A question matching no keyword was answered
   as a session count with no indication; a question naming an unknown patient
   was answered about the first patient in the collection. Now: an unknown
   patient or MRN is **refused** with the known patients listed, an unmatched
   question type produces a stated warning, and a per-patient question naming
   nobody is refused when the collection holds several patients.
4. **A patient name was compiled into the code.** The `consecutive_below` branch
   tested the question for the literal word "rowan" to decide whether to scope
   to one patient or the collection — a direct violation of the brief's
   no-patient-facts rule. Replaced with the signal that was already available:
   whether `resolve_patients` actually matched a patient.

Fixing (4) prompted a guard test asserting that **no name, MRN or encounter id
from the corpus appears anywhere in the package**. It immediately found four
more violations I had not noticed, all of which were worth fixing on their own
merits:

- `bench.py` built its sample questions from the patient's literal name and
  traced a hardcoded encounter id. It now derives both from the store, so the
  benchmark runs on any corpus.
- `cli.py`'s usage example named a real MRN and encounter; genericised.
- `compare.py` embedded the entire hand-verified ledger. Moved to
  `tests/gold_ledger.json`, which the harness loads if present and otherwise
  reports every disagreement as unscored — so the harness now works on any
  corpus, with or without a ledger.
- `rules.py` had the **partner's given name as a `social_support` domain cue**.
  That one actually affected behaviour on unseen data: it would have matched a
  different patient's unrelated text and missed every real partner. Replaced
  with role words (partner, spouse, caregiver, informant…).

Fixing `experiment.py` the same way turned out to improve the experiment rather
than just satisfy the test. It had looked up one named encounter; it now diffs
the two policies' event tables to *find* where they diverge. On the supplied
corpus that surfaced two divergences my hardcoded version had missed — two
non-patient contacts whose `occurred` flips under the naive policy — alongside
the two it already reported.

---

**Hour 7 — second review round.** Twelve more findings, all fixed, each pinned
by a test in `tests/test_review_fixes.py`. The four that mattered:

1. **An unresolved attendance stored `0-0`.** Duration was computed only when
   `occurred == "yes"`, so a disputed attendance contributed nothing to the
   *upper* bound either. A week whose result hinged on it came out `NOT_MET`
   instead of `CANNOT_DETERMINE`, and such a patient could never reach the
   "inclusion depends on unresolved documentation" group — defeating the
   mechanism §10 exists for. Now the duration is computed anyway and kept as
   `[0, what the records would support]`.
2. **The compliance renderer assumed exactly two metrics.** A plan stating only
   an hours goal, or only a days goal, raised a `TypeError` on
   `d['comparator']`. The table now builds one column per metric the plan
   actually states.
3. **`intervals.mk` read any end-before-start as crossing midnight**, so
   "12:30-1:15" became 765 minutes instead of 45 — a 17x overstatement that
   would have looked like a plausible total. It now tries the 12-hour reading
   first and only assumes midnight when no 12-hour reading gives a plausible
   session length. `to_min` also learned meridiems and bare hours, and the
   extractor's range pattern learned "AM/PM", "10:45-11" and "from X until Y".
4. **A correction with no stated old value rewrote every interval** of every
   candidate claim, which would silently corrupt a split session. It is now
   applied only when the claim has one interval, and the ambiguity is logged.

The rest: the weekly verdict's `NOT_MET`/`CANNOT_DETERMINE` precedence
(a conjunction means one definite failure settles the week); zero-therapy weeks
vanishing from `minutes()`; the router's silent fallbacks and its hardcoded
patient name; `"week of <date>"` being read as the whole episode; a days
arithmetic string that always printed `0` (`len(set())`); progress statements
that were canned sentences switched on by domain presence, now counted per
domain from the observations actually found; substring keyword matching
("meet" firing on "meeting", "count" on "account"), now whole-word;
`event_claims` recording `RULE-MATCH-01` for every link regardless of how the
claim actually matched; a stated duration used as-written next to a documented
break; a re-import omitting its form id failing to collapse onto the original
administration; a narrative claim taking the first date in its section when the
encounter's own header line carried the service date; and a dead `absent` test
inside the positive branch of `_presence` that could never fire.

None of these changed the answers to the five development questions, which is
the point of keeping the hand-verified ledger as a regression test: it lets a
dozen fixes land without wondering whether the numbers moved.

---

**Hour 8 — third review round.** Five findings, all of which a live demo would
have exposed:

1. **The decision log leaked between patients and accumulated.** Two defects in
   one place. `_count_caveats` read the whole `decisions` table with no patient
   filter, so with a second patient one patient's answer listed the other's
   rejected claims. And `clear_patient_events` tried to delete the decisions
   that are not tied to a session with
   `WHERE event_id IS NULL AND from_claims IN (SELECT '[]' WHERE 0)` -- a
   subquery that returns no rows and therefore matched nothing, so every
   rebuild or `add` appended another copy and DEV-01's exclusion list grew.
   Fixed by stamping every decision with the patient being reconciled (a new
   `decisions.patient_mrn`, with an `ALTER TABLE` migration for existing
   databases), filtering caveats by it, and deleting by it. Pinned by a test
   that builds a genuine two-patient corpus, asserts neither answer cites the
   other's documents, and reconciles three more times asserting the decision
   count does not move.
2. **The `corrections` table was never written.** The correction relationship
   existed only inside a claim's `fields` JSON, so anyone opening the database
   would find the table empty. `insert_claim` now writes the row, and
   `Store.corrections()` reads it back with its source span.
3. **Documents could be skipped or doubled.** Directory ingest globbed `*.txt`
   only, silently ignoring anything else dropped into the folder; it now reads
   any text-bearing extension and *reports* what it skipped. And a file edited
   in place hashes differently, so it was ingested as a second document while
   the old row and its claims remained -- both versions counted. Ingest now
   detects a known path whose content has changed and drops the superseded
   version first (RULE-DEDUPE-03).
4. **Presence was decided by position in a list.** A note mentioning a
   "cancellation" anywhere was read as cancelled even when it also attested
   attendance. My first attempt -- longest phrase wins -- was *wrong and I had
   to throw it away*: "cancellation" is longer than "attended", so it changed
   nothing. The real discriminator is context, so each cue occurrence is now
   scored by where it sits (3 in a disposition field, 2 in a sentence naming the
   patient, 1 anywhere else) and the highest score wins. An equal-authority tie
   between a positive and a negative cue asserts *nothing*, letting other
   records decide rather than believing whichever phrase was longer.
5. **A questionnaire copy with a different score was silently absorbed.**
   `upsert_measure` used `COALESCE(total, ?)`, so the first score won and the
   disagreement vanished. Both scores are now kept, the measure is flagged
   `disputed`, a decision records it (RULE-MEAS-02), and `progress` explicitly
   lists "a single score for that administration" among the things the record
   does not support.

While fixing (3) I introduced an O(n^2) membership test and a second directory
walk, which is the wrong shape for a 500K-document corpus; it is now a single
walk partitioned in one pass.

---

**Hour 9 — submission readiness.** A line-by-line pass against the brief found
one substantial gap and three smaller ones.

**The gap: per-patient questions only ever answered for one patient.** The brief
says the collection spans multiple patients and asks these questions "for each
patient", but `router.route` set `mrn = mrns[0]`, so "how many sessions did each
patient attend" answered for whichever MRN sorted first. Since the supplied
corpus holds one patient, nothing in the dev answers could reveal it -- and a
second patient dropped in live would have exposed it immediately. Fixed with
`queries.per_patient`, which runs a per-patient query for each patient and keeps
each result, calculation and evidence under its own MRN; `render` emits one
section per MRN.

Implementing it surfaced a second-order bug I had to fix on top: the window was
being defaulted from the *first* patient's episode and then applied to
everybody. A question that fixes no date range now measures each patient over
their own episode, and `router.window_in_question` is what distinguishes the two
cases. The first version of my own fix passed its test only because the
synthetic second patient had no parseable episode -- a reminder that a fixture
too weak to exhibit the bug makes a test look green for the wrong reason.

**Collection-wide findings had no sources.** `consecutive_below` returned
`evidence: []`, so the one genuinely cross-patient answer was the only one a
reviewer could not trace to a passage. Every below-goal week now carries its
sessions, their quotes with offsets, and the plan requirement that was in force,
each tagged with its patient and week.

**The plan-change comparison compared raw totals.** Across unequal periods that
is meaningless -- three weeks at 40 min/week and one week at 120 min/week have
identical totals. It now reports per-week rates as the headline with raw totals
beside them, counts the weeks in each slice, and flags the week containing the
change date as having both plans in force. Tested against a real amendment: a
four-week episode whose goal rises from 60 to 120 minutes three weeks in, which
also exercises the dated-requirement machinery for the first time.

**Smaller:** global flags now work on either side of the subcommand
(`build documents --extractor llm` used to fail, which is a trap when somebody
else is driving); the last corpus-specific sentences came out of `compare.py`'s
report text; and the README's row counts, test count and bottleneck caveat were
brought in line with a clean build.

Two guards were added because this round was where I kept damaging my own
source: every module must parse, and no source file may contain a raw control
character -- a shell heredoc had silently turned `backslash-b` into a literal
backspace three times, disabling regex anchors in a way that renders as nothing
on screen.

---

## 16. Limitations and what I would do next

**1. The deterministic extractor is tuned to this corpus's vocabulary, and
nothing in the output says when it has read less than it should.**
Interval classification depends on cue lists. A document writing *"the group
paused for refreshments from 10:45"* instead of "break", or *"client was in the
room from 10:15"*, yields a claim with no patient-present interval. The event
then falls back to stated minutes or to `occurred=unknown` — honest — but a
wrong-but-plausible number is possible where a different cue shifts a range from
`break` to `patient_present`. The generalization tests pass on synthetic
documents with new phrasings, but that is four hand-written documents, not a
measurement.

*Next:* the disagreement harness now exists and gives the ratio (§14). Add a
**coverage assertion** to ingest: flag any document containing a clock-time range
that no claim accounts for, and any `N minutes` phrase no claim uses. That is a
cheap, answer-independent "I did not read this" signal of the same kind as the
wall-clock check, and I would run it before trusting the extractor on an unseen
corpus. Only then widen the cue lists, driven by what those two surface.

**2. The hybrid mode is measured but not adopted as the default.** §14 shows
it agrees on every number and finds 3.5x the narrative evidence, yet `rules`
remains the default because it is free, 1,800x faster, and byte-reproducible
across runs where the model is not. The honest position is that the right
default depends on the question: for counts and compliance, deterministic; for
anything resting on narrative evidence, hybrid. I have not built the switch
that would choose per query, and a reviewer should know the default is a
trade-off rather than a verdict.

**3. No materialized `patient_week` table.** The measured first bottleneck
(§13). Deliberately not built at this size; the fix is specified and the
measurement that justifies it is in the repo.

**4. Observation extraction is conservative and lossy.** 35 observation spans from 31
documents, filtered to sentences that name the patient, name a domain and carry
a change cue. It will miss evidence a careful reader would keep. The model path
exists mainly to replace this component — and run B's zero observations shows
that substitution is not yet reliable either.

**5. Week bucketing materially affects the answers.** Monday–Sunday is read from
the plan's own wording; Sunday–Saturday is available. The Jan 26–30 week is
partial at the episode end and is flagged rather than prorated, because the
documents do not say to prorate. Prorating would change the DEV-03 verdict.

**6. No plan amendment in the supplied data.** Requirements are stored as dated
rows and a later plan closes the earlier one; `plan_change_comparison` is
implemented and reachable. On this corpus it correctly reports that no
mid-episode change is documented — so the path is exercised but not validated
against a real amendment.

**7. One patient in the supplied data.** Nothing is keyed to that: the patient
is discovered from headers, every query takes a patient parameter, and the
scaling harness drives 200 patients through the same code.

**8. Ambiguous matches are reported, not resolved.** Six claims are unmatched on
this corpus, all administrative. That is the intended behaviour, but a reviewer
should know the number is not zero.
