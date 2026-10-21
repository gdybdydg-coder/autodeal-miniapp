import unittest,json
import test_owner_pilot as fixtures
from experiments.free_search.owner_comparison_batch import matches,collect_details
import test_owner_stage_pipeline as stages
from dataclasses import replace
from experiments.free_search.owner_pilot import _page
from experiments.free_search.owner_pair_report import comparison

class ComparisonBatchTests(unittest.TestCase):
    def test_filter_gate_and_decimal_report(self):
        page=fixtures.page();record=_page(page,fixtures.NOW,300);record['listing_id']=page.listing_id
        self.assertTrue(matches(record,'passenger',{'brand':'Example'}))
        self.assertFalse(matches(record,'passenger',{'brand':'Audi'}))
        self.assertFalse(matches(record,'passenger',{'price':{'to':7000}}))
        encoded=json.dumps({'details':[record]},default=str)
        self.assertEqual(json.loads(encoded)['details'][0]['listing_id'],page.listing_id)

    def test_pair_never_compares_other_epoch_or_invents_missing_paid(self):
        free={'source_id':'123','search_id':4,'epoch':'e','state':'sent','message_id':1,
              'first_seen':100,'send_started_at':110,'accepted_at':111}
        pair={'free':[free],'at':120,'paid_seen':[],'paid_deliveries':[]}
        self.assertIsNone(comparison(pair)['free_minus_paid_accepted_seconds'])
        pair['paid_seen']=[{'search_id':4,'epoch':'old','first_seen':99}]
        self.assertIsNone(comparison(pair)['free_minus_paid_first_seen_seconds'])
        pair['paid_seen'][0]['epoch']='e'
        self.assertEqual(comparison(pair)['free_minus_paid_first_seen_seconds'],1)


class MultipleCandidateTests(unittest.TestCase):
    def setUp(self):
        stages.StagePipelineTests.setUp(self)
        self.second=fixtures.page('23456789')
        second=replace(self.card,listing_id=self.second.listing_id,url=self.second.url,preview_usd=8000)
        self.obs.ingest([second],self.now)
        self.pipeline.ingest([second],self.now)
        self.cards={self.card.listing_id:replace(self.card,preview_usd=8000),second.listing_id:second}
        self.pages={p.url:p for p in (self.page,self.second)}
        self.calls=[]

    def run_batch(self,limit):
        def get(url,cap):
            self.calls.append(url)
            return self.pages[url].html,{'completed_at':self.now}
        report={'details':[]}
        collect_details(self.pipeline,self.cards,[{'id':4,'filters':{'brand':'Example'}}],
            get,report,self.now+120,limit,clock=lambda:self.now)
        return report

    def test_first_eligible_car_does_not_stop_second_or_mix_identity(self):
        report=self.run_batch(2)
        self.assertEqual(len(self.calls),2)
        self.assertEqual([t['envelope']['ticket']['listing_id'] for t in report['tickets']],
                         [self.page.listing_id,self.second.listing_id])
        self.assertEqual(self.queue.get(self.second.listing_id)['stage'],'detail')
        self.assertNotIn('detail_backlog',report)

    def test_bounded_work_reports_backlog_and_resume_does_not_refetch_first(self):
        report=self.run_batch(1)
        self.assertTrue(report['detail_backlog'])
        resumed=self.run_batch(1)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(resumed['ticket']['ticket']['listing_id'],self.second.listing_id)
        self.assertEqual(self.queue.get(self.page.listing_id)['first_seen'],self.now-10)

    def test_disappeared_listing_does_not_starve_next_candidate(self):
        def get(url,cap):
            if url==self.page.url:return None,{'status':404,'completed_at':self.now}
            return self.pages[url].html,{'status':200,'completed_at':self.now}
        report={'details':[]}
        collect_details(self.pipeline,self.cards,[{'id':4,'filters':{'brand':'Example'}}],
            get,report,self.now+120,2,clock=lambda:self.now)
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'rejected')
        self.assertEqual(report['details'][0]['reason'],'listing_disappeared')
        self.assertEqual(report['ticket']['ticket']['listing_id'],self.second.listing_id)
