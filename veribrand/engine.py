"""VeriBrand review engine.

Layers (mirrors the Round 1 plan):
  1. Rules first      - plain code/regex, no AI guessing.
  2. Claim ledger     - every claim is matched to an approved fact (supported / contradicted /
                        stale / unsupported). Unsupported means "uncertain -> human".
  3. Grounded AI pass - OPTIONAL. Retrieval -> LLM (JSON with source ids + quotes) -> verification.
                        Findings whose citation cannot be verified are dropped, never shown.
  4. Intent lock      - fixes that would delete protected text are flagged.
The engine is pure (no DB, no network) so it can be unit-tested and evaluated offline.
"""
import re, json, difflib
from datetime import date, datetime

GENERIC = {'price', 'plan', 'cost', 'rate', 'the', 'a', 'an', 'of', 'for', 'and', 'pricing', 'fee', 'fees'}
ALIASES = {'annual': {'year', 'yearly', 'annually'}, 'monthly': {'month'}}
MULT = {'': 1, 'k': 1e3, 'lakh': 1e5, 'million': 1e6, 'm': 1e6, 'crore': 1e7, 'billion': 1e9}
LABEL_RULE = {'ledger:price': 'Price vs approved fact', 'ledger:superlative': 'Ranking / superlative claims',
              'ledger:absolute': 'Absolute promises', 'ledger:stat': 'Numeric claims (users, downloads)',
              'ledger:percent': 'Percentage claims', 'ledger:time': 'Time / speed claims',
              'check:readability': 'Readability (long sentences)', 'ai:grounded': 'AI grounded findings'}

# ---------------------------------------------------------------- helpers
def _num(s):
    m = re.search(r'\d[\d,]*(?:\.\d+)?', s or '')
    try: return float(m.group(0).replace(',', '')) if m else None
    except ValueError: return None

def _numtok(s):
    m = re.search(r'\d[\d,]*(?:\.\d+)?', s or '')
    return m.group(0) if m else ''

def parse_date(s):
    try: return datetime.strptime((s or '')[:10], '%Y-%m-%d').date()
    except ValueError: return None

def is_stale(fact, today):
    d = parse_date(fact.get('review_by'))
    return bool(d and d < today)

def _norm(s): return re.sub(r'\s+', ' ', (s or '')).strip().lower()
def tokens(s): return set(re.findall(r'[a-z0-9]+', (s or '').lower()))

_BOUND = re.compile(r'(?<!\brs)(?<!\bno)[.!?]+(?:\s|$)|\n', re.I)

def split_sentences(text):
    out, last = [], 0
    for m in _BOUND.finditer(text):
        seg = text[last:m.start()]
        if seg.strip(): out.append((last + len(seg) - len(seg.lstrip()), m.start(), seg.strip()))
        last = m.end()
    tail = text[last:]
    if tail.strip(): out.append((last + len(tail) - len(tail.lstrip()), len(text), tail.strip()))
    return out

def sentence_of(sents, pos, text):
    for s, e, t in sents:
        if s <= pos <= e: return t
    return text[max(0, pos - 60):pos + 60]

def label(conf, uncertain=False):
    if uncertain: return 'Uncertain'
    return 'High' if conf >= 0.85 else 'Medium' if conf >= 0.6 else 'Low'

def _finding(origin, category, rule_id, issue, severity, reason, source, conf, m_start, m_end, matched,
             replacement, fix, uncertain=False, needs_human=False, extra=None):
    f = dict(origin=origin, category=category, rule_id=rule_id, issue=issue, severity=severity, reason=reason,
             source=source, confidence=round(conf, 2), confidence_label=label(conf, uncertain),
             uncertain=uncertain, needs_human=needs_human or uncertain, start=m_start, end=m_end,
             matched=matched, replacement=replacement, fix=fix, decision='Pending', decision_note='',
             custom_replacement='', intent_conflict='')
    if extra: f.update(extra)
    return f

# ---------------------------------------------------------------- layer 1: rules
def run_rules(draft, rules):
    out = []
    for r in rules:
        if not r.get('enabled', 1) or not r.get('pattern'): continue
        try: rx = re.compile(r['pattern'], re.I)
        except re.error: continue
        for n, m in enumerate(rx.finditer(draft)):
            if n >= 15 or m.end() == m.start(): break
            rep = None
            if r.get('replacement') not in (None, ''):
                try: rep = m.expand(r['replacement'])
                except (re.error, IndexError): rep = r['replacement']
            fix = r.get('suggestion') or 'Rewrite to follow the guideline.'
            if rep is not None: fix += f' Suggested wording: "{rep}".'
            out.append(_finding('rule', r['kind'], f"rule:{r['id']}", r['rule'], r['severity'],
                                f"This text matches a configured {r['kind']} rule. {r['rule']}.", r['source'],
                                0.95, m.start(), m.end(), m.group(0), rep, fix))
    return out

# ---------------------------------------------------------------- layer 2: claim-evidence ledger
PRICE = re.compile(r'(?:\b(?:rs|inr)\.?\s*|₹\s*)(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*rupees\b', re.I)
SUP = re.compile(r"(?:\b(?:india|world|nation)['’]?s\s+)?(?:#\s?1\b|\bno\.?\s?1\b|\bnumber\s+(?:one|1)\b|"
                 r"\b(?:best|leading|fastest|cheapest|only|top[- ]rated)\b|"
                 r"\bmost\s+(?:trusted|popular|loved|secure|reliable|downloaded|advanced)\b)", re.I)
ABS = re.compile(r"\bguaranteed\b|\brisk[- ]free\b|\b100\s?%|\bzero\s+(?:fees|charges)\b|"
                 r"\bno\s+hidden\s+(?:fees|charges|costs)\b|\bfree\s+forever\b", re.I)
STAT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(lakh|crore|million|billion|k|m)?\+?\s+'
                  r'(users|customers|downloads|people|businesses|teams|merchants|reviews|ratings)\b', re.I)
PCT = re.compile(r'(\d+(?:\.\d+)?)\s?%')
TIME = re.compile(r'\b(?:in|within|under)\s+(\d+)\s+(seconds|minutes|mins|hours|days)\b', re.I)

def _sup_phrase(t):
    t = t.lower()
    t = re.sub(r"^(india|world|nation)['’]?s\s+", '', t)
    if re.search(r'#\s?1|no\.?\s?1|number\s+(one|1)', t): return '#1'
    return _norm(t.replace('-', ' '))

def _fact_has_phrase(fact, phrase):
    t = (fact['name'] + ' ' + fact['value']).lower()
    if phrase == '#1':
        return bool(re.search(r'#\s?1\b|\bno\.?\s?1\b|\bnumber\s+(?:one|1)\b', t))
    return bool(re.search(r'\b' + re.escape(phrase) + r'\b', t.replace('-', ' ')))

def _fact_tokens(f):
    toks = {t for t in tokens(f['name']) if t not in GENERIC and not t.isdigit()}
    for t in list(toks): toks |= ALIASES.get(t, set())
    return toks

def _values(text):
    vals = set()
    for m in re.finditer(r'(\d[\d,]*(?:\.\d+)?)\s*(lakh|crore|million|billion|k|m)?\b', text or '', re.I):
        try: vals.add(float(m.group(1).replace(',', '')) * MULT.get((m.group(2) or '').lower(), 1))
        except ValueError: pass
    return vals

def build_ledger(draft, facts, today):
    price_facts = [f for f in facts if f.get('category') == 'price' and _num(f['value']) is not None]
    claim_facts = [f for f in facts if f.get('category') == 'claim']
    sents = split_sentences(draft)
    entries, taken = [], []
    free = lambda s, e: all(e <= a or s >= b for a, b in taken)

    def add(typ, m, status, fact, reason, replacement=None):
        taken.append((m.start(), m.end()))
        entries.append(dict(id=f'c{len(entries)+1}', type=typ, claim=m.group(0), start=m.start(), end=m.end(),
                            sentence=sentence_of(sents, m.start(), draft), status=status,
                            fact_id=fact['id'] if fact else None, source=fact['source'] if fact else 'none found',
                            evidence=f"{fact['name']}: {fact['value']}" if fact else '', reason=reason,
                            replacement=replacement, review_by=(fact or {}).get('review_by', '')))

    def settle(typ, m, status, fact, ok_msg, bad_msg, replacement=None):
        """Apply stale handling then record."""
        if fact and is_stale(fact, today) and status in ('supported', 'contradicted'):
            add(typ, m, 'stale', fact, f"This matches '{fact['name']}' in '{fact['source']}', but that source was due for "
                f"review on {fact['review_by']}. VeriBrand cannot confirm the figure until the source is re-approved.")
        elif status == 'supported': add(typ, m, 'supported', fact, ok_msg)
        elif status == 'contradicted': add(typ, m, 'contradicted', fact, bad_msg, replacement)
        else: add(typ, m, 'unsupported', None, 'No approved source in the brand kit backs this claim.')

    # prices
    for m in PRICE.finditer(draft):
        if not free(m.start(), m.end()): continue
        amt = _num(m.group(1) or m.group(2)); ctx = sentence_of(sents, m.start(), draft).lower()
        named = [f for f in price_facts if _fact_tokens(f) & tokens(ctx)]
        fact, status = None, 'unsupported'
        pool = named
        if not pool and re.search(r'\b(start|starts|starting|from|as low as|begin|begins)\b', ctx) and price_facts:
            pool = [min(price_facts, key=lambda f: _num(f['value']))]
        if pool:
            hit = next((f for f in pool if _num(f['value']) == amt), None)
            fact, status = (hit, 'supported') if hit else (pool[0], 'contradicted')
        else:
            hit = next((f for f in price_facts if _num(f['value']) == amt), None)
            if hit: fact, status = hit, 'supported'
        rep = re.sub(r'\d[\d,]*(?:\.\d+)?', _numtok(fact['value']), m.group(0), count=1) if fact else None
        settle('price', m, status, fact, f"Matches approved fact '{fact['name']}: {fact['value']}' ({fact['source']})." if fact else '',
               f"Draft says {m.group(0)}, but the approved {fact['name'] if fact else 'price'} is {fact['value'] if fact else '?'} "
               f"({fact['source'] if fact else ''}).", rep)
    # superlatives
    for m in SUP.finditer(draft):
        if not free(m.start(), m.end()): continue
        ph = _sup_phrase(m.group(0)); fact = next((f for f in claim_facts if _fact_has_phrase(f, ph)), None)
        settle('superlative', m, 'supported' if fact else 'unsupported', fact,
               f"Backed by approved claim '{fact['name']}: {fact['value']}' ({fact['source']})." if fact else '', '')
    # absolutes
    for m in ABS.finditer(draft):
        if not free(m.start(), m.end()): continue
        ph = _norm(m.group(0).replace('-', ' ')); fact = next((f for f in claim_facts if _fact_has_phrase(f, ph)), None)
        settle('absolute', m, 'supported' if fact else 'unsupported', fact,
               f"Backed by approved claim '{fact['name']}: {fact['value']}' ({fact['source']})." if fact else '', '')
    # numeric claims
    def numeric(typ, m, value, noun, ok_filter):
        if not free(m.start(), m.end()): return
        cands = [f for f in claim_facts if ok_filter(f)]
        hit = next((f for f in cands if value in _values(f['value'])), None)
        if hit: settle(typ, m, 'supported', hit, f"Backed by approved claim '{hit['name']}: {hit['value']}' ({hit['source']}).", '')
        elif cands:
            f = cands[0]
            settle(typ, m, 'contradicted', f, '', f"Draft says '{m.group(0)}', but the approved claim is '{f['name']}: {f['value']}' ({f['source']}).")
        else: settle(typ, m, 'unsupported', None, '', '')
    for m in STAT.finditer(draft):
        v = float(m.group(1).replace(',', '')) * MULT.get((m.group(2) or '').lower(), 1); noun = m.group(3).lower()
        numeric('stat', m, v, noun, lambda f, n=noun: n in (f['name'] + ' ' + f['value']).lower())
    for m in PCT.finditer(draft):
        numeric('percent', m, float(m.group(1)), '%', lambda f: '%' in f['value'])
    for m in TIME.finditer(draft):
        numeric('time', m, float(m.group(1)), m.group(2).lower(), lambda f, u=m.group(2).lower(): u in f['value'].lower())
    entries.sort(key=lambda e: e['start'])
    for i, e in enumerate(entries): e['id'] = f'c{i+1}'
    return entries

def ledger_findings(entries):
    out = []
    for e in entries:
        if e['status'] == 'supported': continue
        typ = e['type']; rid = f'ledger:{typ}'; common = dict(extra={'ledger_id': e['id']})
        if e['status'] == 'contradicted':
            issue = 'Price differs from approved fact' if typ == 'price' else 'Claim differs from approved source'
            out.append(_finding('ledger', 'claim', rid, issue, 'High', e['reason'], e['source'], 0.92, e['start'], e['end'],
                                e['claim'], e['replacement'],
                                f"Use the approved value" + (f': "{e["replacement"]}".' if e['replacement'] else '.'), **common))
        elif e['status'] == 'stale':
            out.append(_finding('ledger', 'claim', rid, 'Approved source is overdue for review', 'Medium', e['reason'],
                                e['source'], 0.8, e['start'], e['end'], e['claim'], None,
                                'Ask the guideline owner to re-approve the source, or remove the figure.', needs_human=True, **common))
        else:
            sev = 'High' if typ in ('superlative', 'absolute') else 'Medium'
            out.append(_finding('ledger', 'claim', rid, 'Unsupported claim', sev,
                                f"No approved source backs “{e['claim']}”. VeriBrand will not guess, so this is marked UNCERTAIN and sent to a reviewer.",
                                'none found', 0.5, e['start'], e['end'], e['claim'], None,
                                'Remove the claim or attach an approved source to the brand kit.', uncertain=True, **common))
    return out

# ---------------------------------------------------------------- readability
def text_stats(draft):
    words = re.findall(r"[A-Za-z0-9’']+", draft); sents = split_sentences(draft)
    if not words or not sents: return {'words': len(words), 'sentences': len(sents), 'flesch': None}
    syl = sum(max(1, len(re.findall(r'[aeiouy]+', w.lower()))) for w in words)
    flesch = 206.835 - 1.015 * len(words) / len(sents) - 84.6 * syl / len(words)
    return {'words': len(words), 'sentences': len(sents), 'flesch': round(flesch, 1)}

def readability_findings(draft):
    out = []
    for s, e, t in split_sentences(draft):
        n = len(t.split())
        if n > 30:
            out.append(_finding('rule', 'readability', 'check:readability', 'Very long sentence', 'Low',
                                f'This sentence has {n} words; readers skim and miss details past ~25.', 'VeriBrand readability check',
                                0.9, s, e, t[:80] + ('…' if len(t) > 80 else ''), None, 'Split it into two shorter sentences.'))
    return out

# ---------------------------------------------------------------- layer 3: grounded AI (optional) + verification
def build_passages(facts, rules):
    ps = [dict(id=f"F{f['id']}", text=f"{f['name']}: {f['value']}", source=f['source'], policy=f.get('category') in ('policy', 'term')) for f in facts]
    ps += [dict(id=f"R{r['id']}", text=f"{r['rule']}. {r.get('suggestion') or ''}".strip(), source=r['source'], policy=True) for r in rules if r.get('enabled', 1)]
    return ps

def retrieve(draft, passages, k=6):
    dt = tokens(draft); scored = sorted(passages, key=lambda p: (-(len(dt & tokens(p['text'])) + (1 if p['policy'] else 0)), p['id']))
    return scored[:k]

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'); _PHONE = re.compile(r'(?<!\w)\+?\d[\d\s-]{8,}\d(?!\w)')
def redact(text):
    n = 0
    text, a = _EMAIL.subn('[EMAIL]', text); n += a
    text, b = _PHONE.subn('[PHONE]', text); n += b
    return text, n

AI_SYSTEM = ("You are a brand-governance reviewer. Use ONLY the numbered brand passages provided. Return JSON: "
             '{"findings":[{"issue":str,"span":str (exact text copied from the draft),"source_id":str (a passage id),'
             '"quote":str (exact words copied from that passage),"reason":str,"suggested_replacement":str|null}]}. '
             "Report only tone/messaging problems that a passage clearly supports. If nothing, return {\"findings\":[]}.")

def verify_ai(raw, draft, passages):
    """Drop every finding whose citation cannot be verified. Returns (findings, stats)."""
    pmap = {p['id']: p for p in passages}; ok, proposed, dropped = [], 0, 0
    try:
        data = json.loads(re.sub(r'^```(?:json)?|```$', '', (raw or '').strip(), flags=re.M).strip())
        items = data.get('findings', []) if isinstance(data, dict) else []
    except (ValueError, TypeError): items = []
    for it in items[:12]:
        proposed += 1
        try:
            p = pmap.get(str(it.get('source_id'))); span = str(it.get('span', '')); quote = str(it.get('quote', ''))
            if not p or len(quote) < 6 or _norm(quote) not in _norm(p['text']): dropped += 1; continue
            i = draft.find(span) if span else -1
            if i < 0 and span: m = re.search(re.escape(span), draft, re.I); i = m.start() if m else -1
            if i < 0: dropped += 1; continue
            rep = it.get('suggested_replacement'); rep = str(rep) if isinstance(rep, str) and rep.strip() else None
            ok.append(_finding('ai', 'ai', 'ai:grounded', str(it.get('issue', 'Possible brand-voice issue'))[:120], 'Medium',
                               str(it.get('reason', ''))[:400] + ' (AI finding; citation verified against the brand kit.)',
                               p['source'], 0.7, i, i + len(span), draft[i:i + len(span)], rep,
                               'Review the suggested wording.' if rep else 'Rewrite to follow the cited guideline.',
                               extra={'cited_passage': p['text'], 'cited_id': p['id']}))
        except Exception: dropped += 1
    return ok, {'proposed': proposed, 'verified': len(ok), 'dropped': dropped}

# ---------------------------------------------------------------- intent lock
def intent_conflict(draft, start, end, replacement, protected):
    if replacement is None or start < 0: return ''
    new = draft[:start] + replacement + draft[end:]
    for p in protected:
        if p and p.lower() in draft.lower() and p.lower() not in new.lower(): return p
    return ''

# ---------------------------------------------------------------- orchestration
def analyze(draft, facts, rules, protected=(), today=None, call_llm=None):
    today = today or date.today()
    ledger = build_ledger(draft, facts, today)
    findings = run_rules(draft, rules) + ledger_findings(ledger) + readability_findings(draft)
    ai = {'used': False, 'proposed': 0, 'verified': 0, 'dropped': 0, 'redactions': 0}
    if call_llm:
        try:
            ps = retrieve(draft, build_passages(facts, rules)); safe, n = redact(draft)
            ctx = '\n'.join(f"[{p['id']}] {p['text']} (source: {p['source']})" for p in ps)
            raw = call_llm(AI_SYSTEM, f'BRAND PASSAGES:\n{ctx}\n\nDRAFT:\n{safe}')
            if raw is not None:
                good, st = verify_ai(raw, draft, ps); ai = {'used': True, 'redactions': n, **st}
                spans = [(f['start'], f['end']) for f in findings]
                findings += [g for g in good if all(g['end'] <= a or g['start'] >= b for a, b in spans)]
        except Exception: ai['error'] = True
    findings.sort(key=lambda f: (f['start'] if f['start'] >= 0 else 10**9, f['end']))
    for i, f in enumerate(findings):
        f['id'] = f'f{i+1}'; f['intent_conflict'] = intent_conflict(draft, f['start'], f['end'], f['replacement'], protected)
    warnings = [f"Protected text “{p}” is not in the draft." for p in protected if p and p.lower() not in draft.lower()]
    return {'findings': findings, 'ledger': ledger, 'stats': {**text_stats(draft), 'ai': ai, 'warnings': warnings}}

# ---------------------------------------------------------------- diff / apply
def word_diff(a, b):
    ta, tb = re.findall(r'\s+|\w+|[^\w\s]', a), re.findall(r'\s+|\w+|[^\w\s]', b); ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == 'equal': ops.append({'t': 'eq', 's': ''.join(ta[i1:i2])})
        else:
            if i2 > i1: ops.append({'t': 'del', 's': ''.join(ta[i1:i2])})
            if j2 > j1: ops.append({'t': 'ins', 's': ''.join(tb[j1:j2])})
    return ops

def apply_decisions(draft, findings):
    edits = []
    for f in findings:
        rep = None
        if f['decision'] == 'Accept' and f.get('replacement') is not None: rep = f['replacement']
        elif f['decision'] == 'Edit' and f.get('custom_replacement'): rep = f['custom_replacement']
        if rep is not None and f['start'] >= 0: edits.append((f['start'], f['end'], rep, f['id']))
    edits.sort(reverse=True); text, applied, skipped, floor = draft, [], [], len(draft) + 1
    for s, e, rep, fid in edits:
        if e > floor: skipped.append(fid); continue
        text = text[:s] + rep + text[e:]; applied.append(fid); floor = s
    return text, applied[::-1], skipped
