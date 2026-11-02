"""Market-first research contract. Structural validation is NOT factual verification."""
import argparse
from datetime import date as Date, datetime
import hashlib
import json
from pathlib import Path
import re

EFFECTIVE_DATE = '2026-09-29'
VERSION = 'research-brief-v1'
EDITORIAL_VERSION = 'premarket-close-v1'
EDITORIAL_REQUIRED_DATE = '2026-10-01'


class ResearchBriefError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ResearchBriefError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def make_template(editorial=False):
    """Deliberately incomplete: author must research before validating."""
    result = {
        'schema_version': VERSION, 'date': '', 'session': '', 'report_sha256': '',
        'market': {'headline': '', 'risk_change': '', 'dominant_question': '',
                   'evidence_refs': []},
        'evidence': [],  # id, source, observed_at (ISO), kind: fact/opinion/rumor
        'causes': [],  # explanation, mechanism, evidence_refs, counterevidence,
                      # alternative, priority_reason; ranked by author, not software
        'magnitude': {
            'day': '', 'five_day': '', 'ten_day': '', 'turnover_context': '',
            'key_levels': '', 'breadth_and_leaders': '', 'evidence_refs': []},
        'narrative_updates': [],  # topic, prior, new_information, information_kind,
                                  # event_id, price_response, judgement, evidence_refs
        'strategy': {'implication': '', 'exceptions_vs_market': '', 'next_checks': []},
        'review_notes': {'opening_risk': '', 'cause_priority': '',
                         'magnitude': '', 'opportunity_balance': ''},
        'gaps': [],  # Named auxiliary gaps; never means zero opportunities.
        'continuity': {
            'previous_record': {'path': '', 'sha256': '', 'gap_reason': ''},
            'funding': [],  # topic_key, dates, scope, amount_and_share, relative_strength,
                           # breadth, core_absorption, continuity_judgement, gaps, evidence_refs
            'mainlines': [],  # topic_key, prior_stage, stage, prior, independent_update,
                              # counterevidence, mechanism, judgement, next_check, evidence_refs
            'patterns': [],  # pattern_key, assessment, sample_scope, sample_ids,
                             # prospective_count, exploration_note, implication, next_check
            'strategy_change': '',
        },
    }
    if editorial:
        result['editorial'] = {
            'version': EDITORIAL_VERSION, 'mode': '',
            'focus': [], 'no_priority_reason': '',
            'frozen_conditions': [],
            'premarket_ref': {'path': '', 'sha256': '', 'gap_reason': ''},
            'verification': [], 'material_changes': [],
            'no_material_change_reason': '', 'pool_changes': [],
            'pool_review': '', 'dedup_review': '',
        }
    return result


def validate_editorial(brief, body, refs, fields):
    e = brief.get('editorial')
    if e is None:
        require(brief['date'] < EDITORIAL_REQUIRED_DATE, 'new research requires editorial contract')
        return
    require(isinstance(e, dict) and e.get('version') == EDITORIAL_VERSION, 'unknown editorial contract')
    session = brief['session']
    require(e.get('mode') == ('full' if session == 'premarket' else 'delta'), 'editorial mode/session mismatch')
    fields(e, ['pool_review', 'dedup_review'], 'editorial review')
    require(isinstance(e.get('pool_changes'), list), 'pool changes must be explicit list')
    for change in e['pool_changes']:
        fields(change, ['code', 'prior', 'current', 'reason', 'next_check'], 'pool change')
        refs(change)
    if session == 'premarket':
        require(re.findall(r'^## (.+)$', body, re.M) == ['市场背景', '题材与驱动', '今日计划与观察名单'], 'premarket headings mismatch')
        focus = e.get('focus')
        frozen = e.get('frozen_conditions')
        require(isinstance(frozen, list) and frozen, 'premarket must freeze testable conditions')
        condition_ids = set()
        for condition in frozen:
            fields(condition, ['id', 'subject', 'condition', 'deadline'], 'frozen condition')
            require(condition['id'] not in condition_ids, 'duplicate frozen condition id')
            condition_ids.add(condition['id'])
        require(isinstance(focus, list), 'focus must be a list')
        require(bool(focus) or text(e.get('no_priority_reason')), 'no priorities requires research explanation')
        for item in focus:
            fields(item, ['company', 'theme_evolution', 'increment', 'business_role', 'benefit_mechanism',
                          'market_role', 'peer_comparison', 'prior_price_story', 'priced_in',
                          'catalyst_type', 'price_stage', 'alpha_beta', 'why_now', 'validation_basis'], 'priority company')
            require(item['company'] in body, 'priority company absent from prose')
            refs(item)
    else:
        prior = None
        reference = e.get('premarket_ref', {})
        if reference.get('path'):
            p = Path(reference['path'])
            require(p.is_file(), 'same-day premarket brief missing')
            raw = p.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == reference.get('sha256'), 'premarket brief SHA mismatch')
            prior = json.loads(raw.decode('utf-8-sig'))
            require(prior.get('date') == brief['date'] and prior.get('session') == 'premarket', 'premarket reference date/session mismatch')
            require(prior.get('report_sha256') == reference.get('report_sha256'), 'premarket report SHA mismatch')
            if prior.get('editorial', {}).get('version') != EDITORIAL_VERSION:
                cp = Path(reference.get('conditions_path', ''))
                require(cp.is_file(), 'legacy premarket needs its original frozen conditions file')
                cr = cp.read_bytes()
                require(hashlib.sha256(cr).hexdigest() == reference.get('conditions_sha256'), 'legacy frozen conditions SHA mismatch')
                legacy_conditions = json.loads(cr.decode('utf-8-sig'))
                require(isinstance(legacy_conditions, list) and legacy_conditions, 'legacy frozen conditions empty')
                require(all(c.get('report_sha256') == prior['report_sha256'] for c in legacy_conditions), 'legacy conditions report SHA mismatch')
                prior = dict(prior, editorial={'version': EDITORIAL_VERSION, 'frozen_conditions': [{'id': c['id'], 'condition': c['threshold']} for c in legacy_conditions]})
        else:
            require(text(reference.get('gap_reason')), 'missing premarket needs explicit gap')
        verification = e.get('verification')
        require(isinstance(verification, list), 'verification list required')
        require(bool(verification) or text(reference.get('gap_reason')), 'frozen premarket conditions must be reviewed')
        for item in verification:
            fields(item, ['subject', 'original_condition', 'observed', 'result', 'implication'], 'premarket verification')
            require(item['result'] in ['confirmed', 'invalidated', 'pending', 'not_triggered', 'unverified'], 'invalid verification result')
            refs(item)
        if prior and prior.get('editorial', {}).get('version') == EDITORIAL_VERSION:
            frozen = prior['editorial']['frozen_conditions']
            reviewed = {v.get('condition_id'): v for v in verification}
            require(len(reviewed) == len(verification) and set(reviewed) == {c['id'] for c in frozen}, 'review every frozen condition exactly once')
            for condition in frozen:
                require(reviewed[condition['id']]['original_condition'] == condition['condition'], 'frozen condition rewritten')
        changes = e.get('material_changes')
        require(isinstance(changes, list), 'material changes list required')
        require(bool(changes) or text(e.get('no_material_change_reason')), 'no change must be explained')
        for item in changes:
            fields(item, ['summary', 'implication', 'public_anchor'], 'material change')
            require(item['public_anchor'] in body, 'material change missing from close prose')
            refs(item)
        for change in e['pool_changes']:
            require(change['code'] in body or change.get('name', '\0') in body, 'pool state change must appear in close prose')


def validate(raw, brief, date, session):
    Date.fromisoformat(date)
    require(isinstance(brief, dict), 'analysis must be an object')
    require(brief.get('schema_version') == VERSION, 'analysis schema version mismatch')
    require(brief.get('date') == date and brief.get('session') == session,
            'analysis date/session mismatch')
    require(brief.get('report_sha256') == hashlib.sha256(raw).hexdigest(),
            'report changed: review and rebind analysis SHA256')
    evidence = brief.get('evidence')
    require(isinstance(evidence, list) and evidence, 'dated evidence is required')
    ids = set()
    for item in evidence:
        require(isinstance(item, dict), 'evidence item must be an object')
        require(text(item.get('id')) and item['id'] not in ids, 'evidence id missing/duplicate')
        require(text(item.get('source')), 'evidence source missing')
        require(item.get('kind') in ('fact', 'opinion', 'rumor'), 'evidence kind missing')
        try:
            datetime.fromisoformat(item.get('observed_at', ''))
        except (TypeError, ValueError):
            raise ResearchBriefError('evidence observed_at must be ISO date/time')
        ids.add(item['id'])

    def refs(item):
        r = item.get('evidence_refs')
        require(isinstance(r, list) and r and all(isinstance(x, str) and x in ids for x in r),
                'evidence_refs must resolve to evidence ids')

    def fields(item, keys, label):
        require(isinstance(item, dict), label + ' must be an object')
        require(all(text(item.get(k)) for k in keys), label + ': missing substantive fields')

    market = brief.get('market')
    fields(market, ['headline', 'risk_change', 'dominant_question'], 'market diagnosis')
    refs(market)
    body = raw.decode('utf-8-sig')
    prose = re.sub(r'^#{1,6} .*$', '', body, flags=re.M)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', prose) if p.strip()]
    opening = '\n'.join(paragraphs[:2])
    require(market['headline'] in opening and market['risk_change'] in opening,
            'opening two paragraphs must state the reviewed market diagnosis and risk change')
    causes = brief.get('causes')
    require(isinstance(causes, list) and 1 <= len(causes) <= 3, 'rank 1-3 primary research questions')
    for c in causes:
        fields(c, ['explanation', 'mechanism', 'counterevidence', 'alternative', 'priority_reason'], 'cause')
        refs(c)
    magnitude = brief.get('magnitude')
    fields(magnitude, ['day', 'five_day', 'ten_day', 'turnover_context',
                       'key_levels', 'breadth_and_leaders'], 'magnitude')
    refs(magnitude)
    updates = brief.get('narrative_updates')
    require(isinstance(updates, list) and updates, 'narrative continuity records required')
    for n in updates:
        fields(n, ['topic', 'prior', 'new_information', 'event_id', 'price_response', 'judgement'], 'narrative')
        require(n.get('information_kind') in ('new_fact', 'interpretation', 'repeat', 'counterevidence', 'no_verified_increment'),
                'narrative information_kind invalid')
        refs(n)
    strategy = brief.get('strategy')
    fields(strategy, ['implication', 'exceptions_vs_market'], 'strategy')
    require(isinstance(strategy.get('next_checks'), list) and strategy['next_checks']
            and all(text(x) for x in strategy['next_checks']), 'testable next checks required')
    fields(brief.get('review_notes'), ['opening_risk', 'cause_priority', 'magnitude', 'opportunity_balance'],
           'Codex semantic review notes')
    require(isinstance(brief.get('gaps'), list) and all(text(x) for x in brief['gaps']), 'gaps must be explicit list')
    if date >= '2026-09-30':
        c = brief.get('continuity')
        require(isinstance(c, dict), 'continuous market tracking required from 09-30')
        prior = c.get('previous_record', {})
        if prior.get('path'):
            p = Path(prior['path'])
            require(p.is_file(), 'previous tracking record missing')
            require(hashlib.sha256(p.read_bytes()).hexdigest() == prior.get('sha256'), 'previous tracking SHA mismatch')
            previous = json.loads(p.read_text(encoding='utf-8-sig'))
            require((previous['date'], previous['session'] == 'close') < (date, session == 'close'), 'previous record must precede this session')
        else:
            require(text(prior.get('gap_reason')), 'tracking restart needs a real gap reason')
        for key in ('funding', 'mainlines', 'patterns'):
            require(isinstance(c.get(key), list) and c[key], key + ' tracking required')
        keys = set()
        for f in c['funding']:
            fields(f, ['topic_key','scope','amount_and_share','relative_strength','breadth','core_absorption','continuity_judgement'], 'funding')
            require(f['topic_key'] not in keys, 'duplicate funding topic'); keys.add(f['topic_key'])
            dates = f.get('dates')
            require(isinstance(dates,list) and dates == sorted(set(dates)), 'funding dates must be unique and ordered')
            require(all(Date.fromisoformat(d) <= Date.fromisoformat(date) for d in dates), 'future funding date')
            require(isinstance(f.get('gaps'),list), 'funding coverage gaps required')
            require(bool(dates) or bool(f['gaps']), 'missing funding data cannot mean zero')
            refs(f)
        for m in c['mainlines']:
            fields(m, ['topic_key','prior_stage','stage','prior','independent_update','counterevidence','mechanism','judgement','next_check'], 'mainline update')
            require(m['topic_key'] in keys, 'mainline must reference tracked direction')
            require(m['stage'] in ['萌芽','形成','延续','分歧','退潮','修复','无稳定主线'], 'unknown mainline stage')
            refs(m)
        for p in c['patterns']:
            fields(p,['pattern_key','assessment','sample_scope','exploration_note','implication','next_check'],'pattern')
            require(isinstance(p.get('sample_ids'),list), 'pattern sample ids required')
            require(type(p.get('prospective_count')) is int and 0 <= p['prospective_count'] <= len(p['sample_ids']), 'invalid prospective sample count')
        require(text(c.get('strategy_change')), 'tracking must change or justify strategy')
    validate_editorial(brief, body, refs, fields)
    return {'status': 'STRUCTURE_PASS', 'semantic_quality_verified': False, 'schema_version': VERSION,
            'editorial_version': brief.get('editorial', {}).get('version'),
            'date': date, 'session': session, 'report_sha256': brief['report_sha256'],
            'analysis_sha256': hashlib.sha256(json.dumps(brief, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            'formal_research_pass': False, 'gaps': brief['gaps']}


def check(raw, date, session, analysis=None):
    Date.fromisoformat(date)
    if analysis is None and date < EFFECTIVE_DATE:
        return {'status': 'LEGACY_COMPATIBLE', 'formal_research_pass': False}
    require(analysis is not None, 'market-first research requires --analysis research-brief.json')
    p = Path(analysis)
    require(p.is_file(), 'analysis file is missing')
    result = validate(raw, json.loads(p.read_text(encoding='utf-8-sig')), date, session)
    brief = json.loads(p.read_text(encoding='utf-8-sig'))
    pool_ref = brief.get('opportunity_pool_ref')
    if pool_ref:
        import importlib.util
        pool_path = Path(pool_ref['path'])
        require(pool_path.is_file() and hashlib.sha256(pool_path.read_bytes()).hexdigest() == pool_ref.get('sha256'), 'opportunity pool SHA mismatch')
        validator_path = Path('D:/向日葵/yansheng-public-preview/opportunity_view.py')
        spec = importlib.util.spec_from_file_location('reviewed_opportunity_contract', validator_path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        pool = json.loads(pool_path.read_text(encoding='utf-8-sig'))
        require(pool.get('date') == date and pool.get('session') == session and pool.get('research_report_sha256') == result['report_sha256'], 'opportunity report/date/session mismatch')
        result['opportunity_check'] = module.validate_pool(pool)
    result['analysis_content_sha256'] = result['analysis_sha256']
    result['analysis_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date', required=True)
    p.add_argument('--session', choices=['premarket', 'close'], required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--analysis', type=Path)
    a = p.parse_args()
    result = check(a.report.read_bytes(), a.date, a.session, a.analysis)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
