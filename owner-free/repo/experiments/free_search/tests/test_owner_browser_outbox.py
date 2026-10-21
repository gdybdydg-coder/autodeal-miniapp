import tempfile,json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_browser_handoff import digest
from experiments.free_search.owner_browser_outbox import BrowserOutbox
import test_owner_stage_pipeline as stages
from test_owner_browser_mean import capture


class OutboxTests(unittest.TestCase):
    def test_restart_does_not_renew_ticket_or_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs';box=BrowserOutbox(path)
            ticket={'listing_id':'123','first_seen':100,'detail_sha256':'a','expires_at':200}
            work={'envelope':{'ticket':ticket,'ticket_sha256':digest(ticket)},'search_ids':[4]}
            self.assertTrue(box.publish(work))
            ticket['expires_at']=400;work['envelope']['ticket_sha256']=digest(ticket)
            self.assertFalse(BrowserOutbox(path).publish(work))
            self.assertEqual(json.loads((path/'123.job.json').read_text())['envelope']['ticket']['expires_at'],200)
            ticket['first_seen']=300;work['envelope']['ticket_sha256']=digest(ticket)
            with self.assertRaisesRegex(ValueError,'outbox_identity_changed'):box.publish(work)

    def test_path_and_digest_cannot_be_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            box=BrowserOutbox(Path(tmp)/'jobs')
            for ident in ['../x','１２３']:
                ticket={'listing_id':ident}
                with self.assertRaises(ValueError):box.publish({'envelope':{'ticket':ticket,'ticket_sha256':digest(ticket)}})

    def test_result_is_consumed_once_with_current_research_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            box=BrowserOutbox(Path(tmp)/'jobs');q=CandidateQueue(Path(tmp)/'queue.sqlite3')
            try:
                result={'listing_id':'123','state':'captured','envelope':{'ticket':{'listing_id':'123'}},'search_ids':[4]}
                (box.path/'123.result.json').write_text(json.dumps(result))
                with patch('experiments.free_search.owner_browser_outbox.accept_capture',return_value={'send_authorized':False}) as accept:
                    pipeline=SimpleNamespace(queue=q)
                    self.assertEqual(box.consume(pipeline,[],100),[])
                    self.assertEqual(len(box.consume(pipeline,[{'id':4,'filters':{'minDiscount':18}}],100)),1)
                    self.assertEqual(accept.call_args.kwargs['research_percent'],18)
                    self.assertEqual(box.consume(pipeline,[{'id':4,'filters':{}}],101),[])
                    self.assertEqual(accept.call_count,1)
            finally:q.close()


class OutboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        stages.StagePipelineTests.setUp(self)
        self.pipeline.detail(self.page,self.now)
        self.box=BrowserOutbox(self.path.parent/'outbox')
        self.searches=[{'id':4,'filters':{'minDiscount':10}}]

    def result(self):
        self.box.recover(self.pipeline,self.searches,self.now,self.now+120)
        job=json.loads((self.box.path/'12345678.job.json').read_text())
        envelope={**job['envelope'],'capture':{**capture(self.page),'observed_at':self.now+1}}
        (self.box.path/'12345678.result.json').write_text(json.dumps({'listing_id':'12345678','state':'captured','envelope':envelope,'search_ids':[4]}))

    def test_real_pipeline_qualification_without_send_and_no_replay(self):
        self.result()
        result=self.box.consume(self.pipeline,self.searches,self.now+2)[0]
        self.assertTrue(result['rule']['eligible']);self.assertFalse(result['send_authorized'])
        self.assertEqual(self.queue.get('12345678')['stage'],'qualified')
        self.assertEqual(self.queue.get('12345678')['first_seen'],self.now-10)
        self.assertEqual(self.box.consume(self.pipeline,self.searches,self.now+3),[])

    def test_expired_capture_does_not_qualify(self):
        self.result()
        result=self.box.consume(self.pipeline,self.searches,self.now+130)[0]
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(self.queue.get('12345678')['stage'],'detail')

    def test_restart_recovers_detail_before_outbox_commit_once(self):
        self.assertEqual(self.box.recover(self.pipeline,self.searches,self.now,self.now+120),['12345678'])
        self.assertEqual(BrowserOutbox(self.box.path).recover(self.pipeline,self.searches,self.now+1,self.now+200),[])
