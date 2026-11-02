"""Read-only method/pool consistency; never certifies research truth."""
import argparse
import hashlib
import json
from pathlib import Path

GATES = ('first_funding','first_sector','second_leg','structure','story','capital')
STATES = ('confirmed','candidate','paused','exited')
STAGES = ('awaiting_confirmation','started','extended','unknown')

def active_condition(c):
    if c is None: return False
    if not isinstance(c,dict): return bool(c)
    # Legacy exited records retain deadline/basis metadata, not executable rules.
    return any(v for k,v in c.items() if k not in ('deadline','basis'))

def evidence_errors(g, label):
    if not isinstance(g, dict): return [label+': result object required']
    e=[]
    if g.get('result') not in ('pass','fail','unknown'): e.append(label+': invalid result')
    if not g.get('reason') or not isinstance(g.get('evidence'),list) or not g['evidence'] or not all(isinstance(v,str) and v.strip() for v in g['evidence']):
        e.append(label+': missing reason/evidence references')
    return e

def item_errors(x):
    e=[]; state=x.get('lifecycle_state'); gates=x.get('gates',{})
    if state not in STATES: e.append('invalid lifecycle')
    if x.get('model_type') not in ('A','B','C','D','E','unknown'): e.append('invalid/missing model')
    if x.get('trade_stage') not in STAGES: e.append('invalid/missing stage')
    if x.get('model_type')=='unknown' and not x.get('model_unknown_reason'): e.append('unknown model lacks reason')
    if x.get('trade_stage')=='unknown' and not x.get('stage_unknown_reason'): e.append('unknown stage lacks reason')
    if not isinstance(gates,dict): return e+['six gates required']
    if set(gates)!=set(GATES): e.append('six gates required')
    for k,g in gates.items(): e.extend(evidence_errors(g,k))
    for k in ('renewed_authentication','current_sector_resonance'): e.extend(evidence_errors(x.get(k),k))
    failed=any(isinstance(g,dict) and g.get('result')=='fail' for g in gates.values())
    unknown=any(not isinstance(g,dict) or g.get('result')!='pass' for g in gates.values())
    blocker=x.get('primary_blocker'); gaps=x.get('data_gaps')
    if not isinstance(gaps,list): e.append('data_gaps list required'); gaps=['missing']
    critical=[]
    for g in gaps:
        if isinstance(g,str) and g.strip(): critical.append(g)
        elif isinstance(g,dict) and g.get('description') and isinstance(g.get('affects_admission'),bool) and g.get('basis'):
            if g['affects_admission']: critical.append(g)
        else: e.append('gap requires description/applicability/basis'); critical.append(g)
    if blocker not in ('none','known_failure','research_incomplete','model_pending','trigger_pending','risk_pause','exited'): e.append('invalid blocker')
    if failed and blocker not in ('known_failure','exited'): e.append('known failure concealed')
    if blocker=='known_failure' and not failed: e.append('known failure lacks failed gate')
    if blocker=='research_incomplete' and not critical: e.append('missing specific admission gaps')
    if blocker=='trigger_pending' and (unknown or critical or x.get('model_type')=='unknown' or x.get('trade_stage')=='unknown'): e.append('incomplete research presented as trigger pending')
    if state=='confirmed':
        if unknown or critical or blocker!='none' or x.get('model_type')=='unknown' or x.get('trade_stage')=='unknown': e.append('unqualified confirmed')
        for k in ('renewed_authentication','current_sector_resonance'):
            if not isinstance(x.get(k),dict) or x[k].get('result')!='pass': e.append('confirmed requires '+k)
        c=x.get('current_condition')
        if not isinstance(c,dict) or any(not c.get(k) for k in ('participation','validation','invalidation','deadline','basis')): e.append('confirmed lacks actionable condition')
    if x.get('trade_stage')=='extended' and x.get('latent'): e.append('extended shown latent')
    if state=='paused' and (blocker not in ('known_failure','risk_pause','model_pending') or not x.get('state_reason')): e.append('pause lacks cause')
    if state=='exited' and (blocker!='exited' or active_condition(x.get('current_condition'))): e.append('exit retains current condition')
    for k in ('code','setup_id','revision_id','research_as_of','state_reason'):
        if not x.get(k): e.append('missing '+k)
    if not isinstance(x.get('history'),list): e.append('history list required')
    return e

def transition_errors(before,after):
    e=[]
    if before.get('code')!=after.get('code'): e.append('security identity changed')
    old=before.get('history',[]); new=after.get('history',[])
    if new[:len(old)]!=old: e.append('prior history overwritten')
    reentry=before.get('lifecycle_state')=='exited' and after.get('lifecycle_state')!='exited'
    if reentry:
        if after.get('setup_id')==before.get('setup_id'): e.append('reentry requires new setup')
        if before.get('setup_id') not in after.get('archived_setups',[]): e.append('reentry lacks archived setup')
    elif before.get('setup_id')!=after.get('setup_id') and not after.get('new_structure_evidence'): e.append('ordinary revision changed setup')
    if before.get('frozen_premarket_sha')!=after.get('frozen_premarket_sha'): e.append('frozen premarket changed')
    changed=before.get('lifecycle_state')!=after.get('lifecycle_state') or before.get('setup_id')!=after.get('setup_id')
    if changed and not any(isinstance(v,dict) and v.get('reason') for v in new[len(old):]): e.append('state/setup change lacks appended event reason')
    if before.get('current_condition')!=after.get('current_condition'):
        if not any(isinstance(v,dict) and v.get('reason') for v in new[len(old):]): e.append('condition revision lacks event')
        oldc=before.get('current_condition')
        archives=after.get('archived_conditions',[])
        if isinstance(archives,dict): archives=[archives]
        if active_condition(oldc) and not any(v.get('condition')==oldc for v in archives if isinstance(v,dict)): e.append('prior condition not archived')
    return e

def load_ref(ref,base,label,e):
    if not isinstance(ref,dict) or not ref.get('path') or not ref.get('sha256'): e.append(label+': path/SHA required'); return None
    p=Path(ref['path']);p=p if p.is_absolute() else base/p
    try:
        b=p.read_bytes()
        if hashlib.sha256(b).hexdigest()!=ref['sha256']: e.append(label+': SHA mismatch');return None
        return json.loads(b.decode('utf-8-sig'))
    except (OSError,ValueError) as ex: e.append(label+': unreadable '+str(ex));return None

def index(items,e,label):
    by={}
    for x in items:
        k=x.get('setup_id')
        if not k or k in by: e.append(label+': missing/duplicate setup')
        by[k]=x
    return by

def check(doc,base):
    e=[]
    if doc.get('method_version')!='v3-adoption-q': e.append('method version mismatch')
    pool=load_ref(doc.get('pool_ref'),base,'pool',e)
    if pool is None: return e
    if (doc.get('research_date'),doc.get('session'))!=(pool.get('date'),pool.get('session')): e.append('pool date/session differs')
    by=index(pool.get('items',[]),e,'pool'); records=index(doc.get('items',[]),e,'method')
    if set(by)!=set(records): e.append('sidecar pool coverage differs')
    for sid,x in records.items():
        e.extend(sid+': '+s for s in item_errors(x)); raw=by.get(sid)
        if not raw: continue
        for k in ('code','lifecycle_state','current_condition','history','archived_conditions','archived_setups','frozen_premarket_sha'):
            default=[] if k in ('history','archived_conditions','archived_setups') else None
            if x.get(k,default)!=raw.get(k,default): e.append(sid+': '+k+' differs from actual pool')
        if x.get('research_as_of')!=doc.get('research_date'): e.append(sid+': method date mismatch')
    prev=None
    if doc.get('previous_pool_ref'): prev=load_ref(doc['previous_pool_ref'],base,'previous pool',e)
    elif not isinstance(doc.get('initial_baseline'),dict) or not doc['initial_baseline'].get('reason') or not doc['initial_baseline'].get('checked_scope'): e.append('previous pool or explicit first baseline required')
    if prev:
        rank={'premarket':0,'close':1}
        if (prev.get('date',''),rank.get(prev.get('session'),-1))>=(pool.get('date',''),rank.get(pool.get('session'),-1)): e.append('previous pool must precede current session')
        old=index(prev.get('items',[]),e,'previous pool')
        for sid,b in old.items():
            a=by.get(sid)
            if a is None:
                matches=[v for v in by.values() if v.get('code')==b.get('code') and sid in v.get('archived_setups',[])]
                if len(matches)!=1: e.append(sid+': prior setup missing without reentry archive');continue
                a=matches[0]
            e.extend(sid+': '+s for s in transition_errors(b,a))
    if doc.get('previous_method_ref'):
        olddoc=load_ref(doc['previous_method_ref'],base,'previous method',e)
        if olddoc:
            if olddoc.get('pool_ref')!=doc.get('previous_pool_ref'): e.append('previous method/pool reference differs')
            oldrecords=index(olddoc.get('items',[]),e,'previous method')
            for sid,b in oldrecords.items():
                a=records.get(sid)
                if a is None: a=next((v for v in records.values() if v.get('code')==b.get('code') and sid in v.get('archived_setups',[])),None)
                if a: e.extend(sid+': '+s for s in transition_errors(b,a))
                else: e.append(sid+': previous method setup lost')
    elif not isinstance(doc.get('method_baseline'),dict) or not doc['method_baseline'].get('reason'): e.append('previous method or explicit method baseline required')
    if 'transitions' in doc: e.append('unbound transitions unsupported; actual referenced pools are compared')
    return e

def main():
    a=argparse.ArgumentParser();a.add_argument('--input',required=True);p=Path(a.parse_args().input)
    try: errors=check(json.loads(p.read_text(encoding='utf-8-sig')),p.parent)
    except (KeyError,TypeError,ValueError) as ex: errors=['malformed input: '+str(ex)]
    print(json.dumps({'consistency_pass':not errors,'errors':errors,'research_quality_verified':False},ensure_ascii=False))
    return 1 if errors else 0
if __name__=='__main__': raise SystemExit(main())
