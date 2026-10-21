from unittest import TestCase
from unittest.mock import patch
import test_owner_pilot as fixtures
import test_owner_stage_pipeline as stages
from test_owner_browser_mean import capture
from experiments.free_search.owner_queue_sender import send_queued
from sqlalchemy.orm import Session
from sqlalchemy import select
from backend.models import User,Delivery
import hashlib


def reviewed(page,now,findings=None):
    return {'listing_id':page.listing_id,'listing_url':page.url,
            'detail_sha256':hashlib.sha256(page.html.encode()).hexdigest(),
            'observed_at':now,'visible_history_checked':True,
            'findings':[] if findings is None else findings}


class QueueSenderTests(TestCase):
    def setUp(self):
        fixtures.OwnerPilotTests.setUp(self)
        stages.StagePipelineTests.setUp(self)
        self.pipeline.detail(self.page,self.now)
        self.pipeline.capture(self.page.listing_id,capture(self.page),self.now)
        self.pipeline.qualify(self.page.listing_id,0,self.now) # Research value, NOT saved threshold.
        self.calls=[]
        self.review=reviewed(self.page,self.now)

    def send(self,**kw):
        def transport(uid,text):
            self.calls.append((uid,text))
            return {'ok':True,'result':{'message_id':456}}
        return send_queued(self.engine,self.settings,self.pipeline,self.page.listing_id,
                           fixtures.OWNER,transport,enabled=kw.pop('enabled',True),
                           clock=lambda:self.now,condition_review=self.review,**kw)

    def test_current_saved_percentage_and_no_replay(self):
        self.assertEqual(self.send()['state'],'sent')
        self.assertEqual(len(self.calls),1)
        self.assertIn('15%',self.calls[0][1])
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'sent')
        self.assertEqual(self.send()['reason'],'queue_not_qualified_or_already_claimed')
        self.assertEqual(len(self.calls),1)

    def test_disabled_and_stopped_owner_do_not_claim(self):
        self.assertEqual(self.send(enabled=False)['state'],'disabled')
        with Session(self.engine) as db,db.begin():db.get(User,fixtures.OWNER).ready=False
        self.assertEqual(self.send()['reason'],'owner_stopped')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'qualified')
        self.assertEqual(self.calls,[])

    def test_stale_evidence_cannot_send(self):
        self.now+=301
        self.assertEqual(self.send()['reason'],'stale_observation')
        self.assertEqual(self.calls,[])

    def test_other_client_search_does_not_claim(self):
        result=send_queued(self.engine,self.settings,self.pipeline,self.page.listing_id,
                          fixtures.CLIENT,lambda *args:self.fail('must not send'),
                          enabled=True,clock=lambda:self.now,condition_review=self.review)
        self.assertEqual(result['reason'],'current_owner_search_missing')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'qualified')

    def test_expired_entitlement_does_not_claim(self):
        from backend.billing_models import Entitlement
        with Session(self.engine) as db,db.begin():
            db.get(Entitlement,fixtures.OWNER).expires_at=self.now-1
        self.assertEqual(self.send()['state'],'blocked')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'qualified')
        self.assertEqual(self.calls,[])

    def test_research_qualified_but_current_rule_rejects_before_shared_claim(self):
        from experiments.free_search.owner_public_mean import browser_quote
        # Rebuild a qualified research fixture whose price passes 0% but fails saved15%.
        cap={**capture(self.page),'lower_text':['від 8 500 $'],'upper_text':['до 9 500 $']}
        import json
        self.queue.db.execute("UPDATE stage_events SET evidence=? WHERE stage='range'",
            (json.dumps({'capture':cap,'source':browser_quote(cap,self.page,self.now)}),))
        self.assertEqual(self.send()['reason'],'owner_discount_threshold')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'cancelled')
        with Session(self.engine) as db:self.assertIsNone(db.scalar(select(Delivery.id)))

    def test_crash_claim_survives_and_prevents_replay(self):
        with patch('experiments.free_search.owner_queue_sender.send_one',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.send()
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'uncertain')
        self.assertEqual(self.send()['reason'],'queue_not_qualified_or_already_claimed')
        self.assertEqual(self.calls,[])

    def test_ambiguous_transport_terminal(self):
        with patch('experiments.free_search.owner_queue_sender.send_one',return_value={'state':'free_uncertain','delivery_id':42}):
            self.assertEqual(self.send()['state'],'free_uncertain')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'uncertain')

    def test_real_sender_ambiguous_transport_retains_shared_claim(self):
        attempts=[]
        def transport(uid,text):
            attempts.append(uid)
            return None
        result=send_queued(self.engine,self.settings,self.pipeline,self.page.listing_id,
                          fixtures.OWNER,transport,enabled=True,clock=lambda:self.now,condition_review=self.review)
        self.assertEqual(result['state'],'free_uncertain')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'uncertain')
        self.assertEqual(self.send()['reason'],'queue_not_qualified_or_already_claimed')
        self.assertEqual(attempts,[fixtures.OWNER])
        with Session(self.engine) as db:
            self.assertEqual(db.get(Delivery,result['delivery_id']).state,'free_uncertain')

    def test_missing_condition_review_blocks_before_claim(self):
        self.review=None
        self.assertEqual(self.send()['reason'],'condition_review_required')
        self.assertEqual(self.queue.get(self.page.listing_id)['stage'],'qualified')
        self.assertEqual(self.calls,[])

    def test_damage_disclosure_is_sent_as_escaped_text(self):
        self.review['findings']=['За історією: переднє пошкодження <неперевірений ремонт>']
        self.assertEqual(self.send()['state'],'sent')
        self.assertIn('переднє пошкодження &lt;неперевірений ремонт&gt;',self.calls[0][1])
        self.assertIn('Повноту історії та якість ремонту не підтверджено',self.calls[0][1])

    def test_review_of_other_page_or_stale_review_blocks(self):
        self.review['listing_id']='999'
        self.assertEqual(self.send()['reason'],'condition_review_evidence_mismatch')
        self.review=reviewed(self.page,self.now-301)
        self.assertEqual(self.send()['reason'],'condition_review_stale')
        self.assertEqual(self.calls,[])
