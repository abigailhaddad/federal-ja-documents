"""Score the extract3 validation runs against the answer key (strict: no
adjudication). Usage, from this directory: python score.py . A B"""
import json, re, sys, glob
from pathlib import Path
from collections import Counter
S = Path(sys.argv[1]); runs = sys.argv[2:]
rnd = [l.split()[0] for l in open(S/'sample_random.txt') if l.strip()]
edge = [l.split()[0] for l in open(S/'sample_edge.txt') if l.strip()]
ids = rnd + edge
stratum = {i: 'random' for i in rnd} | {i: 'edge' for i in edge}
SIG = {"signer_or_approver", "contracting_officer", "competition_advocate", "legal_reviewer"}
ws = lambda s: re.sub(r'\s+', ' ', s or '').strip()
sq = lambda s: re.sub(r'[^0-9a-z]', '', (s or '').lower())
keyp = {json.loads(l)['notice_id']: json.loads(l) for l in open(S/'answer_key'/'people.jsonl')}
keyf = {json.loads(l)['notice_id']: json.loads(l) for l in open(S/'answer_key'/'fields.jsonl')}
adj = json.load(open(S/'adjudication.json')) if (S/'adjudication.json').exists() else {}

def load(run):
    out = {}
    for f in glob.glob(str(S/'runs'/f'run_{run}_extractions.jsonl')):
        for l in open(f):
            r = json.loads(l); out[r['notice_id']] = r
    return out

def match_people(kp, pp):
    """one-to-one: exact (whitespace) on any variant, else loose (letters/digits)."""
    pairs, used = [], set()
    for mode in ('exact', 'loose'):
        for pi, p in enumerate(pp):
            if any(pi == x[0] for x in pairs): continue
            for ki, k in enumerate(kp):
                if ki in used: continue
                vs = [k.get('name')] + (k.get('name_variants') or [])
                ok = (ws(p['name']) in {ws(v) for v in vs if v}) if mode == 'exact' else (sq(p['name']) in {sq(v) for v in vs if v})
                if ok:
                    pairs.append((pi, ki, mode)); used.add(ki); break
    return pairs

def wilson(k, n):
    if not n: return 'n/a'
    from math import sqrt
    z=1.96; p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return f"{k}/{n} = {100*p:.0f}% ({100*(c-h):.0f}-{100*(c+h):.0f}%)"

rep = {}
for run in runs:
    R = load(run); res = {'run': run, 'docs': len(R)}
    for scope in ('all', 'signature'):
        for strat in ('random', 'edge', 'both'):
            tp_e = tp_l = npred = nkey = role_ok = role_n = 0
            red_key = red_pred = red_match = 0
            fn_list, fp_list = [], []
            for i in ids:
                if strat != 'both' and stratum[i] != strat: continue
                if i not in R: continue
                kp = [k for k in keyp[i]['people'] if not k['redacted'] and (scope == 'all' or k['role_in_document'] in SIG)]
                pp = [p for p in R[i]['fields']['people'] if not p['redacted'] and (scope == 'all' or p['role_in_document'] in SIG)]
                pairs = match_people(kp, pp)
                tp_e += sum(1 for x in pairs if x[2] == 'exact'); tp_l += len(pairs)
                npred += len(pp); nkey += len(kp)
                for pi, ki, _ in pairs:
                    role_n += 1; role_ok += pp[pi]['role_in_document'] == kp[ki]['role_in_document']
                mp = {x[0] for x in pairs}; mk = {x[1] for x in pairs}
                fp_list += [(i, pp[j]['name'], pp[j]['role_in_document']) for j in range(len(pp)) if j not in mp]
                fn_list += [(i, kp[j]['name'], kp[j]['role_in_document']) for j in range(len(kp)) if j not in mk]
                kr = [k for k in keyp[i]['people'] if k['redacted'] and (scope == 'all' or k['role_in_document'] in SIG)]
                pr = [p for p in R[i]['fields']['people'] if p['redacted'] and (scope == 'all' or p['role_in_document'] in SIG)]
                red_key += len(kr); red_pred += len(pr); red_match += min(len(kr), len(pr))
            res[f'{scope}/{strat}'] = {
                'named_precision_exact': wilson(tp_e, npred), 'named_recall_exact': wilson(tp_e, nkey),
                'named_precision_loose': wilson(tp_l, npred), 'named_recall_loose': wilson(tp_l, nkey),
                'role_accuracy': wilson(role_ok, role_n),
                'redacted_blocks_key': red_key, 'redacted_blocks_pred': red_pred, 'redacted_count_matched': red_match,
                'fp': fp_list if strat == 'both' else len(fp_list), 'fn': fn_list if strat == 'both' else len(fn_list)}
    # raw-level checks: invented names and quote verification
    raw_n = kept = 0; reasons = Counter(); invented = []
    for i, r in R.items():
        vt = ' '.join(d.get('quote') or '' for d in r['dropped']) + ' ' + ' '.join(p['name_quote'] for p in r['fields']['people'])
        raw = r['raw'].get('people') or []
        raw_n += len(raw); kept += len(r['fields']['people'])
        for d in r['dropped']:
            if d['field'] == 'people': reasons[d['reason']] += 1
        for p in raw:
            if p.get('name') and (p.get('redacted') or sq(p['name']) not in sq(vt)):
                invented.append((i, p.get('name'), p.get('redacted')))
    res['raw_people_entries'] = raw_n; res['kept_people_entries'] = kept; res['drop_reasons'] = dict(reasons)
    res['raw_named_entries_marked_redacted_or_not_spelled_as_in_document'] = invented
    res['kept_named_entries_with_redaction_marker_in_name'] = sum(1 for r in R.values() for p in r['fields']['people'] if p['name'] and re.search(r'REDACT', p['name'], re.I))
    # extract3 scalar fields
    def scal(field, pred_key, eq):
        c = Counter(); bad = []
        for i in ids:
            if i not in R: continue
            k = keyf[i][field]; kv = k.get('value'); acc = k.get('acceptable') or []
            pv = R[i]['fields'].get(pred_key)
            okv = [kv] + acc
            if pv is None:
                if kv is None or None in acc: c['null_ok'] += 1
                else: c['missed'] += 1; bad.append((i, 'missed', kv, pv))
            else:
                if any(v is not None and eq(pv, v) for v in okv): c['correct'] += 1
                elif kv is None: c['false_positive'] += 1; bad.append((i, 'fp', kv, pv))
                else: c['wrong'] += 1; bad.append((i, 'wrong', kv, pv))
        npred = c['correct'] + c['false_positive'] + c['wrong']; nk = c['correct'] + c['wrong'] + c['missed']
        return {'counts': dict(c), 'precision': wilson(c['correct'], npred), 'recall': wilson(c['correct'], nk), 'errors': bad}
    res['need_by_date'] = scal('need_by_date', 'need_by_date', lambda a, b: a == b)
    res['stated_pop_limit_days'] = scal('stated_pop_limit_days', 'stated_pop_limit_days', lambda a, b: abs(a - b) <= max(2, 0.03 * b))
    res['incumbent_piid'] = scal('incumbent_piid', 'incumbent_piid', lambda a, b: sq(a) == sq(b))
    def lst(field, get, norm):
        tp = npred = nk = 0; bad = []
        for i in ids:
            if i not in R: continue
            kk = {norm(x['value']) for x in keyf[i][field]}; pp = {norm(x) for x in get(R[i]['fields'])}
            tp += len(kk & pp); npred += len(pp); nk += len(kk)
            if kk != pp: bad.append((i, sorted(kk - pp), sorted(pp - kk)))
        return {'precision': wilson(tp, npred), 'recall': wilson(tp, nk), 'diffs (missed, extra)': bad}
    res['covered_piids'] = lst('covered_piids', lambda f: f.get('covered_piids') or [], sq)
    res['market_research_methods'] = lst('market_research_methods', lambda f: [m['method'] for m in f.get('market_research_methods') or []], lambda x: x)
    merge = lambda x: 'sources_sought' if x in ('rfi', 'sources_sought') else x
    res['market_research_methods_rfi=sources_sought'] = {k: v for k, v in lst('market_research_methods', lambda f: [m['method'] for m in f.get('market_research_methods') or []], merge).items() if k != 'diffs (missed, extra)'}
    cm = Counter(); bad = []
    for i in ids:
        if i not in R: continue
        k = keyf[i]['redaction_significant']; pv = R[i]['fields'].get('redaction_significant')
        ok = pv == k['value'] or pv in (k.get('acceptable') or [])
        cm[(k['value'], pv)] += 1
        if not ok: bad.append((i, k['value'], k.get('acceptable'), pv))
    res['redaction_significant'] = {'accuracy': wilson(sum(cm.values()) - len(bad), sum(cm.values())),
                                    'confusion (key, pred)': {f'{a}->{b}': n for (a, b), n in cm.items()}, 'errors': bad}
    rep[run] = res
print(json.dumps(rep, indent=1, default=str))
