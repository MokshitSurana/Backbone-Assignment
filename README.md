# Backbone — clinical abstraction prototype

Turns a folder of behavioral-health documents into a small, auditable clinical
ledger, then answers questions by computing over that ledger in Python. No model
is called at any point: every number is produced by code, and every number
traces back to a character span in a source document.

Python 3.11+, standard library only. Nothing to install.

```bash
python -m backbone build documents          # ingest + reconcile (first run)
python -m backbone answer questions.json    # the five development questions
python -m backbone ask "How many group sessions did Rowan attend in week 2?"
python -m backbone trace "HG-M042|HG-E110"  # follow one event to its sources
python -m backbone verify                   # integrity checks + 107 tests
```

### Scope note

The brief suggests 2–5 hours. `main` is that submission: the pipeline, the
tests, the five answers, one tested design decision, and the benchmark — about
4,650 lines of code and 1,400 of tests.

Work done **after** the time box, in later review passes, is on the
**`post-timebox`** branch and deliberately not here: an optional model-backed
extractor with a Groq/Anthropic provider layer, a rules-vs-model comparison
harness, a synthetic multi-patient scaling harness, and a long design document.
Two findings from that branch are cited in §4 and §6 below, because they change
what I would claim about this design. Roughly 5 further hours.

---

## 1. Approach

```
documents ──► claims ──► events ──► query functions ──► answers
           (what a     (one real    (deterministic    (+ evidence
            document    contact,     arithmetic)       spans)
            asserts)    many claims)
```

**Claims, not conclusions.** Each document is read once into claims — assertions
about an encounter, a correction, a measure, a plan requirement, an observation.
A claim records what the document *says*, its authority class, and the exact
character offsets it says it at. Nothing is overwritten, so a later document
never destroys an earlier one.

**Events, not files.** Claims are grouped into the real-world contacts they
describe. An encounter id is the event key; an appointment id resolves to its
encounter; a telehealth call id never keys an event. With no id, claims match on
patient + date + service type confirmed by time overlap, and an ambiguous match
is reported rather than guessed.

**A stated authority policy, per field,** applied in `reconcile.py` with a named
rule for every outcome:

| rule | policy |
|---|---|
| RULE-PRES-02 | a schedule, a draft note, a posted charge, an authorization letter and a resent copy can never establish that care was delivered |
| RULE-PRES-03 | a final disposition *can* establish absence or cancellation, and a no-show is distinguished from a contact the patient was absent from |
| RULE-PRES-01 | an entry naming the record and field it replaces supersedes that field; a later document that does not reference it does not |
| RULE-DUR-01/02 | for group services the attendance desk owns arrival and departure; otherwise the treating clinician's own patient-contact interval wins |
| RULE-DUR-03 | equal authority, different values, no correction → keep both as bounds |
| RULE-DUR-04 | a break reduces a session by its *overlap* with the patient's presence interval, never by a flat amount |
| RULE-INC-01 | which services count toward the goal is read from the treatment plan's own sentence, not hardcoded |
| RULE-MEAS-01/02 | a measure is identified by instrument + form id + completion date, so a re-import collapses onto the original; two records of one administration with different scores are a flagged conflict |

**Uncertainty as bounds, not labels.** Every event carries
`[min_minutes, max_minutes]`. A requirement is `MET` when the minimum clears the
threshold, `NOT_MET` when even the maximum misses, and `CANNOT_DETERMINE` when
the threshold falls inside the band. An unresolved *attendance* bounds the
minutes at `[0, what the records would support]`, so a week turning on one
carries uncertainty rather than silently reading as zero.

**Arithmetic in Python.** `queries.py` is the only module that produces a total.
Each function returns `{value, calculation, evidence, caveats}`, where
`calculation` is the sum written out (`50 + 45 + 45 = 140`) so it can be checked
by hand.

### Answering a new question

`router.py` maps a question to one of ten parameterized functions and fills in
the patient, window and dates — no free-form SQL, so every answer path is logic
a reviewer can read once. A per-patient question that does not pin a single
patient is answered **for every patient in scope**, each over their own
documented episode. It does not guess silently: an unknown patient name is
refused rather than answered about somebody else, and a question matching no
question type is answered with a stated warning.

### Adding documents later

```bash
python -m backbone add documents/new_note.txt   # ~10 ms: extract + reconcile that patient only
```

Identity is the SHA-256 of the *normalized* text (receipt stamps, export
headers, whitespace and smart quotes removed), so a resent or re-stamped copy
lands on the same row and contributes the same single set of claims. A file
edited in place replaces its previous version's claims rather than sitting
alongside them. Directory ingest reads any text-bearing extension, not only
`.txt`, and reports what it skipped.

---

## 2. Answers to the five development questions

Full output with sources: [`out/answers.md`](out/answers.md) (machine-readable in
`out/answers.json`). Reproduce with `python -m backbone answer questions.json`.

**DEV-01** — 12 therapy sessions on 11 distinct days: 5 individual, 5 group,
2 family. Eight encounters are excluded with a stated reason. The January 27
no-show is the interesting one: an unsigned template note says "Patient attended
the full session" and a charge for one group session was posted; both are
rejected against the signed register.

**DEV-02** — 585–595 minutes (9.75–9.92 hours).

| week (Mon–Sun) | therapy days | minutes | calculation |
|---|---:|---:|---|
| Jan 5–11 | 3 | 140 | 50 + 45 + 45 |
| Jan 12–18 | 2 | 120 | 75 + 45 |
| Jan 19–25 | 3 | 180 | 60 + 30 + 45 + 45 |
| Jan 26–30 | 3 | 145–155 | (40–50) + 75 + 30 |

Not settled: the January 26 encounter has two signed notes from two
participating clinicians, 50 minutes (09:00–09:50) and 40 minutes (09:10–09:50),
neither referring to the other. Kept as bounds.

**DEV-03** — Goal read from the plan: ≥3 therapy days **and** ≥150
patient-present minutes per Monday–Sunday week, counting individual, group and
family therapy. Week 1 `NOT_MET` (140 < 150), week 2 `NOT_MET` (2 days, 120
min), week 3 `MET`, week 4 `CANNOT_DETERMINE` — 150 falls inside 145–155. The
system also reports what would settle it: an addendum from either author naming
the other's value, or a check-in timestamp for the start of the contact.

**DEV-04** — January 19: two therapy contacts, 90 patient minutes (group 60 +
individual 30). The group roster was signed at 11:30 departure; a January 20
correction naming that roster and field sets it to 11:15; a copy of the
*original* roster received January 26 still reads 11:30 and is marked
superseded. 60 = (10:00→11:15) minus the 10:45–11:00 break. January 21: one
contact, 45 minutes — 13:00–13:20 plus 13:30–13:55, dropped connection excluded.
The two platform call ids are evidence on one encounter, not two encounters.

**DEV-05** — Three distinct PHQ-9 administrations: 18 (Jan 5), 14 (Jan 16, form
HG-Q116), 10 (Jan 30, item 9 = 0). The January 26 batch import is a second
record of the January 16 administration and collapses onto it. The additional
January 19 contact is explained by the record itself: *"This visit was added
because Rowan became anxious during group and needed individual grounding and
review of coping strategies."* Supported: the score fell 18 → 10, plus
per-domain observation counts. Not supported: any severity band (no document
states one), or attribution of improvement to a single service. Observations
from the medication visits and the partner collateral contact are used as
progress evidence and labelled by reporter, though those contacts contribute no
therapy minutes.

---

## 3. The design decision I tested

**How should conflicting records be resolved?**
[`docs/experiment-conflict-policy.md`](docs/experiment-conflict-policy.md),
`python -m backbone experiment`. Both policies are real code paths selected by
`--policy`, run over the same corpus with the same extractor. The harness does
not know which encounter they will disagree about: it diffs the two event tables
to find where they diverge.

| | explicit-correction (shipped) | latest-document |
|---|---|---|
| Jan 19 group minutes | **60** | 75 |
| Jan 19 therapy minutes | **90** | 105 |
| week of Jan 26 | **145–155, CANNOT_DETERMINE** | 145, NOT_MET |
| wall-clock check | passes | **fails** |

**What I learned.** Latest-document is not just a different judgement call — it
makes the record internally impossible, and the system detects that *without
being told the right answer*. With the resent roster winning, the group contact
runs 10:00–11:30 while the same-day individual session runs 11:15–11:45: the
patient is in two therapy rooms for 15 minutes. That check (`queries.integrity`,
comparing resolved presence intervals pairwise within a day) now runs on every
build, and is why recency is only ever a tiebreak in the shipped policy.

The quieter effect is worse: recency also *resolves* the January 26 conflict the
documents do not resolve, turning an honest `CANNOT_DETERMINE` into a confident
`NOT_MET`. That one leaves no trace in the output at all, which is the argument
for carrying bounds rather than a chosen value.

An aside worth recording: my first version of this experiment was **invalid** —
the baseline still applied the retransmission rule and the correction override,
so both arms came out identical and it looked like a null result. A comparison
that cannot distinguish its own arms is a bug, not a finding.

---

## 4. An observed limitation, and how I investigated it

**The deterministic extractor is tuned to this corpus's vocabulary, and nothing
in the output says when it has read less than it should.** Interval
classification depends on cue lists in `extract/rules.py`. A document writing
*"the group paused for refreshments from 10:45"* instead of "break" yields a
claim with no patient-present interval. The event then falls back to stated
minutes or to `occurred=unknown` — honest — but a wrong-but-plausible number is
possible where a different cue shifts a range from `break` to `patient_present`.

**How I investigated it.** I built the model-backed extractor on the
`post-timebox` branch specifically to measure this: run both front-ends over the
same corpus through identical reconciliation and arithmetic, and diff the
result. Measured — the deterministic extractor keeps **35** observation spans;
the model found **120**, including 94% of those 35 plus 85 more. So the cue
lists miss roughly two thirds of the usable narrative evidence. That is a
number, not a worry.

**What I would do next.** Add a **coverage assertion** to ingest: flag any
document containing a clock-time range that no claim accounts for, and any
`N minutes` phrase no claim uses. That is a cheap, answer-independent "I did not
read this" signal of the same kind as the wall-clock check, and I would run it
before trusting the extractor on an unseen corpus — only then widen the cue
lists, driven by what it surfaces.

---

## 5. Measured performance

`python -m backbone bench` → [`out/benchmark.json`](out/benchmark.json).
31 documents, 48.9 KiB of source text, on Windows 11 / Python 3.13.5.

| | measured |
|---|---|
| cold build (empty database) | **0.158 s** — 4.9 ms/document ingest, 0.005 s reconcile |
| claims → events | 99 claims → 20 events (12 therapy, 1 disputed) |
| warm re-ingest of the same corpus | **0.011 s**, 0 of 31 re-extracted |
| one new document + reconcile its patient | **0.010 s** |
| per-patient question | **0.65–1.07 ms** (counts, minutes, compliance, progress) |
| collection-wide question | **0.85 ms** |
| trace one event to its sources | **0.06 ms** |
| natural-language question, end to end | **1.25 ms** median |
| abstraction on disk | 276 KiB — 31 documents, 99 claims, 20 events, 48 event-claim links, 156 decisions, 54 observations, 3 measures, 2 plan requirements, 1 correction |
| model calls / tokens / cost | **0 / 0 / $0.00** |

---

## 6. The first bottleneck at a million documents

**Collection-wide queries, not extraction** — measured, not assumed. On the
`post-timebox` branch a synthetic multi-patient harness built the abstraction at
1, 10, 50 and 200 patients (31 to 6,200 documents):

- ingest is **flat per document** (15–17 ms)
- reconcile is **flat per patient** (~9–15 ms), because it partitions by patient
- a **per-patient** question stays at **~1 ms** while the corpus grows 200×
- a **collection-wide** question grows linearly: **426 ms at 200 patients**

`queries.consecutive_below` loops over patients and recomputes every weekly total
from events on each call. At 426 ms per 200 patients that is ~34 s at 16,000
patients — for a question a reviewer asks repeatedly. The fix is a materialized
`patient_week` table written during reconcile, which already visits exactly the
affected patient. I did not build it because at 31 documents it would be
unmeasurable, and I would rather ship the measurement that says where it is
needed.

Behind that, in order: SQLite's single writer during parallel ingest (shard by
patient, or move to Postgres); then, **if production needs a model in the
extraction path, its throughput and cost overtake everything else** — measured
on the branch at ~1,550 input tokens and ~1.5 s per document, so 500K documents
is ~776M input tokens and, at a free tier's 8,000 tokens/minute, about 67 days
serially. That is a rate-limit problem rather than a code problem, and the
deterministic default is what keeps the figure here at an estimated 2.1 hours.

Estimates at 500K documents, labelled as estimates: ~2.1 h single-process ingest
(~8 min across 16 shards), ~4 min reconcile, ~2.4 GB storage, ~1 ms per-patient
queries unchanged, ~34 s collection-wide queries as written today.

---

## 7. Models, assistance, runtime and cost

**No model is used on `main`.** `python -m backbone build documents` makes zero
model calls, costs $0.00, and is byte-reproducible across runs. Every number in
this README and in `out/answers.md` was produced that way.

**On the `post-timebox` branch**, an optional extractor can use
`openai/gpt-oss-120b` via Groq (or Anthropic), temperature 0, with every
returned quote required to appear verbatim in the source or the claim is
dropped. Measured there: 48,114 / 13,851 tokens and $0.018 for one pass over
these 31 documents; 12 quotes dropped as not verbatim; and **identical answers to
all five questions**, because the model never touches structure or arithmetic.
One finding worth carrying back: the same document returned a materially
different answer across runs at temperature 0, so on that path the response
cache is what makes an abstraction reproducible at all.

**Runtime to produce everything on `main`:** a few seconds of compute — 0.16 s
build, ~4 s for 107 tests, 0.3 s benchmark, 0.2 s experiment.

**Coding assistance:** written with Claude Code (Opus 5). About 4 hours for
`main`, plus roughly 5 further hours of review passes whose output is on the
`post-timebox` branch. The hand-computed ledger in `tests/test_gold.py` was
worked out by reading all 31 documents before any code was written; those
expected values live only in the tests and are never imported by the pipeline —
it has to derive them from the documents.

---

## 8. Layout

```
backbone/
  schema.sql        documents, claims, events, event_claims, decisions, ...
  normalize.py      normalization for identity, date parsing and validation
  taxonomy.py       service synonyms, document classes, authority tables
  intervals.py      interval algebra; every duration comes from here
  extract/rules.py  deterministic claim extraction
  reconcile.py      matching + per-field resolution + decision log
  queries.py        the ten query functions; the only place numbers are made
  router.py         question -> function + parameters
  render.py         result -> reviewable Markdown (formats only, never computes)
  bench.py          measured performance
  experiment.py     the conflict-policy A/B
  cli.py            build / add / answer / ask / trace / export / bench /
                    experiment / verify
tests/              107 tests: hand-verified ledger, idempotence, restart,
                    router safety, week coverage, interval algebra, source hygiene
tests/gold_ledger.json   the hand-computed ledger as data, cross-checked
                         against the assertions in test_gold.py
scripts/run_all.sh  full reproduction, captured to out/logs/execution.log
docs/               the conflict-policy experiment write-up
out/                answers.md/.json, abstraction.json, benchmark.json,
                    logs/execution.log, clinical.db
```

---

## 9. Incomplete work and tradeoffs

- **No materialized `patient_week` table.** The measured first bottleneck (§6).
  Deliberately not built at this corpus size; the fix is specified and the
  measurement that justifies it is in the repo.
- **Observation extraction is conservative and lossy, quantified.** 35 spans
  against the model path's 120 (§4). The cue lists are the weakest part of the
  extractor and the clearest case for a model.
- **Week bucketing materially affects the answers.** Monday–Sunday is read from
  the plan's own wording; Sunday–Saturday is available via
  `compliance(..., scheme="week_sun_sat")`. The January 26–30 week is partial at
  the episode end and is flagged rather than prorated, because the documents do
  not say to prorate. Prorating would change the DEV-03 verdict.
- **One patient in the supplied data.** Nothing is keyed to that: the patient is
  discovered from document headers, every query takes a patient parameter, a
  per-patient question with no patient named fans out across all of them, and
  the tests build a synthetic two-patient corpus to prove one patient's answer
  never cites another's documents.
- **No plan amendment in the supplied data.** Requirements are stored as dated
  rows and a later plan closes the earlier one; `plan_change_comparison` reports
  per-week rates rather than raw totals and flags the week containing the
  change. Tested against a synthetic amendment, not a real one.
- **Ambiguous matches are reported, not resolved.** Six claims are unmatched on
  this corpus, all administrative — import receipts and questionnaire reviews
  that state of themselves that they record no visit.
