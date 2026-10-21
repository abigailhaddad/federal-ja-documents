# The daily "WOAH! J&As" email digest (experimental)

Once a day, a short email with the few most remarkable J&A notices that the
collector picked up since the previous email: at most five, often three to
five, sometimes none. Each pick has a headline, a line with the agency,
vendor, value and authority, one to three sentences on why it is notable,
one or two verbatim quotes from the document with their page, and links to
the SAM.gov notice and the documents. After the picks (or after the one-line
"nothing WOAH-worthy" note), a compact "All new notices (N)" list has every
notice the email covered, one line each: agency, title, and vendor, value and
authority only where the extraction found them with a checked quote, with a
link to SAM.gov; picks first, marked with a star, then by value.

Code: `src/federal_ja/digest/`. Prompts and models:
[`src/federal_ja/digest/prompts.yaml`](../src/federal_ja/digest/prompts.yaml).
Workflow: [`.github/workflows/digest.yml`](../.github/workflows/digest.yml).
Sample digests from a 7-day backtest: [`docs/digest_samples/`](digest_samples/).

```sh
uv run python -m federal_ja.analysis digest --dry-run --out work/digest     # writes digest.md + picks.json
uv run python -m federal_ja.analysis digest --send                          # sends, then saves the state
```

`--dry-run` sends nothing, never calls Buttondown and never writes the state.
It needs `ANTHROPIC_API_KEY`; `--send` also needs `BUTTONDOWN_API_KEY` and
`HF_TOKEN`.

## Which notices

* **New since the last email.** A notice is new when its `first_seen_at`
  (when the collector first saw it, in `data/notices.parquet`) is later than
  the watermark saved with the last sent email. `posted_date` is not used for
  the window: SAM.gov's notice lists can surface a notice days after its
  posting date.
* **J&A-type notices:** every `Justification` notice, and every Special
  Notice the collector picked up by title whose title says it is a sole-source
  intent or justification. Special Notices picked up only because the title
  says "fair opportunity" (in practice competition-schedule bulletins, 18 of
  341 on 2026-10-21) are left out.
* **Not old news:** notices posted more than 14 days before the email
  (`--max-age-days`) are left out, and the email's footer says how many. When
  the collector picks up a yearly archive or a new collection path, hundreds of
  months-old notices get a new `first_seen_at` on one day (on 2026-10-20, 254
  of 362 newly collected notices were posted more than 14 days before that
  day's backtest run).
* Every left-out notice is counted by reason in `picks.json`.

All tables are read at one dataset commit (recorded in `picks.json`) with
DuckDB, only the needed columns and rows.

## What the model sees and does

For each candidate: the SAM.gov metadata; the fields from
`analysis/extractions.parquet` when the notice has a row (vendor, value,
authority, urgency trigger, bridge, prior extensions, letter contract,
funding, the model-written summary), labelled as hints; and an excerpt of its
text with page markers, the attachment whose filename looks like the
justification first. The SAM.gov description is included too (all of it when
the notice has no document text, as many Special Notices do). The digest
works without extraction rows; a notice first seen today usually gets one in
the same day's extraction run, which the digest waits for.

1. **Screen** (only when there are more than 20 candidates): Claude Haiku 5.5
   scores every candidate 0-10 from a ~1,800-character excerpt, in batches of
   40. The top 20 go on.
2. **Rank**: one Claude Sonnet 5.5 call (strict tool schema) with up to
   7,000 characters per candidate picks at most five and writes each one up.
   When a notice has several justification attachments (often an original
   and a revised version), they share a 1.5 times larger budget so each
   version's opening pages are seen; the prompt asks for the latest
   version's figures.

The rubric (in `prompts.yaml`): very large dollar values or extensions;
candid admissions of agency dysfunction (staff losses, deferred resignation,
"manpower reductions", a shutdown, missed deadlines); politically salient
urgency (Congress or oversight deadlines, litigation, executive orders);
unusual or aggressive authorities (urgency, letter contracts, national
security exceptions, new legislation such as the One Big Beautiful Bill
Act); notable vendors (large consultancies, AI or surveillance technology,
detention and border operations); weird or candid language; serial bridges
and protest-driven chaos. Routine buys (OEM spare parts, brand-name lab
equipment, software maintenance renewals, leases, small purchases) are
downranked unless something in them is genuinely surprising. The prompt says
to pick fewer, or none, rather than pad.

## Checks (code, not the model, decides what is sent)

`src/federal_ja/digest/verify.py`:

* **Quotes.** Every quote must be in the notice's own text (its document
  pages or its SAM.gov description), at least 5 words, no ellipsis, using the
  analysis package's `locate_quote`: exact, or after normalisation (curly
  quotes and dashes straightened, ligatures expanded, whitespace collapsed,
  line-wrap hyphens), or differing only in case; failing that, with words
  split across a line break rejoined ("reduc-/tion" as "reduction"). A
  failed quote is dropped; a pick with no quote left is dropped. The page in
  the email is where the code found the quote, and a machine-transcribed page
  is labelled.
* **Dollar figures** in the headline and the "why" must match an amount in
  one of the pick's verified quotes or the extraction's quote-checked
  `estimated_value_usd`: the same amount, or that amount rounded or truncated
  at the precision written ("$1.2B" or "$1.23 billion" for $1,229,179,038;
  "$170M" for $170,829,743). Totals, conversions and other arithmetic fail.
  When the model wrote a figure without quoting where it comes from, the code
  looks for a sentence of the document that contains a matching amount (not
  approval-threshold language) and adds it as a quote, marked "source of the
  figure above, quoted by the checks"; if there is none, the figure fails.
* **Other numbers** (digits) in the headline and "why" must appear in the
  notice's text or metadata.
* A "why" sentence that fails is removed; a failing headline drops the pick.
* **Summary line:** vendor, value and authority come from the extraction's
  quote-checked fields when there is a row; otherwise the model's, only if
  found in the text (a value only if it is an amount in a verified quote).
* The prompt tells the model to state only what the document says, with no
  speculation about motives or wrongdoing. Code cannot check that; the
  backtest below was read by hand for it.

Everything dropped is listed with its reason in `picks.json`
(`dropped_by_checks`), next to the model's raw answer.

## Sending and state

* **Buttondown:** `POST https://api.buttondown.com/v1/emails` with
  `Authorization: Token $BUTTONDOWN_API_KEY`, `status: about_to_send` (send
  now), `X-API-Version: 2026-04-01` (under which a missing status means a
  draft, never an accidental send) and `X-Buttondown-Live-Dangerously: true`
  (Buttondown requires it the first time an API key sends immediately). Any
  non-201 answer fails the run with Buttondown's error code; the key is never
  printed. `--buttondown-status draft` saves a draft instead (state
  unchanged).
* **State:** `analysis/digest_state.json` in the dataset: the watermark (the
  latest `first_seen_at` among the notices the last sent email looked at), the
  date it was sent, and the last 90 runs. It is written only after Buttondown
  accepted the email, so a dry run, a failure or a rerun never skips notices,
  and a rerun after a send finds nothing new. Kept in the dataset because the
  workflows already have `HF_TOKEN`, an upload is one atomic commit, nothing
  needs to push to this repository, and `publish --delete-stale` never
  touches `analysis/`. Each email carries its watermark in Buttondown
  metadata; if a send succeeded but saving the state failed, the next run
  finds that email in Buttondown and adopts its watermark instead of sending
  the same notices again.
* **Zero picks:** the email is the one line "Nothing WOAH-worthy today (N new
  notices reviewed)." so it is clear the job ran (`--zero-picks skip` sends
  nothing instead). With no new notices at all and an email already sent
  that day, nothing is sent.

## Web archive and RSS feed

Every sent email is also published at **https://woah-jas.abigailhaddad.com**
(Cloudflare Pages project `woah-jas`; added 2026-10-21): an index of all
issues (newest first: date, subject, number of picks), one page per issue
with the picks, their quotes and links and the "All new notices" list, an
about page, and an RSS 2.0 feed of the last 30 issues with their full content
(`/feed.xml`). Static HTML and one stylesheet, light and dark; no JavaScript,
cookies, trackers or external fonts. Every email's footer links to it.

* **Archive:** after a successful send (Buttondown accepted the email with
  `about_to_send`), right after the state file, the run saves the email to
  `analysis/digest_archive/YYYY-MM-DD.json` in the dataset (Eastern-time date;
  a second email the same day is `YYYY-MM-DD-2.json`): subject, date,
  `sent_at`, the Buttondown email id and URL, the dataset commit, the Markdown
  body as sent, the picks (headline, why, agency/vendor/value/authority line,
  quotes with their page, document links) and the "All new notices" rows. Dry
  runs, drafts and failed sends write nothing there (every run writes the
  entry it would archive to `archive_entry.json` under `--out`, for
  previews). A file is never overwritten; a rerun that finds the same email id
  for that day writes nothing; the upload is one commit made against the
  commit the name was chosen at, so two writers cannot take the same name.
  If the archive upload fails, the run fails (the state is already saved, so
  nothing is re-sent) and `archive_entry.json` in the run's artifact can be
  uploaded with
  `uv run python -m federal_ja.analysis digest-archive-add archive_entry.json`.
  A digest adopted from Buttondown by the recovery step (see above) is not
  archived automatically.
* **Site:** `uv run python -m federal_ja.analysis digest-site --out site/dist`
  reads every archive entry and writes the site (`--archive-dir DIR` reads a
  local directory instead, e.g. for a preview). Pages are rendered from the
  stored structure, not by parsing the Markdown, and all text (headlines,
  quotes, titles, summaries) is HTML-escaped; only http(s) links are kept.
  The issue pages show what was sent; nothing is regenerated.
* **Deploy:** `.github/workflows/deploy-site.yml` builds `site/dist` (an
  allow-list: the builder wipes it and writes only the site's files), runs
  `tests/test_site_dist.py` on the built tree (no file types other than
  html/css/xml/txt/svg/ico, no source/data/dot files, Cloudflare's 25 MiB per
  file and 20,000 files limits, no dangling links, no scripts or external
  assets), then deploys with `cloudflare/wrangler-action`. `digest.yml` calls
  it after every sent digest; it can also be run by hand (Actions, "Deploy
  digest web archive") to rebuild from the archive. Without the
  `CLOUDFLARE_API_TOKEN` secret it builds and checks the site, skips the
  deploy with a notice, and the email is unaffected.
* **Setup (once):** in the Cloudflare dashboard, My Profile, API Tokens,
  Create Token: the "Edit Cloudflare Workers" template, or a custom token with
  Account, Cloudflare Pages, Edit, limited to this account. Then
  `gh secret set CLOUDFLARE_API_TOKEN -R abigailhaddad/federal-ja-documents`
  and `gh secret set CLOUDFLARE_ACCOUNT_ID -R abigailhaddad/federal-ja-documents`.
* The 2026-10-21 issue, sent before the archive existed (with the earlier
  "Whoa J&As" subject prefix), was archived afterwards from that run's
  `picks.json` (whose body is the one Buttondown holds) and the notices and
  extractions at its dataset commit. Re-rendered, that reconstruction matches
  the sent picks and notice list line for line; the only difference is a note
  sentence about one-line summaries, added to the list after that email.

## When it runs

`.github/workflows/digest.yml`:

* after each "Daily experimental field extraction" run that the daily
  collection triggered (not manual backfill runs), whether it succeeded or
  failed, so the picks can use that day's extraction fields;
* after each successful "Daily J&A collection" run while the extraction job
  is off (`EXTRACT_DAILY_ENABLED` not `true`);
* a fallback schedule at 22:30 UTC, for days when neither fired (it sends
  only if nothing was sent that day, and then says how many new notices there
  were, possibly 0);
* by hand (`workflow_dispatch`, a dry run unless "dry_run" is unticked).

All triggers share the watermark, so a notice is covered once. Automatic runs
send only when the repository variable `DIGEST_ENABLED` is `true`; until then
the workflow-run triggers make a dry run and upload it as the `digest`
artifact, and the schedule does nothing. `workflow_run` and `schedule`
triggers only fire for the workflow file on the default branch.

## Cost

In the 7-day backtest ([`digest_samples/`](digest_samples/README.md)): $0.01-0.14
a day, $0.10 on average, almost all of it the one Sonnet ranking call (about
40,000 input tokens for 20 candidates, 750-1,700 output tokens); the Haiku
screen costs under a cent per 40 candidates. `--max-usd` (default 0.35) caps a
run: before each call the worst case (estimated input plus the full output
allowance) is checked against what is left, the ranking shortlist is cut until
it fits, and if even five candidates do not fit the run stops and sends
nothing.

## Turning it on

1. In Buttondown, create an API key (Settings, API) and store it:
   `gh secret set BUTTONDOWN_API_KEY -R abigailhaddad/federal-ja-documents`.
   The workflow also uses the existing `ANTHROPIC_API_KEY` and `HF_TOKEN`
   secrets (the token must be able to write to the dataset, for the state file).
2. Check a real send by hand first: Actions, "Daily J&A digest email", Run
   workflow, untick "dry_run". This sends one email and saves the state.
3. `gh variable set DIGEST_ENABLED -R abigailhaddad/federal-ja-documents --body true`.

To stop: set `DIGEST_ENABLED` to anything else. To start over from scratch,
delete `analysis/digest_state.json` from the dataset; the next run then
covers the notices first seen in the last 36 hours.

## Backtest

See [`digest_samples/README.md`](digest_samples/README.md).

### Delivery time (2026-10-21)

The run finishes around 3am ET (after the daily collection), but the email is created as a Buttondown **scheduled** email for **7:00 AM ET** (`--send-at`, default `07:00`). A run that finishes after 7am (the 22:30 UTC fallback) sends at once. Collecting later would not catch more notices: SAM.gov refreshes its active-notices file once a night (~03:30 UTC), and about 1% of J&As are posted between midnight and 6am ET.
