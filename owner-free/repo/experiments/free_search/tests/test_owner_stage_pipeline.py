import tempfile
from pathlib import Path
from dataclasses import replace
from unittest import TestCase
import test_owner_pilot as fixtures
from test_owner_browser_mean import capture,mean_capture
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_stage_pipeline import StagePipeline
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import PublicCard


class StagePipelineTests(TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'queue.sqlite3'
        self.queue=CandidateQueue(self.path);self.addCleanup(self.queue.close)
        self.obs=Observations(Path(temp.name)/'test.owner-observations.sqlite3');self.addCleanup(self.obs.close)
        self.page=fixtures.page();self.now=fixtures.NOW
        self.card=PublicCard(self.page.listing_id,self.page.url,None,None,None,False,(),())
        baseline=replace(self.card,listing_id='999',url=self.page.url.replace('12345678','999'))
        self.obs.ingest([baseline],self.now-20)
        self.obs.ingest([self.card],self.now-10)
        self.pipeline=StagePipeline(self.queue,self.obs)
        self.assertEqual(self.pipeline.ingest([baseline,self.card],self.now),['12345678'])

    def test_restart_retains_public_evidence_and_sequential_rule(self):
        self.pipeline.detail(self.page,self.now)
        other=CandidateQueue(self.path)
        try:
            resumed=StagePipeline(other,self.obs)
            resumed.capture('12345678',capture(self.page),self.now+1)
            rule=resumed.qualify('12345678',10,self.now+2)
            self.assertTrue(rule['eligible'])
            self.assertEqual(rule['maximum_price_usd'],'12967.785')
            self.assertFalse(other.evidence('12345678','qualified')['send_authorized'])
            self.assertEqual(other.get('12345678')['first_seen'],self.now-10)
        finally: other.close()

    def test_visible_id_without_detail_link_does_not_crash_or_invent_url(self):
        self.assertEqual(self.pipeline.ingest([replace(self.card,url=None)],self.now),[])
        self.assertEqual(self.pipeline.skipped,[{'listing_id':'12345678','reason':'invalid_candidate_identity'}])
        self.assertEqual(self.queue.get('12345678')['url'],self.page.url)

    def test_passat_regression_rejected_without_claim(self):
        self.pipeline.detail(fixtures.page(price=4950),self.now)
        range_capture={**capture(self.page),'lower_text':['від 5 776 $'],'upper_text':['до 6 384 $']}
        self.pipeline.capture('12345678',range_capture,self.now+1)
        result=self.pipeline.qualify('12345678',10,self.now+2)
        self.assertFalse(result['eligible'])
        self.assertEqual(self.queue.get('12345678')['stage'],'rejected')
        self.assertEqual(self.queue.pending(),[])

    def test_mean_stale_or_other_id_does_not_advance(self):
        self.pipeline.detail(self.page,self.now)
        for bad in (mean_capture(self.page),{**capture(self.page),'observed_at':self.now-301},
                    {**capture(self.page),'visible_listing_id':'999'}):
            with self.assertRaises(ValueError): self.pipeline.capture('12345678',bad,self.now)
        self.assertEqual(self.queue.get('12345678')['stage'],'detail')

    def test_expired_after_restart_never_qualifies_or_refreshes_timestamp(self):
        self.pipeline.detail(self.page,self.now)
        self.pipeline.capture('12345678',capture(self.page),self.now)
        with self.assertRaises(ValueError): self.pipeline.qualify('12345678',10,self.now+301)
        self.assertEqual(self.pipeline.expire(self.now+301),['12345678'])
        self.assertFalse(self.queue.discover('12345678',self.page.url,self.now+302))
        self.assertEqual(self.queue.get('12345678')['stage'],'rejected')

    def test_tampered_detail_is_rejected_before_range(self):
        self.pipeline.detail(self.page,self.now)
        self.queue.db.execute("UPDATE stage_events SET evidence=replace(evidence,'8000','9000') WHERE stage='detail'")
        with self.assertRaisesRegex(ValueError,'detail_evidence_changed'):
            self.pipeline.capture('12345678',capture(self.page),self.now)

