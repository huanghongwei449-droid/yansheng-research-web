import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import export_research as m

class ExportTests(unittest.TestCase):
    def test_revision_gap_still_selects_new_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'report.md'
            report.write_text('# 新历史稿\n' + '\n'.join('## 章节'+str(n)+'\n'+('新稿正文。'*30) for n in range(3)), encoding='utf-8')
            old = [dict(date='2026-09-24', session='premarket', revision=n, identity='历史重编', sha256=str(n), page=str(n)+'.html', html='<p>OLD</p>') for n in (1, 3)]
            state = root / 'history.json'
            state.write_text(json.dumps({'reports':old, 'pools':[]}), encoding='utf-8')
            args = ['export', '--date', '2026-09-24', '--session', 'premarket', '--identity', '历史重编', '--report', str(report), '--receipt', str(root/'receipt.json')]
            with patch.multiple(m, ROOT=root, OUT=root/'out', STATE=state), patch.object(m, 'build'), patch('sys.argv', args):
                m.main()
            records = json.loads(state.read_text(encoding='utf-8'))['reports']
            self.assertEqual(records[-1]['revision'], 4)
            self.assertEqual(max(records, key=lambda r:r['revision'])['sha256'], m.sha(report.read_bytes()))

    def test_close_attaches_only_to_exact_morning(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'public').mkdir()
            (root/'public/today.html').write_text('<style>body{}</style>')
            pre=dict(date='2026-09-30',session='premarket',revision=1,page='pre.html',identity='审阅版',sha256='pre-sha',html='<p>MORNING</p>')
            close=dict(date='2026-09-30',session='close',revision=1,page='close.html',identity='审阅版',sha256='close-sha',premarket_sha256='pre-sha',html='<p>CLOSE-DELTA</p>')
            with patch.multiple(m,ROOT=root,OUT=root/'out',STATE=root/'state.json'):
                m.build({'reports':[pre,close]})
                page=(root/'out/today.html').read_text(encoding='utf-8')
                self.assertLess(page.index('MORNING'),page.index('CLOSE-DELTA'))
                self.assertEqual(page.count(' hidden'),2)
                self.assertIn('data-session="premarket"',page)
                self.assertIn('data-session="close"',page)
                self.assertIn('class="toc"',page)
                self.assertIn("document.getElementById('premarket')||docs[0]",page)
                close['premarket_sha256']='other-revision'
                m.build({'reports':[pre,close]})
                self.assertNotIn('CLOSE-DELTA',(root/'out/today.html').read_text(encoding='utf-8'))
                close['date']='2026-09-29'
                m.build({'reports':[pre,close]})
                page=(root/'out/today.html').read_text(encoding='utf-8')
                self.assertIn('2026-09-29',page)
                self.assertNotIn('CLOSE-DELTA',page)

    def test_input_html_cannot_execute(self):
        rendered = m.render('<script>alert(1)</script>\n\n[link](javascript:alert(1))')
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('href=', rendered)

    def test_replay_idempotency_and_late_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'public').mkdir()
            (root / 'public/today.html').write_text('<style>body{}</style>')
            report = root / 'report.md'
            report.write_text('# 历史测试\n' + '\n'.join('## 章节'+str(n)+'\n'+('测试正文。'*30) for n in range(3)), encoding='utf-8')
            args = ['export', '--date', '2026-09-24', '--session', 'premarket', '--identity', '历史重编', '--report', str(report), '--receipt', str(root / 'receipt.json')]
            with patch.multiple(m, ROOT=root, OUT=root/'out', STATE=root/'history.json'):
                with patch('sys.argv', args): m.main()
                with patch('sys.argv', args): m.main()
                self.assertEqual(len(json.loads((root/'history.json').read_text(encoding='utf-8'))['reports']), 1)
                args[2] = '2026-09-23'
                with patch('sys.argv', args), self.assertRaises(ValueError): m.main()
                self.assertIn('2026-09-24', (root/'out/today.html').read_text(encoding='utf-8'))

if __name__ == '__main__': unittest.main()
