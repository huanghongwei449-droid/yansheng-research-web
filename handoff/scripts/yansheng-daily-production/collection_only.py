"""Scheduled data collection without external Brain handoff or delivery."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from calendar_gate import DEFAULT_ROOT, SHANGHAI, check


def run(root: Path, session: str, *, now: datetime | None = None,
        collector=None, dry_run: bool = False) -> dict:
    gate = check(root, session, now=now)
    if gate['decision'] != 'RUN':
        return {**gate, 'publish_allowed': False, 'collection_only': True}
    if dry_run:
        return {'decision': 'DRY_RUN', 'date': gate['date'], 'session': session,
                'root': str(root), 'collector': 'bus_v1.collect',
                'external_brain': False, 'ima_write': False}
    if collector is None:
        # Retain the installed public-only/data-only pipeline and its inputs.
        production = root / '.agents/skills/daily-recap-trading/scripts/production'
        sys.path.insert(0, str(production))
        from bus_v1 import collect
        collector = collect
    result = collector(root, gate['date'], session, branch='daily-bus')
    return {**result, 'collection_only': True, 'external_brain': False, 'ima_write': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--session', required=True, choices=['premarket', 'close'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        result = run(args.root, args.session, dry_run=args.dry_run)
    except Exception as exc:
        result = {'ok': False, 'status': 'FAILED_COLLECTION', 'error': str(exc),
                  'collection_only': True, 'external_brain': False, 'ima_write': False}
    if not args.dry_run:
        folder = Path(__file__).resolve().parents[4] / '.tmp/yansheng-collection'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SHANGHAI).strftime('%Y%m%d-%H%M%S-%f')
        (folder / f'{stamp}-{args.session}.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    skipped = result.get('decision') in {'SKIP_NON_TRADING_DAY', 'DRY_RUN'}
    return 0 if skipped or result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
