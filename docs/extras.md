# Extras: a second call for facts the extraction doesn't ask for

EXPERIMENTAL. `python -m federal_ja.analysis extras` makes one more model call per notice and stores the answers on their own, in `extras.jsonl` and `extras.csv`. It never reads or writes the extraction records.

## Why a second call

Adding fields to the extraction prompt means a new `EXTRACT_VERSION`. A new version makes every stored record out of date, the whole table is extracted again, and every published value is replaced by a fresh answer. Two runs of the same prompt don't give the same text: in the extract6 validation (49 notices, runs A and B) the dollar value never changed, the vendor's spelling changed in 4 and the summary in all 49. This call has its own version (`extras.version` in `prompts.yaml`), so adding to it changes nothing already published.

It costs a second read of the document. A pilot of the first version (extras1, 160 notices, batch, 2026-11-10) cost $0.0949, which is about $7 for the 12,295 extracted notices, against an estimated $32 for a full re-extraction.

That pilot is also why the list below changed. Of about 50 kept facts read against their quotes, the source lists and ceiling changes held up, two of twelve "incumbents" were only the proposed contractor, one notice's "interested sources" were accreditation bodies the document merely listed, and `stated_total` picked up prior awards and figures that were really increases. extras2 drops `stated_total` and makes the code check what the quote shows for the other two.

## What it asks for

One list of `{kind, value, quote}` entries.

| kind | what |
|---|---|
| `increase_amount` | the dollars this action adds to an existing contract or order |
| `prior_ceiling`, `new_ceiling` | the ceiling before and after, when the document states them |
| `not_to_exceed` | a maximum given beside a lower estimate ("$782,650.53 (NTE $900K)") |
| `unit_price`, `quantity` | when the action is priced per unit ("$19,166.00 each, quantity 2") |
| `incumbent_vendor` | the firm holding the current or previous contract; the quote has to say so |
| `approved_source` | one entry per firm the document lists as an approved source, with CAGE code if given |
| `interested_source` | one entry per firm the document says expressed interest |
| `barrier_action` | what the document says will be done to open competition later |
| `source_approval_path` | a statement that a firm not yet approved can apply or offer an alternate item |
| `data_rights_basis` | which technical-data reason blocks competition: `government_does_not_own_data`, `government_lacks_rights`, `data_not_available`, `data_uneconomical_to_obtain`, `proprietary_to_contractor` |
| `cost_to_compete` | a dollar figure for bringing in another source (reverse engineering, data purchase, qualification, duplication of cost) |
| `delay_to_compete` | how long the document says that would take |

## Read by rule, no model call

`extras.rule_fields` reads from the text: GAO protest numbers (`B-424920`), Court of Federal Claims case numbers (only in a document that names the court), NSNs, and whether FAR 52.217-8 is cited.

For the Defense Logistics Agency's "AVN Form S53" (about 1,300 notices), `dla_form.read` gives the ticked AMC code and AMSC letters and the form's two source tables. Nine in ten of these forms are scans, so the ticks come from a transcription. Checked against page images on 2026-11-10 (36 forms): the AMC matched in 31 of the 32 with tick marks, and three of six forms whose text showed two AMC ticks had only one on the page. So a code is given only when exactly one AMC is ticked in one tick style, and every part is `None` when its guard fails. `None` means not read. On the 1,316 forms in the dataset that day the code was read on 1,211, the approved table on 1,199 and the interested table on 1,008 (453 with firms, 555 an explicit "none").

The model's two source lists and the form's two tables are independent readings. `sources_match_form` says, per list, whether they name the same CAGE codes. A list that doesn't match shouldn't be shown.

The table's `approved_sources_confirmed` and `interested_sources_confirmed` are the lists to show. On a DLA form a list is there only when the form's own table was read and agrees. Off the form, interested firms are kept, since each has a quote saying it responded; "approved source" is not, because there the phrase turned up as "FDA approved" and "only approved reseller".

## Checks added after the pilots

Three pilots on 2026-11-10 (160 chosen notices twice, then 150 at random). The checks they led to, all applied to stored answers with `reverify`, no new calls:

- A quote that sits beside an empty tick box everywhere it appears is dropped. On the DLA form, item 11's "technical data is not available..." line has an empty box on 278 of 1,316 forms.
- An increase needs a quote that says it is one ("The government estimate is $1,801,453.52" came back as an increase).
- A prior or new ceiling needs a quote that says ceiling, maximum, total value or "from X to Y" (a row of nine bare figures came back as a prior ceiling).
- An incumbent's "extend" has to be about a contract, order or period ("the only source qualified to extend their marketing efforts" came back as an incumbent).

## The rules behind it

These came out of comparing two extractions of the same documents and finding where a single "value" misled.

1. Show only a figure the document prints. No arithmetic. A lease that states annual rent "plus annual Operating Cost Escalations" has no term total we can compute.
2. Every figure needs a verbatim quote found in the document, with the amount in the quote.
3. A figure is shown with what it is: a ceiling increase, a per-year rent, a unit price. Never a bare "value".
4. For a ceiling increase the headline is the increase; the new ceiling is shown second.
5. For an estimate with a not-to-exceed, the headline is the estimate.
6. A unit price is shown as "each" with the quantity. They are not multiplied.

So `unit_price` × `quantity` is never stored.

## What the code checks

An entry is kept only if its quote is found in the document text and the value is visible in the quote: the amount parses out of it, the firm's name and CAGE code are in it, the quantity is in it. A `not_to_exceed` that is really an approval threshold ("exceeding $900,000 but not exceeding $20 million") is dropped. Everything dropped is listed in `values_dropped_by_checks` with the reason.

When a record has both ceilings and the increase, `ceiling_arithmetic_ok` says whether new = prior + increase. Some documents don't add up. Their figures are kept as written and the flag is false.

An empty list means the model found none of these facts in the text it was given. It does not mean the document has none: a scanned form whose text layer lost its tables reads as empty.

## Running it

```
python -m federal_ja.analysis extras --out work/extras --notice-file ids.txt --max-usd 0.50 --dry-run
python -m federal_ja.analysis extras --out work/extras --notice-file ids.txt --max-usd 0.50 --batch
```

`--max-usd` is applied to an estimate before anything is sent, in both modes. The estimate is set above the largest answer the extras1 pilot produced, so a run spends less than its cap.

Not built yet: publishing the table to the dataset, and a workflow.
