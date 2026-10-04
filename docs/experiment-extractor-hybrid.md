# Tested design decision: deterministic vs model extraction

Same documents, same reconciliation policy, same arithmetic. Only the
front-end that turns text into claims changes. Reproduce with
`python -m backbone compare`.

Model under test: **openai/gpt-oss-120b** via `groq`, temperature 0, prompt `v3`.

## Headline

| | rules | llm |
|---|---|---|
| therapy sessions | **12** | 12 |
| distinct therapy days | **11** | 11 |
| episode minutes | **585-595** | 585-595 |
| events built | 20 | 20 |
| claims extracted | 99 | 184 |
| observations kept | 54 | 154 |
| distinct measures | 3 | 3 |
| wall-clock integrity | passes | passes |
| ingest wall time | 0.15 s | 394.97 s |

### Weekly totals and verdicts

This is the table that matters, and it is the one an agreement
percentage on the event table can hide.

| week | gold minutes | rules | llm | rules verdict | llm verdict |
|---|---|---|---|---|---|
| 2026-01-05 | 140 | 140 | 140 | NOT_MET | NOT_MET |
| 2026-01-12 | 120 | 120 | 120 | NOT_MET | NOT_MET |
| 2026-01-19 | 180 | 180 | 180 | MET | MET |
| 2026-01-26 | 145-155 | 145-155 | 145-155 | CANNOT_DETERMINE | CANNOT_DETERMINE |

## Agreement on the event table

- 20 events x 6 compared fields = 120 field comparisons
- **120/120 agree (100.0%)**
- 0 disagreements: rules correct in 0, llm correct in 0, 0 both wrong or partially scored, 0 not covered by the hand-verified ledger

No disagreement on any compared event field.

## The prose layer

In hybrid mode the model never sees encounter structure: tables, ids, dispositions and clock times are parsed by code either way. So the identical event table above is **true by construction and is not evidence of anything** -- it only confirms the wiring. The measurement that matters is this section.

| | rules | llm |
|---|---|---|
| observation spans | 35 | 120 |
| spans both found | 34 | 34 |
| spans only this side found | 1 | 86 |
| of the other side's spans, how many it also found | 34/120 (28%) | 34/35 (97%) |
| Jaccard overlap | 28% | 28% |
| domains covered | 8 | 8 |
| reporters distinguished | clinician, informant, patient | clinician, informant, patient |


Read that carefully: the low Jaccard figure is an **asymmetry, not a disagreement**. llm found 34 of the 35 spans the deterministic extractor found (97%) and then 86 more. That is the conservative-and-lossy observation extraction named as limitation 4 in DESIGN.md, measured.

**Found only by the deterministic extractor** (1, first 8):

- BH-D013 [435:611]

**Found only by llm** (86, first 8):

- BH-D002 [535:610]
- BH-D002 [611:735]
- BH-D002 [918:990]
- BH-D002 [1023:1109]
- BH-D002 [1110:1184]
- BH-D002 [1185:1239]
- BH-D002 [1240:1392]
- BH-D002 [1394:1449]

## Measured model usage

- documents sent to the model: 25 (of 31 processed; 0 served from cache)
- input tokens: **19,692**
- output tokens: **28,195**
- cost at listed on-demand prices: **$0.0241** (approximate; free-tier usage is $0)
- total model latency: 71.9 s (2877 ms per document)
- quotes dropped because they were not found verbatim in the source: **0**
- responses that were not parseable JSON: 0

Per 500K documents, extrapolated linearly from the measured tokens above: ~394M input tokens. Labelled an estimate because it is one.

## Reproducibility

Three consecutive calls on the same document with the same prompt at temperature 0 returned identical JSON (1 distinct response(s) in 3 calls).

Across runs it is a different story. For the group facilitator note probed in `out/determinism.json`, the model returned `patient_present_intervals: []` during the comparison build (correct: that note states no patient times and says they are held on the attendance roster) and `[['10:00', '10:45'], ['11:00', '11:30']]` when called again about fifteen minutes later -- the scheduled slot, split around the break. Same prompt, same temperature, clinically material difference.

So the response cache is not only a cost optimisation; on this path it is what makes an abstraction reproducible at all. The rules extractor is byte-identical across runs by construction.

## Plan requirements read by each front-end

| | rules | llm |
|---|---|---|
| therapy_days | >= 3 per week_mon_sun, counts 3 | >= 3 per week_mon_sun, counts 3 |
| therapy_minutes | >= 150 per week_mon_sun, counts 3 | >= 150 per week_mon_sun, counts 3 |

## Measures found by each front-end

| instrument | completed | rules total | llm total |
|---|---|---|---|
| PHQ-9 | 2026-01-05 | 18.0 | 18.0 |
| PHQ-9 | 2026-01-16 | 14.0 | 14.0 |
| PHQ-9 | 2026-01-30 | 10.0 | 10.0 |
