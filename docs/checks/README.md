# Proof files for the second-call columns

These are the random samples behind the counts on the dataset card ("30 of 30" and so on) for the
columns from the second model call in `analysis/extractions.parquet`. They were made 2026-11-10 and
2026-11-11 and are published unchanged.

Each folder has a sample file and a verdicts file, matched by `notice_id`:

- **Sample** (`<field>.json`): what was shown for the notice (`value`, `quote`, or the list), with the
  surrounding document text (`context` or `document`) and the SAM.gov link.
- **Verdicts** (`verdicts_<field>.json`): `correct`, `wrong` or `unclear`, with a reason that quotes the
  document.

**Who judged.** The verdicts were made by Claude subagents (an AI model) reading the document text in the
sample file against written criteria, not by a person. For the first two rounds (`group1_check`,
`group2_check`), `docs/extras.md` records that every item marked wrong or unclear, and a sample of those
marked right, was read again. To check an item yourself, open its `sam_link`.

**Samples, not guarantees.** 30 right out of 30 is still consistent with up to about 10% of the whole
column being wrong.

## Which file backs which count on the card

| Column | Card says | File | Verdicts in the file |
|---|---|---|---|
| `contractor_named` | 29 of 30 right, 1 unclear | `group1_check/verdicts_incumbent.json` | 29 correct, 1 unclear |
| `new_source_approval_quote` | 30 of 30 | `group2_check/verdicts_source_approval_path.json` | 30 correct |
| `ceiling_*` | 30 of 30 | `group2_check/verdicts_ceilings.json` | 30 correct |
| `delay_to_compete_quote` | 30 of 30 | `round3_check/verdicts_delay_to_compete.json` | 30 correct |
| `cost_to_compete_quote` | 29 of 30, 1 unclear | `round3_check/verdicts_cost_to_compete.json` | 29 correct, 1 unclear |
| `not_to_exceed_usd` | all 43 values read, 43 right | `round3_check/verdicts_not_to_exceed.json` | 46 read: 43 correct, 2 wrong, 1 unclear. The 3 that were not right (an initial-phase cap, a cap on one line item, a negotiation variance) are excluded by the published rule; the 43 published values are the 43 read as correct |
| `interested_firms_named` | in 20 lists none named a wrong firm; 4 left out firms | `round4_check/verdicts_interested.json` | 15 correct, 4 wrong, 1 unclear; all 4 wrong are missing firms (`missing_firms`), none has `extra_firms` |
| `interested_firms_contractor_entry` | 30 of 30 matches were the same firm | `awardee_entry/sample.json` | 30 correct, judged by Claude (in the session that wrote the rule) from the names alone |
| `technical_data_reasons` | 39 of 41 judged right, 1 unclear, 1 a software subscription | `round3_check/verdicts_data_rights.json`, `round4_check/verdicts_data_rights.json` | These verdicts include the "proprietary to the contractor" reason, which failed (11 of 20, `*_data_rights_proprietary.json`) and is not published, so their totals are not the card's 41. `docs/extras.md` gives the 41; these files are not broken down to it here |

Earlier rounds of the same fields are kept because the docs refer to them: `group1_check/verdicts_other_interested.json`
(26 of 30) and `interested_recheck/` (23 of 30) are earlier versions of the interested-firms rule, and
`group2_check/` delay, cost, not-to-exceed and data-rights files tested the first version of those checks.
`awardee_entry/rejected_prefix_rule_sample.json` is the looser match that was tried and dropped (it caught
parent companies and sibling units).
