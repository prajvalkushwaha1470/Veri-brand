"""Measure VeriBrand on a labelled test set.   python eval/run_eval.py
Compares a keyword-only BASELINE (flags every claim-like span, no brand-kit grounding)
with the VeriBrand ENGINE (rules + claim-evidence ledger).  Also reports label accuracy
(contradicted / stale / unsupported / rule), the part a baseline cannot do at all."""
import os, sys, json, tempfile
os.environ['DATA_DIR'] = tempfile.mkdtemp(); # Use the synthetic Lumio knowledge base only for this offline regression evaluation.
os.environ['DEMO_MODE'] = '1'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backend_main as bm, engine

with bm.db() as c:
    FACTS = [dict(r) for r in c.execute('SELECT * FROM facts')]; RULES = [dict(r) for r in c.execute('SELECT * FROM rules')]
data = json.load(open(os.path.join(os.path.dirname(__file__), 'testset.json'), encoding='utf-8'))

def engine_keys(draft):
    out = set()
    for f in engine.analyze(draft, FACTS, RULES)['findings']:
        if f['category'] == 'readability': continue
        label = 'rule'
        if f['origin'] == 'ledger': label = {'Unsupported claim': 'unsupported', 'Approved source is overdue for review': 'stale'}.get(f['issue'], 'contradicted')
        out.add((f['matched'].lower(), label))
    return out

def baseline_keys(draft):
    out = set()
    for rx in (engine.PRICE, engine.SUP, engine.ABS, engine.STAT, engine.PCT, engine.TIME): out |= {(m.group(0).lower(), 'any') for m in rx.finditer(draft)}
    for f in engine.run_rules(draft, RULES): out.add((f['matched'].lower(), 'any'))
    return out

def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0; r = tp / (tp + fn) if tp + fn else 1.0
    return round(p, 3), round(r, 3), round(2 * p * r / (p + r), 3) if p + r else 0.0

T = dict(b=[0, 0, 0], e=[0, 0, 0], l=[0, 0, 0]); misses = []
for d in data:
    exp = {(x['text'], x['label']) for x in d['expected']}; exp_text = {t for t, _ in exp}
    b = {t for t, _ in baseline_keys(d['draft'])}; e = engine_keys(d['draft'])
    T['b'][0] += len(b & exp_text); T['b'][1] += len(b - exp_text); T['b'][2] += len(exp_text - b)
    et = {t for t, _ in e}
    T['e'][0] += len(et & exp_text); T['e'][1] += len(et - exp_text); T['e'][2] += len(exp_text - et)
    T['l'][0] += len(e & exp); T['l'][1] += len(e - exp); T['l'][2] += len(exp - e)
    if e != exp: misses.append((d['draft'], sorted(exp - e), sorted(e - exp)))
rows = [('Keyword-only baseline (detection)', T['b']), ('VeriBrand engine (detection)', T['e']), ('VeriBrand engine (detection + correct label)', T['l'])]
lines = [f'Test set: {len(data)} labelled drafts ({sum(len(d["expected"]) for d in data)} expected findings, {sum(1 for d in data if not d["expected"])} clean drafts)\n',
         '| System | TP | FP | FN | Precision | Recall | F1 |', '|---|---|---|---|---|---|---|']
for n, (tp, fp, fn) in rows: lines.append(f'| {n} | {tp} | {fp} | {fn} | {prf(tp, fp, fn)[0]} | {prf(tp, fp, fn)[1]} | {prf(tp, fp, fn)[2]} |')
if misses:
    lines.append('\nMismatches (expected-but-missed / unexpected):')
    for d, m, x in misses: lines.append(f'- "{d}"  missed={m}  extra={x}')
st = json.load(open(os.path.join(os.path.dirname(__file__), 'stress_set.json'), encoding='utf-8')); hit = tot = 0
for d in st:
    exp = {(x['text'], x['label']) for x in d['expected']}; got = engine_keys(d['draft']); hit += len(exp & got); tot += len(exp)
lines.append(f'\nStress set (paraphrases / spelled-out numbers / slang the rules do not cover): recall {hit}/{tot}. '
             'These are KNOWN LIMITATIONS of the rule+ledger layer; the optional grounded-AI pass is meant to cover them.')
out = '\n'.join(lines); print(out); open(os.path.join(os.path.dirname(__file__), 'results.md'), 'w', encoding='utf-8').write(out + '\n')
