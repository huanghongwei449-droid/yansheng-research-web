from datetime import datetime
import unittest
from unittest.mock import Mock

from calendar_gate import DEFAULT_ROOT, SHANGHAI
from collection_only import run


class CollectionOnlyTests(unittest.TestCase):
    def test_holiday_has_no_collector_side_effect(self):
        collector = Mock()
        result = run(DEFAULT_ROOT, 'close', now=datetime(2026, 10, 7, 15, 5, tzinfo=SHANGHAI), collector=collector)
        collector.assert_not_called()
        self.assertEqual(result['decision'], 'SKIP_NON_TRADING_DAY')
        self.assertFalse(result['publish_allowed'])

    def test_real_day_calls_only_public_data_collector(self):
        collector = Mock(return_value={'ok': True, 'status': 'PUBLISHED'})
        result = run(DEFAULT_ROOT, 'close', now=datetime(2026, 10, 8, 15, 5, tzinfo=SHANGHAI), collector=collector)
        collector.assert_called_once_with(DEFAULT_ROOT, '2026-10-08', 'close', branch='daily-bus')
        self.assertFalse(result['external_brain'])
        self.assertFalse(result['ima_write'])

    def test_dry_run_never_collects(self):
        collector = Mock()
        result = run(DEFAULT_ROOT, 'premarket', now=datetime(2026, 10, 9, 6, 30, tzinfo=SHANGHAI),
                     collector=collector, dry_run=True)
        collector.assert_not_called()
        self.assertEqual(result['decision'], 'DRY_RUN')


if __name__ == '__main__':
    unittest.main()
