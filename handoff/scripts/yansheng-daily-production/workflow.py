"""Record real artifacts and enforce order. This does not judge research quality."""
import argparse
import hashlib
import importlib.util
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# This file is also loaded by path from the public exporter and publisher.
_audit_spec = importlib.util.spec_from_file_location('yansheng_reading_audit', Path(__file__).with_name('reading_audit.py'))
_audit_module = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(_audit_module)
audit_reading = _audit_module.audit

STAGES = ('preflight', 'sources', 'research', 'draft', 'review', 'build', 'publish')
CRITERIA = {'market', 'causes', 'continuity', 'narratives', 'companies', 'action', 'writing'}

def require(ok, message):
    if not ok:
        raise ValueError(message)

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(p)

def load_state(path):
    s = read(path)
    require(s.get('schema') == 'yansheng-workflow-v1', 'unknown workflow schema')
    stages = s.get('stages', [])
    require([x['stage'] for x in stages] == list(STAGES[:len(stages)]), 'stages are not sequential')
    for stage in stages:
        require(bool(stage['artifacts']), 'stage without artifacts')
        for f in stage['artifacts']:
            require(Path(f['path']).is_file() and digest(f['path']) == f['sha256'],
                    'artifact changed or missing; create a revision and review again: ' + f['path'])
    return s

def artifact(s, stage, name):
    entries = next((x['artifacts'] for x in s['stages'] if x['stage'] == stage), [])
    found = [x for x in entries if Path(x['path']).name == name]
    require(len(found) == 1, f'{stage} requires exactly one {name}')
    return Path(found[0]['path'])

def check_coverage_scope(s, coverage, now=None):
    # A complete night snapshot cannot represent the current morning/close.
    if coverage.get('status') != 'COMPLETE_RECORDED':
        return
    local_tz = timezone(timedelta(hours=8))
    local = (now or datetime.now(timezone.utc)).astimezone(local_tz)
    if local.date().isoformat() != s['date']:
        return
    anchor_time = time(6, 30) if s['session'] == 'premarket' else time(15, 5)
    anchor = datetime.combine(local.date(), anchor_time, local_tz)
    if local >= anchor:
        cutoff = datetime.fromisoformat(coverage['window_cutoff'].replace('Z', '+00:00')).astimezone(local_tz)
        require(cutoff >= anchor, 'complete coverage stops before current session; collect current increment')

def review_check(s):
    report = artifact(s, 'draft', 'report.md')
    body = report.read_text(encoding='utf-8-sig')
    brief = read(artifact(s, 'research', 'research-brief.json'))
    review = read(artifact(s, 'review', 'editorial-review.json'))
    check = read(artifact(s, 'review', 'research-check.json'))
    sha = digest(report)
    require(brief.get('date') == s['date'] and brief.get('session') == s['session'], 'brief date/session mismatch')
    require(all(x.get('report_sha256') == sha for x in (brief, review, check)), 'review/report SHA mismatch')
    require(check.get('status') in ('STRUCTURE_PASS', 'STRUCTURE_PASS_SEMANTICS_REVIEWED'), 'research structure failed')
    require(check.get('date') == s['date'] and check.get('session') == s['session'], 'research check date/session mismatch')
    require(check.get('analysis_sha256') == digest(artifact(s, 'research', 'research-brief.json')), 'research check brief SHA mismatch')
    require(review.get('verdict') in ('ready', 'limited'), 'editorial review requires revision')
    require(bool(review.get('central_claim', '').strip()), 'central claim missing')
    require(review['central_claim'] in body, 'central claim must quote the actual report')
    if review['verdict'] == 'limited':
        require(bool(review.get('scope_reason', '').strip()), 'limited report needs a concrete scope reason')
    if s['date'] >= '2026-10-28':
        source_audit = read(artifact(s, 'sources', 'zsxq-coverage-audit.json'))
        source_review = review.get('source_coverage', {})
        require(source_review.get('status') == source_audit.get('status'), 'editorial source coverage differs from audit')
        require(bool(source_review.get('reason', '').strip()), 'editorial source coverage needs actual review reason')
        if source_audit['status'] != 'COMPLETE_RECORDED':
            require(bool(source_review.get('affected_judgments', '').strip()), 'incomplete source coverage needs affected judgments')
    criteria = review.get('criteria', [])
    require(len(criteria) == len(CRITERIA) and {x['id'] for x in criteria} == CRITERIA, 'review criteria missing/duplicated')
    for item in criteria:
        require(bool(item.get('reason', '').strip()), 'review reason missing')
        if item.get('result') == 'not_applicable':
            require(s['session'] == 'close' and item['id'] == 'narratives', 'required criterion cannot be skipped')
        else:
            require(item.get('result') == 'pass', 'editorial issue remains: ' + item['id'])
            excerpt = item.get('excerpt', '').strip()
            require(bool(excerpt) and excerpt in body, 'review excerpt not present in report: ' + item['id'])
    if brief.get('opportunity_pool_ref'):
        pool_path = artifact(s, 'research', 'public-pool.json')
        ref = brief['opportunity_pool_ref']
        require(Path(ref['path']).resolve() == pool_path.resolve() and ref['sha256'] == digest(pool_path), 'classified pool not registered')
        pool = read(pool_path)
        require(pool.get('research_report_sha256') == sha, 'pool/report SHA mismatch')
        opp = review.get('opportunity_review', {})
        require(opp.get('counts') == check.get('opportunity_check', {}).get('counts'), 'classification content review/count mismatch')
        require(opp.get('excerpt') and opp['excerpt'] in body and opp.get('reason') and opp.get('history_preserved') and opp.get('conditions_reviewed'), 'actual opportunity content review missing')
    return report

def check_export(path, raw, research_date, session, analysis, pool_path=None):
    require(path is not None, 'new research requires --workflow')
    s = load_state(path)
    require(s['date'] == research_date and s['session'] == session, 'workflow date/session mismatch')
    require(len(s['stages']) >= 5, 'complete sources/research/draft/review before export')
    report = review_check(s)
    require(report.read_bytes() == raw, 'export report differs from reviewed report')
    require(Path(analysis).resolve() == artifact(s, 'research', 'research-brief.json').resolve(), 'export brief not registered')
    brief = read(Path(analysis))
    if brief.get('opportunity_pool_ref'):
        require(pool_path and Path(pool_path).resolve() == artifact(s, 'research', 'public-pool.json').resolve(), 'export must use reviewed classification pool')
    return {'status': 'ARTIFACTS_AND_REVIEW_RECORD_VERIFIED', 'semantic_quality_verified': False,
            'report_sha256': digest(report), 'workflow': str(Path(path).resolve())}

def record(path, stage, files):
    s = load_state(path)
    require(len(s['stages']) < len(STAGES) and stage == STAGES[len(s['stages'])], 'cannot skip or overwrite a stage')
    require(files and all(Path(f).is_file() for f in files), 'real artifact files required')
    if stage == 'sources' and s['date'] >= '2026-10-28':
        reports = [Path(f) for f in files if Path(f).name == 'zsxq-coverage-audit.json']
        require(len(reports) == 1, 'sources requires zsxq-coverage-audit.json; partial or unknown must be explicit')
        recorded = read(reports[0])
        require(recorded.get('schema') == 'yansheng-zsxq-reading-audit-v1', 'invalid source audit schema')
        inputs = recorded.get('input_files', [])
        require(len(inputs) == 3, 'source audit requires enumeration, ledger and coverage files')
        current = audit_reading(*(x['path'] for x in inputs),
                                sealed_at=(recorded.get('temporal_integrity') or {}).get('reference_sealed_at'))
        require(recorded == current, 'source audit stale or input data changed; rerun audit')
        require(recorded.get('status') in ('COMPLETE_RECORDED','PARTIAL','UNKNOWN'), 'invalid coverage status')
        check_coverage_scope(s, recorded)
    s['stages'].append({'stage': stage, 'completed_at': datetime.now(timezone.utc).isoformat(),
                        'artifacts': [{'path': str(Path(f).resolve()), 'sha256': digest(f)} for f in files]})
    if stage in ('review', 'build', 'publish'):
        report = review_check(s)
    if stage in ('build', 'publish'):
        receipt = read(artifact(s, 'build', 'web-build-receipt.json'))
        require(receipt.get('date') == s['date'] and receipt.get('session') == s['session'], 'build date/session mismatch')
        require(receipt.get('body_sha256') == digest(report), 'build report SHA mismatch')
        require(receipt.get('status') == 'BUILT_NOT_DEPLOYED', 'invalid build receipt')
        artifact(s, 'build', 'page-review.md')
    if stage == 'publish':
        receipt = read(artifact(s, 'publish', 'web-deployment-receipt.json'))
        require(receipt.get('status') == 'succeeded', 'publication has not succeeded')
        require(receipt.get('research_date', receipt.get('date')) == s['date'] and receipt.get('session') == s['session'], 'publication date/session mismatch')
        require(receipt.get('report_sha256', receipt.get('body_sha256')) == digest(report), 'publication report SHA mismatch')
        require(all(receipt.get(k) for k in ('project_id', 'version_id', 'url')), 'publication identifiers missing')
        require(receipt.get('deployment_id', receipt.get('id')), 'deployment id missing')
    save(path, s)
    return s

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['init', 'status', 'record'])
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--date')
    ap.add_argument('--session', choices=['premarket', 'close'])
    ap.add_argument('--stage', choices=STAGES)
    ap.add_argument('--files', nargs='+', type=Path)
    a = ap.parse_args()
    path = a.run / 'workflow-state.json'
    if a.command == 'init':
        require(not path.exists(), 'existing run must be resumed, not overwritten')
        require(a.date and a.session, 'date and session required')
        date.fromisoformat(a.date)
        save(path, {'schema': 'yansheng-workflow-v1', 'date': a.date, 'session': a.session, 'stages': []})
    elif a.command == 'record':
        record(path, a.stage, a.files)
    s = load_state(path)
    print(json.dumps({'completed': [x['stage'] for x in s['stages']],
                      'next': STAGES[len(s['stages'])] if len(s['stages']) < len(STAGES) else None,
                      'publication_complete': len(s['stages']) == len(STAGES),
                      'semantic_quality_verified': False}, ensure_ascii=False))

if __name__ == '__main__':
    main()
