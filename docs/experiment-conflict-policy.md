# Tested design decision: conflict-resolution policy

Same documents, same extractor, same query code. Only the policy in
`reconcile.py` changes. Reproduce with `python -m backbone experiment`.

- **explicit-correction (shipped)**: only an entry naming the record and field it replaces supersedes that field.
- **latest-document**: the most recently received document wins.

## Totals

| patient | | explicit-correction (shipped) | latest-document |
|---|---|---|---|
| `HG-M042` | sessions | **12** | 12 |
| | episode minutes | **585-595** | 600 |
| | wall-clock check | passes | FAILS |

## Weeks that change

| patient | week | shipped | latest-document |
|---|---|---|---|
| `HG-M042` | 2026-01-19 | 180 min, MET | 195 min, MET |
| `HG-M042` | 2026-01-26 | 145-155 min, CANNOT_DETERMINE | 145 min, NOT_MET |

## Events that resolve differently

| patient | event | date | field | shipped | latest-document |
|---|---|---|---|---|---|
| `HG-M042` | `HG-E109` | 2026-01-16 | occurred | yes | no |
| `HG-M042` | `HG-E110` | 2026-01-19 | minutes | 60 | 75 |
| `HG-M042` | `HG-E114` | 2026-01-23 | occurred | yes | no |
| `HG-M042` | `HG-E115` | 2026-01-26 | minutes | 40-50 | 40 |
| `HG-M042` | `HG-E115` | 2026-01-26 | disputed | 1 | 0 |

### Decision logs side by side

**`HG-E109`** — shipped policy

- `[RULE-PRES-02]` rejected_presence = present -- schedule_export cannot establish that care was delivered (BH-D006)
- `[RULE-PRES-03]` occurred = yes -- contact is documented but the patient was not present (BH-D012: absent for the entire)

**`HG-E109`** — latest-document policy

- `[POLICY-LATEST]` occurred = no -- latest-document policy: BH-D012 (2026-01-16) states absent

**`HG-E110`** — shipped policy

- `[RULE-PRES-02]` rejected_presence = present -- retransmission cannot establish that care was delivered (BH-D104)
- `[RULE-PRES-02]` occurred = yes -- attested or clinically documented patient contact: BH-D102
- `[RULE-DUR-05]` rejected_duration = [['10:00', '11:30']] -- retransmission contributes no duration (BH-D104)
- `[RULE-DUR-01]` minutes = 60 -- attendance_register BH-D102 is the authority for duration on a group_therapy event; intervals+correction; correction 6853e674a5cd-C01 applied

**`HG-E110`** — latest-document policy

- `[POLICY-LATEST]` occurred = yes -- latest-document policy: BH-D104 (2026-01-26) states present
- `[POLICY-LATEST]` minutes = 75 -- latest-document policy: BH-D104 (2026-01-26) -> 75 min

**`HG-E114`** — shipped policy

- `[RULE-PRES-03]` occurred = yes -- contact is documented but the patient was not present (BH-D109: patient participation: none)

**`HG-E114`** — latest-document policy

- `[POLICY-LATEST]` occurred = no -- latest-document policy: BH-D109 (2026-01-23) states absent

**`HG-E115`** — shipped policy

- `[RULE-PRES-02]` occurred = yes -- attested or clinically documented patient contact: BH-D110, BH-D111
- `[RULE-DUR-03]` minutes = 40-50 -- records of equal authority disagree and neither is a correction: kept as bounds (BH-D111=40min; BH-D110=50min)

**`HG-E115`** — latest-document policy

- `[POLICY-LATEST]` occurred = yes -- latest-document policy: BH-D111 (2026-01-26) states present
- `[POLICY-LATEST]` minutes = 40 -- latest-document policy: BH-D111 (2026-01-26) -> 40 min

## What the wall-clock check found

The naive policy is not merely a different judgement call: it makes the record internally impossible, and the system detects that *without being told the right answer*. A patient cannot be in two therapy rooms at once, so an overlap between two counted contacts on one day means the reconciliation is wrong somewhere.

    {"check": "wall_clock", "patient": "HG-M042", "date": "2026-01-19", "detail": [{"a": "HG-E110", "b": "HG-E111", "overlap_minutes": 15}]}

That check (`queries.integrity`) runs on every build. It is the cheapest answer-independent signal I found that a reconciliation policy is wrong, and it is why recency is only ever a tiebreak in the shipped policy, never authority.

## The quieter effect

Recency also *resolves* conflicts the documents do not resolve. Where the shipped policy keeps bounds and reports `CANNOT_DETERMINE`, picking the later signature produces a confident verdict from a record that does not support one:

- `HG-M042` week of 2026-01-26: `CANNOT_DETERMINE` (145-155 min) becomes `NOT_MET` (145 min)

Unlike the overlap, this one leaves no trace in the output at all. It is the argument for carrying bounds rather than a chosen value.
