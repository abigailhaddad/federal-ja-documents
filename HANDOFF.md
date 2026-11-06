# Handoff: federal-ja-documents

## What this is
Justification notices from SAM.gov (why a federal agency skipped competition), with the original files, extracted text and LLM-extracted fields. A daily email digest ("WOAH! J&As") and a small archive site come with it.

- Code: https://github.com/abigailhaddad/federal-ja-documents (README: sources, tables, how collection works)
- Data: https://huggingface.co/datasets/abigailhaddad/federal-ja-documents (the dataset card has the columns and known limits)
- Sites: https://federal-ja-documents.abigailhaddad.com (browse the data), https://woah-jas.abigailhaddad.com (email archive)

## What runs on its own
Triggers and schedules are at the top of each file in `.github/workflows/`.

- `daily.yml`: collects new notices, files, text and award matches, and publishes to Hugging Face.
- `extract-daily.yml`: LLM field extraction after collection (experimental).
- `digest.yml`: picks the day's notable notices and sends the email through Buttondown. See `docs/digest.md`.
- `deploy-site.yml`: rebuilds the email archive site on Cloudflare Pages.
- `tests.yml`: tests on every push.
- `analysis.yml`, `sam-awards-backfill.yml`: manual only.

`daily.yml` is started by a Cloudflare Worker in `cron-trigger/` (deploy command at the top of `wrangler.toml`), because GitHub's own schedule fired hours late. `daily.yml` also has a GitHub schedule as a backup, which runs only when `DAILY_ENABLED` is `true`.

## Secrets and variables
Repository secrets (none transfer with the repo): `HF_TOKEN` (write access to the dataset), `ANTHROPIC_API_KEY`, `BUTTONDOWN_API_KEY` (digest only), `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` (site deploy only). The Worker has its own secret, `GITHUB_TOKEN`; see the top of `cron-trigger/worker.js`.

Variables: `DAILY_ENABLED`, `EXTRACT_DAILY_ENABLED`, `DIGEST_ENABLED` (each `true` turns that part on), plus optional `ANTHROPIC_WORKSPACE_ID` and `CLOUDFLARE_PROJECT` (default `woah-jas`).

## If you take it over
- Most owner names live in `src/federal_ja/collect/config.py`; each default can be set with an environment variable (`JA_GITHUB_REPO`, `JA_HF_REPO`, `JA_USASPENDING_DATASET`, `JA_SITE_URL`, `JA_BUTTONDOWN_USER`). The rest: `cron-trigger/wrangler.toml` and the docs. To find them all: `grep -rn abigailhaddad --exclude-dir=.git .`
- Create: a Hugging Face dataset and token, a Cloudflare Pages project and Worker, a Buttondown account, and the variables above.
- Award matching reads a second dataset, `abigailhaddad/usaspending-bulk-awards` (public). Point `JA_USASPENDING_DATASET` elsewhere if you copy it.
- Seed an empty dataset with the first-publish step in the README ("Running it"). The daily job starts by downloading the dataset's tables, and I haven't tried it against an empty one.
- PDF text is read with `pdftotext` (poppler), falling back to `pypdf`. `pages.text_engine` says which read each page; older pages used `pypdf`. `JA_PDF_ENGINE=pypdf` forces the old reader.

## When it breaks
- Failed runs show in the Actions tab. Nothing sends alerts, so turn on GitHub notifications for the repo.
- Rerun by hand from the Actions tab (every workflow here has "Run workflow").
- Collection reads SAM.gov through `src/federal_ja/collect/sam_api.py`, `sam_award.py` and `sources.py`. The failed step's log says which call failed.
- Email problems: `docs/digest.md`.

## What it costs
The LLM calls are in `daily.yml` (transcribing scanned pages), `extract-daily.yml`, `digest.yml` and `analysis.yml` (tagging). Each passes a spend cap (`--max-usd`; defaults in the workflow files: 2, 2, 0.35 and 40). Actual spend is recorded in the dataset: `transcription_cost_usd` in `data/attachments.parquet` and `usage.cost_usd` inside `record_json` in `analysis/extractions.parquet`. The digest's cost is in `docs/digest.md`.
