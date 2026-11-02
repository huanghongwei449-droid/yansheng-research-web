import json
import unittest
from pathlib import Path
import export_research as export
import workbench


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent
        cls.state = json.loads((cls.root/'research-history.json').read_text(encoding='utf8'))

    def test_trigger_recovery_and_observation_do_not_upgrade(self):
        d=workbench.project(self.root,self.state)
        c=d['companies']['600657']
        self.assertEqual('暂缓参与',c['status'])
        self.assertTrue(any('降级' in h['to'] for h in c['pool_history']))
        self.assertIn('已触发',c['conditions'][0]['result'])
        self.assertIn('不撤销',c['conditions'][0]['result'])
        self.assertTrue(any('收复' in h['reason'] for h in c['pool_history']))
        self.assertIn('未形成个股参与结论',d['directions'][2]['change'])

    def test_company_current_date_is_individual(self):
        d=workbench.project(self.root,self.state)
        latest_pool=max(self.state['pools'],key=lambda p:(p['date'],p['session']=='close',str(p.get('revision',''))))
        self.assertEqual(d['companies']['600657']['research_date'],latest_pool['date'])
        self.assertEqual(d['companies']['600276']['research_date'],'2026-09-30')
        self.assertEqual(d['companies']['000678']['research_date'],'2026-09-24')
        self.assertTrue(d['companies']['600657']['records'])

    def test_all_references_are_published_and_no_chat_drafts(self):
        d=workbench.project(self.root,self.state)
        ids={r['id'] for r in d['reports']}
        for direction in d['directions']:
            for k in ('premarket_ref','close_ref'):
                if k in direction:self.assertIn(direction[k],ids)
        self.assertFalse(any(r['date']=='2026-09-30' and r['revision']>5 for r in d['reports']))
        latest=max(self.state['reports'],key=lambda r:(r['date'],r['session']=='close',r['revision']))
        self.assertIn(f"{latest['date']}:{latest['session']}:{latest['sha256']}",ids)
        self.assertTrue(any(r['date']=='2026-09-30' and r['session']=='close' and r['sha256']=='982dfdb7abf07216acdb13a73aa9e357adf205a17b57fbfaa312a13311ebf3a4' for r in self.state['reports']))

    def test_mention_does_not_replace_company_judgment(self):
        baseline=json.loads((self.root/'company-baseline.json').read_text(encoding='utf8'))
        data=workbench.project(self.root,self.state)
        company=data['companies']['688628']
        self.assertEqual(company['research_date'],baseline['date'])
        self.assertEqual(company['summary'],baseline['companies']['688628']['summary'])
        self.assertTrue(any(r['date']>baseline['date'] for r in company['records']))
        self.assertNotIn('三祥新材',company['summary'])

    def test_release_hash_completeness_and_public_boundary(self):
        export.build(self.state)
        m=json.loads((self.root/'out/publication.json').read_text(encoding='utf8'))
        raw=(self.root/'out'/m['bundle']['path']).read_bytes()
        self.assertEqual(workbench.digest(raw),m['bundle']['sha256'])
        payload=json.loads(raw)
        self.assertEqual(payload['release_id'],m['release_id'])
        self.assertEqual(payload['data']['latest'],m['latest'])
        self.assertNotIn('C:\\Users',raw.decode('utf8'))
        self.assertNotIn('D:/',raw.decode('utf8'))
        for page in ('today.html','companies.html','company.html','directions.html','opportunities.html'):
            self.assertIn(page,payload['pages'])

    def test_repeat_build_reuses_release(self):
        archives = {p.name: p.read_bytes() for p in (self.root/'out').glob('report-*.html')}
        export.build(self.state)
        first=(self.root/'out/publication.json').read_bytes()
        export.build(self.state)
        self.assertEqual(first,(self.root/'out/publication.json').read_bytes())
        for name, raw in archives.items():
            self.assertEqual(raw, (self.root/'out'/name).read_bytes())

    def test_summary_has_real_session_and_current_direction_date(self):
        export.build(self.state)
        latest=max(self.state['reports'],key=lambda r:(r['date'],r['session']=='close',r['revision']))
        page=(self.root/'out/today.html').read_text(encoding='utf8')
        label='盘前要点' if latest['session']=='premarket' else '收盘变化'
        summary=page.split('<section class="changes">',1)[1].split('</section>',1)[0]
        self.assertIn(latest['date']+' '+label,summary)
        for direction in self.state['connections']['directions']:
            if direction['date']!=latest['date']:
                self.assertNotIn('directions.html#'+direction['id']+'"',summary)

    def test_original_archive_body_and_no_cross_company_note(self):
        export.build(self.state)
        for r in self.state['reports']:
            page=(self.root/'out'/r['page']).read_text(encoding='utf8')
            self.assertIn(r['html'],page)
        company=(self.root/'out/company.html').read_text(encoding='utf8')
        self.assertNotIn('康强电子：两日整理',company)


if __name__=='__main__':unittest.main()
