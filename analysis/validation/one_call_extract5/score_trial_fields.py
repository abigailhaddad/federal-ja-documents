"""Score the one-call runs (prompt extract5) on the extract4 trial fields,
against the trial's answer key, with the trial's strict scoring
(trial_fields_extract4/score.py), minus policy_citations (dropped in
extract5). Notices a run failed on are left out and listed (n_scored).
Also scores the trial's two-call runs on the same notices, for comparison.
Usage, from this directory: python score_trial_fields.py ."""
import json, re, sys
from pathlib import Path
from collections import Counter
S = Path(sys.argv[1]).resolve(); T = S.parent/'trial_fields_extract4'
sel = dict(l.split() for l in open(T/'sample.txt') if l.strip()); ids = list(sel)
K = {json.loads(l)['notice_id']: json.loads(l) for l in open(T/'answer_key.jsonl')}
sq = lambda s: re.sub(r'[^0-9a-z]', '', (s or '').lower())
def load(p): return {json.loads(l)['notice_id']: json.loads(l) for l in open(p)}
runs = {'one_call_A': load(S/'runs/run_A_extractions.jsonl'), 'one_call_B': load(S/'runs/run_B_extractions.jsonl'),
        'two_call_A': load(T/'runs/run_A_extractions.jsonl'), 'two_call_B': load(T/'runs/run_B_extractions.jsonl')}
def w(k, n): return f"{k}/{n}" if n else "n/a"
def same(a, b):
    na, nb = re.findall(r'\d{4,5}', a or ''), re.findall(r'\d{4,5}', b or '')
    if na and nb: return bool(set(na) & set(nb))
    a, b = sq(a), sq(b)
    return bool(a and b and (a in b or b in a))
out = {}
for run, R in runs.items():
    have = [i for i in ids if i in R]
    res = {'n_scored': len(have), 'not_extracted': [(i[:8], sel[i]) for i in ids if i not in R]}
    def scalar(kf, pf, eq=lambda a, b: a == b, positive=lambda v: v is not None):
        c = Counter(); errs = []
        for i in have:
            k = K[i][kf]; acc = [k.get('value')] + (k.get('acceptable') or []); p = R[i]['fields'].get(pf)
            ok = any((p is None and v is None) or (p is not None and v is not None and eq(p, v)) for v in acc)
            kpos, ppos = positive(k.get('value')), positive(p)
            c['ok' if ok else 'wrong'] += 1
            if ppos: c['pred_pos'] += 1; c['pred_pos_ok'] += ok
            if kpos: c['key_pos'] += 1; c['key_pos_found'] += ok and ppos
            if not ok: errs.append((i[:8], sel[i], k.get('value'), k.get('acceptable'), p))
        return {'accuracy': w(c['ok'], len(have)), 'precision': w(c['pred_pos_ok'], c['pred_pos']), 'recall': w(c['key_pos_found'], c['key_pos']), 'errors': errs}
    res['bridge_or_extension'] = scalar('bridge_or_extension', 'bridge_or_extension', positive=lambda v: v == 'yes')
    res['prior_extension_count'] = scalar('prior_extension_count', 'prior_extension_count')
    res['is_letter_contract'] = scalar('is_letter_contract', 'is_letter_contract', positive=lambda v: v == 'yes')
    res['urgency_trigger'] = scalar('urgency_trigger', 'urgency_trigger')
    def lst(kf, pf, key):
        tp = npred = nk = 0; diffs = []
        for i in have:
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
    res['cost_usd_total'] = round(sum(R[i]['usage']['cost_usd'] for i in have), 4)
    out[run] = res
print(json.dumps(out, indent=1, default=str))
