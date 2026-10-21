# Digest backtest, 2026-10-15 to 2026-10-21 (dry runs)

Seven digests built by `digest --dry-run` on GitHub Actions (prompt
`2026-10-21.digest2`, the final code of this branch), one per day, all read at
dataset commit `e93882dcc18c1d76244974093b3696fd5e5e016e`. Nothing was sent.
Each day has the email as it would go out (`<day>.md`) and the full run record
(`<day>.picks.json`: candidates, exclusions, screen scores, the model's raw
answer, what the checks dropped, token use and cost).

## Windows

The collector's first full load stamped 10,487 notices with `first_seen_at`
2026-10-16, so the two days before 2026-10-17 are simulated by POSTING date
within that load; the other days use the real `first_seen_at` windows.

| Email date | Notices covered | Candidates | Ranked | Picks | Cost |
|---|---|---|---|---|---|
| Oct 15 | posted Oct 14 (bulk load of Oct 16) | 34 | 17 (screened) | 5 | $0.135 |
| Oct 16 | posted Oct 15 (bulk load of Oct 16) | 27 | 17 (screened) | 5 | $0.137 |
| Oct 17 | first seen Oct 17 | 17 | 17 | 4 | $0.123 |
| Oct 18 | first seen Oct 18 (77 older postings left out) | 12 | 12 | 2 | $0.060 |
| Oct 19 | first seen Oct 19 | 1 | 1 | 0 | $0.011 |
| Oct 20 | first seen Oct 20 (254 older postings and 18 "fair opportunity" bulletins left out) | 90 | 19 (screened) | 3 | $0.121 |
| Oct 21 | first seen Oct 21 | 34 | 20 (screened) | 5 | $0.122 |

Total $0.71 for the seven days (mean $0.10/day). The backtest ran with
`--max-usd 0.18` per day so that it and an earlier run of the same seven days
(first prompt version, $0.74) stayed under $2.00 together even in the worst
case; on the larger days that cap trimmed the ranking shortlist from 20 to 17-19
candidates. The production default (`--max-usd 0.35`) sends all 20.

Extraction rows: at this dataset commit the notices first seen Oct 18-21 had
none yet (the extraction backfill was running; 31 of 34, 25 of 27 and 17 of 17
candidates on Oct 15-17 had one), so Oct 18-21 were picked from document text
alone and their "All new notices" lists show no
vendor or value. In production the digest runs after the day's extraction.

Subjects:

* Whoa J&As — Oct 15: HUD sole-sources $170M property-preservation contracts after vendors suspended operations over unpaid invoices
* Whoa J&As — Oct 16: NIEHS blames DOGE executive order and staffing changes for missed epidemiology recompete
* Whoa J&As — Oct 17: HHS extends Deloitte repatriation support six months, citing recent real-world emergency repatriations
* Whoa J&As — Oct 18: Navy sole-sources more KPMG audit, AI and budget-system work, citing Congress and SecDef urgency
* Whoa J&As — Oct 19: nothing whoa-worthy
* Whoa J&As — Oct 20: ICE definitizes Camp East Montana letter contract with Amentum at about $2.8 billion
* Whoa J&As — Oct 21: CBP sole-sources contract attorneys to review body-camera footage for litigation, citing unusual urgency

## How the samples were checked

* **Quotes, a second way.** An independent script (not the repo's
  `normalize`/`locate_quote`) read the page text from `data/pages.parquet` /
  `data/attachments.parquet` / the SAM.gov description at the same commit,
  reduced quote and page to lowercase alphanumeric tokens, and looked for the
  quote's token sequence in one page (or two consecutive pages). All 31 quotes
  in the seven digests were found, every one on the page the email shows.
* **Every claim by hand.** For each of the 24 picks, each fact in the
  headline and "why" (dates, counts, durations, dollar figures, named
  programs, causes) was looked up in the page text with targeted searches and
  read in context.

## Quality assessment

The picks are mostly the kind of thing the rubric asks for: candid admissions
(an executive order on DOGE delaying a recompete; three FY2026 appropriations
lapses and furloughs; shutdowns and departed developers delaying DHS's HSIN;
VA staffing shortages), politically salient urgency (CBP contract attorneys
for body-worn camera footage in litigation; ICE detainee medical claims after
the VA "abruptly terminated" support; an executive-order approval rule forcing
a bridge), big or notable vendors (Amentum at Camp East Montana at $2.8B,
KPMG, Deloitte, Pen-Link wiretap software with a generative AI assistant), and
protest-driven bridges. Oct 19 (one routine notice) correctly got the
"nothing whoa-worthy" email, and Oct 18 picked only two of 12.

Problems found:

* **One overstated headline** (Oct 21, #5, NASA/EraDrive): "a correction
  admits a competitor's response was missed". The correction says the JOFOC's
  statement that no capability statements were received was a scrivener's
  error and that the one received was evaluated before approval; the response
  was left out of the write-up, not missed in the evaluation. The "why" text
  under it is accurate.
* **Mild wording.** "Forced airlifts" (DLA: the document says "the critical
  need to utilize airlifts"); "missed" recompete (NIEHS: the document says the
  delays affected the ability to recompete before the contract ends). The
  first prompt version also wrote "vendors quit" for "vendors suspending
  operations"; the final prompt's instruction to keep the document's strength
  of language fixed that one.
* **Some picks are only moderately remarkable** (a $5M DTRA reprocurement
  after a default termination, a FLETC roleplayer bridge after a GAO protest,
  a VA lab-analyzer bridge). The prompt allows fewer than five but the model
  usually fills five on days with 25+ candidates.
* **Dropped by the checks:** HHS's notice of intent to sole-source Claude
  enterprise access (Oct 20) was picked but came back with no quote, so the
  pick was dropped. In the first run, the Camp East Montana and HUD picks
  were dropped because their headline figures had no quoted source; that led
  to the figure-source rule (the code quotes the document sentence holding
  the amount) and the truncation rule ("$170M" for $170,829,743).
* **Multi-version notices.** The Camp East Montana notice holds both the
  original letter-contract J&A ($1,229,179,038.02) and the revised one
  ($2,796,540,325.26). The first run's headline used the older figure; the
  final code shows the model both versions and the prompt asks for the
  latest, and the Oct 20 sample uses $2.8B from the revised J&A.
* **Reposts.** The same ICE Hootsuite buy was posted as two notices (Oct 18
  and Oct 21); the first run picked it on both days. There is no
  cross-day de-duplication.
