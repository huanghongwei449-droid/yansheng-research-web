"""Deterministic publication adapter. Research and admission remain the owner's responsibility."""
import argparse
import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path
import markdown

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'out'
STATE = ROOT / 'research-history.json'
LABEL = {'premarket': '盘前', 'close': '收盘'}

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)

def esc(s):
    return html.escape(str(s), quote=True)

def render(raw):
    # Research prose contains no external links; keep link labels, never execute raw HTML.
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', raw)
    return markdown.markdown(html.escape(text), extensions=['tables', 'sane_lists'])

def shell(body):
    template = (ROOT / 'public/today.html').read_text(encoding='utf-8')
    style = re.search(r'<style>(.*?)</style>', template, re.S).group(1)
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>研盛 · 每日研究</title><style>' + style +
            '.wide{max-width:1050px;margin:auto;padding:25px}select{max-width:100%;padding:10px}'
            'article table,main table{display:block;overflow:auto;min-width:0;max-width:100%}main table td,main table th{white-space:nowrap}a{text-decoration:underline}</style>'
            '<header><a class="brand" href="today.html">研盛</a><nav>'
            '<a href="today.html">今日研究</a><a href="opportunities.html">机会跟踪</a>'
            '<a href="companies.html">公司研究</a></nav></header>' + body + '</html>')

def build(state):
    records = state['reports']
    # Published archive URLs are immutable, including their original release binding.
    archives = {r['page']: (OUT / r['page']).read_bytes()
                for r in records if (OUT / r['page']).exists()}
    latest = {s: max((r for r in records if r['session'] == s),
                    key=lambda r: (r['date'], r['revision']), default=None) for s in LABEL}
    newest = max(records, key=lambda r: (r['date'], r['session'] == 'close', r['revision']))
    options = ''.join('<option value="' + esc(r['page']) + '">' +
                      esc(f"{r['date']} {LABEL[r['session']]} · 第{r['revision']}版") + '</option>'
                      for r in reversed(records))
    morning = latest['premarket']
    closing = latest['close']
    body = '<div class="frame"><aside><div class="date"></div><div class="session">'
    if morning:
        body += '<button data-session="premarket">盘前</button>'
    if closing:
        body += '<button data-session="close">收盘</button>'
    body += '</div><div class="toc"></div><p>过往研究</p><select aria-label="过往研究" onchange="if(this.value)location.href=this.value"><option value="">选择日期与时段</option>' + options + '</select></aside><main>'
    if morning:
        body += '<article class="doc" id="premarket" data-date="' + esc(morning['date']) + '" hidden><div class="label">' + esc(morning['date']) + ' / 盘前研究</div>' + morning['html'] + '</article>'
    if closing:
        linked = morning and closing['date'] == morning['date'] and closing.get('premarket_sha256') == morning['sha256']
        if linked:
            body += '<article class="doc" id="close" data-date="' + esc(closing['date']) + '" hidden><div class="label">' + esc(closing['date']) + ' / 收盘更新</div>' + closing['html'] + '</article>'
        else:
            body += '<article class="doc" id="close" data-date="' + esc(closing['date']) + '" hidden><h2>收盘更新 · ' + esc(closing['date']) + '</h2><p><a href="' + esc(closing['page']) + '">阅读该日收盘研究</a></p></article>'
    body += '</main></div><footer>研盛 · 每日研究</footer>'
    body += '''<script>
    const docs=[...document.querySelectorAll('article.doc')];
    function show(s){
      if(s==='pre')s='premarket';
      let selected=document.getElementById(s)||document.getElementById('premarket')||docs[0];
      docs.forEach(a=>a.hidden=a!==selected);
      document.querySelectorAll('button[data-session]').forEach(b=>{
        const active=b.dataset.session===selected.id;
        b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));
      });
      document.querySelector('.date').textContent=selected.dataset.date;
      const toc=document.querySelector('.toc');toc.replaceChildren();
      selected.querySelectorAll('h2,h3').forEach((h,i)=>{
        h.id=selected.id+'-'+i;const a=document.createElement('a');a.href='#'+h.id;a.textContent=h.textContent;toc.append(a);
      });
    }
    document.querySelectorAll('button[data-session]').forEach(b=>b.onclick=()=>{
      show(b.dataset.session);history.pushState(null,'','?session='+b.dataset.session);window.scrollTo(0,0);
    });
    function restore(){show(new URLSearchParams(location.search).get('session'));}
    window.addEventListener('popstate',restore);restore();
    </script>'''
    write(OUT / 'today.html', shell(body))
    for r in records:
        # Archive sits beside today.html so ordinary navigation keeps working.
        write(OUT / r['page'], shell('<main class="wide"><p>' + esc(r['date'] + ' ' + LABEL[r['session']]) + '</p>' + r['html'] + '</main>'))
    pools = state.get('pools', [])
    if pools:
        pool = max(pools, key=lambda p: (p['date'], p['session'] == 'close', p['revision']))
        body = '<main class="wide"><div class="label">' + esc(pool['date'] + ' / ' + LABEL[pool['session']]) + '</div><h1>机会跟踪</h1><h2>方向变化</h2>' + render(pool['directions']) + '<h2>二波个股池</h2>'
        if pool.get('schema_version') == 'yansheng-opportunity-pool-v2':
            from opportunity_view import render_pool, STYLE
            body = '<main class="wide"><div class="label">' + esc(pool['date'] + ' / ' + LABEL[pool['session']]) + '</div><h1>机会跟踪</h1><p>' + esc(pool['directions']) + '</p><style>'+STYLE+'</style>'+render_pool(pool)
        elif not pool['items']:
            body += '<p>暂无符合条件的二波标的。</p>'
        for c in pool['items']:
            if pool.get('schema_version') == 'yansheng-opportunity-pool-v2':
                break
            body += '<section><h3><a href="company.html?code=' + esc(c['code']) + '">' + esc(c['name'] + ' ' + c['code']) + '</a></h3><p>' + esc(c['status']) + '</p><p>' + esc(c['summary']) + '</p><p>下一步：' + esc(c['next']) + '</p><p>失效条件：' + esc(c['invalidation']) + '</p><details><summary>变化记录</summary>'
            body += ''.join('<p>' + esc(h['date'] + '：' + h['reason']) + '</p>' for h in c['history']) + '</details></section>'
        body += '</main>'
        write(OUT / 'opportunities.html', shell(body))
    manifest = [{'date': r['date'], 'session': r['session'], 'identity': r['identity'], 'sha256': r['sha256'], 'page': r['page']} for r in records]
    write(OUT / 'publication.json', json.dumps({'reports': manifest, 'poolDate': max((p['date'] for p in pools), default=None)}, ensure_ascii=False, indent=2))
    if (ROOT / 'company-baseline.json').exists():
        from workbench import finalize
        finalize(ROOT, OUT, state, shell)
    for name, raw in archives.items():
        (OUT / name).write_bytes(raw)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', required=True, type=Path)
    ap.add_argument('--date', required=True)
    ap.add_argument('--session', choices=LABEL, required=True)
    ap.add_argument('--identity', choices=['正式版', '审阅版', '历史重编'], required=True)
    ap.add_argument('--pool', type=Path)
    ap.add_argument('--analysis', type=Path)
    ap.add_argument('--workflow', type=Path)
    ap.add_argument('--connections', type=Path, help='Reviewed public company/direction connections for this report')
    ap.add_argument('--checker', type=Path, default=Path('C:/Users/15707/.codex/worktrees/codex-owned-daily-ima/alphaengine/.agents/skills/daily-recap-trading/scripts/production/research_brief.py'))
    ap.add_argument('--receipt', required=True, type=Path)
    a = ap.parse_args()
    date.fromisoformat(a.date)
    raw = a.report.read_bytes()
    text = raw.decode('utf-8-sig')
    brief = None
    research_check = None
    if a.analysis or a.date >= '2026-09-29':
        import importlib.util
        spec = importlib.util.spec_from_file_location('publication_research_check', a.checker)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        research_check = checker.check(raw, a.date, a.session, a.analysis)
        brief = json.loads(a.analysis.read_text(encoding='utf-8-sig'))
    delta = brief and brief.get('editorial', {}).get('mode') == 'delta'
    workflow_check = None
    if a.workflow or a.date >= '2026-10-01':
        import importlib.util
        helper = a.checker.parents[3] / 'yansheng-daily-production/scripts/workflow.py'
        ws = importlib.util.spec_from_file_location('yansheng_workflow', helper)
        workflow = importlib.util.module_from_spec(ws)
        ws.loader.exec_module(workflow)
        workflow_check = workflow.check_export(a.workflow, raw, a.date, a.session, a.analysis, a.pool)
    if brief and brief.get('editorial', {}).get('pool_changes') and not a.pool:
        raise ValueError('二波状态已改变，必须同步机会池')
    if not text.strip() or (not delta and (len(text.strip()) < 100 or len(re.findall(r'^## ', text, re.M)) < 3)):
        raise ValueError('正文不完整，收盘短更新必须通过新版研究契约')
    if re.search(r'[A-Za-z]:[\\/]|(?:api[_-]?key|access_token)\s*[:=]', text, re.I):
        raise ValueError('正文含本机路径或凭据信息')
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'reports': [], 'pools': []}
    same = [r for r in state['reports'] if r['date'] == a.date and r['session'] == a.session]
    existing = next((r for r in same if r['sha256'] == sha(raw) and r['identity'] == a.identity), None)
    if existing:
        record = existing
    else:
        if any(r['date'] > a.date for r in state['reports']):
            raise ValueError('拒绝晚到旧稿；请使用历史归档流程')
        record = {'date': a.date, 'session': a.session, 'identity': a.identity, 'sha256': sha(raw),
                  'revision': max((r['revision'] for r in same), default=0) + 1, 'html': render(text),
                  'page': f'report-{a.date}-{a.session}-{sha(raw)[:12]}.html'}
        if brief:
            record['editorial_version'] = brief.get('editorial', {}).get('version')
            record['premarket_sha256'] = brief.get('editorial', {}).get('premarket_ref', {}).get('report_sha256')
        state['reports'].append(record)
    if brief and a.session == 'close':
        ref = brief.get('editorial', {}).get('premarket_ref', {})
        if ref.get('path') and not any(r['date'] == a.date and r['session'] == 'premarket' and r['sha256'] == ref.get('report_sha256') for r in state['reports']):
            raise ValueError('收盘引用的盘前版本未在网站发布')
    if a.pool:
        pool = json.loads(a.pool.read_text(encoding='utf-8-sig'))
        if a.date >= '2026-10-08':
            from opportunity_view import validate_pool
            previous = max(state.get('pools', []), key=lambda p:(p['date'],p['session']=='close',p['revision']),default=None)
            validate_pool(pool, previous)
        if pool['date'] != a.date or pool['session'] != a.session:
            raise ValueError('机会池日期/时段与正文不一致')
        for k in ['directions', 'items', 'coverage']:
            if k not in pool: raise ValueError('机会池缺字段: ' + k)
        codes = set()
        for c in pool['items']:
            for k in ['code', 'name', 'status', 'summary', 'next', 'invalidation', 'history']:
                if k not in c: raise ValueError('个股缺字段: ' + k)
            if not re.fullmatch(r'\d{6}', c['code']) or c['code'] in codes or not c['history']:
                raise ValueError('代码重复、错误或历史缺失')
            codes.add(c['code'])
        for change in (brief or {}).get('editorial', {}).get('pool_changes', []):
            actual = next((c for c in pool['items'] if c['code'] == change['code']), None)
            if not actual or actual['status'] != change['current']:
                raise ValueError('机会池与研究状态变化不一致')
        previous = next((p for p in reversed(state['pools']) if p['date'] <= a.date), None)
        if previous and not {c['code'] for c in previous['items']}.issubset(codes):
            raise ValueError('旧池个股不可静默消失；退出也需保留历史')
        pool['revision'] = record['revision']
        state['pools'] = [p for p in state['pools'] if (p['date'], p['session'], p['revision']) != (a.date, a.session, record['revision'])] + [pool]
    if a.connections:
        connections = json.loads(a.connections.read_text(encoding='utf-8-sig'))
        if connections.get('report_sha256') != sha(raw):
            raise ValueError('Company connections must bind the reviewed report SHA')
        if not a.analysis or not a.workflow:
            raise ValueError('Connections require research analysis and workflow review')
        previous_connections = state.get('connections', json.loads((ROOT/'workbench-reviewed.json').read_text(encoding='utf8')))
        merged = dict(previous_connections)
        for key, identity_key in [('companies','code'), ('directions','id')]:
            keyed = {x[identity_key]: x for x in previous_connections.get(key, [])}
            keyed.update({x[identity_key]: {**x, 'report_sha256': sha(raw)} for x in connections.get(key, [])})
            merged[key] = list(keyed.values())
        state['connections'] = merged
    build(state)
    write(STATE, json.dumps(state, ensure_ascii=False, indent=2))
    receipt = {'status': 'BUILT_NOT_DEPLOYED', 'date': a.date, 'session': a.session,
               'body_sha256': sha(raw), 'identity': a.identity, 'page': record['page'],
               'research_check': research_check, 'workflow_check': workflow_check, 'pool_updated': bool(a.pool), 'site_project_id': 'appgprj_6ab8df19bdd881918dfc7997fd01fca5'}
    write(a.receipt, json.dumps(receipt, ensure_ascii=False, indent=2))
    print(json.dumps(receipt, ensure_ascii=True))

if __name__ == '__main__':
    main()
