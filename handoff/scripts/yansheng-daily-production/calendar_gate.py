"""Read the existing reconciled exchange calendar before any daily research work."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

PRODUCTION = Path(__file__).resolve().parents[2] / 'daily-recap-trading' / 'scripts' / 'production'
sys.path.insert(0, str(PRODUCTION))
from trading_context_v1 import resolve_trading_context

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path('D:/向日葵/alphaengine')


def check(root: Path, session: str, target_date: str | None = None,
          now: datetime | None = None) -> dict:
    if session not in ('premarket', 'close'):
        raise ValueError('session must be premarket or close')
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ValueError('now must include a timezone')
    local_date = target_date or current.astimezone(SHANGHAI).date().isoformat()
    context = resolve_trading_context(root, local_date)
    result = {
        'schema': 'yansheng-calendar-gate-v1',
        'date': local_date, 'session': session,
        'calendar_path': str(root / 'calendar/published/current.json'),
        'context': context,
        'run_research': False, 'publish_allowed': False,
        'resume_historical_work': False,
    }
    if context.get('validation_status') != 'validated':
        result['decision'] = 'BLOCKED_CALENDAR'
        result['reason'] = context.get('error', 'calendar unavailable')
        return result
    if not context['is_trading_day']:
        result['decision'] = 'SKIP_NON_TRADING_DAY'
        result['reason'] = 'Exchange closed; do not collect, revise, publish, or recover historical runs.'
        return result
    result.update(decision='RUN', run_research=True, publish_allowed=True)
    result['source_window_start'] = (
        context['previous_trading_day'] + 'T15:00:00+08:00'
        if session == 'premarket' else local_date + 'T06:30:00+08:00'
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--session', required=True, choices=['premarket', 'close'])
    parser.add_argument('--date', help='Explicit date for tests/manual inspection; scheduled runs omit this.')
    args = parser.parse_args()
    result = check(args.root, args.session, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['decision'] == 'BLOCKED_CALENDAR' else 0


if __name__ == '__main__':
    raise SystemExit(main())
