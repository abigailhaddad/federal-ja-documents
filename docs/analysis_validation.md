# Validation of the experimental J&A issue tagger

**Status: experimental.** As of 2026-10-17, three categories are shipped at
"screening" status: `agency_delay_cause`, `sources_rejected` and
`workforce_cause`. No category reached "ready". `political_direction` and
`expiring_funds` are not shipped.

`workforce_cause` failed in round 3 with a whole-document method. It was
rebuilt as a keyword gate followed by model confirmation, and re-tested in
round 4 on fresh documents (see
[Round 4](#round-4-workforce_cause-rebuilt-and-re-tested)). The round-3
tables below still describe the old method for that category. Every tag is a possible issue for a person
to check against the document. It is never a finding.

This page holds aggregate results only: counts, rates and 95% intervals. The
per-document judgements (the answer key) are not published. The tagger's own
outputs from both runs of rounds 3 and 4 are published on Hugging Face under
`analysis/validation/theme_tags/`.

## What was tested

- **Code.** `federal_ja.analysis` with `PROMPT_VERSION = "2026-10-16.frozen1"` and model `claude-haiku-5-5`.
- **Same method as the development rounds.** Before this round, a check on 370 random documents confirmed that this code builds byte-identical prompts and identical keyword hits to the code used in the earlier development rounds.
- **Dataset.** The Hugging Face dataset at commit `6bffaa9b0c4c` (last modified 2026-10-17 13:58 UTC).
- **Document text.** Every page, either the native text layer or the machine transcription of an image-only page, in attachment order and then page order.

### Methods (frozen from the development rounds)

| Category | Method |
|---|---|
| `agency_delay_cause` | Keyword patterns pick candidate passages, and the model confirms or rejects them, reading only those excerpts. |
| `sources_rejected` | Same: keyword patterns, then the model confirms. |
| `political_direction` | Same: keyword patterns, then the model confirms. |
| `expiring_funds` | Same: keyword patterns, then the model confirms. |
| `workforce_cause` | The model reads the whole document. |

The development rounds tuned only on development documents and checked
held-out documents. Those held-out documents were native-text only. **This
round is the first test on documents with machine-transcribed pages**, and the
first on a large random sample.

### Rules fixed before scoring

- **Pre-registered.** Methods, sets, scoring and status rules were written down and committed before any truth was judged, any model run on these documents, or any score computed.
- **ready:** on the random set, the 95% Wilson lower bound on precision is at least 80%, and stability is at least 90%.
- **screening:** on the random set, the 95% Wilson lower bound on precision is at least 50%.
- **Not shipped:** everything else, including a category with too few random-set tags for the lower bound to reach 50%.

## Sets

The universe is the published Justification notices whose assembled text is
200 to 60,000 characters long. That is 9,710 of 10,504 notices: 748 had no
usable text and 46 were longer than 60,000 characters. Notices used in
earlier development rounds were excluded, which leaves a pool of 9,347. Of
those, 72.0% have at least one transcribed page.

- **Random set: 400 J&As, seeded.** It is stratified: 160 have at least one transcribed page and 240 have none. Prevalence is re-weighted to the pool's 72% transcribed share.
- **Enriched sets: 40 J&As per category, seeded.** Each is drawn from documents with a keyword hit for that category. The sets are disjoint from the random set and from each other.

## Answer key

- **Who judged.** Every document × category was judged from the full document text: 2,000 random-set pairs, plus 200 enriched pairs, one per enriched document.
- **Blind.** The judges saw only the document and a written guide (the category definitions and exclusions). They never saw model output or keyword hits.
- **Labels.** Each pair got `yes`, `no` or `borderline`, plus a verbatim quote.
- **No human labeller.** The judges were Claude Opus 5.5 instances.
- **Second judgement.** A seeded random 10% of documents (60 documents, 224 pairs) was re-judged by separate fresh instances that had no access to the first labels.
  - **What agreement shows.** The same model family judged both times with the same guide. High agreement therefore shows the guide is applied consistently. It does not show that a human expert would agree.
  - **First judgement is the key.** The first judgement stays the answer key. Disagreements were reported, not resolved.
- **Scoring.** Scoring is per document: a document counts as tagged if the run produced at least one verified tag in that category.
- **Precision.** `borderline` counts as not-yes; the sensitivity column counts it as yes.
- **Recall.** Recall counts only the positives the answer key found.

## Results

### Random set (400 J&As)

| Category | Status | Precision (95% CI) | Precision, borderline counted as yes | Recall (95% CI) | Prevalence in sample | Prevalence, pool-weighted |
|---|---|---|---|---|---|---|
| `agency_delay_cause` | **screening** | 12/15 = 80% (55–93%) | 14/15 = 93% (70–99%) | 12/35 = 34% (21–51%) | 35/400 = 8.8% (6.4–11.9%) | 7.7% (4.7–10.8%) |
| `sources_rejected` | **screening** | 24/34 = 71% (54–83%) | 27/34 = 79% (63–90%) | 24/34 = 71% (54–83%) | 34/400 = 8.5% (6.1–11.6%) | 9.3% (5.8–12.8%) |
| `political_direction` | not shipped | 10/14 = 71% (45–88%) | 13/14 = 93% (69–99%) | 10/15 = 67% (42–85%) | 15/400 = 3.8% (2.3–6.1%) | 2.4% (0.9–3.9%) |
| `workforce_cause` | not shipped | 15/32 = 47% (31–64%) | 20/32 = 62% (45–77%) | 15/17 = 88% (66–97%) | 17/400 = 4.2% (2.7–6.7%) | 3.0% (1.3–4.7%) |
| `expiring_funds` | not shipped | 1/3 (6–79%) | 2/3 (21–94%) | 1/4 (5–70%) | 4/400 = 1.0% (0.4–2.5%) | 1.1% (0.0–2.4%) |

- **Intervals.** Precision, recall and sample-prevalence intervals are Wilson 95%.
- **Pool-weighted prevalence.** These are point estimates with normal-approximation 95% intervals.
- **The second run.** Run B's random-set precision was 12/14 for `agency_delay_cause`, 25/34 for `sources_rejected`, 10/15 for `political_direction`, 15/35 for `workforce_cause` and 1/3 for `expiring_funds`.

### By stratum (random set, run A)

| Category | Precision, native-only | Precision, has a transcribed page | Recall, native-only | Recall, has a transcribed page |
|---|---|---|---|---|
| `agency_delay_cause` | 7/9 (45–94%) | 5/6 (44–97%) | 7/24 (15–49%) | 5/11 (21–72%) |
| `sources_rejected` | 13/20 (43–82%) | 11/14 (52–92%) | 13/18 (49–88%) | 11/16 (44–86%) |
| `political_direction` | 9/12 (47–91%) | 1/2 | 9/13 (42–87%) | 1/2 |
| `workforce_cause` | 12/24 (31–69%) | 3/8 (14–69%) | 12/14 (60–96%) | 3/3 |
| `expiring_funds` | 1/2 | 0/1 | 1/2 | 0/2 |

The strata are small. None of the differences between native-only and
transcribed documents is distinguishable from chance at these sizes.

### Enriched sets (40 J&As each, keyword-selected)

| Category | Answer-key yes | Precision (95% CI) | Recall (95% CI) |
|---|---|---|---|
| `agency_delay_cause` | 7 | 5/5 (57–100%) | 5/7 (36–92%) |
| `sources_rejected` | 11 | 10/13 (50–92%) | 10/11 (62–98%) |
| `political_direction` | 23 | 19/26 (54–86%) | 19/23 (63–93%) |
| `workforce_cause` | 32 | 32/34 (81–98%) | 32/32 (89–100%) |
| `expiring_funds` | 2 | 2/8 (7–59%) | 2/2 (34–100%) |

Enriched precision does not set the status. One pattern is worth noting:
`workforce_cause` precision is high on keyword-selected documents but low on
random ones, because on random documents the whole-document model tags
generic causes (vendor changes, funding decisions, ordinary contract
expirations). A keyword prefilter in front of the model might fix this. That
would be a new method, and it would need its own validation round.

### Stability (two independent runs of the full pipeline)

Each run is two passes with the union of verified tags kept, as the shipped
tool does. The figures cover random and enriched documents together.

| Category | Tagged in both runs / tagged in either | Documents whose tagged/not-tagged status flipped |
|---|---|---|
| `agency_delay_cause` | 19/20 (95%) | 1 of 440 |
| `sources_rejected` | 46/48 (96%) | 2 of 440 |
| `political_direction` | 39/41 (95%) | 2 of 440 |
| `workforce_cause` | 61/75 (81%) | 14 of 440 |
| `expiring_funds` | 11/11 (100%) | 0 of 440 |

### Agreement between the first and second judgements (60 re-judged documents)

| Category | Pairs | Yes (first / second) | Raw agreement (yes vs not-yes) | Cohen's kappa |
|---|---|---|---|---|
| `agency_delay_cause` | 45 | 3 / 4 | 98% | 0.85 |
| `sources_rejected` | 45 | 2 / 2 | 100% | 1.00 |
| `political_direction` | 44 | 6 / 6 | 100% | 1.00 |
| `workforce_cause` | 45 | 6 / 6 | 100% | 1.00 |
| `expiring_funds` | 45 | 1 / 0 | 98% | 0.00 |

- **Few positives.** The re-judged sample has only a handful of positives per category, so kappa is unstable.
- **expiring_funds.** Its kappa of 0 comes from a single disagreement on the only positive.
- **Same model.** Both judges are the same model with the same guide; see the caveat under "Answer key".

### Quote check

- **Every quote verified.** All 420 tags from the two runs have a quote that was found in the page text: 380 exact and 40 after whitespace or quote-mark normalisation.
- **Second code path.** A second, independent code path re-checked every quote against text built differently: the whole-attachment text column instead of the page table. All 420 passed.
- **Located on a page.** Each tag records the page(s) its quote is on. 120 of the 420 quotes sit on a machine-transcribed page.
- **Image check.** 20 tags, drawn as a seeded sample of 10 on transcribed pages and 10 on native pages, were checked against images of the original PDF pages.
  - **Quotes.** All 20 quotes appear on the page image, and the transcription matched the image for every transcribed quote.
  - **Support.** On reading, 16 of the 20 tags were supported by the document, 3 were borderline, and 1 was not (an `expiring_funds` tag).

## What these numbers mean

- **agency_delay_cause (screening).** About 4 in 5 tags are right, but the interval runs down to 55%.
  - **Low recall.** The tool finds about a third of the J&As in which the answer key saw an admitted agency delay.
  - **Why.** Most misses (16 of 23) had no keyword hit, so the model never saw them.
  - **Use.** Use it to find examples, not to count them.
- **sources_rejected (screening).** About 7 in 10 tags are right.
  - **Main false positive.** Another firm responded, but the document never says the agency found it incapable.
- **Not shipped.**
  - **political_direction:** the lower bound, 45%, is just under 50%. Most false tags are an Executive Order cited as alignment rather than direction.
  - **workforce_cause:** 47% precision on random documents, and 81% stability.
  - **expiring_funds:** too rare to measure; only 4 positives in 400.
- **No category is "ready", so no example tags are published here.**

## Changes from the earlier development rounds

The development rounds scored native-text documents only, on smaller
held-out sets, with labels checked by one person against the model's quotes.
This round changes several of those conclusions:

- **workforce_cause no longer holds up.** It had been the most reliable category, at 8/9 on a 120-document holdout. On 400 random documents it is 15/32.
- **agency_delay_cause recall is much lower against a full-document answer key.** The earlier figure was 5/6; here it is 34%. Its precision held: 80% here, against 5/5 before.
- **sources_rejected and political_direction precision is about 71%** in both cases. The preliminary development estimates were 80% and 75%.

## Round 4: workforce_cause, rebuilt and re-tested

### Method (`PROMPT_VERSION` for this category: `2026-10-17.wfgate1`)

- **Keyword gate.** Patterns for federal workforce and administrative events select candidate passages. They cover the deferred resignation program, RIFs, hiring freezes, DOGE, workforce and cost-efficiency Executive Orders, VERA/VSIP, staff losses and shortages, vacancies, attrition, departures, shutdowns, lapses and furloughs, and reorganisations.
  - The patterns match on normalised text and on a copy with hyphens removed, so words split by a line-wrap hyphen and OCR'd text still match.
  - Only documents with a gate hit go to the model.
- **Model confirmation.** Claude Haiku 5.5 reads only the excerpts around the hits. It must answer yes only if the excerpts show both (1) the event and (2) that the event is a reason for this action, its timing, or a delay in the competitive action.
- **Quote rule.** The tag's quote must name the event. The tool enforces this with a deterministic check: the quoted text itself must contain a keyword-gate hit.
- **Design data.** The method was designed only on documents from earlier rounds. It was frozen, with its prompt and patterns pinned by a hash in the tests, before any round-4 test document was judged or run.

### Test sets

- **Random set.** 400 random J&As, none of them used in any earlier round: 160 with at least one transcribed page and 240 without.
- **Enriched set.** 40 documents with a gate hit, not in the random set.
- **Snapshot.** The same dataset snapshot as round 3.
- **Answer key.** A full-text blind judgement of every document. The rules are those of round 3: Claude Opus 5.5 judges, no human labeller, and the judges never saw model output or gate hits.

### Results

| Measure | Value (Wilson 95%) |
|---|---|
| Precision, random set, run A | 11/12 = 92% (65–99%) |
| Precision, random set, borderline counted as yes | 12/12 (76–100%) |
| Precision, random set, run B | 10/12 = 83% (55–95%) |
| Recall, random set, run A | 11/16 = 69% (44–86%) |
| Recall, random set, run B | 10/16 = 62% (39–82%) |
| Precision, native-only / has a transcribed page (run A) | 6/7 (49–97%) / 5/5 (57–100%) |
| Recall, native-only / has a transcribed page (run A) | 6/8 (41–93%) / 5/8 (31–86%) |
| Prevalence in sample | 16/400 = 4.0% (2.5–6.4%); pool-weighted 4.6% (2.0–7.1%) |
| **Gate recall** (answer-key positives with any gate hit) | 11/16 = 69% (44–86%) |
| Gate hit rate | 21 of 400 random documents |
| Precision, enriched set (runs A / B) | 16/18 = 89% (67–97%) / 17/19 = 89% (69–97%) |
| Stability (tagged in both runs / tagged in either) | 29/32 = 91%; 3 of 440 documents flipped |
| Status | **screening**: the lower bound, 65%, is at least 50% but below 80% |

- **The gate limits recall.** Every random-set positive that the gate surfaced was tagged. All 5 misses had no gate hit. The documents described:
  - a departing employee who could not be backfilled, in wording the patterns do not cover;
  - Executive Orders not on the list;
  - a return-to-office space review;
  - two reorganisations described without the word "reorganisation" (a program office moving between directorates, and a program moving between agencies).
- **False tags.** Every random-set false tag in run A was a document the answer key marked borderline. The second run added one false tag on a document the key marked no.

### Agreement between the first and second judgements

- **Planned 10% sample.** It held 44 random documents, none of them a positive in either judgement. Kappa is therefore undefined. Raw agreement was 100% on yes vs not-yes and 95% across the three labels.
- **Supplementary re-judgement.** I added it after seeing that the planned sample held no positives. A fresh independent judge re-read all 57 gate-hit documents (the 21 random-set hits and the 40 enriched documents, less those already in the planned sample).
  - Agreement there was 95% on yes vs not-yes, with kappa 0.89 (29 vs 26 yes). Across the three labels it was 84%; most differences were between borderline and no.
  - The selection used only the keyword gate, not any label.

### Quote check

- **All quotes verified.** All 64 tags from the two runs had quotes found in the page text, re-checked by a second code path.
- **Image check.** 20 tags (10 on transcribed pages, 10 on native pages) were checked against images of the original pages.
  - **Quotes.** 19 of the 20 quotes are visible on the page.
  - **The 20th: text under a redaction box.** It is in the PDF's text layer but sits under a black redaction box: an improperly redacted PDF. **A quote can therefore come from text the agency meant to redact; check the page image before reusing any quote.** This applies to every category.
  - **Support.** On reading, 17 tags were supported by the document and 3 were borderline.

### What changed from round 3

On fresh random documents, the gated method's precision is 92% (lower
bound 65%), against 47% for the whole-document method. Recall is 69%, against
88%. The status moves from not shipped to screening. Gate coverage is the main
limit; widening the patterns would be a new method and would need its own test.
