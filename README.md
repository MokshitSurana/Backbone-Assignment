# Backbone — clinical abstraction prototype

Turns a pile of behavioral-health documents into a small, auditable clinical
ledger, then answers questions by computing over that ledger in Python. The
model is never in the numerical loop, and every figure traces back to a
character span in a source document.

Python 3.11+, standard library only. SQLite is the store. No network access is
required for anything in this README.

```bash
python -m backbone build documents          # first run: ingest + reconcile
python -m backbone answer questions.json    # answer the five dev questions
python -m backbone ask "How many group sessions did Rowan attend in week 2?"
python -m backbone verify                   # integrity checks + 108 tests

bash scripts/run_all.sh                     # everything, logged to out/logs/execution.log
```

Optional, to enable and test the model path (copy `.env.example` to `.env` and
add one key):

```bash
python -m backbone models                   # what this key can actually run
python -m backbone compare                  # rules vs model, scored against the ledger
```

Nothing to install: the default path is standard library only, run from the
repository root. `requirements.txt` exists only for the optional model-backed
extractor.

**[DESIGN.md](DESIGN.md)** is the full design document: what every file does,
why claims-then-events rather than the alternatives, why SQLite, exactly where a
model is and is not used, the complete rule catalogue, the audit chain, both
experiments, and a build chronology including every bug found and what it
changed.

Everything below that is a number was measured on this machine (Windows 11,
Python 3.13.5) by `python -m backbone bench` and `python scripts/scale_test.py`.
Figures that are extrapolated say so.

---

## 1. Approach

```
documents ──► claims ──► events ──► query functions ──► answers
             (what a    (one real   (deterministic     (+ evidence
              document   contact,    arithmetic)        spans)
              asserts)   many
                         claims)
```

**Claims, not conclusions.** Each document is read once into claims: assertions
about an encounter, a correction, a measure, a plan requirement, an observation.
A claim records what the document *says*, its authority class, and the exact
character offsets of the span it says it in. Nothing is overwritten at this
stage, so a later document never destroys an earlier one.

**Events, not files.** Claims are grouped into the real-world contacts they
describe. The encounter id is the key when a document states one; an appointment
id resolves to its encounter; a call/session id never keys an event. Claims with
no id fall back to patient + date + service type, confirmed by time overlap, and
an ambiguous match is reported rather than guessed.

**A stated authority policy, per field.** `taxonomy.py` ranks document classes,
and `reconcile.py` applies them with a named rule for every outcome:

| rule | policy |
|---|---|
| RULE-PRES-02 | a schedule, a draft note, a posted charge, an authorization letter and a retransmission can never establish that care was delivered |
| RULE-PRES-03 | a final disposition *can* establish absence or cancellation |
| RULE-PRES-01 | an entry that names the record and field it replaces supersedes that field; a later document that does not reference it does not |
| RULE-DUR-01/02 | for group services the attendance desk owns arrival and departure; for everything else the treating clinician's own patient-contact interval wins |
| RULE-DUR-03 | equal authority, different values, no correction → keep both as bounds |
| RULE-DUR-04 | a break reduces a session by its *overlap* with the patient's presence interval, never by a flat amount |
| RULE-DUR-05 | non-delivery classes contribute no duration at all |
| RULE-INC-01 | which services count toward the goal is read out of the treatment plan's own sentence, not hardcoded |
| conjunction | the plan requires *every* metric, so one definite failure settles the week even when another metric is uncertain: `NOT_MET` outranks `CANNOT_DETERMINE` |
| week coverage | every week in the review window is evaluated, including weeks with no attendance at all -- a week with no therapy is a week below the requirement, not a missing row |
| RULE-PRES-04 (bounds) | an unresolved attendance bounds the minutes at `[0, what the records would support]`, so a week turning on a disputed attendance comes out `CANNOT_DETERMINE` rather than `NOT_MET` |
| RULE-DUR-04 (stated) | a stated duration alongside a documented break is kept as a band, because the document does not say whether the figure already excludes the break |
| RULE-MEAS-01 | a measure is identified by instrument + form id + completion date, so a re-import collapses onto the original administration; a record omitting the form id still collapses |
| RULE-MEAS-02 | two records of one administration stating different scores are a flagged conflict, not a first-one-wins overwrite |
| RULE-PRES-05 | presence is decided by where the cue sits -- a disposition field outranks a sentence about the patient, which outranks a passing mention; an equal-authority tie asserts nothing |
| RULE-DEDUPE-03 | a file edited in place replaces its previous version's claims rather than sitting alongside them |

**Uncertainty as bounds, not as a label.** Every event carries
`[min_minutes, max_minutes]`. A requirement is `MET` when the minimum clears the
threshold, `NOT_MET` when even the maximum misses, and `CANNOT_DETERMINE` when
the threshold falls inside the band. That is what makes "which patients had two
consecutive weeks below" answerable twice — once under the minimum reading of
the record and once under the maximum — so the patients whose inclusion *depends
on unresolved documentation* fall out of the comparison instead of being
asserted.

**Arithmetic in Python.** `queries.py` is the only place a total is produced.
Each function returns `{value, calculation, evidence, caveats}`; `calculation`
is the sum written out (`50 + 45 + 45 = 140`) and `evidence` is claim-level with
document id, offsets and quote.

**Where the model is used, and where it is not.** The default extractor
(`extract/rules.py`) is fully deterministic: it finds every clock time in a
document and classifies it *by the words around it* (break / scheduled /
therapist-only / patient-present), which is what lets one code path read a
facilitator narrative, a desk roster and a billing extract. The optional hybrid
extractor (`extract/llm.py`, `--extractor llm`) keeps all of that and hands the
model only the job it is actually better at — turning clinical prose into
structured observations and reading requirement language. Every string the model
returns must be found verbatim in the source or the claim is dropped. The model
never computes a duration and never resolves a conflict.

### Answering a new question

`router.py` maps a question to one of ten parameterized functions and fills in
the patient, window and dates. **A per-patient question that does not pin a
single patient is answered for every patient in scope** -- "how many sessions
did each patient attend" returns one result per MRN, each with its own
calculation and evidence, and each measured over that patient's own documented
episode unless the question fixes a window. No free-form SQL, so every answer path is one a
reviewer can read. It does not guess silently: a question naming a patient the
collection does not contain is **refused** rather than answered about somebody
else, a question matching no question type is answered with a stated warning,
and a per-patient question that names nobody is refused when the collection
holds more than one patient. Scope for collection-wide questions follows whether
the question actually named a patient -- no patient name appears anywhere in the
package, which `tests/test_units.py` enforces. With `--router llm` a model picks the function and
parameters instead; the numbers still come from the function. The abstraction is
read from disk, so a new question costs ~1 ms and no reprocessing:

```bash
python -m backbone ask "did Rowan meet the minute goal in the week of Jan 12?"
python -m backbone ask "which patients had three consecutive weeks below?"
python -m backbone trace "HG-M042|HG-E110"      # one event, all the way down
```

### Adding documents later

```bash
python -m backbone add documents/new_note.txt   # ~10 ms: extract + reconcile that patient only
```

Directory ingest reads any text-bearing extension (`.txt`, `.md`, `.csv`, …),
not only `.txt`, and reports what it skipped rather than dropping it silently.

Identity is the SHA-256 of the *normalized* text (receipt stamps, export
headers, whitespace and smart quotes removed), so a resent or re-stamped copy
lands on the same row and contributes the same single set of claims. Exact
hashing alone would not catch that, which is why the real safeguard is
event-level reconciliation: two copies produce two claims that merge into one
event. `tests/test_gold.py::Idempotence` drops in both an exact copy and a
copy with new fax furniture and asserts that no clinical number moves.

---

## 2. Answers to the five development questions

Full output with sources: [`out/answers.md`](out/answers.md) (machine-readable in
`out/answers.json`). Reproduce with `python -m backbone answer questions.json`.

**DEV-01** — 12 therapy sessions on 11 distinct days: 5 individual, 5 group,
2 family. Eight encounters are excluded with a stated reason (two medication
visits, one collateral contact where the patient was absent, one care
coordination call with no patient, one clinic cancellation, one patient
cancellation, two no-shows). The January 27 no-show is the interesting one: an
unsigned template note says "Patient attended the full session" and a charge for
one group session was posted, and both are rejected against the signed register.

**DEV-02** — 585–595 minutes (9.75–9.92 hours).

| week (Mon–Sun) | therapy days | minutes | calculation |
|---|---:|---:|---|
| Jan 5–11 | 3 | 140 | 50 + 45 + 45 |
| Jan 12–18 | 2 | 120 | 75 + 45 |
| Jan 19–25 | 3 | 180 | 60 + 30 + 45 + 45 |
| Jan 26–30 | 3 | 145–155 | (40–50) + 75 + 30 |

Not settled: the January 26 individual encounter has two signed notes from two
participating clinicians, 50 minutes (09:00–09:50) and 40 minutes (09:10–09:50),
neither referring to the other. Kept as bounds.

**DEV-03** — Goal read from the plan: ≥3 therapy days **and** ≥150 patient-present
minutes per Monday–Sunday week, counting individual, group and family therapy.
Week 1 NOT_MET (140 < 150), week 2 NOT_MET (2 days, 120 min), week 3 MET,
week 4 CANNOT_DETERMINE — 150 falls inside 145–155. The system also reports what
would settle it: an addendum from either author naming the other's value, or a
check-in timestamp for the start of the contact.

**DEV-04** — January 19: two therapy contacts, 90 patient minutes (group 60 +
individual 30). The group roster was signed at 11:30 departure; a January 20
correction naming that roster and field sets it to 11:15; a copy of the
*original* roster received January 26 still reads 11:30 and is marked superseded.
60 = (10:00→11:15) minus the 10:45–11:00 break. January 21: one contact, 45
minutes — 13:00–13:20 plus 13:30–13:55, with the dropped connection excluded. The
two platform call ids are evidence on one encounter, not two encounters.

**DEV-05** — Three distinct PHQ-9 administrations: 18 (Jan 5), 14 (Jan 16, form
HG-Q116), 10 (Jan 30, item 9 = 0). The January 26 batch import is a second
record of the January 16 administration and collapses onto it. The additional
January 19 individual contact is explained by the record itself: *"This visit was
added because Rowan became anxious during group and needed individual grounding
and review of coping strategies."* Supported: the score fell 18 → 10; partial
improvement in mood, activity and support; persistence of work-communication
avoidance and disrupted sleep. Not supported: any severity band for these scores
(no document states one), and attribution of improvement to any single service.
Observations from the medication visits and the partner collateral contact are
used as progress evidence and labelled by reporter (`patient` / `clinician` /
`informant`) even though those contacts contribute no therapy minutes.

---

## 3. The design decision I tested

**How should conflicting records be resolved?** Full write-up and reproduction:
[`docs/experiment-conflict-policy.md`](docs/experiment-conflict-policy.md)
(`python -m backbone experiment`). Both policies are real code paths selected by
`--policy`, run over the same corpus with the same extractor.

| | explicit-correction (shipped) | latest-document |
|---|---|---|
| Jan 19 group minutes | **60** | 75 |
| Jan 19 therapy minutes | **90** | 105 |
| episode total | **585–595** | 600 |
| week of Jan 26 | **145–155, CANNOT_DETERMINE** | 145, NOT_MET |
| wall-clock check | passes | **fails** |

**What I learned.** Latest-document is not just a different judgement call — it
makes the record internally impossible, and the system can detect that *without
being told the right answer*. With the resent roster winning, the group contact
runs 10:00–11:30 while the same-day individual session runs 11:15–11:45: the
patient is in two therapy rooms for 15 minutes. That check
(`queries.integrity`, which compares resolved presence intervals pairwise within
a day) now runs on every build. It is the cheapest answer-independent signal I
found that a reconciliation policy is wrong, and it is why recency is only ever a
tiebreak in the shipped policy, never authority.

The second effect is quieter and worse: recency also *resolves* the January 26
conflict that the documents do not resolve, turning an honest
`CANNOT_DETERMINE` into a confident `NOT_MET`. That one leaves no trace in the
output at all, which is the argument for carrying bounds rather than a chosen
value.

---

## 4. Measured performance

`python -m backbone bench` → [`out/benchmark.json`](out/benchmark.json).
31 documents, 48.9 KiB of source text.

| | measured |
|---|---|
| cold build (empty database) | **0.149 s** total — 0.144 s ingest (4.6 ms/document), 0.005 s reconcile |
| claims → events | 99 claims → 20 events (12 therapy, 1 disputed) |
| warm re-ingest of the same corpus | **0.009 s**, 0 of 31 documents re-extracted |
| one new document + reconcile its patient | **0.010 s** |
| per-patient question (counts 0.80, minutes 0.61, compliance 0.77, progress 1.10) | **0.6–1.1 ms** |
| collection-wide question (consecutive weeks below, all patients) | **0.78 ms** |
| trace one event to its sources | **0.09 ms** |
| natural-language question, end to end | **0.37–1.77 ms** (median 1.10) |
| whole-corpus integrity check | **1.51 ms** |
| abstraction on disk | 284 KiB, holding 31 documents / 99 claims / 20 events / 48 event-claim links / 156 decisions / 54 observations / 3 measures / 2 plan requirements / 1 correction. Mostly fixed SQLite page and index overhead at this size; the marginal figure below is the one that scales |
| JSON export of the whole abstraction | 125 KiB |
| model calls, tokens, cost | **0 / 0 / $0.00** — the default path makes none; the measured model path is in §6 |

### Measured scaling curve

`python scripts/scale_test.py --patients 1 10 50 200` →
[`out/scaling.json`](out/scaling.json). The script clones the corpus into
synthetic patients (new MRN, name and identifiers; dates shifted by whole weeks
so Monday–Sunday bucketing still lines up).

| patients | documents | claims | ingest | reconcile | per-patient query | collection-wide query | integrity (all) | bytes/document |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 31 | 100 | 0.55 s | 0.009 s | 1.92 ms | 1.7 ms | 2.0 ms | 9,381 |
| 10 | 310 | 1,000 | 5.0 s | 0.063 s | 0.96 ms | 10.9 ms | 20 ms | 5,298 |
| 50 | 1,550 | 5,000 | 24.3 s | 0.395 s | 1.05 ms | 67 ms | 157 ms | 4,878 |
| 200 | 6,200 | 20,000 | 93.6 s | 2.98 s | **0.92 ms** | **426 ms** | 1,164 ms | 4,857 |

Three things this shows rather than assumes: ingest is flat per document
(15–17 ms), reconcile is flat-to-mildly-superlinear per patient (9 → 15 ms), and
a per-patient question stays at ~1 ms while the corpus grows 200× — but a
collection-wide question grows linearly or worse with the patient count, and so
does the whole-corpus integrity check. (Absolute timings move 10–50% between runs
on this machine; the shape is the point, and `out/scaling.json` holds the run
these figures came from.)

(The 200-patient run reports 4,005 events rather than 4,000; that is an
identifier collision in the synthetic *generator*, not in the pipeline. The
pipeline's own numbers on the supplied corpus are asserted by the test suite.)

### Estimated at 500K documents — labelled as estimates

Scaling from the 200-patient row (31 documents per episode, so ~16,000 patients):

| | estimate | basis |
|---|---|---|
| ingest, single process | **~2.1 hours** | 6,200 docs in 93.6 s (15.1 ms/doc), linear |
| ingest, 16 processes partitioned by patient | ~8 minutes | embarrassingly parallel; SQLite single-writer means one file per shard |
| reconcile all patients | ~4 minutes | 2.98 s per 200 patients |
| storage | **~2.4 GB** | 4,857 B/document, converged |
| per-patient question | **~1 ms, unchanged** | flat across 200× growth |
| collection-wide question, as written today | **~34 s** | 426 ms per 200 patients, linear |

### The first bottleneck at a million documents

With the deterministic extractor shipped as default, it is **not** extraction
cost — it is **collection-wide queries**, and the measured curve above is why.
`queries.consecutive_below` loops over patients and recomputes every weekly
total from events on each call (`queries.py`, `consecutive_below` →
`compliance` → `minutes`). At 426 ms per 200 patients that is ~34 s at 16,000
patients and ~70 s at a million documents' worth of patients, for a question a
reviewer will ask repeatedly. The whole-corpus integrity check (1.16 s per 200
patients) sits on the same code path and has the same fix.

The fix is a materialized `patient_week` table — one row per patient-week
holding `(min_minutes, max_minutes, min_days, max_days, status)` — written
during reconcile, which already visits exactly the affected patient. Collection
queries then scan that table instead of recomputing, turning a linear-in-events
scan into an indexed scan over ~4 rows per patient-episode. I did not build it
because at 31 documents it would be unmeasurable, and I would rather ship the
measurement that says where it is needed.

What I expect behind that, in order: (2) SQLite's single writer during parallel
ingest, which is why I would shard by patient and merge, or move to Postgres;
(3) **if production needs the model path, its throughput and cost overtake
everything else and become the real first bottleneck** — measured at ~1,550
input tokens per document and ~1.5 s of model latency, 500K documents is ~776M
input tokens and, at the free tier's 8,000 tokens/minute, would take about
**67 days** serially. That is a rate-limit problem rather than a code problem:
it needs a paid tier, concurrency, and the response cache doing real work. The
deterministic default is what keeps the shipped figure at 2.1 hours; (4) only
then the per-document extraction CPU, which is flat and trivially parallel.

---

## 5. An observed limitation, and how I would investigate it

**The deterministic extractor is tuned to the vocabulary of this corpus, and
nothing in the output says when it has silently read less than it should.**

Concretely: interval classification depends on cue lists in
`extract/rules.py` (`BREAK_CUES`, `PRESENT_CUES`, `OTHER_CUES`). A document that
writes "the group paused for refreshments from 10:45" instead of "break", or
"client was in the room from 10:15", produces a claim with no patient-present
interval. The event then falls back to a stated-minutes claim, or to
`occurred=unknown` — which is at least honest — but a *wrong-but-plausible*
number is possible where a different cue shifts a time range from `break` to
`patient_present`. I have evidence the extractor generalizes somewhat
(`tests/test_units.py::ExtractorGeneralisation` passes on synthetic documents
with a new patient, an hours-based requirement, a new correction and a new break
phrasing), but that is four hand-written documents, not a measurement.

**How I would investigate it next, in order.** First, build a disagreement
harness: run `--extractor rules` and `--extractor llm` over the same corpus and
diff the event tables field by field. Every disagreement is either a rules gap
or a model error, and the ratio is the number I actually want. That harness is
the reason the extractors share one `Claim` schema and one interface. Second,
add a coverage assertion to ingest: flag any document whose text contains a
clock-time range that no claim accounts for, and any `N minutes` phrase that no
claim uses. That is a cheap, answer-independent "I did not read this" signal of
the same kind as the wall-clock check, and I would run it before trusting the
extractor on an unseen corpus. Third, only then widen the cue lists, driven by
what the first two surface rather than by imagination.

---

## 6. Model names, settings, assistance, runtime and cost

**Shipped default path: no model.** `python -m backbone build documents` makes
zero model calls, costs $0.00, and is byte-reproducible. Every number in this
README and in `out/answers.md` was produced that way.

**Model path, as measured.** Two modes, both behind a flag and neither used by
the default build:

| | |
|---|---|
| provider / model | **Groq / `openai/gpt-oss-120b`** (131k context) |
| settings | `temperature=0`, `max_tokens=9000`, prompt version `v3`, strict JSON mode, reasoning budget left at the provider default |
| `--extractor llm` | hybrid: code parses tables, ids, dispositions and clock times; the model reads only prose |
| `--extractor llm_full` | the model also extracts encounter structure, for the comparison in `docs/experiment-extractor-comparison.md`. Not a deployment mode |
| `--router llm` | a model picks the query function and its parameters; the numbers still come from the function |

Measured over the 31 supplied documents in **hybrid** mode (`--extractor
llm`, the configuration the design recommends):

| | measured |
|---|---|
| documents sent to the model | 25 of 31 (6 skipped by class: no clinical prose) |
| input / output tokens | **19,692 / 29,648** for the 17 not already cached |
| cost at listed on-demand prices | **$0.0252** (free tier: $0) |
| quotes dropped as not verbatim | **1** |
| answers to the five dev questions | **identical to the deterministic path** |
| plan requirements found | both |
| observation spans found | **121, against the deterministic extractor's 35** |
| of the deterministic spans, how many it also found | **33/35 (94%)** |

That last pair is the result worth having: the hybrid agrees on every number
and finds 3.5x as much narrative evidence, recovering 88 spans the cue-list
extractor misses while missing only 2 of its own. It measures limitation 4
below rather than asserting it.

And in `llm_full` mode, where the model also extracts encounter structure --
a comparison mode, not a deployment mode:

| | measured |
|---|---|
| documents sent | 31 |
| input / output tokens | **48,114 / 13,851** |
| cost at listed on-demand prices | **$0.0176** (free tier: $0) |
| model latency | 45.2 s total, ~1.46 s per document |
| wall time for the pass | 623 s, dominated by the free tier's 8,000 tokens/minute ceiling |
| quotes dropped as not verbatim | **12** (~1 in 9 extracted spans) |
| unparseable JSON responses | 0 |
| agreement with the deterministic extractor | **100% of 120 event-field comparisons** |

Responses are cached in `out/llm-cache.db`, keyed by
`(normalized document text, prompt version, mode, provider, model)`, so a
rebuild costs nothing and a prompt change re-extracts only what changed. On this
path the cache is also what makes an abstraction reproducible: the same document
returned a materially different answer across runs at temperature 0 (§14 of
DESIGN.md). The deterministic path is byte-identical across runs by
construction.

Extrapolated to 500K documents, labelled an estimate: ~1,550 input tokens per
document gives ~776M input tokens, order **$120–250** at this model's listed
prices, once, because the cache makes reruns free. Anthropic is also supported
(`ANTHROPIC_API_KEY`, `BACKBONE_MODEL=claude-sonnet-5`) but was not exercised:
this environment had no Anthropic key.

Both passes are written up in `docs/experiment-extractor-hybrid.md` and
`docs/experiment-extractor-comparison.md`.

**Total runtime to produce everything in this repository:** about 13 minutes
of compute, almost all of it rate-limited model calls — 0.15 s build, 3 s for
83 tests, ~0.3 s benchmark, ~2 minutes for the 200-patient scaling run, and
~10 minutes for the one model-extraction pass.

**Coding assistance:** written with Claude Code (Opus 5) over roughly 8 hours
across several review rounds,
including reading all 31 documents and hand-computing the ledger in
`tests/test_gold.py` before any code was written. Those expected values live only
in the tests and are never imported by the pipeline — the pipeline has to derive
them from the documents.

---

## 7. Layout

```
backbone/
  schema.sql        documents, claims, events, event_claims, decisions, ...
  normalize.py      normalization for identity, date parsing and validation
  taxonomy.py       service synonyms, document classes, authority tables
  intervals.py      interval algebra; every duration comes from here
  extract/rules.py  deterministic claim extraction (default)
  extract/llm.py    model extraction: hybrid and full modes, + optional router
  provider.py       Groq/Anthropic over urllib, token accounting, rate governor
  reconcile.py      matching + per-field resolution + decision log
  queries.py        the ten query functions; the only place numbers are made
  router.py         question -> function + parameters
  render.py         result -> reviewable Markdown (formats only, never computes)
  bench.py          measured performance
  experiment.py     the conflict-policy A/B
  compare.py        the rules-vs-model extractor disagreement harness
  cli.py            build / add / answer / ask / trace / export / bench /
                    experiment / compare / models / verify
scripts/run_all.sh      full reproduction, captured to out/logs/execution.log
scripts/scale_test.py   synthetic multi-patient corpora + measured scaling curve
tests/                  108 tests: hand-verified ledger, idempotence, restart,
                        router safety, week coverage, units
tests/gold_ledger.json  the hand-computed ledger as data (also read by compare.py)
docs/                   the two experiment write-ups
.env.example            copy to .env to enable the model path
out/                    answers.md/.json, abstraction.json, benchmark.json,
                        scaling.json, logs/execution.log, clinical.db
```

---

## 8. Incomplete work and tradeoffs, stated plainly

- **No materialized `patient_week` table.** This is the measured first
  bottleneck (§4). Deliberately not built at this corpus size; the fix is
  described and the measurement that justifies it is in the repo.
- **The LLM path is unmeasured.** No API key in this environment. Implemented,
  cached, quote-verified, and reported as unavailable rather than estimated.
- **Observation extraction is conservative and lossy.** 35 observation spans from 31
  documents, filtered to sentences that name the patient, name a clinical domain
  and carry a change cue; goal and plan language is excluded. It will miss
  evidence a careful reader would keep. The LLM extractor exists mainly to
  replace exactly this component.
- **Week bucketing is configurable but the default matters.** Monday–Sunday is
  read from the plan's own wording (`week_mon_sun`), and
  `compliance(..., scheme="week_sun_sat")` is available. The January 26–30 week
  is a partial week at the episode end and is flagged `partial_week` rather than
  prorated; prorating would change the DEV-03 verdict and the documents do not
  say to prorate.
- **No plan amendment in the supplied data.** The schema stores requirements as
  dated rows with `effective_start`/`effective_end`, a later plan automatically
  closes the earlier one, and `plan_change_comparison` is implemented and
  reachable. On this corpus it correctly reports that no mid-episode plan change
  is documented, so that path is exercised but not validated against a real
  amendment.
- **Ambiguous matches are reported, not resolved.** A claim with no identifier
  that matches more than one candidate event is left unmatched with the candidate
  list in the decision log. On this corpus six claims are unmatched, all
  administrative (import receipts and questionnaire reviews that state of
  themselves that they record no visit).
- **One patient in the supplied data.** Nothing is keyed to that: the patient is
  discovered from document headers, every query takes a patient parameter, and
  the scaling harness exercises 200 patients through the same code.
