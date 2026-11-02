from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from night_preparation import SHANGHAI, inspect, seal
from reading_audit import audit, temporal_integrity

SEAL = datetime(2026, 11, 1, 23, 41, 28, tzinfo=SHANGHAI)
NEXT_MORNING = datetime(2026, 11, 2, 6, 40, tzinfo=SHANGHAI)
START = '2026-10-30T15:00:00+08:00'
CUTOFF = '2026-11-01T23:31:29+08:00'


def entry(topic_id='1', state='full_body_read', read_at=None, group='15552822254242'):
    return {'topic_id': topic_id, 'group': group, 'read_state': state, 'read_at': read_at}


def build_package(root, entries, declared=None):
    """Minimal but schema-valid two-group package for audit-level checks."""
    root = Path(root).resolve()
    pages, groups = {}, []
    for group in ('15552822254242', '28512858211281'):
        topics = [e for e in entries if e['group'] == group] or [
            entry('empty-' + group, 'enumerated_not_read', None, group)]
        rows = [{'topic_id': e['topic_id'], 'content': '正文内容 %s' % e['topic_id'],
                 'create_time': '2026-11-01T20:00:00.000+08:00', 'type': 'talk',
                 'modify_time': ''} for e in topics]
        page = root / ('page-%s.json' % group)
        payload = {'success': True, 'has_more': False, 'next_end_time': None, 'topics_brief': rows}
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        page.write_bytes(raw)
        pages[group] = str(page)
        groups.append({'group': group, 'reached_start': True, 'pages': [
            {'page': 1, 'path': str(page), 'sha256': hashlib.sha256(raw).hexdigest(),
             'fetched_at': CUTOFF}]})
    enumeration = {'start': START, 'cutoff': CUTOFF, 'captured_at': CUTOFF, 'groups': groups}
    ledger = {'schema': 'zsxq-reading-ledger-v1', 'window_start': START, 'cutoff': CUTOFF,
              'entries': [{'topic_id': e['topic_id'], 'group': e['group'],
                           'source_page': pages[e['group']],
                           'content_sha256': hashlib.sha256(
                               ('正文内容 %s' % e['topic_id']).encode('utf-8')).hexdigest(),
                           'read_state': e['read_state'], 'read_at': e['read_at']}
                          for e in entries]}
    coverage = {'window_start': START, 'cutoff': CUTOFF, 'read_body_count': declared}
    paths = []
    for name, data in (('enumeration.json', enumeration), ('ledger.json', ledger),
                       ('coverage.json', coverage)):
        path = root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        paths.append(path)
    return paths


class TemporalIntegrityTests(unittest.TestCase):
    def test_true_read_before_seal_is_reusable(self):
        result = temporal_integrity([entry(read_at='2026-11-01T21:40:00+08:00')],
                                    sealed_at=SEAL, now=NEXT_MORNING)
        self.assertEqual(result['decision'], 'TIMES_VALID')
        self.assertEqual(result['reusable_before_seal'], 1)
        self.assertFalse(result['proves_understanding'])

    def test_future_time_is_impossible(self):
        result = temporal_integrity([entry(read_at='2026-11-02T00:10:00+08:00')],
                                    sealed_at=SEAL, now=SEAL)
        self.assertEqual(result['decision'], 'INVALID_READ_TIME')
        self.assertEqual(len(result['future_timestamp']), 1)
        self.assertEqual(result['reusable_before_seal'], 0)

    def test_post_seal_read_does_not_become_valid_when_clock_passes(self):
        result = temporal_integrity([entry(read_at='2026-11-02T06:30:00+08:00')],
                                    sealed_at=SEAL, now=NEXT_MORNING)
        self.assertEqual(result['decision'], 'INVALID_READ_TIME')
        self.assertEqual(len(result['post_seal_read']), 1)
        self.assertEqual(result['future_timestamp'], [])
        self.assertEqual(result['reusable_before_seal'], 0)

    def test_attachment_skip_needs_no_read_time(self):
        result = temporal_integrity([entry(state='file_only_skipped')],
                                    sealed_at=SEAL, now=NEXT_MORNING)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['invalid_count'], 0)
        self.assertEqual(result['decision'], 'TIMES_VALID')

    def test_partial_validity_keeps_valid_entries_and_flags_the_rest(self):
        result = temporal_integrity([
            entry('a', read_at='2026-11-01T22:00:00+08:00'),
            entry('b', read_at='2026-11-02T06:30:00+08:00'),
            entry('c', read_at='unknown'),
        ], sealed_at=SEAL, now=NEXT_MORNING)
        self.assertEqual(result['decision'], 'INVALID_READ_TIME')
        self.assertEqual(result['valid_before_seal'], ['a'])
        self.assertEqual(result['post_seal_read'], ['b'])
        self.assertEqual(result['time_unknown'], ['c'])

    def test_missing_seal_reference_never_validates(self):
        result = temporal_integrity([entry(read_at='2026-11-01T22:00:00+08:00')], now=NEXT_MORNING)
        self.assertEqual(result['decision'], 'SEAL_REFERENCE_MISSING')
        self.assertEqual(result['reusable_before_seal'], 0)


class AuditIntegrationTests(unittest.TestCase):
    def test_post_seal_entries_are_not_counted_as_read(self):
        with tempfile.TemporaryDirectory() as folder:
            enum, ledger, coverage = build_package(folder, [
                entry('a', read_at='2026-11-01T22:00:00+08:00'),
                entry('b', read_at='2026-11-02T06:30:00+08:00',
                      group='28512858211281'),
            ], declared=1)
            result = audit(enum, ledger, coverage, sealed_at=SEAL, now=NEXT_MORNING)
            self.assertEqual(result['states'].get('full_body_read'), 1)
            self.assertEqual(result['temporal_integrity']['post_seal_read'], ['b'])
            self.assertFalse(result['read_time_verified'])
            self.assertEqual(result['status'], 'PARTIAL')
            self.assertEqual(result['semantic_reading_verified'], False)

    def test_unknown_time_is_caveat_not_unread(self):
        with tempfile.TemporaryDirectory() as folder:
            enum, ledger, coverage = build_package(folder, [
                entry('a', read_at='unknown'),
                entry('b', state='file_only_skipped', group='28512858211281'),
            ], declared=1)
            result = audit(enum, ledger, coverage, sealed_at=SEAL, now=NEXT_MORNING)
            self.assertEqual(result['states'].get('full_body_read'), 1)
            self.assertEqual(result['temporal_integrity']['decision'], 'TIME_UNVERIFIED')
            self.assertFalse(result['read_time_verified'])
            self.assertEqual(result['temporal_caveats'][0]['read_time_unknown_count'], 1)


class SealAndReuseTests(unittest.TestCase):
    def fixture(self, folder, read_at):
        root = Path(folder)
        artifacts = []
        for role in ('coverage', 'reading_ledger', 'research', 'handoff', 'source'):
            path = root / (role + '.json')
            data = ({'pending': [{'item': 'audio', 'affected_judgment': 'order visibility',
                                  'next_action': 'read written minutes'}],
                     'next_action': 'supplement overnight news'} if role == 'handoff' else
                    {'entries': [{'topic_id': 'a', 'group': '15552822254242',
                                  'read_state': 'full_body_read', 'read_at': read_at}]}
                    if role == 'reading_ledger' else {'actual': 'partial'})
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            artifacts.append({'role': role, 'path': str(path.resolve())})
        method = root / 'method.md'
        method.write_text('approved method', encoding='utf-8')
        plan = {'date': '2026-10-08', 'source_window_start': '2026-09-30T15:00:00+08:00',
                'source_cutoff': '2026-10-07T21:40:00+08:00', 'artifacts': artifacts,
                'dependencies': [{'name': 'method', 'path': str(method.resolve())}]}
        return plan, root / 'preparation-manifest.json', datetime(2026, 10, 7, 22, tzinfo=SHANGHAI)

    def test_seal_rejects_future_read_time(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder, '2026-10-07T23:10:00+08:00')
            with self.assertRaises(ValueError):
                seal(plan, manifest, now=now)
            self.assertFalse(manifest.exists())

    def test_seal_records_plausible_time_without_certifying(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder, '2026-10-07T21:50:00+08:00')
            payload = seal(plan, manifest, now=now)
            self.assertEqual(payload['reading_time_check']['decision'], 'TIMES_VALID')
            self.assertFalse(payload['reading_time_check']['proves_understanding'])
            self.assertFalse(payload['quality_pass'])

    def test_reuse_flags_post_seal_entries_from_legacy_seal(self):
        with tempfile.TemporaryDirectory() as folder:
            plan, manifest, now = self.fixture(folder, '2026-10-07T21:50:00+08:00')
            seal(plan, manifest, now=now)
            sealed = json.loads(manifest.read_text(encoding='utf-8'))
            ledger = Path(next(a['path'] for a in sealed['artifacts']
                               if a['role'] == 'reading_ledger'))
            data = json.loads(ledger.read_text(encoding='utf-8'))
            data['entries'][0]['read_at'] = '2026-10-08T06:30:00+08:00'
            ledger.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            result = inspect(manifest, '2026-10-08', now=datetime(2026, 10, 8, 6, 40, tzinfo=SHANGHAI))
            self.assertEqual(result['decision'], 'REPAIR_REQUIRED')
            self.assertIn('reading_ledger', result['affected_roles'])
            self.assertEqual(result['reading_time_check']['post_seal_read'], ['a'])


if __name__ == '__main__':
    unittest.main()
