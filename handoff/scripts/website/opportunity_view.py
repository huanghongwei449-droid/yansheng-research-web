"""Reviewed opportunity states and deterministic presentation; no research inference."""
import html
from datetime import date, datetime

SCHEMA = 'yansheng-opportunity-pool-v2'
LABELS = {'confirmed':'二波确认','candidate':'候选观察','paused':'暂缓参与','exited':'已退出'}
EVENT_LABELS = {'added':'新增候选','confirmed':'确认','downgraded':'降级','exited':'退出','risk':'风险提示','reevaluated':'重新评估'}
EVENT_ORDER = {'exited':0,'downgraded':1,'risk':2,'added':3,'confirmed':4,'reevaluated':5}

def validate_pool(pool, previous=None):
    if pool.get('schema_version') != SCHEMA:
        raise ValueError('机会池须提供已审阅的四状态分类，禁止猜测旧标签')
    date.fromisoformat(pool['date'])
    if pool.get('session') not in ('premarket','close'): raise ValueError('pool session')
    items={}; setups=set()
    for c in pool['items']:
        code=c['code']; state=c.get('lifecycle_state'); setup=c.get('setup_id')
        if code in items or not isinstance(code,str) or len(code)!=6 or not code.isdigit(): raise ValueError('duplicate/invalid current stock')
        if state not in LABELS or not setup or setup in setups: raise ValueError('unknown state or duplicate setup')
        setups.add(setup);items[code]=c
        for field in ('state_reason','research_as_of','role','stage','watch_reason','change_summary','review_trigger'):
            if not c.get(field): raise ValueError('missing reviewed field: '+field)
        date.fromisoformat(c['research_as_of'])
        if c['research_as_of']>pool['date']: raise ValueError('future research date')
        if c.get('state_since'):
            date.fromisoformat(c['state_since'])
            if c['state_since']>pool['date']: raise ValueError('future state date')
        if not c.get('history') or not isinstance(c.get('current_condition'),dict): raise ValueError('history/current condition missing')
        cond=c['current_condition']
        if state=='exited' and any(cond.get(x) for x in ('participation','validation','invalidation')): raise ValueError('exited setup has active condition')
        if state in ('confirmed','candidate') and not all(cond.get(x) for x in ('validation','invalidation','deadline','basis')): raise ValueError('active condition incomplete')
        if state=='confirmed' and (not c.get('full_due_diligence_verified') or not cond.get('participation')): raise ValueError('unreviewed confirmation')
    ids=set()
    for e in pool.get('events',[]):
        if e.get('event_id') in ids or not e.get('event_id'): raise ValueError('duplicate event')
        ids.add(e['event_id']);c=items.get(e.get('code'))
        if not c or e.get('setup_id')!=c['setup_id'] or e.get('event_type') not in EVENT_LABELS: raise ValueError('event identity/type')
        if e.get('to')!=c['lifecycle_state'] or e.get('from') not in list(LABELS)+['untracked']: raise ValueError('event state mismatch')
        for k in ('observed_at','observed_date','condition_ref','summary','action','source_report_ref','comparison_report_ref'):
            if not e.get(k): raise ValueError('missing event field '+k)
        date.fromisoformat(e['observed_date'])
        if e['observed_date']!=pool['date']: raise ValueError('today event outside research date')
        if len(e['observed_at'])>10:
            dt=datetime.fromisoformat(e['observed_at']);
            if dt.tzinfo is None or dt.date().isoformat()!=e['observed_date']:raise ValueError('event timezone/date')
        elif date.fromisoformat(e['observed_at']).isoformat()!=e['observed_date']:raise ValueError('event date')
        condition=pool.get('condition_catalog',{}).get(e['condition_ref'])
        if not condition or condition.get('code')!=e['code'] or not condition.get('original_condition') or not condition.get('evidence_refs'):raise ValueError('unbound event condition')
        current_ref=pool['date']+':'+pool['session']+':'+pool.get('research_report_sha256','')
        if e['source_report_ref']!=current_ref or condition.get('source_report_ref')!=current_ref:raise ValueError('event report binding')
        if e['event_type'] in ('downgraded','exited','confirmed') and e['from']==e['to']: raise ValueError('routine check disguised as transition')
        if e['event_type']=='added' and (e['from']!='untracked' or e['to']!='candidate'):raise ValueError('addition is not candidate')
        if e['event_type']=='exited' and e['to']!='exited': raise ValueError('invalid exit')
        if e['event_type']=='downgraded' and e['to']!='paused': raise ValueError('invalid downgrade')
        if e['event_type']=='risk' and not e.get('condition_result'):raise ValueError('risk lacks actual condition result')
    if previous:
        for old in previous.get('items',[]):
            new=items.get(old['code'])
            if not new:raise ValueError('old stock silently removed')
            if new['history'][:len(old['history'])]!=old['history']:raise ValueError('old history changed')
            if old.get('lifecycle_state')=='exited' and new['lifecycle_state']!='exited':
                if old.get('setup_id')==new['setup_id'] or not new.get('archived_setups') or not any(a.get('setup_id')==old['setup_id'] and a.get('history')==old['history'] for a in new['archived_setups']):raise ValueError('reentry erased old setup')
    return {'schema':SCHEMA,'counts':{s:sum(c['lifecycle_state']==s for c in items.values()) for s in LABELS},'events':len(ids),'semantic_quality_verified':False}

def esc(v): return html.escape(str(v),quote=True)
def link(c):return '<a href="company.html?code='+esc(c['code'])+'">'+esc(c['name']+' '+c['code'])+'</a>'
def history(c):
    return '<details class="pool-history"><summary>变化记录</summary>'+''.join('<p>'+esc(h['date']+'：'+h['reason'])+'</p>' for h in c['history'])+'</details>'

def render_pool(pool):
    result=validate_pool(pool);counts=result['counts'];events=sorted(pool.get('events',[]),key=lambda e:(EVENT_ORDER[e['event_type']],e['event_id']))
    bycode={c['code']:c for c in pool['items']}
    body='<nav class="pool-counts" aria-label="二波状态分类">'+''.join('<a href="#pool-'+s+'">'+LABELS[s]+' '+str(counts[s])+'</a>' for s in LABELS)+'</nav>'
    def event_row(e):
        c=bycode[e['code']]
        return '<p class="pool-event"><strong>'+EVENT_LABELS[e['event_type']]+' · <a href="#setup-'+esc(c['code'])+'">'+esc(c['name'])+'</a></strong>：'+esc(e['summary'])+'</p>'
    if events:
        body+='<section class="pool-changes"><h2>今日变化</h2>'+''.join(map(event_row,events[:3]))
        if len(events)>3:body+='<details><summary>其余变化 '+str(len(events)-3)+'</summary>'+''.join(map(event_row,events[3:]))+'</details>'
        body+='</section>'
    for state,label in LABELS.items():
        group=[c for c in pool['items'] if c['lifecycle_state']==state]
        changed={e['code'] for e in events}
        group.sort(key=lambda c:(c.get('research_priority') if c.get('research_priority') is not None else 999,c['code'] not in changed,-date.fromisoformat(c['state_since']).toordinal() if c.get('state_since') else 0,c['code']))
        if state=='exited':body+='<details class="pool-exited" id="pool-exited"><summary>已退出 · '+str(len(group))+'</summary>'
        else:body+='<section class="pool-group" id="pool-'+state+'"><h2>'+label+' · '+str(len(group))+'</h2>'
        if not group:body+='<p class="pool-empty">'+('暂无确认的二波机会。' if state=='confirmed' else '暂无'+label+'对象。')+'</p>'
        for c in group:
            cond=c['current_condition'];body+='<section class="pool-item" id="setup-'+c['code']+'"><h3>'+link(c)+'</h3>'
            if state in ('confirmed','candidate'):
                body+='<p><strong>'+esc(c['stage'])+'</strong> · '+esc(c['role'])+'</p><p>'+esc(c['watch_reason'])+'</p><p>变化：'+esc(c['change_summary'])+'</p><p>'+('参与条件：'+esc(cond['participation']) if state=='confirmed' else '待确认：'+esc(cond['validation']))+'；'+esc(cond['invalidation'])+'</p>'
            elif state=='paused':body+='<p>'+esc(c['state_reason'])+'</p><p>重评条件：'+esc(c['review_trigger'])+'</p>'
            else:body+='<p>退出日期：'+esc(c.get('state_since') or '待核')+' · '+esc(c['state_reason'])+'</p>'
            body+='<details class="pool-details"><summary>条件与依据</summary><p>'+esc(c['summary'])+'</p>'
            if state!='exited':body+='<p>'+esc(c['next'])+'</p><p>'+esc(c['invalidation'])+'</p><p>观察期限：'+esc(cond.get('deadline') or '新结构形成后重评')+'</p>'
            else:body+='<p>本轮已结束；重新入池须建立新结构。</p>'
            body+='</details>'+history(c)+'</section>'
        body+='</details>' if state=='exited' else '</section>'
    return body

STYLE='.pool-counts{display:flex;flex-wrap:wrap;gap:12px 24px;margin:18px 0}.pool-group,.pool-changes{border-top:1px solid #d8d3c8;padding:12px 0}.pool-item{padding:10px 0;border-bottom:1px solid #e1dcd2;overflow-wrap:anywhere}.pool-item h3{margin:8px 0}.pool-item p{margin:8px 0}.pool-details,.pool-history{font-size:14px;color:#66655c;margin:10px 0}.pool-exited{border-top:1px solid #d8d3c8;padding:20px 0}.pool-exited>summary{font-size:24px;font-weight:600}.pool-empty{color:#66655c}.pool-event{font-size:15px}.pool-counts a{white-space:nowrap}@media(max-width:600px){.wide{padding:20px!important}.pool-counts{gap:8px 15px}.pool-exited>summary{font-size:22px}}'
