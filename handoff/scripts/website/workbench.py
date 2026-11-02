"""Public projections and immutable release packages. No research inference or model calls."""
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def report_id(r):
    return f"{r['date']}:{r['session']}:{r['sha256']}"


def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', '', value)).strip()


ALIASES = {'600657': ['信达'], '002074': ['国轩'], '300450': ['先导'],
           '001979': ['招商蛇口', '招商'], '600276': ['恒瑞'], '603259': ['药明'],
           '300308': ['中际'], '300502': ['新易盛'], '300394': ['天孚'],
           '002242': ['九阳'], '002119': ['康强'], '002080': ['中材']}


def paragraphs(r, name, code):
    return [plain(p) for p in re.findall(r'<p\b[^>]*>(.*?)</p>', r['html'], re.S)
            if any(alias in plain(p) for alias in [name] + ALIASES.get(code, []))]


def project(root, state):
    baseline = json.loads((root / 'company-baseline.json').read_text(encoding='utf8'))
    reviewed = state.get('connections') or json.loads((root / 'workbench-reviewed.json').read_text(encoding='utf8'))
    records = sorted(state['reports'], key=lambda r: (r['date'], r['session'] == 'close', r['revision']))
    funding_path = root / 'funding-view.json'
    funding_views = json.loads(funding_path.read_text(encoding='utf8')) if funding_path.exists() else {}
    indexed = {report_id(r): r for r in records}
    for link in reviewed.get('directions', []):
        for key in ('premarket_ref', 'close_ref'):
            if key in link and not link[key]:
                del link[key]  # Newly discovered directions have no fictional morning reference.
            if link.get(key) and link[key] not in indexed:
                raise ValueError('Unknown reviewed report reference: ' + link[key])
        link['thesis_id'] = 'direction:' + link['id']
        if link['id'] in funding_views:
            link['funding_view'] = funding_views[link['id']]
    companies = baseline['companies']
    for c in companies.values():
        c['research_date'] = baseline['date']
        c['history_original'] = c.pop('history', [])
        c['records'] = []
        c['depth'] = '历史观察'
    for c in reviewed.get('companies', []):
        if not re.fullmatch(r'\d{6}', c['code']):
            raise ValueError('Invalid company code')
        companies.setdefault(c['code'], {'code': c['code'], 'name': c['name'], 'research_date': None,
                                         'records': [], 'history_original': [], 'depth': '当日观察'})
        companies[c['code']].update(c)
        companies[c['code']]['depth'] = c.get('depth', '当日观察')
        companies[c['code']]['judgment_report_sha256'] = c.get('report_sha256', reviewed['report_sha256'])
        for i, condition in enumerate(companies[c['code']].get('conditions', [])):
            condition['id'] = condition.get('id', 'condition:2026-09-30:' + c['code'] + ':' + str(i))
            condition['purpose'] = '事件参与条件的历史复核' if c['code'] in ('603727', '688143') else '观察与降级条件的历史复核'
            condition['premarket_report_ref'] = next((d.get('premarket_ref') for d in reviewed['directions'] if c['code'] in d['codes']), None)
    for c in companies.values():
        for r in records:
            excerpts = paragraphs(r, c['name'], c['code'])
            if excerpts:
                c['records'].append({'report_id': report_id(r), 'date': r['date'], 'session': r['session'],
                                     'revision': r['revision'], 'page': r['page'], 'excerpts': excerpts})
        # Name matches are only related-text links, never a reviewed company judgment.
        if c.get('judgment_report_sha256'):
            judgment = next((r for r in records if r['sha256'] == c['judgment_report_sha256']), None)
            if judgment is None:
                raise ValueError('Unknown company judgment report: ' + c['code'])
            c['research_date'] = judgment['date']
            c['summary'] = c.get('change') or c.get('role') or c.get('summary', '')
        c['id'] = 'company:' + c['code']
    pools = state.get('pools', [])
    pool = max(pools, key=lambda p: (p['date'], p['session'] == 'close', p['revision']), default=None)
    for p in (pool or {}).get('items', []):
        c = companies.setdefault(p['code'], {'code': p['code'], 'name': p['name'], 'records': [],
                                             'history_original': [], 'depth': '当日观察'})
        c.update({'id': 'company:' + p['code'], 'research_date': pool['date'], 'summary': p['summary'],
                  'status': p['status'], 'next': p['next'], 'boundary': p['invalidation'],
                  'pool_history': p['history'], 'depth': '当日观察'})
        c['boundary'] = c['boundary'].replace('新条件不得回填盘前。', '')
    return {'companies': companies, 'directions': reviewed['directions'],
            'reports': [{**r, 'id': report_id(r)} for r in records], 'pool': pool}


def esc(v):
    return html.escape(str(v or ''), quote=True)


def company_link(c):
    return f'<a href="company.html?code={esc(c["code"])}">{esc(c["name"])}</a>'


def company_pages(data, shell):
    cards = []
    for c in sorted(data['companies'].values(), key=lambda c: (c.get('research_date') or '', c['code']), reverse=True):
        summary = c.get('change') or c.get('status') or c.get('role') or c.get('summary', '公司观察')[:100]
        cards.append(f'<section class="record" data-name="{esc(c["name"]+c["code"])}"><small>{esc(c["code"])} · {esc(c["research_date"])} · {esc(c["depth"])}</small>'
                     f'<h3>{company_link(c)}</h3><p>{esc(summary)}</p></section>')
    listing = '<main class="wide"><h1>公司研究</h1><p>从今日判断，继续看公司。</p><input id="company-search" aria-label="搜索公司" placeholder="公司名称或代码"><div id="company-list">' + ''.join(cards) + '</div><p id="company-empty" hidden>未找到相关公司。</p></main>'
    detail = '<main class="wide"><a id="company-back" href="companies.html">← 返回公司研究</a><div id="company-content"></div></main>'
    return {'companies.html': shell(listing), 'company.html': shell(detail)}


def direction_page(data, shell):
    records = {r['id']: r for r in data['reports']}
    parts = ['<main class="wide"><p><a href="today.html">← 今日研究</a></p><h1>方向变化</h1>']
    for d in data['directions']:
        parts.append(f'<section id="{esc(d["id"])}"><h2>{esc(d["name"])}</h2><p>{esc(d["date"])} · {esc(d["status"])}</p>')
        for label, key in [('盘前为什么看', 'premise'), ('当天验证', 'observed'), ('判断变化', 'change'), ('下一步', 'next')]:
            parts.append(f'<h3>{label}</h3><p>{esc(d[key])}</p>')
        if d.get('funding'):
            parts.append('<h3>资金与扩散</h3><p>' + esc(d['funding']) + '</p>')
        view = d.get('funding_view')
        if view:
            parts.append('<details><summary>查看成交与上涨面变化 · ' + esc(view['scope']) + '</summary>')
            parts.append('<p>成交占比以' + esc(view['denominator']) + '为分母；3/5/10日平均占比分别为' + '/'.join(esc(view['mean_share'][str(n)])+'%' for n in (3,5,10)) + '。</p>')
            parts.append('<div class="table-wrap"><table><tr><th>交易日</th><th>成交（亿元）</th><th>成交占比</th><th>上涨/下跌家数</th></tr>')
            for row in view['series']:
                parts.append('<tr><td>'+esc(row['date'])+'</td><td>'+esc(row['amount_yi'])+'</td><td>'+esc(row['share_percent'])+'%</td><td>'+esc(str(row['advance'])+'/'+str(row['decline']))+'</td></tr>')
            parts.append('</table></div></details>')
        parts.append('<p>' + ' / '.join(company_link(data['companies'][code]) for code in d['codes']) + '</p>')
        for label, key in [('原盘前', 'premarket_ref'), ('当日收盘', 'close_ref')]:
            r = records.get(d.get(key))
            if r:
                parts.append(f'<p><a href="{esc(r["page"])}">{label} · {esc(r["date"])}</a></p>')
        parts.append('</section>')
    return shell(''.join(parts) + '</main>')


def finalize(root, out, state, shell):
    # Company display, pool state and reports share one immutable payload.
    data = project(root, state)
    latest = {s: max((r for r in data['reports'] if r['session'] == s),
                    key=lambda r: (r['date'], r['revision']), default=None) for s in ('premarket', 'close')}
    data['latest'] = {s: r['id'] if r else None for s, r in latest.items()}
    assets = {p.name: p.read_text(encoding='utf8') for p in out.glob('*.html')}
    assets.update(company_pages(data, shell))
    assets['index.html'] = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=today.html"><a href="today.html">今日研究</a>'
    assets['directions.html'] = direction_page(data, shell)
    current_report = max(data['reports'], key=lambda r: (r['date'], r['session'] == 'close', r['revision']))
    change_date = current_report['date']
    change_label = '盘前要点' if current_report['session'] == 'premarket' else '收盘变化'
    changes = '<section class="changes"><h3>' + esc(change_date) + ' ' + change_label + '</h3><p>' + ' / '.join(
        f'<a href="directions.html#{esc(d["id"])}">{esc(d["name"])}：{esc(d["status"])}</a>' for d in data['directions'] if d['date'] == change_date) + '</p></section>'
    target = '<article class="doc" id="' + current_report['session'] + '"'
    pattern = r'(<article class="doc" id="' + re.escape(current_report['session']) + r'"[^>]*>)'
    if target not in assets['today.html']:
        raise ValueError('current session article missing from today page')
    assets['today.html'] = re.sub(pattern, lambda match: match.group(1) + changes, assets['today.html'], count=1)
    # The complete snapshot is part of the release identity; changes in code also create a release.
    public_js = (root / 'public/workbench.js').read_bytes()
    fingerprint = digest((canonical(data) + canonical(assets)).encode('utf8') + public_js + Path(__file__).read_bytes())
    release_id = fingerprint[:24]
    old = json.loads((out / 'publication.json').read_text(encoding='utf8')) if (out / 'publication.json').exists() else {}
    # Exporter retains the previous published_at through rebuilding its legacy manifest.
    previous = json.loads((root / '.openai/last-release.json').read_text(encoding='utf8')) if (root / '.openai/last-release.json').exists() else {}
    stamp = (previous.get('prepared_at') if previous.get('release_id') == release_id else None) or datetime.now(timezone.utc).isoformat()
    css = '<style>.record{padding:16px 0;border-bottom:1px solid #d8d3c8}.record small{color:#66655c}.changes{border-bottom:1px solid #d8d3c8;margin-bottom:20px}.changes h3{font-size:15px;margin:12px 0}.changes p{font-size:14px}.update-notice{position:sticky;top:0;background:#eee9df;padding:8px 20px;z-index:3;font-size:14px}.update-notice[hidden]{display:none}.company-links{font-size:14px}</style>'
    banner = '<div class="update-notice" id="update-notice" hidden role="status"><span></span><button id="accept-update">更新</button></div>'
    for name, page in list(assets.items()):
        historical = name.startswith('report-')
        assets[name] = page.replace('</style>', '</style>' + css, 1).replace('<header>', banner + '<header>', 1).replace(
            '</html>', f'<script>window.YANSHENG_RELEASE={json.dumps(release_id)};window.YANSHENG_HISTORICAL={str(historical).lower()};</script><script src="workbench.js"></script></html>')
    payload = {'schema': 'yansheng-workbench-v1', 'release_id': release_id, 'prepared_at': stamp,
               'published_at': None,
               'data': data, 'pages': assets}
    raw = canonical(payload).encode('utf8')
    bundle = 'release-' + release_id + '.json'
    (out / bundle).write_bytes(raw)
    (out / 'workbench.js').write_bytes(public_js)
    # Remove stale prototype script from the public surface; baseline remains in source only.
    if (out / 'research-data.js').exists():
        (out / 'research-data.js').unlink()
    for name, page in assets.items():
        page = page.replace('<script src="workbench.js">', '<script>window.YANSHENG_BUNDLE_SHA=' + json.dumps(digest(raw)) + ';</script><script src="workbench.js">')
        (out / name).write_text(page, encoding='utf8')
    manifest = {**old, 'schema': 'yansheng-publication-v2', 'release_id': release_id,
                'published_at': None, 'prepared_at': stamp, 'bundle': {'path': bundle, 'sha256': digest(raw)},
                'latest': data['latest'], 'poolDate': (data['pool'] or {}).get('date'),
                'companyDates': {code: c.get('research_date') for code, c in data['companies'].items()}}
    (out / 'publication.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    (root / '.openai/last-release.json').write_text(json.dumps({'release_id': release_id, 'prepared_at': stamp}), encoding='utf8')
    return manifest
