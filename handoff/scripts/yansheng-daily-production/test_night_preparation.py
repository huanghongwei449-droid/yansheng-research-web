from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from night_preparation import DEFAULT_ROOT, SHANGHAI, gate, inspect, seal


class PreparationTests(unittest.TestCase):
    def test_target_tomorrow_and_whole_holiday(self):
        result = gate(now=datetime(2026, 10, 7, 21, 30, tzinfo=SHANGHAI))
        self.assertEqual(result['date'], '2026-10-08')
        self.assertEqual(result['decision'], 'RUN')
        self.assertEqual(result['source_window_start'], '2026-09-30T15:00:00+08:00')
        self.assertFalse(result['publish_allowed'])
        self.assertFalse(result['freeze_allowed'])

    def test_friday_saturday_skip_sunday_prepares(self):
        for day in (9, 10):
            self.assertEqual(gate(now=datetime(2026, 10, day, 21, tzinfo=SHANGHAI))['decision'],
                             'SKIP_NON_TRADING_DAY')
        result = gate(now=datetime(2026, 10, 11, 21, tzinfo=SHANGHAI))
        self.assertEqual(result['date'], '2026-10-12')
        self.assertEqual(result['source_window_start'], '2026-10-09T15:00:00+08:00')

    def test_unknown_calendar_and_utc_date(self):
        result = gate(now=datetime(2026, 10, 7, 16, tzinfo=timezone.utc))
        self.assertEqual(result['date'], '2026-10-09')
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(gate(Path(folder))['decision'], 'BLOCKED_CALENDAR')

    def fixture(self, folder):
        root = Path(folder)
        artifacts = []
        for role in ('coverage', 'reading_ledger', 'research', 'handoff', 'source', 'draft'):
            path = root / (role + '.json')
            data = {'pending': [{'item': 'audio', 'affected_judgment': 'company qualification',
                                 'next_action': 'find written minutes'}],
                    'next_action': 'supplement overnight news'} if role == 'handoff' else {'actual': 'partial'}
            path.write_text(json.dumps(data), encoding='utf-8')
            artifacts.append({'role': role, 'path': str(path.resolve())})
        method = root / 'method.md'
        method.write_text('approved method', encoding='utf-8')
        plan = {'date': '2026-10-08', 'source_window_start': '2026-09-30T15:00:00+08:00',
                'source_cutoff': '2026-10-07T21:40:00+08:00', 'artifacts': artifacts,
                'dependencies': [{'name': 'method', 'path': str(method.resolve())}]}
        return plan, root / 'preparation-manifest.json', datetime(2026, 10, 7, 22, tzinfo=SHANGHAI)

    def test_unchanged_retry_and_morning_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            first = seal(plan, manifest, now=now)
            before = manifest.read_bytes()
            self.assertEqual(first, seal(plan, manifest, now=now))
            self.assertEqual(before, manifest.read_bytes())
            result = inspect(manifest, '2026-10-08')
            self.assertEqual(result['decision'], 'REUSE_WITH_INCREMENT')
            self.assertTrue(result['needs_remote_edit_check'])
            self.assertFalse(result['quality_pass'])
            self.assertEqual(result['increment_from'], plan['source_cutoff'])
            self.assertEqual(len(result['reusable']), 7)

    def test_source_changed_removes_dependent_reading_and_research(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            seal(plan, manifest, now=now)
            Path(folder, 'source.json').write_text('corrected', encoding='utf-8')
            result = inspect(manifest, '2026-10-08')
            self.assertEqual(result['decision'], 'REPAIR_REQUIRED')
            self.assertIn('reading_ledger', result['affected_roles'])
            self.assertIn('research', result['affected_roles'])
            self.assertFalse(any(row.get('role') == 'research' for row in result['reusable']))
            self.assertFalse(result['quality_pass'])
            with self.assertRaises(ValueError):
                seal(plan, manifest, now=now)

    def test_method_changes_retain_reading_but_invalidate_judgment(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            seal(plan, manifest, now=now)
            Path(folder, 'method.md').write_text('new method', encoding='utf-8')
            result = inspect(manifest, '2026-10-08')
            self.assertIn('draft', result['affected_roles'])
            self.assertTrue(any(row.get('role') == 'reading_ledger' for row in result['reusable']))
            self.assertFalse(any(row.get('role') == 'research' for row in result['reusable']))

    def test_window_target_and_future_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            for change in ({'source_window_start': '2026-10-07T15:00:00+08:00'},
                           {'source_cutoff': '2026-10-07T23:00:00+08:00'},
                           {'date': '2026-10-10'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    seal({**plan, **change}, manifest, now=now)
            seal(plan, manifest, now=now)
            with self.assertRaises(ValueError):
                inspect(manifest, '2026-10-09')

    def test_missing_role_deleted_file_and_pending_specificity(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            with self.assertRaises(ValueError):
                seal({**plan, 'artifacts': plan['artifacts'][1:]}, manifest, now=now)
            Path(folder, 'handoff.json').write_text('{"pending":[{"item":"audio"}],"next_action":"read"}')
            with self.assertRaises(ValueError):
                seal(plan, manifest, now=now)
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder)
            seal(plan, manifest, now=now)
            Path(folder, 'reading_ledger.json').unlink()
            self.assertEqual(inspect(manifest, '2026-10-08')['decision'], 'REPAIR_REQUIRED')


if __name__ == '__main__':
    unittest.main()
