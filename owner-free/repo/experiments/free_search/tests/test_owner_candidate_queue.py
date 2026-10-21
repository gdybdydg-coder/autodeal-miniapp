import tempfile
from pathlib import Path
from unittest import TestCase
from experiments.free_search.owner_candidate_queue import CandidateQueue


class CandidateQueueTests(TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'queue.sqlite3'
        self.q=CandidateQueue(self.path);self.addCleanup(self.q.close)
        self.url='https://auto.ria.com/uk/auto_example_model_123.html'
        self.q.discover('123',self.url,100)

    def test_restart_preserves_first_seen_and_stage_and_does_not_replay_claim(self):
        self.q.advance('123','discovered','detail',101,{'receipt':'synthetic'})
        other=CandidateQueue(self.path)
        try:
            self.assertFalse(other.discover('123',self.url,999))
            self.assertEqual(other.pending()[0]['first_seen'],100)
            self.assertEqual(other.pending()[0]['stage'],'detail')
            other.advance('123','detail','range',102,{'capture':'synthetic'})
            other.advance('123','range','qualified',103,{'rule':'synthetic'})
            other.advance('123','qualified','claimed',104,{'delivery_id':1})
            self.assertEqual(self.q.pending(),[])
            with self.assertRaises(ValueError): self.q.advance('123','claimed','discovered',105,{'retry':True})
            self.assertEqual(self.q.durations()['discovered->detail'],[1])
        finally: other.close()

    def test_competing_transition_wins_once_and_keeps_evidence_atomic(self):
        other=CandidateQueue(self.path)
        try:
            self.q.advance('123','discovered','detail',101,{'receipt':'first'})
            with self.assertRaises(ValueError): other.advance('123','discovered','detail',102,{'receipt':'second'})
            self.assertEqual(other.db.execute('SELECT count(*) FROM stage_events').fetchone()[0],2)
        finally: other.close()

    def test_invalid_evidence_clock_and_identity_do_not_change_stage(self):
        for stamp in (99,float('nan'),True):
            with self.assertRaises(ValueError): self.q.advance('123','discovered','detail',stamp,{'receipt':1})
        with self.assertRaises(ValueError): self.q.advance('123','discovered','detail',101,{})
        with self.assertRaises(ValueError): self.q.discover('123',self.url.replace('example','other'),101)
        self.assertEqual(self.q.pending()[0]['stage'],'discovered')
