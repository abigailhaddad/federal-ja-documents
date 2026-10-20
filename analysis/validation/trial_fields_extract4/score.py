"""Score the five-field trial (strict, no adjudication). Usage, from this directory: python score.py ."""
import json, re, sys
from pathlib import Path
from collections import Counter
S = Path(sys.argv[1])
sel = dict(l.split() for l in open(S/'sample.txt') if l.strip()); ids = list(sel)
K = {json.loads(l)['notice_id']: json.loads(l) for l in open(S/'answer_key.jsonl')}
sq = lambda s: re.sub(r'[^0-9a-z]', '', (s or '').lower())
def load(p): return {json.loads(l)['notice_id']: json.loads(l) for l in open(p)}
runs = {'A': load(S/'runs/run_A_extractions.jsonl'), 'B': load(S/'runs/run_B_extractions.jsonl')}
base = load(S/'runs/baseline_extract3_extractions.jsonl')
def w(k, n): return f"{k}/{n}" if n else "n/a"
def same(a, b):
    na, nb = re.findall(r'\d{4,5}', a or ''), re.findall(r'\d{4,5}', b or '')
    if na and nb: return bool(set(na) & set(nb))
    a, b = sq(a), sq(b)
    return bool(a and b and (a in b or b in a))
out = {}
for run, R in runs.items():
    res = {}
    def scalar(name, kf, pf, eq=lambda a, b: a == b, positive=lambda v: v is not None):
        c = Counter(); errs = []
        for i in ids:
            k = K[i][kf]; acc = [k.get('value')] + (k.get('acceptable') or []); p = R[i]['fields'].get(pf)
            ok = any((p is None and v is None) or (p is not None and v is not None and eq(p, v)) for v in acc)
            kpos, ppos = positive(k.get('value')), positive(p)
            c['ok' if ok else 'wrong'] += 1
            if ppos: c['pred_pos'] += 1; c['pred_pos_ok'] += ok
            if kpos: c['key_pos'] += 1; c['key_pos_found'] += ok and ppos
            if not ok: errs.append((i[:8], sel[i], k.get('value'), k.get('acceptable'), p))
        return {'accuracy': w(c['ok'], len(ids)), 'precision': w(c['pred_pos_ok'], c['pred_pos']), 'recall': w(c['key_pos_found'], c['key_pos']), 'errors': errs}
    res['bridge_or_extension'] = scalar('b', 'bridge_or_extension', 'bridge_or_extension', positive=lambda v: v == 'yes')
    res['prior_extension_count'] = scalar('p', 'prior_extension_count', 'prior_extension_count')
    res['is_letter_contract'] = scalar('l', 'is_letter_contract', 'is_letter_contract', positive=lambda v: v == 'yes')
    res['urgency_trigger'] = scalar('u', 'urgency_trigger', 'urgency_trigger')
    def lst(kf, pf, key):
        tp = npred = nk = 0; diffs = []
        for i in ids:
            kk = [x['value'] for x in K[i][kf]]; pp = [x[key] for x in R[i]['fields'].get(pf) or []]
            used = set(); m = 0
            for p in pp:
                j = next((j for j, k in enumerate(kk) if j not in used and same(p, k)), None)
                if j is not None: used.add(j); m += 1
            tp += m; npred += len(pp); nk += len(kk)
            if m != len(pp) or m != len(kk): diffs.append((i[:8], sel[i], kk, pp))
        return {'precision': w(tp, npred), 'recall': w(tp, nk), 'diffs (key, ours)': diffs}
    res['contract_types'] = lst('contract_types', 'contract_types', 'stated')
    res['funding_source_named'] = lst('funding_source_named', 'funding_sources', 'name')
    res['policy_citations'] = lst('policy_citations', 'policy_citations', 'citation')
    cf = Counter(); cfe = []
    for i in ids:
        for p in R[i]['fields'].get('policy_citations') or []:
            k = next((k for k in K[i]['policy_citations'] if same(p['citation'], k['value'])), None)
            if k:
                ok = p['cited_for'] == k['cited_for'] or p['cited_for'] in (k.get('acceptable_cited_for') or [])
                cf['ok' if ok else 'bad'] += 1
                if not ok: cfe.append((i[:8], p['citation'], k['cited_for'], p['cited_for']))
    res['policy_cited_for_on_matched'] = {'accuracy': w(cf['ok'], cf['ok'] + cf['bad']), 'errors': cfe}
    costs = [(R[i]['usage']['cost_usd'], R[i]['usage'].get('facts', {}).get('cost_usd', 0), base[i]['usage']['cost_usd']) for i in ids]
    res['cost'] = {'trial_total': round(sum(c[0] for c in costs), 4), 'facts_call_total': round(sum(c[1] for c in costs), 4),
                   'main_call_total': round(sum(c[0] - c[1] for c in costs), 4), 'base_extract3_total': round(sum(c[2] for c in costs), 4),
                   'facts_per_notice': round(sum(c[1] for c in costs) / len(ids), 5)}
    out[run] = res
print(json.dumps(out, indent=1, default=str))
