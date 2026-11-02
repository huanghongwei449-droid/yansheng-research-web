import copy
import json
import unittest
from pathlib import Path
from opportunity_view import validate_pool, render_pool

class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.pool=json.loads((Path(__file__).parent/'fixtures/opportunity-pool-20261008.json').read_text(encoding='utf-8'))
    def test_real_migration_and_group_order(self):
        self.assertEqual(validate_pool(self.pool)['counts'],dict(confirmed=0,candidate=2,paused=4,exited=4))
        page=render_pool(self.pool)
        self.assertLess(page.index('id="pool-candidate"'),page.index('id="pool-paused"'))
        self.assertIn('<details class="pool-exited" id="pool-exited">',page)
        self.assertNotIn('pool-exited" open',page)
    def test_same_state_risk_is_not_downgrade(self):
        risk=next(x for x in self.pool['events'] if x['code']=='600657')
        self.assertEqual((risk['from'],risk['to']),('paused','paused'))
        self.assertIn('收回不撤销失败',render_pool(self.pool))
        risk['event_type']='downgraded'
        with self.assertRaises(ValueError):validate_pool(self.pool)
    def test_previous_exits_are_not_today_events(self):
        self.assertEqual({x['event_type'] for x in self.pool['events']},{'risk','added'})
        dates={x['code']:x['state_since'] for x in self.pool['items'] if x['lifecycle_state']=='exited'}
        self.assertEqual(dates,{'300502':'2026-09-28','300308':'2026-09-28','300394':'2026-09-28','002080':'2026-09-24'})
    def test_unknown_state_and_active_exit_are_rejected(self):
        p=copy.deepcopy(self.pool);p['items'][0]['lifecycle_state']='maybe'
        with self.assertRaises(ValueError):validate_pool(p)
        p=copy.deepcopy(self.pool);c=next(x for x in p['items'] if x['lifecycle_state']=='exited');c['current_condition']['participation']='buy'
        with self.assertRaises(ValueError):validate_pool(p)
    def test_reentry_needs_new_setup_and_preserved_archive(self):
        previous=copy.deepcopy(self.pool);c=next(x for x in self.pool['items'] if x['code']=='300502');old=copy.deepcopy(c)
        c['lifecycle_state']='candidate';c['current_condition']=copy.deepcopy(self.pool['items'][-1]['current_condition'])
        with self.assertRaises(ValueError):validate_pool(self.pool,previous)
        c['setup_id']='300502:2026-10-08:new';c['archived_setups']=[{'setup_id':old['setup_id'],'history':old['history']}]
        validate_pool(self.pool,previous)
    def test_old_history_cannot_be_erased(self):
        previous=copy.deepcopy(self.pool);self.pool['items'][0]['history'].pop(0)
        with self.assertRaises(ValueError):validate_pool(self.pool,previous)
    def test_event_condition_and_date_binding(self):
        p=copy.deepcopy(self.pool);p['events'][0]['condition_ref']='not-read'
        with self.assertRaises(ValueError):validate_pool(p)
        p=copy.deepcopy(self.pool);p['events'][0]['observed_at']='2026-10-07'
        with self.assertRaises(ValueError):validate_pool(p)
        p=copy.deepcopy(self.pool);p['events'][0]['source_report_ref']='old-report'
        with self.assertRaises(ValueError):validate_pool(p)
    def test_no_change_has_no_empty_change_column(self):
        self.pool['events']=[]
        self.assertNotIn('今日变化',render_pool(self.pool))
    def test_duplicate_current_stock_and_unverified_confirmation_rejected(self):
        p=copy.deepcopy(self.pool);p['items'].append(copy.deepcopy(p['items'][0]))
        with self.assertRaises(ValueError):validate_pool(p)
        p=copy.deepcopy(self.pool);p['events']=[];c=p['items'][-1];c['lifecycle_state']='confirmed';c['current_condition']['participation']='条件成立后参与'
        with self.assertRaises(ValueError):validate_pool(p)

if __name__=='__main__':unittest.main()
