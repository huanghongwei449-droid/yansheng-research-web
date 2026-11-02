import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from calendar_gate import check, DEFAULT_ROOT


class ExchangeCalendarGateTests(unittest.TestCase):
    def test_national_day_closes_every_day(self):
        for day in range(1, 8):
            for session in ('premarket', 'close'):
                with self.subTest(day=day, session=session):
                    result = check(DEFAULT_ROOT, session, f'2026-10-{day:02d}')
                    self.assertEqual(result['decision'], 'SKIP_NON_TRADING_DAY')
                    self.assertFalse(result['run_research'])
                    self.assertFalse(result['publish_allowed'])
                    self.assertFalse(result['resume_historical_work'])
                    self.assertEqual(result['context']['next_trading_day'], '2026-10-08')

    def test_reopening_includes_whole_holiday(self):
        result = check(DEFAULT_ROOT, 'premarket', '2026-10-08')
        self.assertEqual(result['decision'], 'RUN')
        self.assertEqual(result['source_window_start'], '2026-09-30T15:00:00+08:00')
        self.assertEqual(result['context']['next_trading_day'], '2026-10-09')

    def test_makeup_workday_is_still_closed(self):
        self.assertEqual(check(DEFAULT_ROOT, 'close', '2026-10-10')['decision'], 'SKIP_NON_TRADING_DAY')

    def test_normal_trading_day(self):
        result = check(DEFAULT_ROOT, 'close', '2026-10-09')
        self.assertEqual(result['decision'], 'RUN')
        self.assertEqual(result['source_window_start'], '2026-10-09T06:30:00+08:00')

    def test_uses_shanghai_runtime_date(self):
        # 9/30 UTC evening is already National Day in Shanghai.
        result = check(DEFAULT_ROOT, 'premarket', now=datetime(2026, 9, 30, 23, tzinfo=timezone.utc))
        self.assertEqual(result['date'], '2026-10-01')
        self.assertEqual(result['decision'], 'SKIP_NON_TRADING_DAY')

    def test_missing_expired_and_corrupt_calendars_block(self):
        self.assertEqual(check(DEFAULT_ROOT, 'close', '2027-01-04')['decision'], 'BLOCKED_CALENDAR')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(check(root, 'close', '2026-10-08')['decision'], 'BLOCKED_CALENDAR')
            path = root / 'calendar/published/current.json'
            path.parent.mkdir(parents=True)
            calendar = json.loads((DEFAULT_ROOT / 'calendar/published/current.json').read_text(encoding='utf-8'))
            calendar['normalized_calendar']['exchange_sources'][0]['non_trading_dates'].remove('2026-10-01')
            path.write_text(json.dumps(calendar), encoding='utf-8')
            self.assertEqual(check(root, 'close', '2026-10-01')['decision'], 'BLOCKED_CALENDAR')


if __name__ == '__main__':
    unittest.main()
