"""Second check of the kept quotes, by a different code path from the
extractor's: each kept quote of the list fields (the facts kinds, contract
types, funding sources, market-research methods) and of people is searched
in the attachments table's whole-attachment text (not the pages table the
extractor uses), comparing letters and digits only.
Usage: python quote_check.py . <dir with attachments.parquet at 8b7d8208fef6>"""
import json, re, sys
from pathlib import Path
import pandas as pd
S = Path(sys.argv[1]); A = pd.read_parquet(Path(sys.argv[2])/'attachments.parquet', columns=['notice_id', 'text'])
sq = lambda s: re.sub(r'[^0-9a-z]', '', (s or '').lower())
# A word split across a line ("non-\nnegotiable") loses only the hyphen here.
text = {n: sq(re.sub(r'-\s*\n\s*', '', ' '.join(g.text.fillna('')))) for n, g in A.groupby('notice_id')}
SINGLE = ('need_by_quote', 'stated_pop_limit_quote', 'incumbent_piid_quote', 'redaction_significant_quote',
          'bridge_or_extension_quote', 'letter_contract_quote', 'prior_extension_count_quote', 'urgency_trigger_quote')
out = {}
for x in 'AB':
    n = found = 0; missing = []
    for l in open(S/'runs'/f'run_{x}_extractions.jsonl'):
        r = json.loads(l); f = r['fields']; t = text.get(r['notice_id'], '')
        qs = [f.get(k) for k in SINGLE if f.get(k)]
        qs += [m['quote'] for k in ('contract_types', 'funding_sources', 'market_research_methods') for m in f.get(k) or []]
        qs += [p['name_quote'] for p in f['people']]
        for q in qs:
            n += 1
            if sq(q) in t: found += 1
            else: missing.append((r['notice_id'][:8], q))
    out[f'run_{x}'] = {'kept_quotes': n, 'found': found, 'not_found': missing}
print(json.dumps(out, indent=1))
