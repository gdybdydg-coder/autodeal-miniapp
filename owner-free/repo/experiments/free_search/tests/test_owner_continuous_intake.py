import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_continuous_intake import IntakeState, current_research_searches


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'queue.sqlite3'
        self.q = CandidateQueue(self.path); self.s = IntakeState(self.q)

    def tearDown(self):
        self.q.close(); self.tmp.cleanup()

    def test_head_priority_and_detail_interleaving(self):
        self.assertEqual(self.s.action(100,True),('head',1))
        self.s.accept_head(1,[],100,{'ids':[]})
        self.assertEqual(self.s.action(104,True),('detail',None))
        self.assertEqual(self.s.action(112,True),('head',2))

    def test_restart_retains_cursor_preview_and_first_seen(self):
        self.q.discover('123','https://auto.ria.com/uk/auto_skoda_octavia_123.html',100)
        self.s.accept_head(1,[SimpleNamespace(listing_id='123',preview_usd=6500)],102,{'ids':['123']})
        self.q.close(); self.q=CandidateQueue(self.path); self.s=IntakeState(self.q)
        self.assertEqual(self.s.action(114,False),('head',2))
        self.assertEqual(self.s.previews()['123'].preview_usd,6500)
        self.assertEqual(self.q.get('123')['first_seen'],100)
        self.assertFalse(self.q.discover('123','https://auto.ria.com/uk/auto_skoda_octavia_123.html',114))

    def test_stop_survives_restart_and_resume_is_explicit(self):
        self.s.stop('operator_stop',101)
        self.q.close(); self.q=CandidateQueue(self.path); self.s=IntakeState(self.q)
        self.assertEqual(self.s.action(200,True),('stop',None))
        self.s.resume(201)
        self.assertEqual(self.s.action(201,True),('head',1))

    def test_rotates_head_without_missing_tail_dependency(self):
        for page in [1,2,3,1]:
            self.assertEqual(self.s.action(1000+page*100,False)[1],page)
            self.s.accept_head(page,[],100,{'ids':[]})

    def test_policy_change_does_not_reset_history(self):
        self.s.stop('reason',101)
        with self.assertRaisesRegex(ValueError,'intake_policy_changed'):
            IntakeState(self.q,pages=6)
        self.assertEqual(self.s.action(200,True),('stop',None))

    def test_stale_snapshot_is_not_reused(self):
        p=Path(self.tmp.name)/'access.json'
        p.write_text(json.dumps({'status':'readonly_ok','access':{'ready':True,'entitled':True,'approved':True},'at':100,'searches':[{'id':4,'enabled':True}]}))
        self.assertEqual(len(current_research_searches(p,101)),1)
        self.assertEqual(current_research_searches(p,401),[])
        self.assertEqual(current_research_searches(p,99),[])
        data=json.loads(p.read_text()); data['access']['ready']=False; p.write_text(json.dumps(data))
        self.assertEqual(current_research_searches(p,101),[])
