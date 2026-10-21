from unittest import TestCase
import test_owner_stage_pipeline as stages
from test_owner_browser_mean import capture
from experiments.free_search.owner_browser_handoff import next_ticket,accept_capture,digest


class BrowserHandoffTests(TestCase):
    def setUp(self):
        stages.StagePipelineTests.setUp(self)
        self.pipeline.detail(self.page,self.now)

    def envelope(self):
        ticket=next_ticket(self.pipeline,self.now,self.now+120)
        return {**ticket,'capture':{**capture(self.page),'observed_at':self.now+1}}

    def test_capture_qualifies_without_authorizing_and_cannot_replay(self):
        envelope=self.envelope()
        result=accept_capture(self.pipeline,envelope,self.now+2,research_percent=10)
        self.assertTrue(result['rule']['eligible'])
        self.assertFalse(result['send_authorized'])
        self.assertIsNone(next_ticket(self.pipeline,self.now+2,self.now+120))
        with self.assertRaisesRegex(ValueError,'already_consumed'):
            accept_capture(self.pipeline,envelope,self.now+3,research_percent=10)

    def test_expired_ticket_and_old_capture_do_not_advance(self):
        envelope=self.envelope()
        with self.assertRaisesRegex(ValueError,'expired'):
            accept_capture(self.pipeline,envelope,self.now+120,research_percent=10)
        envelope['capture']['observed_at']=self.now-1
        with self.assertRaisesRegex(ValueError,'outside_handoff'):
            accept_capture(self.pipeline,envelope,self.now+2,research_percent=10)
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'detail')

    def test_changed_ticket_and_recomputed_hash_cannot_change_first_seen(self):
        envelope=self.envelope();envelope['ticket']['first_seen']+=1
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):
            accept_capture(self.pipeline,envelope,self.now+2,research_percent=10)
        envelope['ticket_sha256']=digest(envelope['ticket'])
        with self.assertRaisesRegex(ValueError,'evidence_changed'):
            accept_capture(self.pipeline,envelope,self.now+2,research_percent=10)

    def test_resume_after_capture_before_qualification(self):
        envelope=self.envelope()
        self.pipeline.capture(self.page.listing_id,envelope['capture'],self.now+1)
        result=accept_capture(self.pipeline,envelope,self.now+2,research_percent=10)
        self.assertTrue(result['rule']['eligible'])

    def test_stale_queue_expires_without_browsing(self):
        self.assertIsNone(next_ticket(self.pipeline,self.now+301,self.now+400))
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'rejected')
        self.assertIsNone(next_ticket(self.pipeline,self.now,self.now))
