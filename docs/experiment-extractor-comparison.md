# Tested design decision: deterministic vs model extraction

Same documents, same reconciliation policy, same arithmetic. Only the
front-end that turns text into claims changes. Reproduce with
`python -m backbone compare`.

Model under test: **openai/gpt-oss-120b** via `groq`, temperature 0, prompt `v3`.

## Headline

| | rules | llm_full |
|---|---|---|
| therapy sessions | **12** | 12 |
| distinct therapy days | **11** | 11 |
| episode minutes | **585-595** | 585-595 |
| events built | 20 | 21 |
| claims extracted | 99 | 67 |
| observations kept | 54 | 23 |
| distinct measures | 3 | 3 |
| wall-clock integrity | passes | passes |
| ingest wall time | 0.19 s | 707.77 s |

### Weekly totals and verdicts

This is the table that matters, and it is the one an agreement
percentage on the event table can hide.

| week | gold minutes | rules | llm_full | rules verdict | llm_full verdict |
|---|---|---|---|---|---|
| 2026-01-05 | 140 | 140 | 140 | NOT_MET | NOT_MET |
| 2026-01-12 | 120 | 120 | 120 | NOT_MET | NOT_MET |
| 2026-01-19 | 180 | 180 | 180 | MET | MET |
| 2026-01-26 | 145-155 | 145-155 | 145-155 | CANNOT_DETERMINE | CANNOT_DETERMINE |

## Agreement on the event table

- 21 events x 6 compared fields = 126 field comparisons
- **124/126 agree (98.4%)**
- 2 disagreements: rules correct in 0, llm_full correct in 0, 1 both wrong or partially scored, 1 not covered by the hand-verified ledger

| event | field | rules | llm_full | who is right |
|---|---|---|---|---|
| `2026-01-16|HG-M042|BH-D013` | exists | MISSING | present | neither |
| `2026-01-27|HG-E116` | occurred | no | yes | not scored |

## The prose layer

| | rules | llm_full |
|---|---|---|
| observation spans | 35 | 17 |
| spans both found | 4 | 4 |
| spans only this side found | 31 | 13 |
| of the other side's spans, how many it also found | 4/17 (24%) | 4/35 (11%) |
| Jaccard overlap | 8% | 8% |
| domains covered | 8 | 8 |
| reporters distinguished | clinician, informant, patient | clinician, patient |

**Found only by the deterministic extractor** (31, first 8):

- BH-D002 [378:534]
- BH-D007 [1171:1227]
- BH-D007 [1228:1355]
- BH-D009 [973:1071]
- BH-D009 [1072:1200]
- BH-D011 [509:615]
- BH-D011 [616:733]
- BH-D011 [734:824]

**Found only by llm_full** (13, first 8):

- BH-D002 [395:534]
- BH-D002 [535:610]
- BH-D002 [1185:1239]
- BH-D002 [1240:1305]
- BH-D003 [355:494]
- BH-D003 [576:655]
- BH-D010 [507:574]
- BH-D010 [1175:1249]

## Measured model usage

- documents sent to the model: 31 (of 31 processed; 0 served from cache)
- input tokens: **48,114**
- output tokens: **30,003**
- cost at listed on-demand prices: **$0.0297** (approximate; free-tier usage is $0)
- total model latency: 76.2 s (2457 ms per document)
- quotes dropped because they were not found verbatim in the source: **26**
- responses that were not parseable JSON: 0

Per 500K documents, extrapolated linearly from the measured tokens above: ~776M input tokens. Labelled an estimate because it is one.

## Reproducibility

Three consecutive calls on the same document with the same prompt at temperature 0 returned identical JSON (1 distinct response(s) in 3 calls).

Across runs it is a different story. For the group facilitator note probed in `out/determinism.json`, the model returned `patient_present_intervals: []` during the comparison build (correct: that note states no patient times and says they are held on the attendance roster) and `[['10:00', '10:45'], ['11:00', '11:30']]` when called again about fifteen minutes later -- the scheduled slot, split around the break. Same prompt, same temperature, clinically material difference.

So the response cache is not only a cost optimisation; on this path it is what makes an abstraction reproducible at all. The rules extractor is byte-identical across runs by construction.

## Plan requirements read by each front-end

| | rules | llm_full |
|---|---|---|
| therapy_days | >= 3 per week_mon_sun, counts 3 | >= 3 per week_mon_sun, counts 3 |
| therapy_minutes | >= 150 per week_mon_sun, counts 3 | >= 150 per week_mon_sun, counts 3 |

## Measures found by each front-end

| instrument | completed | rules total | llm_full total |
|---|---|---|---|
| PHQ-9 | 2026-01-05 | 18.0 | 18.0 |
| PHQ-9 | 2026-01-16 | 14.0 | 14.0 |
| PHQ-9 | 2026-01-30 | 10.0 | 10.0 |
