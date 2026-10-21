"""extract6 against extract5 on what the extract6 fixes were for, plus cost
and agreement of the core fields with the extract3 runs. Reads the stored
runs only (no model calls). Usage, from this directory: python compare.py ."""
import json, sys
from pathlib import Path
from collections import Counter
S = Path(sys.argv[1]).resolve(); V = S.parent
X3 = V/'field_extraction_extract3'; T = V/'trial_fields_extract4'
rnd = [l.split()[0] for l in open(X3/'sample_random.txt') if l.strip()]
edge = [l.split()[0] for l in open(X3/'sample_edge.txt') if l.strip()]
trial = [l.split()[0] for l in open(T/'sample.txt') if l.strip()]
sample = [l.split()[0] for l in open(S/'sample.txt') if l.strip()]
def load(p): return {json.loads(l)['notice_id']: json.loads(l) for l in open(p)} if p.exists() else {}
def fails(p): return [json.loads(l) for l in open(p)] if p.exists() else []
runs = {f'extract6_{x}': S/'runs' for x in 'AB'} | {f'extract5_{x}': V/'one_call_extract5'/'runs' for x in 'AB'}
x3 = {x: load(X3/'runs'/f'run_{x}_extractions.jsonl') for x in 'AB'}
CORE = ('is_ja', 'doc_kind', 'estimated_value_usd', 'value_basis', 'value_redacted', 'vendor',
        'signature_date', 'period_of_performance', 'redacted', 'covers_multiple_actions')
out = {}
for name, d in runs.items():
    x = name[-1]; R = load(d/f'run_{x}_extractions.jsonl'); F = fails(d/f'run_{x}_failures.jsonl')
    res = {'extracted': len(R), 'of': len(sample), 'not_in_sample': sorted(set(R) - set(sample)),
           'failures': [(f['notice_id'][:8], f['error']) for f in F]}
    reasons = Counter(); invented = []
    for i, r in R.items():
        for dd in r['dropped']:
            reasons[(dd['field'], dd['reason'])] += 1
            if dd['reason'] == 'unknown fact kind':
                invented.append((i[:8], dd.get('value')))
    res['invented_kind_entries'] = len(invented); res['invented_kinds'] = invented
    res['dropped_by_reason'] = {f'{a}: {b}': n for (a, b), n in sorted(reasons.items())}
    ice = R.get('53d80bf144804de8a5e5138dd62cf873')
    res['ICE_53d80bf1_funding_sources'] = [f['name'] for f in ice['fields']['funding_sources']] if ice else 'not extracted'
    res['ICE_53d80bf1_raw_funding_facts'] = [f for f in ice['raw']['facts'] if f['kind'] == 'funding_source'] if ice else None
    ice2 = R.get('8febc2f3caa144d8b08672d26fbd2a1b')
    res['ICE_8febc2f3_funding_sources'] = [f['name'] for f in ice2['fields']['funding_sources']] if ice2 else 'not extracted'
    va = R.get('085682f126fe48469dc67e2031a3f08a')
    res['VA_CCN_085682f1'] = {'output_tokens': va['usage']['output_tokens'], 'calls': va['usage']['calls'],
                              'cost_usd': va['usage']['cost_usd'], 'people': len(va['fields']['people']),
                              'answer_chars': len(json.dumps(va['raw']))} if va else 'not extracted'
    d72 = R.get('d7207bb1b58346dc88493972f641561b')
    res['d7207bb1_stated_pop_limit_days'] = d72['fields']['stated_pop_limit_days'] if d72 else 'not extracted'
    def cost(ids):
        have = [i for i in ids if i in R]
        if not have: return None
        u = [R[i]['usage'] for i in have]
        return {'n': len(have), 'usd_per_notice': round(sum(z['cost_usd'] for z in u) / len(have), 5),
                'output_tokens_per_notice': round(sum(z['output_tokens'] for z in u) / len(have)),
                'calls': sum(z['calls'] for z in u)}
    res['cost_random24'] = cost(rnd)
    res['cost_random_and_edge'] = cost([i for i in rnd + edge if i != 'b5c3c23b4e65464dadfc362f9fc364da'])
    res['cost_trial17_without_va_ccn'] = cost([i for i in trial if i != '085682f126fe48469dc67e2031a3f08a'])
    res['cost_all_extracted'] = cost(sample)
    res['projected_full_pass_10033_usd_at_random24_rate'] = round(res['cost_random24']['usd_per_notice'] * 10033, 1)
    agree = {}
    for y, X in x3.items():
        n = ok = 0
        for i in rnd + edge:
            if i in R and i in X:
                for c in CORE:
                    n += 1; ok += R[i]['fields'].get(c) == X[i]['fields'].get(c)
        agree[f'vs_extract3_{y}'] = f'{ok}/{n}'
    res['core_fields_agreement'] = agree
    res['spent_usd_recorded'] = round(sum(r['usage']['cost_usd'] for r in R.values()), 4)
    out[name] = res
# run-to-run agreement of the core fields within each prompt
for p in ('extract6', 'extract5'):
    a = load(runs[f'{p}_A']/'run_A_extractions.jsonl'); b = load(runs[f'{p}_B']/'run_B_extractions.jsonl')
    n = ok = 0
    for i in rnd + edge:
        if i in a and i in b:
            for c in CORE:
                n += 1; ok += a[i]['fields'].get(c) == b[i]['fields'].get(c)
    out[f'{p}_A_vs_B_core_fields'] = f'{ok}/{n}'
print(json.dumps(out, indent=1, default=str))
