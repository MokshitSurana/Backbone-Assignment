# The database, table by table

Every table in `backbone/schema.sql`: what it holds, which code writes it, which
code reads it, why every column exists, why it is shaped the way it is, and what
breaks if it is not there.

Line numbers are as of the commit this document ships with; function names are
stable and are the safer reference.

**Scope.** This describes the schema as it ships on `main` — the 13 tables of
the submitted system. This branch (`post-timebox`) carries a 14th,
[`llm_cache`](#llm_cache), which belongs to the model-backed extractor that was
moved off `main` to keep the submission inside the brief's time box; it is
documented last and is not part of the abstraction.

---

## Contents

- [The shape of the whole thing](#the-shape-of-the-whole-thing)
- [Who may touch the database](#who-may-touch-the-database)
- [The write map](#the-write-map)
- [Lifecycle: what gets deleted when](#lifecycle-what-gets-deleted-when)
- **Layer 1 — what arrived**
  - [`documents`](#documents)
  - [`document_aliases`](#document_aliases)
  - [`patients`](#patients)
- **Layer 2 — what documents assert**
  - [`claims`](#claims)
  - [`corrections`](#corrections)
- **Layer 3 — what the system concluded**
  - [`events`](#events)
  - [`event_claims`](#event_claims)
  - [`decisions`](#decisions)
- **Side tables — facts that are not contacts**
  - [`plan_requirements`](#plan_requirements)
  - [`episodes`](#episodes)
  - [`measures`](#measures)
  - [`observations`](#observations)
  - [`run_log`](#run_log)
  - [`llm_cache`](#llm_cache) *(this branch only)*
- [Recurring design decisions](#recurring-design-decisions)
- [What is deliberately NOT a table](#what-is-deliberately-not-a-table)
- [Row counts on the supplied corpus](#row-counts-on-the-supplied-corpus)

---

## The shape of the whole thing

Three layers, and the layering is the entire design:

```
LAYER 1   documents, document_aliases, patients
          "a file arrived, here is its identity"
                    │  ingest.ingest_path
                    ▼
LAYER 2   claims, corrections
          "this document ASSERTS X, in these exact characters"
                    │  reconcile.reconcile_patient
                    ▼
LAYER 3   events, event_claims, decisions
          "we CONCLUDED Y, by rule Z, from these claims"
                    │  queries.*  (reads only, writes nothing)
                    ▼
          answers + evidence
```

Each layer is derived from the one above and never writes back upward. That
gives three properties the brief asks for:

1. **Layer 2 is append-only in spirit.** A document's claims are never edited to
   reflect another document. Disagreement survives as two claims rather than one
   overwritten value.
2. **Layer 3 is disposable.** It can be deleted and rebuilt from layer 2 at any
   time, and `reconcile` does exactly that per patient on every run. So a policy
   change is a rebuild, not a migration.
3. **Everything in layer 3 is explained.** `decisions` holds one row per
   conclusion naming the rule that produced it. Nothing enters layer 3 silently.

The side tables (`plan_requirements`, `episodes`, `measures`, `observations`)
are also derived from layer 2, but hold facts that are not clinical *contacts*
and so do not belong in `events`.

---

## Who may touch the database

`backbone/store.py` is the only module that writes SQL. Nothing else in the
codebase contains an `INSERT`, `UPDATE` or `DELETE` — with one deliberate
exception noted below. The rule exists so that every write is a named method
with a docstring, reviewable in one file, rather than SQL scattered through
extraction and reconciliation logic.

| module | may write? | how |
|---|---|---|
| `ingest.py` | yes | `upsert_document`, `add_alias`, `insert_claim`, `upsert_patient`, `drop_document`, `drop_doc_derivations`, `decide`, `log_run` |
| `reconcile.py` | yes | `insert_event`, `link_claim`, `decide`, `insert_requirement`, `upsert_measure`, `insert_observation`, `clear_patient_events`, plus one raw `ex()` for `episodes` |
| `queries.py` | **no** | read-only: `q`, `q1`, and the typed readers |
| `router.py`, `render.py` | **no** | `router` reads `patients` and `events`; `render` touches nothing |
| `bench.py`, `experiment.py`, `cli.py` | indirectly | they call the pipeline, and `cli` calls `log_run` |

The exception: `reconcile._plan_rows` writes `episodes` through the generic
`Store.ex()` escape hatch (`reconcile.py:698`) rather than a dedicated method,
because `episodes` is a two-column upsert used in exactly one place. It is the
one write that does not have a named method, and the one place the "all SQL in
store.py" rule bends.

`queries.py` being structurally read-only matters: it is where every number
comes from, so a reader can be certain that asking a question cannot change an
answer.

---

## The write map

Every table, and the single function responsible for putting rows in it:

| table | written by | called from |
|---|---|---|
| `documents` | `Store.upsert_document` (store.py:106) | `ingest.ingest_path` |
| `document_aliases` | `Store.add_alias` (store.py:113) | `ingest.ingest_path` |
| `patients` | `Store.upsert_patient` (store.py:179) | `ingest.ingest_path` |
| `claims` | `Store.insert_claim` (store.py:141) | `ingest.ingest_path` |
| `corrections` | `Store.insert_claim` (side effect, store.py:150-159) | `ingest.ingest_path` |
| `events` | `Store.insert_event` (store.py:214) | `reconcile._resolve` |
| `event_claims` | `Store.link_claim` (store.py:221) | `reconcile._resolve` |
| `decisions` | `Store.decide` (store.py:226) | `reconcile` ×31, `ingest` ×3 |
| `plan_requirements` | `Store.insert_requirement` (store.py:264) | `reconcile._plan_rows` |
| `episodes` | raw `Store.ex` (reconcile.py:698) | `reconcile._plan_rows` |
| `measures` | `Store.upsert_measure` (store.py:276) | `reconcile._measures` |
| `observations` | `Store.insert_observation` (store.py:319) | `reconcile._observations` |
| `run_log` | `Store.log_run` (store.py:84) | `cli`, `ingest.ingest_dir` |

Note the asymmetry: **`ingest` owns layers 1 and 2; `reconcile` owns layer 3 and
the side tables.** No function writes across that line.

---

## Lifecycle: what gets deleted when

Deletion is as designed as insertion, because the system is re-run constantly —
`build` on a changed corpus, `add` on one new file, `experiment` twice over the
same documents under two policies.

### `Store.drop_doc_derivations(doc_pk)` — store.py:118

Called when one document is being re-extracted. Removes everything *derived from
that document*, leaving other documents alone:

```
corrections         WHERE claim_id IN (that document's claims)
event_claims        WHERE claim_id IN (...)
observations        WHERE claim_id IN (...)
plan_requirements   WHERE claim_id IN (...)
episodes            WHERE claim_id IN (...)
claims              WHERE doc_pk = ?
```

It deletes the children first and the claims last, because the children are
identified *by* claim id. Reversing the order would orphan them.

### `Store.drop_document(doc_pk)` — store.py:130

The above, plus `document_aliases` and the `documents` row itself. Used when a
file is edited in place: the old version must not survive alongside the new one
(`RULE-DEDUPE-03`, `ingest.ingest_path`).

### `Store.clear_patient_events(mrn)` — store.py:191

Called at the top of every `reconcile_patient`. Wipes **all of layer 3 and all
side tables for one patient**, so reconciliation always starts from a clean
slate and can never append to a previous run:

```
event_claims        for that patient's events
events              WHERE patient_mrn = ?
decisions           WHERE patient_mrn = ?        <- and ALSO
decisions           WHERE event_id LIKE 'mrn|%'  <- two routes, see below
measures            WHERE patient_mrn = ?
observations        WHERE patient_mrn = ?
plan_requirements   WHERE patient_mrn = ?
```

The two `decisions` deletes exist because a decision can be attached to a
patient by either route — some have an `event_id` (and the patient is its
prefix), some have only `patient_mrn` (an unmatched claim, a measure merge).
Deleting by one route alone leaves the other kind behind.

**This is where a real bug lived.** The original clear was:

```sql
DELETE FROM decisions WHERE event_id IS NULL AND from_claims IN (SELECT '[]' WHERE 0)
```

`SELECT '[]' WHERE 0` returns no rows, so `IN (...)` is never true and the
statement matched nothing. Every rebuild therefore appended another full copy of
every patient-level decision, and the exclusion list in DEV-01's answer grew on
each run. It is now tested by
`tests/test_multipatient.py::DecisionLogScoping::test_rebuilding_does_not_duplicate_decisions`,
which reconciles three extra times and asserts the row count does not move.

### Why `documents` and `claims` survive a reconcile

They are layer 1 and 2 — the record of what arrived and what it said. Nothing
about re-deciding should re-read files. This is what makes `reconcile` cheap
(measured 0.005 s for 31 documents) and what makes the conflict-policy
experiment possible: both policies run over the same claims.

---

# Layer 1 — what arrived

## `documents`

One row per distinct document. 14 columns.

```sql
doc_pk          TEXT PRIMARY KEY   -- = sha256_norm; content identity
doc_id          TEXT               -- the publisher's own id, e.g. "BH-D103"
path            TEXT NOT NULL
filename        TEXT NOT NULL
doc_class       TEXT NOT NULL
authority_class TEXT NOT NULL
sha256_raw      TEXT NOT NULL
sha256_norm     TEXT NOT NULL
char_len        INTEGER NOT NULL
received_at     TEXT               -- when the org took it into the chart
authored_at     TEXT               -- signature / entry timestamp
ingested_at     TEXT NOT NULL
extractor       TEXT NOT NULL
extractor_ver   TEXT NOT NULL
INDEX ix_doc_norm ON (sha256_norm)
```

**Written by** `Store.upsert_document`, from `ingest.ingest_path`.
**Read by** `queries` (evidence joins), `cli.cmd_export`, `Store.doc_by_norm`,
`Store.doc_by_path`.

### Why the primary key is a content hash

`doc_pk` is the SHA-256 of the **normalized** text, not a filename and not a
serial number. This single decision gives duplicate-immunity for free:

- re-ingest the same file → same `doc_pk` → `INSERT OR REPLACE` → still one row
- copy it to a new name → same `doc_pk` → recognised as a duplicate
- add a fax header or change whitespace → **still** the same `doc_pk`, because
  normalization strips exactly that

A serial id would have required a separate "have I seen this before?" check that
could be forgotten. With a content-addressed key, idempotence is a property of
the schema rather than a rule in the code.

### Why both hashes are kept

`sha256_raw` is the hash of the bytes as read; `sha256_norm` of the normalized
text. Only `sha256_norm` is identity — but keeping `sha256_raw` lets
`ingest.ingest_path` distinguish two cases a reviewer cares about:

| | | `document_aliases.kind` |
|---|---|---|
| raw hashes match | a byte-identical copy | `exact_duplicate` |
| raw differ, norm match | re-stamped, re-scanned, differently spaced | `normalized_duplicate` |

Without `sha256_raw` both would collapse into "duplicate" and the system could
not tell a plain copy from a re-transmission.

### Why `doc_class` and `authority_class` are both here

They are the same value on this table (both set from `facts.doc_class` in
`ingest.ingest_path`), and that looks redundant. It is deliberate: `doc_class`
is *what kind of document this is*, `authority_class` is *how much it is
trusted*. They are equal by default and separable later — and at claim level
they already diverge (see `claims.authority_class`), because one document can
hold an unsigned draft and a posted charge with different authority.

### Why `received_at` and `authored_at` are separate, and nullable

Two different dates with two different meanings: when the clinician signed it,
and when the records department filed it. The January 26 re-transmission of the
January 19 roster has `received_at = 2026-01-26` and `authored_at = 2026-01-19`
— and conflating them is precisely the error the conflict-policy experiment
measures. Both are nullable because plenty of documents state neither.

### Why `extractor` and `extractor_ver`

So a mixed database is still explicable. `ingest.ingest_path` skips
re-extraction when both match the current values, which is what makes a warm
re-ingest 0.011 s instead of 0.16 s. Bump `taxonomy.EXTRACTOR_VERSION` and every
document re-extracts on the next run — a one-line cache invalidation.

### Without this table

No duplicate detection, no incremental re-ingest, and evidence could not name
its source document.

---

## `document_aliases`

One row per *file path* that resolved to an already-known document. **0 rows on
a clean build** — every file in `documents/` is distinct. Rows appear only when
the same content arrives at a second path: the test suite's `out/tmpdup/`
copies, or a re-ingest of a directory that is itself a copy. It is the one table
whose emptiness is the expected result rather than a sign of breakage.

```sql
path    TEXT PRIMARY KEY   -- the duplicate file's own path
doc_pk  TEXT NOT NULL      -- the document it turned out to be
kind    TEXT NOT NULL      -- exact_duplicate | normalized_duplicate
seen_at TEXT NOT NULL
```

**Written by** `Store.add_alias`, from `ingest.ingest_path` when
`doc_by_norm` hits and the path differs.
**Read by** `queries.duplicates`, surfaced through `ask "which duplicates were detected?"`.

### Why a table rather than just skipping the file

Because "we ignored a file" is a claim a reviewer may want to check. Silently
skipping would be indistinguishable from never having seen the file. The table
turns dedupe from invisible behaviour into a reportable fact, and `path` is the
primary key because a given path resolves to exactly one document.

### Without this table

Dedupe would still work — identity is in `documents.doc_pk` — but the system
could not answer "what did you skip, and why did you think it was a duplicate?"

---

## `patients`

```sql
mrn        TEXT PRIMARY KEY
name       TEXT
given_name TEXT
dob        TEXT
```

**Written by** `Store.upsert_patient`, from `ingest.ingest_path`, using
`DocFacts` the extractor read out of the document header.
**Read by** `router.resolve_patients` (name → MRN), `bench` (sample questions),
`cli.cmd_export`, `Store.patients`.

### Why it exists at all, given MRN is on every other table

Three jobs:

1. **The patient list.** `Store.patients()` is how `reconcile_all` and every
   collection-wide query know who exists. Deriving it with
   `SELECT DISTINCT patient_mrn FROM events` would miss a patient whose
   documents produced no events.
2. **Name → MRN.** `router.resolve_patients` matches a question's words against
   `name` and `given_name`. This is what lets "how many sessions did Rowan
   attend" work **without any patient name in the code** — the brief forbids
   encoding patient facts, and `tests/test_units.py::RouterSafety` enforces it
   across the whole package.
3. **`given_name` for extraction.** `rules._presence_detail` and
   `rules._observations` use the given name to recognise `"Rowan present with
   partner: 13:15-13:45"` as patient-present time and to attribute an
   observation to the patient. The name comes from the document at runtime.

### Why `upsert` and not `insert`

The same patient appears in 31 documents and some state less than others — a
roster extract may carry the MRN but no DOB. `upsert_patient` uses
`COALESCE(?, column)` so a later document can **fill in** a missing field but
never blank out one already known.

### Without this table

The router could not resolve a name without hardcoding it, and collection-wide
queries would silently skip patients with no events.

---

# Layer 2 — what documents assert

## `claims`

**The heart of the design.** 26 columns, 99 rows on the supplied corpus. One row
per assertion by one document about one thing.

```sql
claim_id        TEXT PRIMARY KEY
doc_pk          TEXT NOT NULL      -- which document says this
patient_mrn     TEXT
claim_type      TEXT NOT NULL      -- encounter|correction|measure|
                                   -- plan_requirement|episode|observation|
                                   -- charge|authorization
encounter_ref   TEXT               -- three id systems, kept apart
appointment_ref TEXT
form_ref        TEXT
call_ref        TEXT
service_date    TEXT
service_raw     TEXT               -- the wording, before canonicalisation
service_type    TEXT               -- the canonical type
presence        TEXT               -- present|partial|absent|cancelled|unknown
presence_basis  TEXT               -- scheduled|attested|narrative|
                                   -- platform_log|derived
arrival         TEXT
departure       TEXT
intervals       TEXT               -- JSON [[hh:mm,hh:mm],...] PATIENT-PRESENT only
breaks          TEXT               -- JSON, nontherapeutic
stated_minutes  INTEGER
minutes_basis   TEXT               -- stated_patient|stated_total|
                                   -- computed_from_intervals|scheduled
authority_class TEXT NOT NULL      -- per CLAIM, not per document
fields          TEXT               -- JSON extras
quote           TEXT NOT NULL
quote_start     INTEGER NOT NULL
quote_end       INTEGER NOT NULL
quote_verified  INTEGER NOT NULL DEFAULT 0
extractor       TEXT NOT NULL
INDEX ix_claims_enc  ON (patient_mrn, encounter_ref)
INDEX ix_claims_date ON (patient_mrn, service_date)
INDEX ix_claims_doc  ON (doc_pk)
```

**Written by** `Store.insert_claim`, from `ingest.ingest_path`, from `Claim`
objects built by `rules._mk_claim`.
**Read by** `Store.claims_for_patient` (the whole of reconciliation),
`Store.event_evidence` (every piece of evidence in every answer),
`queries._req_evidence`, `queries.progress`.

### Why one row per *assertion* and not per document

Because the mapping is many-to-many in both directions:

- `BH-D107` describes **two** encounters (Jan 22 and Jan 29) → 2 claims
- `BH-D108` describes **four** → 4 claims
- the appointment export describes **nine** → 9 claims
- while `HG-E110` is described by **four** documents → 4 claims on one event

A per-document table could represent neither direction. The Jan 19 group session
has a facilitator note, a signed roster, a correction and a re-sent copy; they
must be four separate assertions that reconciliation weighs, not one row
overwritten three times.

### Why three identifier columns and not one

`encounter_ref`, `appointment_ref`, `call_ref` are three different id systems in
the source data, and only the first identifies a real-world contact:

| column | example | meaning |
|---|---|---|
| `encounter_ref` | `HG-E112` | the clinical contact — **the event key** |
| `appointment_ref` | `HG-A112` | the booking; resolves *to* an encounter |
| `call_ref` | `VC-112A` | one telehealth connection leg |

The January 21 session dropped its connection and reconnected, producing two
call ids under one appointment under one encounter. Collapsing these into one
`external_id` column would make that session either two sessions or
unrepresentable. `reconcile.RULE-MATCH-03` exists only to say "a call id never
keys an event", and it can only be stated because the columns are separate.

### Why `intervals` is patient-present *only*

The single most important constraint on this table. A document can state several
time ranges meaning entirely different things (`BH-D113` has three on two
adjacent lines: therapist 13:00–13:45, partner-only 13:00–13:15, patient
13:15–13:45). If `intervals` held all of them, every downstream consumer would
have to re-derive which were the patient's — and would eventually get it wrong.

So the extractor classifies first (`rules._classify_interval`) and `intervals`
holds only the patient-present ones. Scheduled slots go to `fields["scheduled"]`;
therapist-only and partner-only go to `fields["other_present"]`; breaks have
their own column. **`queries` can therefore sum `intervals` without ever asking
what they are.**

### Why `breaks` is its own column

Because a break is subtracted, not counted, and it frequently comes from a
*different document* than the presence interval. On January 19 the interval
comes from the signed roster (`BH-D102`) and the 10:45–11:00 break from the
facilitator note (`BH-D101`). `reconcile._resolve` pools breaks across **all**
claims on an event, then subtracts by overlap. That pooling is only possible
because breaks are a first-class column rather than buried in a blob.

### Why both `intervals` and `stated_minutes`

Some documents give clock times, some give a number, some give both. Where both
exist they corroborate; where only a number exists it is all there is.
`minutes_basis` records which kind it was, so `reconcile` knows how much to
trust it — `stated_patient` outranks `stated_total`.

### Why `presence` *and* `presence_basis` — and which one actually decides

`presence` is what the document claims. `presence_basis` records **what kind of
evidence the sentence was**:

- `attested` — a signed attendance record
- `narrative` — a clinician wrote it in prose
- `scheduled` — it is a booking view
- `platform_log` — a connection export
- `derived` — inferred from surrounding wording

**`presence_basis` is written and never read.** `rules.py` sets it at four
sites; no decision logic consults it. The admissibility rule that stops a draft
note from establishing delivery keys off **`authority_class`**, not the basis —
`reconcile.py` references `authority_class` 16 times and `presence_basis` not
once.

The corpus contains the case that proves the basis column is not doing the work:

| document | `presence_basis` | `authority_class` | outcome |
|---|---|---|---|
| BH-D102, Jan 19 roster | `derived` | `attendance_register` | qualifies |
| BH-D112, Jan 27 draft | `derived` | `draft_note` | rejected |

Same basis, opposite outcomes. Drop `presence_basis` and no answer changes;
drop `authority_class` and the two become indistinguishable.

So the honest account is: `presence_basis` is descriptive. It is useful when
reading `claims` by hand, and it is the column to reach for if the system ever
needs to separate "a register asserted this" from "we inferred it from wording"
*within* one authority class — a real distinction, since BH-D102 is `derived`
and BH-D108 is `attested` and both are registers. Today nothing uses it, and it
is the one column in this schema I would defend as documentation rather than as
machinery.

### What `authority_class` does instead: RULE-PRES-02

`taxonomy.ESTABLISHES_DELIVERY` (taxonomy.py:108) is a flat map from authority
class to whether a *positive* claim from that class can establish that care was
delivered. `clinical_note`, `attendance_register` and `correction` are true;
`draft_note`, `billing`, `schedule_export`, `platform_log`, `retransmission`,
`admin_log`, `measure` and `treatment_plan` are false.

January 27, encounter `HG-E116`, is the case it exists for. Two documents
contradict each other outright:

```
BH-D108  (final attendance register)
  "January 27 | HG-E116 | Skills group | 10:00-11:30 | - | - |
   No show; patient did not attend"

BH-D112  (draft note and charge extract)
  "Template attendance text: Patient attended the full session and
   participated in the skills discussion."
```

The second is boilerplate — it labels itself "Template attendance text" — in an
unsigned draft, and the same document carries a **posted charge** (`CH-116`, one
group session). The billing system already believes care happened. Counting
sentences, or preferring the later document, bills for a session the patient did
not attend.

`reconcile._presence` (reconcile.py:385) instead never admits the positive claim
to the qualifying pool, and logs the rejection:

```
[RULE-PRES-02] rejected_presence = present
    draft_note cannot establish that care was delivered (BH-D112)
[RULE-PRES-03] occurred = no
    final disposition records absent (BH-D108: no show)
```

**This is a veto, not a ranking**, and the distinction is the whole point. Under
a ranking ("register beats draft"), a missing register would let the draft win
by default. Under a veto, a draft note *alone* leaves nothing that can establish
delivery and `occurred` stays `unknown` — which is the correct answer rather
than an inconvenient one. A ranking cannot express "none of these qualify".

Note that negative claims use a **different, wider** list (reconcile.py:391):
`attendance_register`, `schedule_export`, `admin_log`, `clinical_note`,
`correction`. The asymmetry is deliberate — a schedule export cannot prove a
session happened, but it is adequate evidence that one was cancelled.

### Why a veto and not just a ranking — the measured answer

`taxonomy.FIELD_AUTHORITY` (taxonomy.py:126) *does* contain a presence ranking:
`correction` 100, `attendance_register` 80, `clinical_note` 70, `draft_note` 5,
`billing` 0. The obvious objection is that a ranking already handles January 27
— the register scores 80, the draft scores 5, the register wins, so why is an
admissibility veto needed at all?

Two reasons, and the second is the real one.

**First: `FIELD_AUTHORITY["presence"]` is never read.** The only reference to
`FIELD_AUTHORITY` anywhere in the package is reconcile.py:565, and it reads the
`"duration"` map. The presence sub-map is dead data — the second such case in
this schema, alongside [`presence_basis`](#why-presence-and-presence_basis--and-which-one-actually-decides).
Presence is decided entirely by admissibility plus a refusal to break ties, with
no scoring involved.

**Second: a ranking cannot return "none of the above".** `max()` over a
one-element list is that element. `draft_note` scoring 5 only loses when
something scores higher; delete the higher thing and 5 is the maximum. Five beats
an empty field.

That is testable, so it was tested. Removing
`BH-D108_final_attendance_and_cancellation_register.txt` from the corpus and
rebuilding leaves `HG-E116` with only the draft note and its posted charge:

```
events:  occurred=unknown  patient_present=unknown  min=0  max=0
         counts_as_therapy=0  disputed=1

[RULE-PRES-02] rejected_presence = present
    draft_note cannot establish that care was delivered (BH-D112)
[RULE-PRES-02] occurred = unknown
    the only records asserting this contact cannot establish delivery
```

Under a ranking the draft would have been the sole candidate and won by default:
`occurred = yes`, a therapy session counted and a charge substantiated for a
contact that never happened. Under the veto the honest answer survives the
disappearance of the authoritative document.

This is the same shape as the `_stated_minutes` veto in the extractor, and the
reason is identical in both places: **a ranking expresses "which of these
wins?", a veto expresses "does anything here qualify at all?"** The second
question has no answer in a scoring scheme.

For completeness, two admissible records that disagree are not ranked either —
they escalate:

```
[RULE-PRES-04] occurred = unknown
    qualified records disagree about whether the contact happened
```

So even `attendance_register` (80) against `clinical_note` (70) yields `unknown`
rather than letting the register win. `RULE-PRES-04` fires zero times on the
supplied corpus — the one disputed event, `HG-E115` on January 26, is disputed
over *duration* (40 vs 50 minutes), where ranking genuinely is used. Presence
never ranks.

### Why `authority_class` is on the claim, not just the document

Because one document can carry claims of different authority. `BH-D112` is an
administrative chart extract containing (a) an unsigned draft note and (b) a
posted charge. The draft claim gets `authority_class = "draft_note"`, the charge
gets `"billing"`, and neither can establish delivery — but for different reasons
a reviewer may want to see separately. `rules._narrative_encounters` overrides
the class per section when it finds "unsigned" and "draft"; `rules._admin` forces
`"billing"` on a charge.

### Why `quote`, `quote_start`, `quote_end` are `NOT NULL`

This is the brief's auditability requirement expressed as a schema constraint
rather than a code convention. **A claim cannot physically exist without a
source span.** There is no code path that could forget to attach evidence,
because the `INSERT` would fail.

Offsets point into the **raw bytes of the file as read**, never into normalized
text, so a reviewer opening the document in any editor lands on the quoted
characters. `quote_verified` records whether `text[start:end] == quote` held at
insert time; `rules._mk_claim` checks it at creation and `ingest.ingest_path`
checks it again before writing, logging `RULE-QUOTE-01` for any failure.

### Why `fields` is an untyped JSON blob

The deliberate compromise. Columns are for anything `reconcile` or `queries`
*dispatches on*; `fields` is for everything else — `negation_kind`,
`service_date_basis`, `presence_ambiguous`, `interval_classes`, `reason_added`,
`evidence_only`, `scheduled`, `status_raw`, `is_import`, `metric`, `threshold`.

The reasoning: these are read by exactly one consumer each, several are
diagnostic rather than functional, and promoting each to a column would give a
40-column table where most columns are null on most rows. The cost is that they
are not indexable or type-checked — acceptable for diagnostics, which is why
anything load-bearing (`presence`, `intervals`, `authority_class`) is a real
column.

Three `fields` keys *are* load-bearing and arguably should be promoted:
`negation_kind` (reconcile dispatches on it), `target_field` (corrections), and
`metric`/`threshold` (plan requirements). The latter two are mitigated by
`corrections` and `plan_requirements` existing as typed tables downstream.

### Why `service_raw` alongside `service_type`

`service_type` is canonical (`group_therapy`); `service_raw` is what the
document actually said (`"Coping skills group"`). Keeping the raw wording means
a mis-canonicalisation is diagnosable after the fact instead of invisible.

### The indexes, and why each one

| index | serves |
|---|---|
| `(patient_mrn, encounter_ref)` | `reconcile._match` grouping claims by encounter — the hot path |
| `(patient_mrn, service_date)` | fallback matching on date, and date-ranged reads |
| `(doc_pk)` | `drop_doc_derivations` when re-extracting one document |

All patient-scoped indexes lead with `patient_mrn`, which is what keeps
per-patient queries flat as the corpus grows (measured: ~1 ms at 1 patient and
at 200).

### Without this table

The system becomes a flat session table, and the Jan 19 correction becomes
unrepresentable: you could store `11:15` or `11:30`, but not *why*, and not the
difference between the correction and the re-sent copy that also says `11:30`.

---

## `corrections`

One row per correction relationship. 1 row on the supplied corpus.

```sql
claim_id     TEXT PRIMARY KEY REFERENCES claims(claim_id)
target_enc   TEXT        -- the encounter being corrected
target_field TEXT NOT NULL  -- arrival|departure|duration|presence|service_date
old_value    TEXT        -- nullable: not every correction states it
new_value    TEXT NOT NULL
```

On this corpus the single row is:

```
claim_id=...-C01  target_enc=HG-E110  target_field=departure
old_value=11:30   new_value=11:15
```

**Written by** `Store.insert_claim` as a *side effect* (store.py:150-159) when
`claim_type == "correction"`, unpacking `fields["target_field"]`,
`fields["old_value"]`, `fields["new_value"]`.
**Read by** `Store.corrections(mrn)`, which joins back to `claims` and
`documents` to return the source span alongside.

### Why a separate table when the data is already in `claims.fields`

Because a correction is a **relationship between records**, not a property of
one. `claims` says "document BH-D103 contains this sentence"; `corrections` says
"this claim replaces the departure field of encounter HG-E110". A reviewer
opening the database should be able to ask *"what has been corrected in this
chart?"* and get an answer from one table rather than by parsing JSON blobs.

### Why it is written as a side effect of `insert_claim`

So it cannot drift. If it were a separate call, some future code path could
insert a correction claim and forget the relationship row. Coupling them in one
method makes the two always consistent, and `drop_doc_derivations` deletes
`corrections` first, before the claims they reference.

### This table was empty for most of the build

The table existed in `schema.sql` from the beginning and **nothing wrote to it**
— the relationship lived only inside `claims.fields` JSON. A review pass caught
it: anyone opening the database would have found a plausible-looking empty
table. That is now tested by `tests/test_multipatient.py::CorrectionsTable`,
including a check that the stored offsets still resolve against the raw file and
that re-ingesting does not duplicate the row.

### Why `old_value` is nullable but `new_value` is not

A correction must state what the value *becomes* or it says nothing. Stating
what it *replaces* is good practice but not universal. The nullability is load
bearing: `reconcile._apply_corrections` applies a correction with no stated old
value **only** when the claim has exactly one interval, because otherwise there
is no way to tell which interval it meant — and it logs the ambiguity rather
than guessing. An earlier version rewrote *every* interval, which would silently
corrupt a split session like the January 21 telehealth contact.

---

# Layer 3 — what the system concluded

## `events`

One row per reconciled real-world contact. 20 rows on the supplied corpus (12
counted as therapy).

```sql
event_id          TEXT PRIMARY KEY    -- "HG-M042|HG-E110"
patient_mrn       TEXT NOT NULL
encounter_ref     TEXT                -- null for fallback-matched events
service_date      TEXT NOT NULL
service_type      TEXT NOT NULL
occurred          TEXT NOT NULL       -- yes|no|unknown
patient_present   TEXT NOT NULL       -- yes|no|partial|unknown
min_minutes       INTEGER NOT NULL DEFAULT 0
max_minutes       INTEGER NOT NULL DEFAULT 0
counts_as_therapy INTEGER NOT NULL DEFAULT 0
exclusion_reason  TEXT
disputed          INTEGER NOT NULL DEFAULT 0
resolved          TEXT NOT NULL       -- JSON: the full resolved field map
match_rule        TEXT NOT NULL
INDEX ix_events_pat ON (patient_mrn, service_date)
```

**Written by** `Store.insert_event`, from `reconcile._resolve` — once per event
per reconcile run.
**Read by** `Store.events(mrn, start, end)`, and through it every function in
`queries.py`. Also `router.corpus_year`, `bench`.

### Why the primary key is `mrn|encounter_ref`

Compound, human-readable, and deterministic. `"HG-M042|HG-E110"` is stable
across rebuilds, which means `trace "HG-M042|HG-E110"` works in a later session
and across a policy change. A surrogate integer would renumber on every
rebuild, breaking any recorded reference. Prefixing with the MRN also means
`DELETE FROM decisions WHERE event_id LIKE 'mrn|%'` can find a patient's
decisions by key alone.

For fallback-matched events the key is `mrn|date|service_type` instead, which is
equally deterministic.

### Why `occurred` and `patient_present` are separate

They are **different facts**, and conflating them loses real information:

| | `occurred` | `patient_present` | example |
|---|---|---|---|
| ordinary session | yes | yes | Jan 5 individual |
| no-show | no | no | Jan 8 |
| clinic cancelled | no | no | Jan 15 |
| collateral contact | **yes** | **no** | Jan 16 partner-only |
| care coordination | **yes** | **no** | Jan 23 |
| disputed attendance | unknown | unknown | synthetic test case |

The January 16 collateral contact *happened* — it is real documented care
involving the partner — but the patient was not there, so it contributes no
patient-present minutes. A single `attended` boolean would force that to be
either a session (wrong) or nothing (losing the fact that care was delivered).

### Why both are tri-state rather than boolean

`unknown` is a required answer. When a clinical note and a signed register
disagree with equal authority, `RULE-PRES-04` sets `occurred = "unknown"` rather
than picking. A boolean would force a guess, and the brief explicitly asks for
"cannot be determined" where the documents do not settle it.

### Why `min_minutes` and `max_minutes` instead of one duration

This is the bounds-not-labels design, in the schema:

| situation | min | max |
|---|---:|---:|
| confirmed 45-minute session | 45 | 45 |
| Jan 26 — two notes, 50 and 40, neither a correction | 40 | 50 |
| attendance unresolved | 0 | what the records would support |
| no-show | 0 | 0 |

`queries.compliance` then has three outcomes instead of two: `MET` when the
minimum clears the threshold, `NOT_MET` when even the maximum misses,
`CANNOT_DETERMINE` when the threshold falls inside the band. Week 4 is 145–155
against a 150-minute goal, so the honest answer is `CANNOT_DETERMINE` — and that
is only expressible because two columns exist.

A single `minutes` column plus a `disputed` flag would not work: a flag says
*that* there is doubt, not *how much*, so it could not be summed into a weekly
band.

**A bug lived here too.** Duration was originally computed only when
`occurred == "yes"`, so an event with `occurred = "unknown"` stored `0, 0` — and
`_band` returned `(0, 0)`. A week hinging on a disputed attendance therefore came
out `NOT_MET` instead of `CANNOT_DETERMINE`, and such a patient could never
reach the "inclusion depends on unresolved documentation" group, defeating the
whole mechanism. `reconcile._resolve` now computes the duration for
`occurred == "unknown"` too and keeps it as the **upper bound only**
(`reconcile.py`, the `elif occurred == "unknown"` branch), tested by
`tests/test_review_fixes.py::DisputedAttendanceBounds`.

### Why `counts_as_therapy` is stored rather than derived

It is a *conclusion* with a *reason*, and `exclusion_reason` holds the reason in
the same row. Deriving it at query time would mean re-deciding — and re-deciding
in `queries`, which is supposed to compute, not judge. The pair means every
excluded event carries its own explanation:

```
HG-E106  medication_management is named in the plan as not contributing
HG-E109  collateral_contact is named in the plan as not contributing
HG-E116  contact did not occur (occurred=no)
```

### Why `resolved` is a JSON blob

It holds the parts of the resolution that are *evidence about the conclusion*
rather than things queries filter on: the pooled `breaks`, the final
`presence_intervals` after corrections, the `duration_claims` that won, the
`evidence_docs`. Example from `HG-E110`:

```json
{"breaks": [["10:45","11:00"]],
 "presence_intervals": [["10:00","10:45"],["11:00","11:15"]],
 "duration_claims": ["c8fbc79cb86f-N01"],
 "evidence_docs": ["BH-D101","BH-D102","BH-D103","BH-D104"],
 "min_minutes": 60, "max_minutes": 60, "occurred": "yes", ...}
```

`presence_intervals` is what `queries._wall_clock` compares pairwise to detect a
patient in two rooms at once — the check that caught the latest-document policy.
That check needs the *resolved* intervals, after correction and break
subtraction, which exist nowhere else.

The duplication of `min_minutes`/`occurred` between columns and blob is
intentional: the columns are for querying, the blob is a self-contained snapshot
that can be read without joining.

### Why `disputed` when bounds already encode it

`min != max` implies dispute, but not every dispute widens the band — a
presence dispute with no duration candidates leaves `0, 0` *and* is disputed.
The flag is also what `queries._dispute_caveats` filters on to produce the
"not settled by the available documents" section, including what would settle it.

### Without this table

Every question would re-run reconciliation. Measured, that is the difference
between a ~1 ms query and re-deciding 99 claims per patient per question.

---

## `event_claims`

The many-to-many join between events and the claims that support them. 48 rows.

```sql
event_id   TEXT NOT NULL REFERENCES events(event_id)
claim_id   TEXT NOT NULL REFERENCES claims(claim_id)
role       TEXT NOT NULL   -- primary|corroborating|correction|superseded|rejected
match_rule TEXT NOT NULL   -- which rule attached it
PRIMARY KEY (event_id, claim_id)
```

**Written by** `Store.link_claim`, from `reconcile._resolve`, once per claim per
event.
**Read by** `Store.event_evidence(event_id)` — which is the source of **every
evidence span in every answer** — and `cli.cmd_export`.

### Why `role` is the most valuable column here

It is the audit story. Without it, a claim that lost would simply be absent, and
a reviewer could not distinguish "we never saw that document" from "we saw it and
rejected it". With it, `trace` prints:

```
(correction,    correction)          BH-D103  "Correction: Patient departure ... is 11:15 ..."
(primary,       attendance_register) BH-D102  "Patient arrival: 10:00 | Patient departure: 11:30 ..."
(superseded,    retransmission)      BH-D104  "Patient arrival: 10:00 | Patient departure: 11:30 ..."
(corroborating, clinical_note)       BH-D101  "Group encounter: HG-E110 | Facilitator: ..."
```

`BH-D104` is visibly *present and superseded*. That is the difference between an
auditable system and one that merely got the right answer. Roles are assigned by
`reconcile._role`. The distribution on this corpus: 28 corroborating, 16
primary, 2 rejected, 1 superseded, 1 correction — more claims corroborate than
decide, which is what a corpus with four documents per contact looks like.

### Why `match_rule` per link

Each claim may have joined the event by a different route: an encounter id
(`RULE-MATCH-01`), an appointment id (`RULE-MATCH-02`), the date+service fallback
(`RULE-MATCH-04`), or as a correction (`RULE-PRES-01`). On the supplied corpus:
42 by encounter id, 2 by appointment id (the telehealth call legs), 3 by
fallback, 1 correction.

**This column was lying for most of the build.** `_resolve` wrote
`"RULE-MATCH-01"` for every link regardless of how the claim actually matched,
because the rule was not threaded through from `_match`. It now is (`matched_by`
dict), and `tests/test_review_fixes.py::SmallerIssues` asserts the telehealth
call rows specifically carry `RULE-MATCH-02`.

### Why a join table rather than `claims.event_id`

Because the relationship is genuinely many-to-many *in time*: claims exist
before events, survive a reconcile, and are re-linked under a different policy.
A column on `claims` would mean layer 2 mutating whenever layer 3 is rebuilt,
breaking the one-way derivation that makes the conflict-policy experiment
possible.

---

## `decisions`

One row per conclusion, naming the rule that produced it. 156 rows for 31
documents on a clean build — the largest table by row count.

```sql
decision_id INTEGER PRIMARY KEY AUTOINCREMENT
event_id    TEXT              -- null for decisions not tied to one contact
patient_mrn TEXT              -- set even when event_id is null
scope       TEXT NOT NULL     -- event|match|dedupe|plan|measure
field       TEXT NOT NULL     -- what was decided
chosen      TEXT              -- the value chosen
rule_id     TEXT NOT NULL     -- RULE-DUR-01, RULE-PRES-02, ...
rationale   TEXT NOT NULL     -- prose, naming documents
from_claims TEXT NOT NULL     -- JSON [claim_id, ...]
INDEX ix_dec_event ON (event_id)
INDEX ix_dec_pat   ON (patient_mrn)
```

**Written by** `Store.decide` — 31 call sites in `reconcile.py`, 3 in
`ingest.py`.
**Read by** `Store.event_decisions(event_id)` (powers `trace`),
`queries._count_caveats`, `queries._dispute_caveats`, `cli.cmd_export`.

### Why this table is the answer to the brief

"A reviewer should be able to trace a finding ... to the individual patients,
services, calculations, and source passages that support it." This is the row
that answers *why*:

```
[RULE-DUR-01] minutes = 60 — attendance_register BH-D102 is the authority for
duration on a group_therapy event; intervals+correction; correction
6853e674a5cd-C01 applied
```

Not "the model thought so". A rule id, a document, and the claims it came from.

### Why `rule_id` is a short code and `rationale` is prose

The code is for machines and for cross-referencing the README's rule table; the
prose is for a human reading `trace` output. Both matter: `queries._count_caveats`
filters by `rule_id` to collect the rejected-claim list, while the rationale is
what makes the output readable without the source open.

### Why `event_id` is nullable, and why `patient_mrn` exists separately

Some decisions are not about one contact: an unmatched claim
(`RULE-MATCH-05`), a measure identity (`RULE-MEAS-01`), a plan requirement
(`RULE-INC-01`), a duplicate document (`RULE-DEDUPE-01`). Those have no
`event_id`.

`patient_mrn` was added later and fixes **two** bugs at once:

1. **A cross-patient leak.** `queries._count_caveats` read the whole table with
   no filter, so with a second patient, one patient's answer listed the *other
   patient's* rejected claims. It now filters
   `WHERE patient_mrn = ? OR (patient_mrn IS NULL AND rule_id = 'RULE-DEDUPE-01')`
   — the exception because dedupe happens before a patient is known, and is
   genuinely corpus-level.
2. **Unbounded growth.** `clear_patient_events` could not delete patient-level
   decisions without this column (see
   [Lifecycle](#lifecycle-what-gets-deleted-when)).

It is populated three ways in `Store.decide`: an explicit `mrn` argument, else
parsed from the `event_id` prefix, else from `Store._patient_ctx` — a context
set by `reconcile_patient` and `ingest_path` so that the 34 call sites did not
each need a new argument.

### Why `from_claims` is JSON rather than a join table

A decisions-to-claims join table would be more normal. It is not here because
`from_claims` is never queried *by* claim — it is only ever read back as part of
rendering one decision. A join table would add a fourth layer-3 table and a
delete cascade for no query benefit.

### Without this table

The system still produces the right numbers and still cites passages. What it
loses is the *why*: which rule fired, what it rejected, and what it would take
to change the answer. That is most of what a 30-minute design review probes.

---

# Side tables

## `plan_requirements`

Each goal the treatment plan states, as a dated row. 2 rows.

```sql
req_id          TEXT PRIMARY KEY   -- "mrn|metric|effective_start"
patient_mrn     TEXT NOT NULL
metric          TEXT NOT NULL      -- therapy_days | therapy_minutes
service_types   TEXT NOT NULL      -- JSON list of types that count
period          TEXT NOT NULL      -- week_mon_sun | week_sun_sat
comparator      TEXT NOT NULL      -- ">="
threshold       REAL NOT NULL
effective_start TEXT NOT NULL
effective_end   TEXT               -- null = still in force
claim_id        TEXT NOT NULL      -- the sentence it was read from
```

On the supplied corpus:

```
therapy_days     >= 3    week_mon_sun  2026-01-05 .. 2026-01-30
therapy_minutes  >= 150  week_mon_sun  2026-01-05 .. 2026-01-30
```

**Written by** `Store.insert_requirement`, from `reconcile._plan_rows`.
**Read by** `Store.requirements(mrn)`, `queries.compliance`,
`queries.included_types`, `queries.plan_change_comparison`.

### Why one row per metric rather than one row per plan

Because the brief asks whether care met "the treatment plan's requirements in
effect for each period", and the two metrics can be in force for different
periods after an amendment. A single plan row with `days` and `minutes` columns
could not express a plan that changes only the minute goal.

It also makes the conjunction explicit: `queries.compliance` iterates the rows
in force for a week and requires **all** of them, which is why one definite
failure settles the week even when another metric is uncertain
(`NOT_MET` outranks `CANNOT_DETERMINE`).

### Why `effective_start` / `effective_end` rather than "current"

Dated rows are what make "in effect for each period" answerable at all. A later
plan does not overwrite an earlier one — `reconcile._plan_rows` sorts by
effective start and closes the previous row at `new_start - 1 day`. Each week is
then checked against the requirement in force *for that week*.

Tested against a synthetic amendment in
`tests/test_per_patient.py::PlanChangePerWeek`: a four-week episode whose minute
goal rises from 60 to 120 three weeks in, asserting week 1 is checked against 60
and week 4 against 120.

### Why `service_types` is JSON and read from the plan

The plan itself states which services count: *"Patient-present individual,
group, and family therapy contribute to the minute goal. Medication management,
contacts with collateral informants only, and care coordination do not
contribute."* `rules._contrib_sets` reads both sentences and
`queries.included_types` unions them.

Hardcoding `["individual","group","family"]` would be a patient fact in the
code, and would silently produce wrong answers on a plan that counts something
else. JSON because the list length varies and nothing queries *by* service type.

### Why `claim_id`

So the requirement can cite the sentence it came from.
`queries._req_evidence` joins through it, which is how the compliance answer
prints:

```
- therapy minutes >= 150 per mon_sun week, effective 2026-01-05 to 2026-01-30
    - BH-D003 [779:918] "Local treatment participation goal: at least 3 therapy
      days and at least 150 minutes of patient-present therapy in each
      Monday-Sunday week."
```

---

## `episodes`

The treatment episode window. 1 row.

```sql
patient_mrn TEXT NOT NULL
start_date  TEXT NOT NULL
end_date    TEXT
claim_id    TEXT NOT NULL
PRIMARY KEY (patient_mrn, start_date)
```

**Written by** a raw `Store.ex()` at `reconcile.py:698` — the one write without a
named store method.
**Read by** `Store.episode(mrn)`, `queries.episode_window`, which is used as the
**default window for every per-patient question**.

### Why it matters more than one row suggests

`queries.episode_window` is what makes `ask "how many sessions did Rowan
attend?"` work with no dates in the question. And in a multi-patient fan-out it
is what gives each patient their own window — `queries.per_patient` fills
`start`/`end` from each patient's own episode when the question fixes none.

That was a bug I introduced and had to fix: the window was being defaulted from
the *first* patient's episode and applied to everyone, so patient B was measured
over patient A's dates. `tests/test_per_patient.py::test_each_patient_is_measured_over_their_own_episode`
asserts the two patients' week sets do not intersect.

### Why the primary key is `(mrn, start_date)`

A patient can have more than one episode. Keying on MRN alone would make a
second episode overwrite the first.

### Why it is extracted from *any* document, not just the plan

`rules._plan` runs the `EPISODE` regex on every document, because the intake
note also states it (*"The current outpatient episode is planned for January 5
through January 30"*). Both produce the same `(mrn, start_date)` key, so the
upsert collapses them — redundancy as resilience, since a corpus might be
missing the plan.

---

## `measures`

One row per distinct symptom-questionnaire **administration**. 3 rows.

```sql
measure_id      TEXT PRIMARY KEY  -- "mrn|instrument|form_ref|completed_at"
patient_mrn     TEXT NOT NULL
instrument      TEXT NOT NULL
form_ref        TEXT
completed_at    TEXT NOT NULL
total           REAL
items           TEXT              -- JSON, e.g. {"item9": 0}
claim_ids       TEXT NOT NULL     -- JSON list: all records of this one administration
disputed        INTEGER NOT NULL DEFAULT 0
total_conflicts TEXT              -- JSON when two records disagree on the score
```

On the supplied corpus:

```
PHQ-9  2026-01-05  18   form=null      1 source claim
PHQ-9  2026-01-16  14   form=HG-Q116   2 source claims   <- the re-import collapses here
PHQ-9  2026-01-30  10   items={item9:0}  1 source claim
```

**Written by** `Store.upsert_measure`, from `reconcile._measures`.
**Read by** `Store.measures(mrn)`, `queries.progress`.

### Why the primary key is instrument + form + completion date

This is `RULE-MEAS-01`, and it is the whole reason the table exists in this
shape. The January 16 PHQ-9 (score 14, form HG-Q116) appears **twice**: once in
the measurement review, and again on January 26 as a batch import receipt. They
are two records of **one** administration.

Keying on the *completion* date rather than the receipt date makes them collide,
so the upsert merges them into one row with two `claim_ids`. Keying on receipt
date, or using a surrogate id, would produce four PHQ-9s instead of three — and
DEV-05 asks exactly how many distinct assessments there are.

`reconcile._measures` also runs a first pass learning which `(instrument, date)`
pairs have a known form id, so **a re-import that omits the form id still
collapses** onto the original. Tested by
`tests/test_review_fixes.py::test_reimport_without_a_form_id_still_collapses`.

### Why `claim_ids` is a list rather than one source

Because the point is that *several documents* record one administration, and the
progress answer should cite all of them:

```
- PHQ-9 2026-01-16 = 14
    - BH-D013 [191:238] "Instrument: PHQ-9 | Patient portal form HG-Q116"
    - BH-D014 [572:615] "PHQ-9   | 14     | 2026-01-16     | HG-Q116"
```

### Why `disputed` and `total_conflicts`

`upsert_measure` originally used `COALESCE(total, ?)`, so if two records of one
administration stated **different scores**, the first silently won and the
disagreement vanished. Now both are kept, the row is flagged, `RULE-MEAS-02`
records it, and `queries.progress` lists "a single score for that
administration" among the things the record does *not* support. Tested by
`tests/test_multipatient.py::MeasureScoreConflict`.

### Why `items` is JSON

Only one item matters clinically here (PHQ-9 item 9, suicidal ideation) but
which items a document reports varies by instrument. A JSON map costs nothing
and nothing queries by item.

### Why measures are not events

A questionnaire is not a clinical contact. It has no duration, no attendance,
and contributes no therapy minutes. Putting it in `events` would mean every
duration query had to exclude it, and `counts_as_therapy` would be carrying two
unrelated meanings.

---

## `observations`

Narrative evidence about symptom course. 54 rows.

```sql
obs_id      TEXT PRIMARY KEY   -- "claim_id|domain"
patient_mrn TEXT NOT NULL
obs_date    TEXT NOT NULL
domain      TEXT NOT NULL      -- mood|sleep|anxiety|avoidance_work|activity|
                               -- social_support|safety|participation
polarity    TEXT NOT NULL      -- improvement|persistence|worsening|mixed
reporter    TEXT NOT NULL      -- patient|clinician|informant
claim_id    TEXT NOT NULL
quote       TEXT NOT NULL
quote_start INTEGER NOT NULL
quote_end   INTEGER NOT NULL
INDEX ix_obs_pat ON (patient_mrn, obs_date)
```

**Written by** `Store.insert_observation`, from `reconcile._observations`, which
fans **one observation claim into one row per domain**.
**Read by** `Store.observations(mrn, start, end)`, `queries.progress`.

### Why the primary key is `claim_id|domain`

One sentence can be about two things: *"They described mood as somewhat less
heavy on days with a planned activity but remained concerned about work
communication"* is evidence about mood **and** activity **and** work avoidance.
Storing it once with a list of domains would make "show me everything about
sleep" a JSON scan; storing one row per domain makes it an indexed lookup, and
the compound key keeps the fan-out idempotent across rebuilds.

### Why `reporter` is a column

Because the brief asks which observations support an account of progress, and
*who said it* changes the weight. The partner's account on January 16 is
genuine evidence but it is an **informant report**, not the patient's own
account, and the answer labels it as such. Three values, and the split on this corpus is 45
`clinician`, 7 `patient`, 2 `informant` — the two informant rows are the
partner's January 16 account, and they are the reason the column exists.

### Why observations are decoupled from events

The most important structural point about this table. Progress evidence lives in
documents that contribute **no therapy minutes**:

| document | therapy minutes | progress evidence |
|---|---|---|
| Jan 13 medication visit | 0 (excluded by the plan) | mood "somewhat less heavy" |
| Jan 16 partner collateral | 0 (patient absent) | walks, mornings still difficult |
| Jan 30 medication visit | 0 | mood "less persistently low" |
| Jan 30 questionnaire review | 0 (not a contact) | partial improvement |

If observations hung off `events`, all four would be unreachable — the first two
are excluded events and the last is not an event at all. Hanging them off
`claim_id` instead means any document can contribute narrative evidence
regardless of whether it contributed care.

### Why `polarity` is stored rather than computed at read time

It is an extraction decision (`rules._observations`, with the `IMPROVE` /
`NEGATED_IMPROVE` / `PERSIST` / `WORSE` cue lists) and it belongs with the
claim that produced it. The corpus yields 22 `persistence`, 12 `improvement`,
12 `mixed`, 8 `worsening` — a mixed picture, which is the honest reading of
these documents and the reason the progress answer does not claim recovery. Recomputing at query time would mean the renderer
re-deriving clinical meaning, which is exactly the boundary the design keeps.

### Why its own quote columns, when `claims` has them

Because the observation's span is **narrower** than its claim's. An observation
quotes one sentence; the claim it came from may span a header. These are the
spans the progress answer prints.

### Without this table

DEV-05 would be answerable only from questionnaire scores, losing every
clinician and informant statement about symptom course.

---

## `run_log`

```sql
run_id     INTEGER PRIMARY KEY AUTOINCREMENT
started_at TEXT NOT NULL
command    TEXT NOT NULL
detail     TEXT
```

**Written by** `Store.log_run`, from `ingest.ingest_dir` (with the per-run
report as JSON) and `cli.cmd_answer`.
**Read by** nothing in the codebase.

### Why keep a table nothing reads

It is provenance for the artifact. `out/clinical.db` is a deliverable that
travels on its own; `run_log` lets someone opening it later see when it was
built, with which command, and with what result. The brief asks for execution
logs, and this is the version that lives *inside* the abstraction rather than
beside it in `out/logs/execution.log`.

It is the one table I would drop if asked to justify every row — but it costs
two rows and answers "where did this file come from?"

---

## `llm_cache`

**On this branch only.** Removed from `main` with the rest of the model-backed
extractor, because nothing on `main` reads or writes it and a table no code
touches is worse than no table.

```sql
cache_key  TEXT PRIMARY KEY   -- sha256(normalized_text | prompt_ver | model)
model      TEXT NOT NULL
prompt_ver TEXT NOT NULL
response   TEXT NOT NULL      -- the raw completion, verbatim
in_tokens  INTEGER
out_tokens INTEGER
usd        REAL
created_at TEXT NOT NULL
```

**Written and read by** `Store.cache_get` / `Store.cache_put`, called from
`backbone/extract/llm.py`.

### Why the cache key is a hash of five things

The schema's inline comment says `sha256(norm_text|prompt_ver|model)`, but
`llm._cache_key` actually hashes **five** components — the comment is stale:

```python
nz.sha256("|".join([nz.normalize_text(text), PROMPT_VERSION, mode,
                    provider.provider(), provider.model()]))
```

Every one of them belongs in the key, because changing any one should produce a
different answer:

- **text** — a different document needs a different call
- **prompt_ver** — editing the extraction prompt invalidates every cached
  response, which is the only safe behaviour; otherwise a prompt change would
  appear to do nothing
- **mode** — `llm` (hybrid) and `llm_full` ask the model for different things
  from the same document, so they must not share a cache entry
- **provider** and **model** — switching either must not silently reuse the
  previous one's output, which would make the comparison meaningless

Using *normalized* text (the same normalization that gives `documents.doc_pk`)
means a re-transmitted document with a new fax header hits the cache rather than
paying for a second call.

Note that `prompt_ver` is stored on the row as `f"{PROMPT_VERSION}:{mode}"`
(`llm.py`, the `cache_put` call) while the key hashes them as separate
components. The row keeps them combined because the column exists for a human
reading the cache, not for lookup — lookup is by key only.

### Why it is a reproducibility guarantee, not just a cost saving

This is the point that matters. A model call is non-deterministic; a cached one
is not. With the cache populated, re-running the model extractor over the same
corpus produces **byte-identical** claims, so:

- the rules-vs-model comparison is re-checkable by a reader rather than being a
  number they have to take on trust
- a test can assert on model-derived output without flaking
- the measured token counts and cost in the benchmark are the real ones from the
  run, stored alongside the response that produced them

`in_tokens`, `out_tokens` and `usd` are on the row rather than aggregated
elsewhere so that cost is attributable per document, which is what makes the
benchmark's model section auditable instead of a single total.

### Why it lives in `out/llm-cache.db`, separate from `clinical.db`

It did not, originally, and that was a real cost. `compare.py` deleted and
rebuilt `clinical.db` between arms — which wiped the cache with it and re-paid
for all 31 documents on a rate-limited free tier. Moving the cache to its own
file made it survive the thing whose whole purpose is to rebuild the
abstraction.

This is the one table whose *file location* is part of its design: it is the
only table that must outlive `clinical.db`, because it is the only one that
cannot be recomputed for free.

---

## Recurring design decisions

### Content-addressed primary keys

`documents.doc_pk` = hash of normalized text. `events.event_id` =
`mrn|encounter`. `measures.measure_id` = `mrn|instrument|form|date`.
`observations.obs_id` = `claim_id|domain`. None of these are surrogate integers.

Every one buys the same property: **the key is derivable from the content, so
re-running produces the same key, so `INSERT OR REPLACE` is idempotent and
external references survive a rebuild.** `trace "HG-M042|HG-E110"` works in a
later session. Autoincrement ids appear only where nothing references the row
(`decisions`, `run_log`).

### `INSERT OR REPLACE` almost everywhere

Paired with deterministic keys, it makes every write path re-runnable without a
"does this exist?" check. Re-ingesting a corpus is safe; reconciling twice is
safe; `experiment` building twice under two policies is safe.

### JSON columns, and the rule for when

JSON is used for `claims.fields`, `claims.intervals`, `claims.breaks`,
`events.resolved`, `measures.items`, `measures.claim_ids`,
`plan_requirements.service_types`, `decisions.from_claims`.

The rule: **if the pipeline dispatches on it, it is a column; if it is carried
along for a human or for one consumer, it is JSON.** So `presence` is a column
(reconcile branches on it) while `negation_kind` is JSON (read in one place);
`min_minutes` is a column (summed by queries) while `presence_intervals` is JSON
(read only by the wall-clock check).

The cost is real: JSON is not indexable or type-checked, and `negation_kind` is
arguably on the wrong side of the line since `reconcile._presence` does dispatch
on it. In a production version I would promote it.

### No `FOREIGN KEY` enforcement in practice

`schema.sql` sets `PRAGMA foreign_keys=ON` and declares references, but the
delete order in `drop_doc_derivations` and `clear_patient_events` is written to
be correct independently — children before parents. The declarations document
intent; the code does not rely on cascade behaviour, which keeps the delete
order explicit and reviewable rather than implicit in the schema.

### Every patient-scoped index leads with `patient_mrn`

`ix_claims_enc`, `ix_claims_date`, `ix_events_pat`, `ix_obs_pat`, `ix_dec_pat`.
This is what makes a per-patient question cost ~1 ms whether the corpus holds 31
documents or 6,200 — measured on the scaling harness. It is also what makes the
patient the natural shard key if this ever needs to leave SQLite, because no
index and no reconciliation step crosses patients.

### Schema migration

`Store._migrate` (store.py) adds columns that were introduced after the first
release — `decisions.patient_mrn`, `measures.disputed`,
`measures.total_conflicts` — with `ALTER TABLE` when `PRAGMA table_info` shows
them missing. `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that
already exists, so without this an older `out/clinical.db` would break.

The ordering in `Store.__init__` matters and was itself a bug: **tables, then
migrations, then indexes.** An index on a column added later cannot be created
until the column exists, and `executescript` aborts the whole script on that
error — so an older database would fail to open at all. The schema is now
applied statement by statement with index creation deferred.

---

## What is deliberately NOT a table

### Weekly totals

The gap a reader notices: `events` stores sessions, and `queries.compliance`
computes weekly sums and verdicts **on every call** without storing them.

That is the measured first bottleneck. For one patient it is 12 event rows and
0.85 ms. At 16,000 patients it is ~192,000 rows and the same six steps run
16,000 times — an estimated ~34 s, repeated on every ask, for an answer that has
not changed since the last ask. The fix is a `patient_week` table
`(patient, week_start, min, max, days, status)`, written during reconcile, which
already visits exactly the affected patient; ~4 rows per patient, so ~64,000
rows where the verdict is already in the row.

It is not built because at 31 documents the query is already sub-millisecond,
so the improvement would be unmeasurable and the invalidation logic unverifiable.
The measurement that justifies it is in `out/scaling.json`, on this branch.

### Answers

Nothing caches a question's answer. Questions are cheap (~1 ms) and the
abstraction changes under them when documents arrive, so a cache would need
invalidation for no measured benefit.

### A `sessions` table separate from `events`

Considered and rejected. A "clinical session" would be `events` filtered to
`counts_as_therapy = 1` — a view, not a table. Materialising it would mean two
places to keep consistent and a second definition of "therapy" to drift from the
plan's own wording.

---

## Row counts on the supplied corpus

31 documents, one patient, 276 KiB on disk, from a pristine
`python -m backbone build documents` into a fresh file.

These differ from a database the test suite has touched. `tests/` writes
duplicate copies under `out/tmpdup/` to exercise dedupe, which adds
`document_aliases` rows and `RULE-DEDUPE-01` decisions keyed by those paths, and
every `build` or `ask` appends a `run_log` row. A test-touched database shows 2
aliases, 158 decisions and 2 run_log rows — and the committed `out/clinical.db`
is one of those, which is why `out/abstraction.json` mentions `out	mpdup`
paths that do not exist for a reviewer. The counts below are the clean ones.

| table | rows | what one row is |
|---|---:|---|
| `documents` | 31 | a distinct document |
| `document_aliases` | 0 | a duplicate file path (none in `documents/`) |
| `patients` | 1 | a patient |
| `claims` | 99 | one assertion by one document |
| `corrections` | 1 | the Jan 19 departure correction |
| `events` | 20 | a reconciled contact (12 counted as therapy) |
| `event_claims` | 48 | a claim supporting an event, with its role |
| `decisions` | 156 | a conclusion, with its rule |
| `plan_requirements` | 2 | a dated goal (days, minutes) |
| `episodes` | 1 | the episode window |
| `measures` | 3 | a distinct PHQ-9 administration |
| `observations` | 54 | a dated narrative observation, per domain |
| `run_log` | 1 | a command that built this file |

The ratio worth noting: **99 claims produce 20 events and 156 decisions.** There
are more explanations than conclusions, which is the intended shape — the system
spends more rows saying *why* than saying *what*.
