# Extras: a second call for facts the extraction doesn't ask for

EXPERIMENTAL. `python -m federal_ja.analysis extras` makes one more model call per notice and stores the answers on their own, in `extras.jsonl` and `extras.csv`. It never reads or writes the extraction records.

## Why a second call

Adding fields to the extraction prompt means a new `EXTRACT_VERSION`. A new version makes every stored record out of date, the whole table is extracted again, and every published value is replaced by a fresh answer. Two runs of the same prompt don't give the same text: in the extract6 validation (49 notices, runs A and B) the dollar value never changed, the vendor's spelling changed in 4 and the summary in all 49. This call has its own version (`extras.version` in `prompts.yaml`), so adding to it changes nothing already published.

It costs a second read of the document. The estimate before any run (2026-11-10, from stored token counts, not measured) was about $11 for the 12,295 extracted notices in batch mode, against about $32 for a full re-extraction.

## What it asks for

One list of `{kind, value, quote}` entries.

| kind | what |
|---|---|
| `increase_amount` | the dollars this action adds to an existing contract or order |
| `prior_ceiling`, `new_ceiling` | the ceiling before and after, when the document states them |
| `not_to_exceed` | a maximum given beside a lower estimate ("$782,650.53 (NTE $900K)") |
| `unit_price`, `quantity` | when the action is priced per unit ("$19,166.00 each, quantity 2") |
| `stated_total` | a whole-action total the document prints beside a per-year or per-unit figure |
| `incumbent_vendor` | the firm holding the current or previous contract |
| `approved_source` | one entry per firm the document lists as an approved source, with CAGE code if given |
| `interested_source` | one entry per firm listed as having expressed interest |
| `barrier_action` | what the document says will be done to open competition later |

## The rules behind it

These came out of comparing two extractions of the same documents and finding where a single "value" misled.

1. Show only a figure the document prints. No arithmetic. A lease that states annual rent "plus annual Operating Cost Escalations" has no term total we can compute.
2. Every figure needs a verbatim quote found in the document, with the amount in the quote.
3. A figure is shown with what it is: a ceiling increase, a per-year rent, a unit price. Never a bare "value".
4. For a ceiling increase the headline is the increase; the new ceiling is shown second.
5. For an estimate with a not-to-exceed, the headline is the estimate.
6. A unit price is shown as "each" with the quantity. They are not multiplied.

So `unit_price` × `quantity` is never stored, and `stated_total` exists only for totals the document itself writes out.

## What the code checks

An entry is kept only if its quote is found in the document text and the value is visible in the quote: the amount parses out of it, the firm's name and CAGE code are in it, the quantity is in it. A `not_to_exceed` that is really an approval threshold ("exceeding $900,000 but not exceeding $20 million") is dropped. Everything dropped is listed in `values_dropped_by_checks` with the reason.

When a record has both ceilings and the increase, `ceiling_arithmetic_ok` says whether new = prior + increase. Some documents don't add up. Their figures are kept as written and the flag is false.

An empty list means the model found none of these facts in the text it was given. It does not mean the document has none: a scanned form whose text layer lost its tables reads as empty.

## Running it

```
python -m federal_ja.analysis extras --out work/extras --notice-file ids.txt --max-usd 0.50 --dry-run
python -m federal_ja.analysis extras --out work/extras --notice-file ids.txt --max-usd 0.50 --batch
```

`--max-usd` is applied to an estimate before anything is sent, in both modes. The estimate's output size (`EST_OUTPUT_TOKENS` in `extras.py`) is a guess until a pilot measures it.

Not built yet: publishing the table to the dataset, a workflow, and reading the DLA checkbox codes (AMC/AMSC) by rule.
