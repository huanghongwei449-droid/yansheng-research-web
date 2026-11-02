from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from night_preparation import SHANGHAI
from reading_carry_forward import carry, evidence_is_traceable, read_time_status

START = '2026-10-30T15:00:00+08:00'
OLD_CUTOFF = '2026-11-01T23:31:29+08:00'
NEW_CUTOFF = '2026-11-02T06:30:00+08:00'
SEALED = '2026-11-01T23:41:28+08:00'

A = '55521421000000001'
B = '55521421000000002'
C = '55521421000000003'
D = '55521421000000004'
E = '55521421000000005'


def write_page(root, gid, tag, topics):
    rows = [{'topic_id': t['id'], 'content': t['content'],
             'create_time': '2026-11-01T20:00:00.000+08:00', 'type': 'talk',
             'modify_time': ''} for t in topics]
    path = root / ('page-%s-%s.json' % (gid, tag))
    raw = json.dumps({'success': True, 'has_more': False, 'next_end_time': None,
                      'topics_brief': rows}, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def write_evidence(root, name, topic_ids, subdir=None):
    folder = root / subdir if subdir else root
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text('\n'.join(json.dumps({'topic_id': ident}, ensure_ascii=False)
                              for ident in topic_ids) + '\n', encoding='utf-8')
    return path


def build(root, sealed=SEALED, bind_pages=False, extra=()):
    root = Path(root)
    topics = {
        '15552822254242': [
            {'id': A, 'content': '封存前读完的正文 A'},
            {'id': B, 'content': '封存后才读完的正文 B'},
            {'id': C, 'content': '时间未知且无指向证据的正文 C'},
            {'id': D, 'content': '时间未知但有可追溯证据的正文 D'},
        ] + [{'id': ident, 'content': '补充正文 %s' % ident} for ident, _, _ in extra],
        '28512858211281': [{'id': E, 'content': '「文件」'}],
    }
    entries = [
        {'id': A, 'state': 'full_body_read', 'read_at': '2026-11-01T23:40:00+08:00',
         'evidence': 'chunk-00.jsonl 逐条读完'},
        {'id': B, 'state': 'full_body_read', 'read_at': '2026-11-01T23:52:00+08:00',
         'evidence': 'chunk-02.jsonl 逐条读完'},
        {'id': C, 'state': 'full_body_read', 'read_at': 'unknown', 'evidence': '逐帖读正文'},
        {'id': D, 'state': 'full_body_read', 'read_at': 'unknown',
         'evidence': 'reading-chunks/chunk-03.jsonl'},
        {'id': E, 'state': 'file_only_skipped', 'read_at': 'unknown', 'evidence': '文件占位'},
    ] + [{'id': ident, 'state': 'full_body_read', 'read_at': 'unknown', 'evidence': evidence}
         for ident, evidence, _ in extra]
    by_id = {e['id']: e for e in entries}
    # 证据文件必须真实存在且含该帖 id，否则只能算“文字里有文件名”
    write_evidence(root, 'chunk-00.jsonl', [A], 'reading-chunks')
    write_evidence(root, 'chunk-02.jsonl', [B], 'reading-chunks')
    write_evidence(root, 'chunk-03.jsonl', [D], 'reading-chunks')
    for ident, evidence, extra_files in extra:
        if extra_files:
            write_evidence(root, extra_files[0], extra_files[1])
    old_pages, new_pages = {}, {}
    for gid, ts in topics.items():
        old_pages[gid] = write_page(root, gid, 'old', ts)
        new_pages[gid] = write_page(root, gid, 'new', ts)
    old_enum = {'start': START, 'cutoff': OLD_CUTOFF, 'captured_at': OLD_CUTOFF,
                'groups': [{'group': gid, 'reached_start': True, 'pages': [
                    {'page': 1, 'path': str(old_pages[gid][0]), 'sha256': old_pages[gid][1],
                     'fetched_at': OLD_CUTOFF}]} for gid in topics]}
    new_enum = {'start': START, 'cutoff': NEW_CUTOFF, 'captured_at': NEW_CUTOFF,
                'groups': [{'group': gid, 'reached_start': True, 'pages': [
                    {'page': 1, 'path': str(new_pages[gid][0]), 'sha256': new_pages[gid][1],
                     'fetched_at': NEW_CUTOFF}]} for gid in topics]}
    ledger_entries = []
    for gid, ts in topics.items():
        for topic in ts:
            item = by_id[topic['id']]
            ledger_entries.append({
                'topic_id': topic['id'], 'group': gid,
                'source_page': str(old_pages[gid][0]),
                'content_sha256': hashlib.sha256(topic['content'].encode('utf-8')).hexdigest(),
                'read_state': item['state'], 'read_at': item['read_at'],
                'reading_evidence': item['evidence']})
    ledger = {'schema': 'zsxq-reading-ledger-v1', 'window_start': START,
              'cutoff': OLD_CUTOFF, 'entries': ledger_entries}
    coverage = {'window_start': START, 'cutoff': OLD_CUTOFF, 'read_body_count': 3}
    paths = {}
    for name, data in (('old-enum.json', old_enum), ('new-enum.json', new_enum),
                       ('ledger.json', ledger), ('coverage.json', coverage)):
        path = root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        paths[name] = path
    artifacts = [{'role': 'source', 'path': str(paths['old-enum.json'].resolve())},
                 {'role': 'reading_ledger', 'path': str(paths['ledger.json'].resolve())},
                 {'role': 'coverage', 'path': str(paths['coverage.json'].resolve())}]
    for row in artifacts:
        row['sha256'] = hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()
    if bind_pages:
        for gid in topics:
            artifacts.append({'role': 'source', 'path': str(old_pages[gid][0].resolve()),
                              'sha256': old_pages[gid][1]})
    manifest = {'schema': 'yansheng-preparation-v1', 'date': '2026-11-02',
                'source_window_start': START, 'source_cutoff': OLD_CUTOFF,
                'sealed_at': sealed, 'artifacts': artifacts, 'dependencies': []}
    paths['manifest.json'] = root / 'manifest.json'
    paths['manifest.json'].write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return paths, ledger_entries


class CarryDirectTests(unittest.TestCase):
    def run_carry(self, folder, **kwargs):
        paths, _ = build(folder, **kwargs)
        return carry(paths['new-enum.json'], paths['old-enum.json'], paths['ledger.json'],
                     paths['coverage.json'], paths['manifest.json'])

    def test_full_binding_separates_before_and_after_seal(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, coverage, summary = self.run_carry(folder, bind_pages=True)
            states = {row['topic_id']: row['read_state'] for row in ledger['entries']}
            self.assertTrue(summary['prior_snapshot_integrity_verified'])
            self.assertEqual(states[A], 'full_body_read')
            self.assertEqual(states[B], 'enumerated_not_read')
            self.assertEqual(states[D], 'full_body_read')
            self.assertEqual(states[E], 'file_only_skipped')
            self.assertEqual(summary['read_time_verified_topic_ids'], [A])
            self.assertEqual(summary['post_seal_read_topic_ids'], [B])
            self.assertEqual(summary['read_time_unverified_topic_ids'], [D])
            self.assertEqual(summary['pending_verification_topic_ids'], [C])
            self.assertEqual(coverage['read_body_count'], 2)
            self.assertEqual(coverage['read_body_count_time_verified'], 1)

    def test_post_seal_entry_is_not_reported_as_read(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, _, summary = self.run_carry(folder, bind_pages=True)
            row = next(x for x in ledger['entries'] if x['topic_id'] == B)
            self.assertNotEqual(row['read_state'], 'full_body_read')
            self.assertEqual(row['carry_status'], 'post_seal_read')
            self.assertNotIn('reading_provenance', row)
            self.assertEqual(summary['carry_status_counts'].get('carried_verified'), 1)

    def test_unknown_time_without_pointer_waits_for_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, _, summary = self.run_carry(folder, bind_pages=True)
            row = next(x for x in ledger['entries'] if x['topic_id'] == C)
            self.assertEqual(row['read_state'], 'enumerated_not_read')
            self.assertEqual(row['carry_status'], 'pending_verification')
            self.assertIn(C, summary['pending_verification_topic_ids'])
            self.assertEqual(summary['pending_verification_reasons']['no_file_or_time_pointer'], [C])

    def test_unknown_time_with_traceable_evidence_is_carried_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, _, summary = self.run_carry(folder, bind_pages=True)
            row = next(x for x in ledger['entries'] if x['topic_id'] == D)
            self.assertEqual(row['read_state'], 'full_body_read')
            self.assertFalse(row['read_time_verified'])
            self.assertEqual(row['reading_provenance']['read_time_status'], 'unknown')
            self.assertEqual(coverage_unverified(summary), 1)

    def test_provenance_records_the_located_evidence_file(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, _, _ = self.run_carry(folder, bind_pages=True)
            row = next(x for x in ledger['entries'] if x['topic_id'] == D)
            self.assertTrue(row['reading_provenance']['evidence_ref'].endswith('chunk-03.jsonl'))

    def test_missing_evidence_file_falls_back_to_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            trap = '55521421000000006'
            ledger, _, summary = self.run_carry(
                folder, bind_pages=True,
                extra=[(trap, 'chunk-09.jsonl 逐条读完', None)])
            row = next(x for x in ledger['entries'] if x['topic_id'] == trap)
            self.assertEqual(row['read_state'], 'enumerated_not_read')
            self.assertEqual(row['carry_status'], 'pending_verification')
            self.assertEqual(summary['pending_verification_reasons']['pointed_file_not_found'], [trap])

    def test_evidence_file_without_this_topic_falls_back_to_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            trap = '55521421000000007'
            ledger, _, summary = self.run_carry(
                folder, bind_pages=True,
                extra=[(trap, 'reading-chunks/chunk-00.jsonl 逐条读完', None)])
            row = next(x for x in ledger['entries'] if x['topic_id'] == trap)
            self.assertEqual(row['read_state'], 'enumerated_not_read')
            self.assertEqual(summary['pending_verification_reasons']
                             ['file_exists_but_topic_absent'], [trap])

    def test_summary_only_evidence_does_not_carry_body_read(self):
        with tempfile.TemporaryDirectory() as folder:
            trap = '55521421000000008'
            ledger, _, summary = self.run_carry(
                folder, bind_pages=True,
                extra=[(trap, 'mid-a-digest.txt 摘要', ('mid-a-digest.txt', [trap]))])
            row = next(x for x in ledger['entries'] if x['topic_id'] == trap)
            self.assertEqual(row['read_state'], 'enumerated_not_read')
            self.assertEqual(summary['pending_verification_reasons']
                             ['evidence_is_summary_only'], [trap])

    def test_attachment_skip_needs_no_time(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger, _, summary = self.run_carry(folder, bind_pages=True)
            row = next(x for x in ledger['entries'] if x['topic_id'] == E)
            self.assertEqual(row['read_state'], 'file_only_skipped')
            self.assertEqual(row['carry_status'], 'carried_skip')

    def test_pages_pinned_only_by_enumeration_still_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, summary = self.run_carry(folder, bind_pages=False)
            self.assertTrue(summary['prior_snapshot_integrity_verified'])
            self.assertEqual(summary['post_seal_read_topic_ids'], [B])

    def test_earlier_seal_turns_every_read_into_post_seal(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, summary = self.run_carry(folder, bind_pages=True,
                                           sealed='2026-11-01T23:00:00+08:00')
            self.assertEqual(summary['post_seal_read_topic_ids'], [A, B])
            self.assertEqual(summary['read_time_verified_topic_ids'], [])

    def test_manifest_without_seal_time_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            paths, _ = build(folder, sealed=None)
            with self.assertRaises(ValueError):
                carry(paths['new-enum.json'], paths['old-enum.json'], paths['ledger.json'],
                      paths['coverage.json'], paths['manifest.json'])


def coverage_unverified(summary):
    return len(summary['read_time_unverified_topic_ids'])


class HelperTests(unittest.TestCase):
    def test_read_time_status_boundaries(self):
        sealed = datetime(2026, 11, 1, 23, 41, 28, tzinfo=SHANGHAI)
        self.assertEqual(read_time_status({'read_at': '2026-11-01T23:40:00+08:00'}, sealed),
                         'before_seal')
        self.assertEqual(read_time_status({'read_at': '2026-11-01T23:52:00+08:00'}, sealed),
                         'post_seal')
        self.assertEqual(read_time_status({'actual_read_at': '2026-11-01T23:50:00+08:00'}, sealed),
                         'post_seal')
        self.assertEqual(read_time_status({'read_at': 'unknown'}, sealed), 'unknown')

    def test_filename_text_alone_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_evidence(root, 'chunk-03.jsonl', [D], 'reading-chunks')
            found = evidence_is_traceable({'reading_evidence': 'chunk-07.jsonl 读完了'}, D, [root])
            self.assertFalse(found['traceable'])
            self.assertEqual(found['missing_files'], ['chunk-07.jsonl'])
            absent = evidence_is_traceable({'reading_evidence': 'chunk-03.jsonl'}, A, [root])
            self.assertFalse(absent['traceable'])
            self.assertTrue(absent['file_without_topic'])
            real = evidence_is_traceable({'reading_evidence': 'chunk-03.jsonl'}, D, [root])
            self.assertTrue(real['traceable'])
            self.assertEqual(real['strength'], 'body')
            self.assertIn('not proof of', real['note'])

    def test_timestamp_alone_is_not_evidence(self):
        found = evidence_is_traceable({'reading_evidence': '2026-11-01T23:46 逐帖读正文'}, D, [])
        self.assertFalse(found['traceable'])
        self.assertEqual(found['checked_refs'], [])


if __name__ == '__main__':
    unittest.main()
