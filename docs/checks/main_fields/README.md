# Sample checks of main-extraction fields (2026-11-11)

Fields from the main extraction (`extract6`) that had been validated on too few cases (2 to 13 in the
October validation) were each checked on a random sample of 30 filled values from the published table
(dataset revision f6e2d5b5, seed 20261111). For `bridge_or_extension` the sample is 30 "yes" plus 10 "no";
for `is_letter_contract`, 30 of the 37 "yes".

Each `<field>.json` has the value, the quote the extraction cited, and the document text around it (or,
for a "no" with no quote, the first ~9,000 characters of the document) with the SAM.gov link. Each
`verdicts_<field>.json` has `correct` / `wrong` / `unclear` and a reason quoting the document.
The verdicts were made by Claude subagents (an AI model) given the field's written definition from
`prompts.yaml`, not by a person, and only from the text in the sample file. A few "wrong" and "correct"
verdicts of each failing field were read again in the session that made the call.

The bar, the same one the second-call fields were held to: about 28 of 30 right, with no pattern of
errors. 25 of 30 was not enough there either.

| Field | Right | Wrong | Unclear | Decision |
|---|---|---|---|---|
| `incumbent_piid` | 30 | 0 | 0 | published |
| `covered_piids` | 28 | 0 | 2 | published (both unclear: the document gives two different numbers) |
| `is_letter_contract` = yes | 28 | 1 | 1 | published (the wrong one definitizes an earlier letter contract) |
| `funding_sources` | 27 | 0 | 3 | published: nothing wrong; the 3 unclear are generic names copied as written ("IT Funds") |
| `prior_extension_count` | 25 | 0 | 5 | removed: the unclear ones may be wrong numbers; the 14 with an explicit count or ordinal were all right, so a version limited to those could come back after its own check |
| `bridge_or_extension` = yes | 23 | 4 | 3 | removed: ceiling increases and multi-year extensions called bridges ("no": 10 of 10 right) |
| `need_by_date` | 19 | 7 | 4 | removed: requested delivery dates on order forms taken as deadlines |
| `stated_pop_limit_days` | 18 | 11 | 1 | removed: the FAR 52.217-8 six-month option taken as a cap on the whole contract |
| `urgency_trigger` | 17 | 6 | 7 | removed: urgency not the cited authority in some, categories applied inconsistently |

`market_research_methods` (77-84% of codes right) and `contract_types` (11 of 18 stated types found) were
removed on their October validation numbers (`docs/field_extraction.md`) without a new sample.
