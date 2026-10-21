import tempfile
from pathlib import Path
import unittest
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_continuous_intake import IntakeState

class ExpansionTests(unittest.TestCase):
    def test_expansion_preserves_stop_cursor_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            q=CandidateQueue(Path(folder)/'queue')
            try:
                s=IntakeState(q);s.accept_head(1,[],100,{});s.stop('operator',101)
                s.expand(6,102)
                self.assertEqual(s.action(1000,False),('stop',None))
                self.assertEqual(q.db.execute('SELECT next_page FROM intake_control').fetchone()[0],2)
                self.assertEqual(q.db.execute('SELECT count(*) FROM intake_events').fetchone()[0],3)
                s.resume(103);s=IntakeState(q,pages=6)
                seq=[]
                for n in range(12):
                    _,page=s.action(1000+n*20,False);seq.append(page)
                    s.accept_head(page,[],1000+n*20,{})
                self.assertEqual(seq,[2,1,3,1,4,1,5,1,6,1,2,1])
            finally:q.close()

    def test_reductions_and_invalid_expansion_rejected(self):
        q=CandidateQueue(':memory:')
        try:
            s=IntakeState(q)
            for value in [2,7,True,6.0]:
                with self.assertRaises(ValueError):s.expand(value,100)
            self.assertEqual(q.db.execute('SELECT pages FROM intake_control').fetchone()[0],3)
        finally:q.close()

    def test_missing_tail_returns_to_head_and_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'queue';q=CandidateQueue(path);s=IntakeState(q)
            s.expand(6,100);s.accept_head(1,[],101,{})
            s.missing_head(2,115,{'status':404})
            self.assertEqual(s.action(130,False),('head',1))
            self.assertEqual(s.available_tail(2,200),3)
            q.close();q=CandidateQueue(path);s=IntakeState(q,pages=6)
            try:
                self.assertEqual(s.available_tail(2,200),3)
                self.assertEqual(s.available_tail(2,716),2)
                self.assertEqual(q.db.execute('SELECT stopped FROM intake_control').fetchone()[0],0)
                with self.assertRaises(ValueError):s.missing_head(1,150,{'status':404})
                with self.assertRaises(ValueError):s.missing_head(2,150,{'status':429})
            finally:q.close()
