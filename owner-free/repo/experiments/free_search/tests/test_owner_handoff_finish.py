from unittest import TestCase
import test_owner_pilot as fixtures
import test_owner_stage_pipeline as stages
from test_owner_queue_sender import reviewed
from test_owner_browser_mean import capture
from experiments.free_search.owner_browser_handoff import next_ticket,finish_owner_handoff


class FinishHandoffTests(TestCase):
    def setUp(self):
        fixtures.OwnerPilotTests.setUp(self)
        stages.StagePipelineTests.setUp(self)
        self.pipeline.detail(self.page,self.now)
        self.envelope={**next_ticket(self.pipeline,self.now,self.now+120),
                       'capture':{**capture(self.page),'observed_at':self.now}}
        self.review=reviewed(self.page,self.now,['Історія ремонту не підтверджена'])
        self.calls=[]

    def finish(self,enabled=True):
        def transport(uid,text):
            self.calls.append((uid,text))
            return {'ok':True,'result':{'message_id':777}}
        return finish_owner_handoff(self.engine,self.settings,self.pipeline,self.envelope,
                fixtures.OWNER,transport,condition_review=self.review,enabled=enabled,clock=lambda:self.now)

    def test_integrated_capture_current_rule_review_and_synthetic_delivery(self):
        self.assertEqual(self.finish()['state'],'sent')
        self.assertEqual(len(self.calls),1)
        self.assertIn('15%',self.calls[0][1])
        self.assertIn('Історія ремонту не підтверджена',self.calls[0][1])
        self.assertEqual(self.finish()['reason'],'handoff_already_consumed')
        self.assertEqual(len(self.calls),1)

    def test_current_saved_rule_rejects_before_send(self):
        self.envelope['capture'].update(lower_text=['від 8 500 $'],upper_text=['до 9 500 $'])
        self.assertEqual(self.finish()['reason'],'owner_discount_threshold')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'rejected')
        self.assertEqual(self.calls,[])

    def test_missing_review_and_disabled_leave_capture_unconsumed(self):
        self.assertEqual(self.finish(enabled=False)['state'],'disabled')
        self.review=None
        self.assertEqual(self.finish()['reason'],'condition_review_required')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'detail')
