import json
import tempfile
import unittest
from pathlib import Path
import workflow as w

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name)
        self.state = self.p/'workflow-state.json'
        w.save(self.state, {'schema':'yansheng-workflow-v1','date':'2026-10-08','session':'close','stages':[]})

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, data):
        p = self.p/name
        if isinstance(data, str): p.write_text(data,encoding='utf-8')
        else: w.save(p,data)
        return p

    def ready(self):
        report=self.put('report.md','市场风险上升。反弹没有延续，观察条件失效。')
        sha=w.digest(report)
        brief=self.put('research-brief.json',{'date':'2026-10-08','session':'close','report_sha256':sha})
        check=self.put('research-check.json',{'status':'STRUCTURE_PASS','date':'2026-10-08','session':'close','report_sha256':sha,'analysis_sha256':w.digest(brief)})
        review=self.put('editorial-review.json',{'report_sha256':sha,'verdict':'ready','central_claim':'市场风险上升。','criteria':[{'id':k,'result':'pass','excerpt':'反弹没有延续','reason':'测试样本：原反弹条件未延续'} for k in w.CRITERIA]})
        for stage, files in [('preflight',[self.put('availability.json',{'date':'2026-10-08'})]),('sources',[self.put('source-coverage.json',{'read':1})]),('research',[brief]),('draft',[report]),('review',[review,check])]:
            w.record(self.state,stage,files)
        return report,brief

    def test_cannot_skip_sources(self):
        with self.assertRaises(ValueError): w.record(self.state,'research',[self.put('brief.json',{})])

    def test_report_edit_invalidates_review(self):
        report,brief=self.ready()
        report.write_text('改过的正文',encoding='utf-8')
        with self.assertRaises(ValueError):w.check_export(self.state,report.read_bytes(),'2026-10-08','close',brief)

    def test_valid_export_is_not_semantic_pass(self):
        report,brief=self.ready()
        result=w.check_export(self.state,report.read_bytes(),'2026-10-08','close',brief)
        self.assertFalse(result['semantic_quality_verified'])

    def test_wrong_session_rejected(self):
        report,brief=self.ready()
        with self.assertRaises(ValueError):w.check_export(self.state,report.read_bytes(),'2026-10-08','premarket',brief)

    def test_fake_quote_rejected(self):
        self.ready()
        s=w.read(self.state);s['stages'].pop();w.save(self.state,s)
        p=self.p/'editorial-review.json';r=w.read(p);r['criteria'][0]['excerpt']='正文没有这句话';w.save(p,r)
        with self.assertRaises(ValueError):w.record(self.state,'review',[p,self.p/'research-check.json'])

    def test_failed_deployment_not_complete(self):
        report,_=self.ready()
        build=self.put('web-build-receipt.json',{'status':'BUILT_NOT_DEPLOYED','date':'2026-10-08','session':'close','body_sha256':w.digest(report)})
        page=self.put('page-review.md','仅静态检查，视觉尚未完成')
        w.record(self.state,'build',[build,page])
        receipt=self.put('web-deployment-receipt.json',{'status':'failed'})
        with self.assertRaises(ValueError):w.record(self.state,'publish',[receipt])
        self.assertEqual(len(w.load_state(self.state)['stages']),6)

    def test_same_report_deployment_completes(self):
        report,_=self.ready()
        build=self.put('web-build-receipt.json',{'status':'BUILT_NOT_DEPLOYED','date':'2026-10-08','session':'close','body_sha256':w.digest(report)})
        w.record(self.state,'build',[build,self.put('page-review.md','测试页面核对')])
        r={'status':'succeeded','research_date':'2026-10-08','session':'close','report_sha256':w.digest(report),'project_id':'test','version_id':'test-v','deployment_id':'test-d','url':'https://example.invalid'}
        receipt=self.put('web-deployment-receipt.json',r)
        w.record(self.state,'publish',[receipt])
        self.assertEqual(len(w.load_state(self.state)['stages']),7)

if __name__=='__main__':unittest.main()
