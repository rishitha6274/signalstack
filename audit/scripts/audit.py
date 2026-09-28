import json, re, datetime, statistics, sys
TODAY=datetime.date(2026,9,28)
S=json.load(open('/Users/rishithal/Documents/SignalStack/data/seed_signals.json'))
C={c['name']:c for c in S['competitors']}
ORDER=['nimbus-ai','palisade-security','lumen-health','corvus-data','halcyon-mobility',
       'brightline-retail','ferrous-systems','vertex-cloud','pathfinder-labs','tidewater-analytics']
WORD={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}
MULT={'day':1,'week':7,'month':30}
GTM={'messaging','pricing','hiring','funding'}
CADW=r'cadence|rhythm|spacing|interval|precede|preceded|lead|lag|after|before|following|follows|shift|gap|consistently|rhythmic'

def norm(s): return re.sub(r'[\u2011\u2012\u2013\u2014]','-',s or '')
def num(t): 
    t=t.strip().lower(); return WORD[t] if t in WORD else (int(t) if t.isdigit() else None)
def load(slug):
    d=json.load(open(f'/tmp/audit/before/{slug}.json'))
    return next(n for n in C if n.lower().replace(' ','-')==slug), d
def mdy(x):
    m=re.match(r'([A-Z][a-z]{2}) (\d{1,2})',x)
    return datetime.date(2026,datetime.datetime.strptime(m.group(1),'%b').month,int(m.group(2))) if m else None

def occurrences(real, start_type, end_type, lo=None, hi=None):
    """count real occurrences of (start_type ... end_type) with gap in [lo,hi] days"""
    out=[]
    for i,a in enumerate(real):
        if a['signal_type']!=start_type: continue
        for j in range(i+1,len(real)):
            if real[j]['signal_type']==end_type:
                gap=(datetime.date.fromisoformat(real[j]['date'])-datetime.date.fromisoformat(a['date'])).days
                if lo is None or lo<=gap<=hi: out.append((a['date'],real[j]['date'],gap))
                break
    return out

def audit(name,d):
    sigs=sorted(C[name]['signals'],key=lambda x:x['date'])
    ds=[datetime.date.fromisoformat(s['date']) for s in sigs]
    gaps=[(b-a).days for a,b in zip(ds,ds[1:])]
    med=statistics.median(gaps) if gaps else None
    P,I,PN,R=(norm(d.get(k,'')) for k in ('patterns','inferred_intent','predicted_next_move','recommendation'))
    T=' '.join([P,I,PN,R])
    low=' '.join([P,I,PN,R]).lower()
    R_={}
    withheld = bool(d.get('narrative_withheld'))
    # Refusal is read off the structured field only. The prose grep this
    # replaces read the model's vocabulary instead of its decision.
    refused = str(d.get('confidence') or '').strip().lower() == 'none' and not withheld
    last_type=sigs[-1]['signal_type']; age=(TODAY-ds[-1]).days

    # ---------- A: overclaimed repetition ----------
    a_f=[]
    m=re.search(r'repeats?\s+(\w+)\s+times?',P,re.I)
    if m:
        n=num(m.group(1))
        for st,en in [('feature','pricing'),('feature','hiring'),('messaging','pricing'),('feature','feature')]:
            occ=occurrences(sigs,st,en)
            if len(occ)>=2: a_f=[]; break
        if a_f==[] and n and n>len(occ):
            a_f.append(f'claims the cycle "repeats {n} times" but only {len(occ)} occurrence(s) exist in the timeline: {occ}')
    if re.search(r'consistently precedes|feature development consistently',P,re.I):
        occ=occurrences(sigs,'feature','pricing',60,90)
        allocc=occurrences(sigs,'feature','pricing')
        if len(occ)<2:
            a_f.append(f'"feature development consistently precedes pricing or commercialization moves by 2-3 months" — 0 of {len(allocc)} feature->pricing pairs fall in the stated 60-90d window (actual gaps: {[g for _,_,g in allocc]}d)')
    m=re.search(r'this repeat of ([^.]+?) constitutes',P,re.I)
    if m:
        occ=occurrences(sigs,'hiring','feature',28,42)+occurrences(sigs,'funding','feature',28,42)+occurrences(sigs,'messaging','feature',28,42)
        if len(occ)<2: a_f.append(f'"This repeat of {m.group(1).strip()[:60]}... constitutes a clear pattern" — only {len(occ)} instance(s) of a go-to-market move followed by a product feature within 4-6 weeks')
    if re.search(r'three-step cycle',P,re.I) or re.search(r'repeating every',P,re.I):
        occ=occurrences(sigs,'feature','pricing')+occurrences(sigs,'feature','messaging')
        if occ and not [o for o in occurrences(sigs,'feature','messaging')]:
            a_f.append('claims a repeating 3-step cycle but feature->messaging does not recur')
    if re.search(r'after each messaging shift',P,re.I):
        seg=P[P.lower().find('after each messaging shift'):P.lower().find('after each messaging shift')+220]
        has_pred=bool(re.search(r'predicted|prediction|expected',seg,re.I))
        obs=occurrences(sigs,'messaging','pricing',0,120)
        if has_pred or len(obs)<2:
            a_f.append(f'"After each messaging shift they have introduced a commercial or product change" — only {len(obs)} observed instance(s); the list it offers to support the claim includes a *prediction* ("predicted next commercial move"), not evidence')
    R_['A']=('PASS' if (refused or not a_f) else 'FAIL', a_f)

    # ---------- B: ignored staleness ----------
    b_f=[]
    if not refused and d.get('evidence_staleness') in ('stale','aging'):
        ack=re.search(r'stale|no signal (?:has )?(?:since|for)|has not (?:produced|emitted|shipped)|quiet|quietly|since (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep)\w*|since \d{4}-\d{2}-\d{2}|\b{d}d\b|gap|overdue|silence|had not|has been \w+ days|\b{age} days'.replace('{d}',str(age)),PN,re.I)
        if not ack:
            b_f.append(f'banner says {d["evidence_staleness"].upper()} ({age}d since last signal) but predicted_next_move never acknowledges the gap and states no staleness-driven confidence reduction')
    R_['B']=('PASS' if (refused or not b_f) else 'FAIL', b_f)

    # ---------- C: prediction contradicts latest trend ----------
    c_f=[]
    if not refused:
        cyc=None
        m=re.search(r'hiring[^.]{0,40}?pricing[^.]{0,40}?messaging[^.]{0,40}?feature',P,re.I)
        if m: cyc=['hiring','pricing','messaging','feature']
        m2=re.search(r'feature\s*(?:[→>-]+|\s+then\s+)\s*hiring[^.]{0,40}?pricing',P,re.I)
        if m2: cyc=['feature','hiring','pricing']
        if cyc and last_type in cyc:
            nxt=cyc[(cyc.index(last_type)+1)%len(cyc)]
            got=re.search(r'(feature|hiring|pricing|messaging|partnership|announcement)',PN,re.I)
            got_t=got.group(1).lower() if got else '?'
            if nxt=='feature' and got_t in ('hiring','pricing'): 
                c_f.append(f'its own declared cycle is {" -> ".join(cyc)}; the last signal is {last_type}, so the next stage should be {nxt}, yet the forecast predicts "{got_t}"')
            elif nxt=='messaging' and got_t in ('feature','hiring'):
                c_f.append(f'its own declared cycle is {" -> ".join(cyc)}; the last signal is {last_type}, so the next stage should be messaging, yet the forecast predicts a {got_t} signal, skipping it')
            elif nxt=='hiring' and got_t in ('feature','pricing'):
                c_f.append(f'its own declared cycle is {" -> ".join(cyc)}; the last signal is {last_type}, so the next stage should be hiring, yet the forecast predicts "{got_t}"')
    R_['C']=('PASS' if not c_f else 'FAIL', c_f)

    # ---------- D: calibration + verbatim ----------
    d_f=[]
    # The app now ships `confidence` and `missing_evidence` as fields; before the
    # fix they did not exist and could only be looked for in the prose. Score
    # whichever channel the read actually used.
    conf_field=d.get('confidence')
    if conf_field: 
        if str(conf_field).lower() not in ('high','medium','low','none'):
            d_f.append(f'confidence field is {conf_field!r}, not high/medium/low/none')
    elif not re.search(r'confidence',T,re.I):
        d_f.append('NO confidence label (high/medium/low) anywhere in the output')
    miss_field=str(d.get('missing_evidence') or '').strip()
    if miss_field:
        pass
    else:
        miss=re.search(r'what evidence is missing|evidence (?:that is )?missing|would be needed|needed to establish|insufficient|cannot|not (?:been )?(?:recorded|available|observed)|more data|additional signals|no data|could not',low)
        if not miss: d_f.append('NO statement of what evidence is missing')
    blob=norm(' '.join(s['summary']+' '+s.get('raw_notes','') for s in sigs)).lower()
    for fld in ('patterns','inferred_intent'):
        for mm in re.finditer(r"['\"‘“]([^'\"’”]{4,70})['\"’”]",norm(d[fld])):
            q=mm.group(1); before=norm(d[fld])[max(0,mm.start()-14):mm.start()].lower()
            if re.search(r'e\.g\.|i\.e\.|such as|for example',before): continue
            pr=re.sub(r'\s+',' ',re.sub(r'[^\w\s-]','',q)).strip().lower()
            if not pr or pr in blob: continue
            if re.fullmatch(r'[a-z -]*(core|monetize|monetisation|monetization|build-then)[a-z -]*',pr): continue  # model-coined sequence label
            d_f.append(f'QUOTE NOT VERBATIM IN TIMELINE ({fld}): "{q}"')
    if refused:
        q=[x for x in d_f if 'QUOTE' in x]
        R_['D']=('FAIL' if q else 'PASS', q)
    else:
        R_['D']=('FAIL' if d_f else 'PASS', d_f)

    # ---------- E: arithmetic ----------
    e_f=[]
    pat=re.compile(r'(?:(\d+)\s*[-–to]+\s*(\d+)|(\d+))\s*(day|week|month)s?',re.I)
    for mm in re.finditer(pat,T):
        ctx=T[max(0,mm.start()-130):mm.end()+30]
        if not re.search(CADW,ctx,re.I): continue
        u=mm.group(4).lower()
        if u not in MULT: continue
        lo=int(mm.group(1)) if mm.group(1) else int(mm.group(3))
        hi=int(mm.group(2)) if mm.group(2) else lo
        lo_d,hi_d=lo*MULT[u],hi*MULT[u]
        tail=T[mm.end():mm.end()+14]
        if re.match(r'\s*(later|earlier|before|after|prior|prior\b)',tail,re.I): continue
        if lo==hi:
            exempt=set(gaps)|{age}
            if med:
                exempt|={int(med),int(med)-1,int(med)+1,age-int(med)}
            if lo_d in exempt: continue   # a measured fact of this stream, not a cadence
            if med and abs(lo_d-med)>max(3,0.15*med):
                e_f.append(f'claims ~{lo} {u} cadence (={lo_d}d) but the median gap is {med}d (gaps {gaps})')
            continue
        out=[g for g in gaps if not(lo_d-1<=g<=hi_d+1)]
        if out and lo_d<=max(gaps) and hi_d>=min(gaps):
            e_f.append(f'claims a {lo}-{hi} {u} cadence (={lo_d}-{hi_d}d) but {len(out)}/{len(gaps)} real gaps fall outside: {out}  (gaps: {gaps})')
    for mm in re.finditer(r'((?:[A-Z][a-z]{2}) (\d{1,2})[^\d]{0,18}(?:([A-Z][a-z]{2}) )?(\d{1,2})?)\s*\(?=\s*(\d+)\s*d',T):
        got=[x for x in (mdy(f'{a} {b}') for a,b in re.findall(r'([A-Z][a-z]{2}) (\d{1,2})',mm.group(1))) if x]
        cl=int(mm.group(5))
        if len(got)==2:
            ac=(got[1]-got[0]).days
            if ac!=cl: e_f.append(f'interval wrong: "{mm.group(1).strip()} = {cl}d" but actual is {ac}d')
    for mm in re.finditer(r'within the next (\w+)\s*(\w+)\D{0,50}?by (?:approximately |around |~ )?(\d{4}-\d{2}-\d{2})',T,re.I):
        n,u,tg=num(mm.group(1)),mm.group(2).lower().rstrip('s'),mm.group(3)
        if not n or u not in MULT: continue
        dl=(datetime.date.fromisoformat(tg)-TODAY).days
        if abs(dl-n*MULT[u])>2: e_f.append(f'horizon wrong: "within the next {mm.group(1)} {u}s (by {tg})" = {dl}d from today, expected {n*MULT[u]}d')
    m=re.search(r'next expected signal[^.]{0,90}?(\d+)\s*days? later',T,re.I)
    if m:
        imp=ds[-1]+datetime.timedelta(days=int(m.group(1)))
        if imp<TODAY: e_f.append(f'cadence continuation lands in the past: last signal {ds[-1]} + {m.group(1)}d = {imp}, already {(TODAY-imp).days}d ago; the forecast jumps to a later date with no accounting for the skipped cycles')
    for mm in re.finditer(r'observed (\w+)[- ]*(\w+)\s*week lag',T,re.I):
        lo,hi=num(mm.group(1)),num(mm.group(2))
        if not lo or not hi: continue
        due=ds[-1]+datetime.timedelta(days=hi*7)
        if due<TODAY: e_f.append(f'stated lag already overdue: asserts a {lo}-{hi} week lag, which from the last signal {ds[-1]} was due by {due} ({(TODAY-due).days}d ago), yet it forecasts forward as if on schedule')
    # cherry-picked cadence: names a rhythm while the most recent gap deviates hard
    m=re.search(r'cadence',P,re.I) if re.search(r'=\s*\d+\s*d',P) else None
    if m and gaps and med:
        last_gap=gaps[-1]
        claimed=[int(x)*MULT[u.lower()] for x,u in re.findall(r'(\d+)[- ]?(day|week|month)s?\s*(?:cadence|rhythm)',P,re.I)]
        claimed+=[int(x) for x in re.findall(r'(?:cadence|rhythm)\D{0,12}?(\d+)\s*[- ]?day',P,re.I)]
        for c in set(claimed):
            if abs(last_gap-c)>0.25*c:
                e_f.append(f'cherry-picked cadence: asserts a {c}d rhythm but the most recent gap is {last_gap}d ({age}d since the last signal); the listed intervals omit it')
    R_['E']=('PASS' if (refused and not e_f) or not e_f else 'FAIL', e_f)
    if withheld:
        # A withheld read is a distinct outcome, not a failed narrative: it
        # offers no forecast to grade, it reports the rejection. Scored as its
        # own mode so it cannot be mistaken for a read that failed D and E.
        for m in 'ABCDE':
            R_[m]=('WITHHELD', [f"narrative withheld by validators: "
                                f"{str(d.get('model_used'))[:100]}"])
    return R_, refused

rows={}
print('='*118)
print('STEP 3 — STRATEGIC READ AUDIT   (10 competitors, live reads, today=2026-09-28)')
print('='*118)
print(f"  {'competitor':22}{'n':>3}  {'staleness':9}  {'A':5}{'B':5}{'C':5}{'D':5}{'E':5}")
print('  '+'-'*114)
tally={m:0 for m in 'ABCDE'}
for slug in ORDER:
    name,d=load(slug)
    R_,refused=audit(name,d)
    line=f"  {name:22}{d['signal_count']:>3}  {d['evidence_staleness']:9}  "
    for m in 'ABCDE':
        v,notes=R_[m]
        if v=='FAIL': tally[m]+=1
        if v=='WITHHELD' and 'W' not in tally: tally['W']=0
        if v=='WITHHELD' and m=='A': tally['W']=tally.get('W',0)+1
        line+=f"{'FAIL' if v=='FAIL' else ('withheld' if v=='WITHHELD' else 'pass'):8}"
    if refused:
        line+='   (refusal)'
        tally['R']=tally.get('R',0)+1
    print(line)
    rows[name]=(R_,d)
print('  '+'-'*114)
print(f"  {'FAILURES':22}{'':14}  "+''.join(f"{tally[m]:<5}" for m in 'ABCDE')
      + f"   withheld_reads={tally.get('W',0)}   refusals={tally.get('R',0)}")
print()
json.dump({k:{'verdicts':{m:R_[m][0] for m in 'ABCDE'},'notes':{m:R_[m][1] for m in 'ABCDE'},
               'read':d} for k,(R_,d) in rows.items()},open('/tmp/audit/report_before.json','w'),indent=2)
print('exact offending text per failure:')
for name,(R_,d) in rows.items():
    for m in 'ABCDE':
        v,notes=R_[m]
        if v=='FAIL':
            print(f"\n  [{m}] {name}  (n={d['signal_count']}, {d['evidence_staleness']}, {d['evidence_age_days']}d)")
            for x in notes: print(f"      - {x}")
