"""Calendar-gated, hash-bound handoff; never reads on behalf of the researcher."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from calendar_gate import DEFAULT_ROOT, SHANGHAI, check as calendar_check
from reading_audit import audit as audit_reading, temporal_integrity

SCHEMA = 'yansheng-preparation-v1'
REQUIRED = {'coverage', 'reading_ledger', 'research', 'handoff'}
ROLES = REQUIRED | {'source', 'draft'}
DEFAULT_BASE = Path(__file__).resolve().parents[4] / '.tmp/yansheng-night-prep'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return result.astimezone(SHANGHAI)


def read_time_check(row: dict, sealed_at=None, now=None) -> dict | None:
    """Plausibility only: rejects impossible times, never certifies understanding."""
    path = Path(row['path'])
    try:
        ledger = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    entries = ledger.get('entries') if isinstance(ledger, dict) else None
    if not isinstance(entries, list):
        return None
    return temporal_integrity(entries, sealed_at=sealed_at, now=now)


def missing_method_dependencies(payload: dict) -> list[str]:
    """References the entry skill marks as required but the seal never bound."""
    skills = [Path(row['path']) for row in payload.get('dependencies', [])
              if Path(row['path']).name == 'SKILL.md' and Path(row['path']).is_file()]
    if not skills:
        return []
    skill = skills[0]
    listed = {Path(row['path']).resolve() for row in payload.get('dependencies', [])}
    names = sorted(set(re.findall(r'references/([\w.-]+\.md)', skill.read_text(encoding='utf-8-sig'))))
    return [name for name in names
            if (skill.parent / 'references' / name).is_file()
            and (skill.parent / 'references' / name).resolve() not in listed]


def gate(root: Path = DEFAULT_ROOT, now: datetime | None = None,
         target_date: str | None = None) -> dict:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ValueError('now requires timezone')
    local = current.astimezone(SHANGHAI)
    target = (local.date() + timedelta(days=1)).isoformat()
    if target_date:
        if date.fromisoformat(target_date) != local.date() or local.hour * 60 + local.minute >= 390:
            raise ValueError('Explicit preparation date requires the current Shanghai day before 06:30')
        target = target_date
    result = calendar_check(root, 'premarket', target_date=target, now=current)
    result.update(mode='early_morning' if target_date else 'night_preparation',
                  publish_allowed=False, freeze_allowed=False)
    return result


def bind(row: dict, *, artifact: bool) -> dict:
    path = Path(row['path'])
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f'Existing absolute file required: {path}')
    result = {'path': str(path.resolve()), 'sha256': digest(path)}
    if artifact:
        if row.get('role') not in ROLES:
            raise ValueError('Unknown artifact role')
        result['role'] = row['role']
    else:
        if not row.get('name'):
            raise ValueError('Dependency name required')
        result['name'] = row['name']
    return result


def validate_window(plan: dict, root: Path, now: datetime) -> dict:
    target = date.fromisoformat(plan['date']).isoformat()
    result = calendar_check(root, 'premarket', target_date=target, now=now)
    if result['decision'] != 'RUN':
        raise ValueError(f"Preparation target blocked: {result['decision']}")
    start = timestamp(plan['source_window_start'])
    cutoff = timestamp(plan['source_cutoff'])
    if start != timestamp(result['source_window_start']):
        raise ValueError('Source window must start at actual previous trading close')
    if not start <= cutoff <= now.astimezone(SHANGHAI):
        raise ValueError('Source cutoff reversed or in the future')
    mode = plan.get('preparation_mode', 'night_preparation')
    if mode not in ('night_preparation', 'early_morning'):
        raise ValueError('Unknown preparation mode')
    if mode == 'early_morning':
        deadline = datetime.combine(date.fromisoformat(target), datetime.min.time(), SHANGHAI) + timedelta(hours=6, minutes=30)
        if cutoff >= deadline:
            raise ValueError('Rescheduled preparation cutoff must precede target day 06:30')
    elif cutoff.date() >= date.fromisoformat(target):
        raise ValueError('Night preparation cutoff must precede target day')
    return result


def seal(plan: dict, output: Path, root: Path = DEFAULT_ROOT,
         now: datetime | None = None) -> dict:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ValueError('now requires timezone')
    context = validate_window(plan, root, current)
    if plan.get('preparation_mode') == 'early_morning':
        local = current.astimezone(SHANGHAI)
        if local.date().isoformat() != plan['date'] or local.hour * 60 + local.minute >= 390:
            raise ValueError('Rescheduled preparation must be sealed on target day before 06:30')
    artifacts = [bind(row, artifact=True) for row in plan['artifacts']]
    if not REQUIRED.issubset({row['role'] for row in artifacts}):
        raise ValueError('coverage, reading_ledger, research and handoff required')
    if len({row['path'] for row in artifacts}) != len(artifacts):
        raise ValueError('Duplicate artifact paths cannot stand in for different roles')
    dependencies = [bind(row, artifact=False) for row in plan['dependencies']]
    if not dependencies:
        raise ValueError('Method/workflow dependencies required')
    handoff_path = next(row['path'] for row in artifacts if row['role'] == 'handoff')
    handoff = json.loads(Path(handoff_path).read_text(encoding='utf-8-sig'))
    if not isinstance(handoff.get('pending'), list) or not handoff.get('next_action'):
        raise ValueError('Handoff requires explicit pending list and next_action')
    for item in handoff['pending']:
        if not all(item.get(key) for key in ('item', 'affected_judgment', 'next_action')):
            raise ValueError('Every pending item needs affected judgment and next action')
    ledger_row = next(row for row in artifacts if row['role'] == 'reading_ledger')
    timing = read_time_check(ledger_row, sealed_at=current, now=current)
    if timing and timing['future_timestamp']:
        # A recorded read cannot postdate the seal it is being sealed in.
        raise ValueError('Reading recorded in the future of this seal: '
                         + ', '.join(timing['future_timestamp'][:5]))
    payload = {
        'schema': SCHEMA, 'date': plan['date'], 'purpose': 'prepared_input_only',
        'source_window_start': plan['source_window_start'],
        'source_cutoff': plan['source_cutoff'],
        'calendar_version': context['context']['calendar_version'],
        'artifacts': artifacts, 'dependencies': dependencies,
        'quality_pass': False, 'publication_allowed': False,
    }
    if timing:
        payload['reading_time_check'] = {
            'decision': timing['decision'], 'checked_read': timing['checked_read'],
            'unknown_count': timing['unknown_count'], 'skipped': timing['skipped'],
            'proves_understanding': False,
            'note': 'Plausibility check at seal time; it does not prove reading or understanding.',
        }
    if plan.get('preparation_mode'):
        payload['preparation_mode'] = plan['preparation_mode']
    # An identical retry is idempotent. A changed package needs a new revision.
    if output.exists():
        existing = json.loads(output.read_text(encoding='utf-8'))
        if {key: existing.get(key) for key in payload} != payload:
            raise ValueError('Existing revision differs; create a new revision directory')
        return existing
    payload['sealed_at'] = current.astimezone(SHANGHAI).isoformat(timespec='seconds')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return payload


def inspect(manifest: Path, target: str, root: Path = DEFAULT_ROOT,
            now: datetime | None = None) -> dict:
    current = now or datetime.now(SHANGHAI)
    result = {'date': target, 'manifest': str(manifest), 'manifest_sha256': digest(manifest),
              'decision': 'RESEARCH_REQUIRED', 'invalid': [], 'reusable': [],
              'needs_remote_edit_check': True, 'quality_pass': False}
    payload = json.loads(manifest.read_text(encoding='utf-8'))
    if payload.get('schema') != SCHEMA or payload.get('date') != target:
        raise ValueError('Wrong preparation schema or target date')
    validate_window(payload, root, current)
    if not REQUIRED.issubset({row.get('role') for row in payload.get('artifacts', [])}):
        raise ValueError('Manifest lacks required roles')
    if not payload.get('dependencies'):
        raise ValueError('Manifest lacks method dependencies')
    for row in payload['dependencies'] + payload['artifacts']:
        path = Path(row['path'])
        if not path.is_absolute() or not path.is_file() or digest(path) != row['sha256']:
            result['invalid'].append(row)
        else:
            result['reusable'].append(row)
    changed_roles = {row.get('role', 'method') for row in result['invalid']}
    missing_refs = missing_method_dependencies(payload)
    if missing_refs:
        # A method the entry skill now marks as required was never bound by this seal.
        result['missing_method_dependencies'] = missing_refs
        changed_roles.add('method')
    reread = bool(changed_roles & {'source', 'coverage', 'reading_ledger'})
    affected = set(changed_roles)
    if reread:
        affected |= REQUIRED | {'draft'}
    if 'method' in changed_roles:
        affected |= {'research', 'draft', 'handoff'}
    if 'research' in changed_roles:
        affected |= {'draft', 'handoff'}
    result['reusable'] = [row for row in result['reusable'] if row.get('role') not in affected]
    result.update(decision='REPAIR_REQUIRED' if result['invalid'] or missing_refs else 'REUSE_WITH_INCREMENT',
                  affected_roles=sorted(affected),
                  increment_from=payload['source_cutoff'],
                  source_window_start=payload['source_window_start'])
    # File reuse and reading coverage are independent. Old packages can still
    # be reused as partial inputs, but their unknown coverage is never a PASS.
    result['text_coverage'] = {'status': 'UNKNOWN', 'semantic_reading_verified': False,
                               'issues': ['No unique enumeration/ledger/coverage available']}
    artifacts = payload.get('artifacts', [])
    sources = [x['path'] for x in artifacts if x.get('role') == 'source'
               and Path(x['path']).name == 'enumeration.json']
    ledgers = [x['path'] for x in artifacts if x.get('role') == 'reading_ledger']
    coverages = [x['path'] for x in artifacts if x.get('role') == 'coverage']
    if len(sources) == len(ledgers) == len(coverages) == 1 and not reread:
        checked = audit_reading(sources[0], ledgers[0], coverages[0],
                                sealed_at=payload.get('sealed_at'), now=current)
        result['text_coverage'] = {k: checked.get(k) for k in
            ('status', 'enumerated', 'states', 'issues', 'semantic_reading_verified',
             'read_time_verified')}
        result['reading_time_check'] = checked.get('temporal_integrity')
    elif reread:
        result['text_coverage']['issues'] = ['Sealed source or reading record changed; re-audit in a new revision']
    if 'reading_time_check' not in result and ledgers:
        result['reading_time_check'] = read_time_check(
            {'path': ledgers[0]}, sealed_at=payload.get('sealed_at'), now=current)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['gate', 'seal', 'check'])
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--date', help='check target or explicit same-day early-morning gate target')
    parser.add_argument('--base', type=Path, default=DEFAULT_BASE)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'gate':
            result = gate(args.root, target_date=args.date)
        elif args.command == 'seal':
            if not args.plan or not args.output:
                raise ValueError('seal requires --plan and --output')
            result = seal(json.loads(args.plan.read_text(encoding='utf-8-sig')), args.output, args.root)
        else:
            target = args.date or datetime.now(SHANGHAI).date().isoformat()
            candidates = list((args.base / target).glob('*/preparation-manifest.json'))
            manifest = args.manifest or (max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else None)
            result = inspect(manifest, target, args.root) if manifest else {
                'date': target, 'decision': 'NO_PREPARATION',
                'next_action': 'Run full source/research stages; no coverage assumed', 'quality_pass': False}
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('decision') == 'BLOCKED_CALENDAR' else 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(json.dumps({'decision': 'REPAIR_REQUIRED', 'error': str(exc), 'quality_pass': False}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
