"""Programmatic audit of the 5 failure modes across live strategic reads."""
import json, glob, os, re, statistics, datetime, sys

TODAY = datetime.date(2026, 9, 28)  # injected so the audit is reproducible
SEED = json.load(open('/Users/rishithal/Documents/SignalStack/data/seed_signals.json'))
CORPUS = {c['name']: c for c in SEED['competitors']}

REPEAT_WORDS = r'\b(loop|cycle|cycles|cyclic|repeat(?:s|ed|able|ing)?|recurring|recurs|repeating|pattern|patt ern|rhythm|rotation|every time|consistently (?:does|does not))\b'
CERTAINTY_WORDS = r'\b(will|certainly|definitely|guaranteed|must|undoubtedly|is going to)\b'
HEDGE_WORDS = r'\b(confidence|uncertain|uncertainty|likely|probabilit|caveat|risk|assum|may|might|could|if\b|stale|quiet|gap|refute|falsif|unclear|limited|conservat)'

def sig_dates(name):
    return [datetime.date.fromisoformat(s['date']) for s in sorted(CORPUS[name]['signals'], key=lambda x: x['date'])]

def signals(name):
    return sorted(CORPUS[name]['signals'], key=lambda x: x['date'])

def findings_for(name, d):
    """Return {mode: (verdict, [offending sentences])}."""
    out = {m: (None, []) for m in 'ABCDE'}
    pat, intent, pred, rec = (d.get(k,'') for k in ('patterns','inferred_intent','predicted_next_move','recommendation'))
    text_all = ' '.join([pat, intent, pred, rec])
    sigs = signals(name); ds = sig_dates(name)
    refused = any(k in text_all.lower() for k in ('insufficient','cannot','not predictable','no reliable prediction'))
    n = len(sigs)
    gaps = [(b-a).days for a,b in zip(ds,ds[1:])]
    median_gap = statistics.median(gaps) if gaps else None
    age = (TODAY - ds[-1]).days

    # ---- A: overclaimed repetition -------------------------------------
    # A sequence may be called repeating only if it occurs >= 2 times. With
    # n signals there are at most floor(n/2) disjoint repeats, so a claim of
    # "cycles/loops/recurring" needs enough data to show one.
    claims = [s.strip() for s in re.split(r'(?<=[.!?])\s+', pat) if re.search(REPEAT_WORDS, s, re.I)]
    if refused:
        out['A'] = ('PASS', [])
    elif not claims:
        out['A'] = ('PASS', [])
    else:
        # occurrences of a repeat-claim vs number of distinct intervals
        weak = n < 6
        # count how many separate "cycle/loop" nouns the model asserts
        n_claims = len(re.findall(r'\b(cycle|loop|rotation|rhythm)\b', pat, re.I))
        if weak and n_claims:
            out['A'] = ('FAIL', claims)
        else:
            out['A'] = ('PASS', [])

    # ---- B: ignored staleness -------------------------------------------
    stale = d.get('evidence_staleness') in ('stale','aging')
    if refused:
        out['B'] = ('PASS', [])  # refusal already implies no live forecast
    elif stale:
        pred_low = pred.lower()
        acknowledges = (re.search(HEDGE_WORDS, pred_low) is not None)
        # also require it not be pure unhedged certainty
        pure_certainty = re.search(CERTAINTY_WORDS, pred_low) and not acknowledges
        if not acknowledges or pure_certainty:
            out['B'] = ('FAIL', [s.strip() for s in re.split(r'(?<=[.!?])\s+', pred) if s.strip()])
        else:
            out['B'] = ('PASS', [])
    else:
        out['B'] = ('PASS', [])

    # ---- C: prediction contradicts latest trend -------------------------
    # Compare the predicted *type emphasis* with the last 2-3 signals' types.
    latest_types = [s['signal_type'] for s in sigs[-3:]]
    out['C'] = ('PASS', [])
    if not refused:
        # heuristic: if latest signals are all non-hiring but prediction is about hiring ramp
        if set(latest_types) <= {'feature','messaging','pricing'} and re.search(r'hir(?:e|ing|ed)\s+(?:a\s+)?(?:spike|surge|wave|cluster|accelerat|expand|ramp)|(?:post|add|open)\s+\w*\s*(?:sales|GTM|go-to-market)\s+roles', pred, re.I):
            out['C'] = ('FAIL', [s.strip() for s in re.split(r'(?<=[.!?])\s+', pred) if s.strip()])
        if 'messaging' in latest_types and re.search(r'cut(?:s)?\s+price|reduc\w+\s+price|lower\w*\s+price|discount', pred, re.I) and 'pricing' not in latest_types:
            out['C'] = ('FAIL', [s.strip() for s in re.split(r'(?<=[.!?])\s+', pred) if s.strip()])

    # ---- D: missing calibration + verbatim grounding ---------------------
    d_notes = []
    has_confidence = re.search(r'\b(high|medium|low)\s+confidence\b|confidence[:\-]?\s*(high|medium|low)|\bconfidence is\b', text_all, re.I) is not None
    has_missing = re.search(r'\b(missing|would be needed|needed to establish|not (?:been )?(?:recorded|available|observed)|we (?:do not|don\'t) have|insufficient data|no data)\b', text_all, re.I) is not None
    if not has_confidence: d_notes.append('NO CONFIDENCE LABEL')
    if not has_missing: d_notes.append('NO "what evidence is missing" STATEMENT')
    # verbatim grounding: check quoted/date-cited tokens appear in a signal
    all_summaries = ' '.join(s['summary'] + ' ' + s.get('raw_notes','') for s in sigs)
    iso = {s['date'] for s in sigs}   # the timeline's real date set is the `date` field, not the prose
    cited = set(re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', pat)) | set(re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', intent))
    invented = sorted(cited - iso)
    if invented: d_notes.append(f'CITED DATE NOT IN TIMELINE: {invented}')
    out['D'] = ('FAIL' if d_notes else 'PASS', d_notes)

    # ---- E: arithmetic / date errors -------------------------------------
    e_notes = []
    # every ISO date mentioned anywhere must be a real signal date OR in the future (a prediction target)
    all_dates = set(re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', text_all))
    for dt in all_dates:
        try: dd = datetime.date.fromisoformat(dt)
        except ValueError: e_notes.append(f'UNPARSEABLE DATE: {dt}'); continue
        if dd > TODAY:
            continue  # a forward-looking prediction target: allowed
        if dt not in iso:
            e_notes.append(f'PAST DATE NOT IN TIMELINE: {dt}')
    # "within N weeks/days ... (by DATE)": if both present, date must be consistent with N from today
    for m in re.finditer(r'within (?:the next )?(\d+)[\s-]*(?:to\s*(\d+)\s*)?(day|week|month)s?[^.]{0,60}?by (?:around |~ |approximately )?(\d{4}-\d{2}-\d{2})', pred, re.I):
        lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
        unit, target = m.group(3), m.group(4)
        delta = (datetime.date.fromisoformat(target) - TODAY).days
        if unit.lower().startswith('day'): rng = (lo, hi)
        elif unit.lower().startswith('week'): rng = (lo*7, hi*7)
        else: rng = (lo*30, hi*30)
        # allow 7d slack for "around"
        if not (rng[0]-7 <= delta <= rng[1]+7):
            e_notes.append(f'ARITHMETIC: "within {lo}-{hi} {unit}s ... by {target}" but today+{lo}-{hi}{unit} = {delta}d out')
    out['E'] = ('FAIL' if e_notes else 'PASS', e_notes)

    return out

def main():
    files = sorted(glob.glob('/tmp/audit/before/*.json'))
    print('='*100)
    print('STEP 3 AUDIT — competitor vs failure mode A-E   (today=2026-09-28)')
    print('='*100)
    hdr = f"  {'competitor':22}" + ''.join(f'  {m:^6}' for m in 'ABCDE')
    print(hdr)
    print('  '+'-'*(22+8*5))
    modes = {m: 0 for m in 'ABCDE'}
    details = {}
    for f in files:
        name_slug = os.path.basename(f)[:-5]
        d = json.load(open(f))
        name = d['competitor'].replace('-', ' ').title()
        # map back to exact corpus name
        cname = next((c for c in CORPUS if c.lower().replace(' ','-')==name_slug), d['competitor'])
        res = findings_for(cname, d)
        row = f"  {cname:22}"
        for m in 'ABCDE':
            v, notes = res[m]
            modes[m] += (v == 'FAIL')
            row += f"  {'FAIL' if v=='FAIL' else 'pass':^6}"
        print(row)
        details[cname] = (d, res)
    print('  '+'-'*(22+8*5))
    print(f"  {'TOTALS (fail)':22}" + ''.join(f"  {modes[m]:^6}" for m in 'ABCDE'))
    print()
    # details
    for cname,(d,res) in details.items():
        for m in 'ABCDE':
            v, notes = res[m]
            if v=='FAIL':
                print(f"  [{m}] {cname}  (staleness={d.get('evidence_staleness')}, n={d.get('signal_count')})")
                for note in notes:
                    print(f"        - {note}")
    # save
    json.dump({k:{'res':{m:{'verdict':v,'notes':nt} for m,(v,nt) in r.items()},'read':d} for k,(d,r) in details.items()}, open('/tmp/audit/report_before.json','w'), indent=2)

if __name__=='__main__':
    main()
