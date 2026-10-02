import copy
import unittest
from experiments.olx_offline.evaluation import evaluate
from experiments.olx_offline.test_pipeline import NOW


def fixture():
    control=[dict(source='olx',id=str(i),recorded_at=NOW,profitable='yes' if i<4 else 'no',publication_verified=False) for i in range(6)]
    results=[dict(source='olx',id=str(i),detected_at=NOW+20,processed=True,valuation='estimated',filter_match='yes',recommended=i in (0,4)) for i in range(5)]
    results[1]['valuation']='unknown'
    results[2]['valuation']='not_attempted';results[2]['processed']=False
    # Positive #3 is absent, negative #5 appears and is correctly not recommended.
    results[3]['id']='5'
    return dict(kind='synthetic',control_origin=dict(method='synthetic_holdout',reference='reference-fixture-v1'),
                collector_run_ref='collector-fixture-v1',window=dict(start=NOW,end=NOW+3600),
                collector_complete=False,control=control,results=results)

class EvaluationTests(unittest.TestCase):
    def test_unknown_missing_count_against_recall(self):
        r=evaluate(fixture());q=r['quality']
        self.assertEqual(q['tp'],1);self.assertEqual(q['fp'],1);self.assertEqual(q['unknown'],1)
        self.assertEqual(q['not_evaluated'],1);self.assertEqual(q['missing_positive'],1)
        self.assertEqual(q['end_to_end_positive_recall'],.25)
        self.assertEqual(q['positive_not_recommended_total'],3)
        self.assertFalse(r['whole_olx_coverage_verified'])
    def test_empty_is_unknown_not_perfect(self):
        m=fixture();m['control']=[];m['results']=[]
        r=evaluate(m);self.assertIsNone(r['coverage']['control_recall']);self.assertIsNone(r['quality']['precision_labeled'])
    def test_collector_cannot_be_own_reference(self):
        m=fixture();m['control_origin']['reference']=m['collector_run_ref']
        with self.assertRaises(ValueError):evaluate(m)
    def test_conflicting_duplicate_fails(self):
        m=fixture();c=copy.deepcopy(m['control'][0]);c['profitable']='no';m['control'].append(c)
        with self.assertRaises(ValueError):evaluate(m)
    def test_identical_duplicates_count_once(self):
        m=fixture();m['control'].append(copy.deepcopy(m['control'][0]));self.assertEqual(evaluate(m)['coverage']['control'],6)
    def test_source_namespace(self):
        m=fixture();c=copy.deepcopy(m['control'][0]);c['source']='auto_ria';m['control'].append(c)
        self.assertEqual(evaluate(m)['coverage']['missing_from_control'],2)
    def test_latency_only_verified_publication(self):
        m=fixture();m['control'][0].update(publication_verified=True,published_at=NOW-10)
        r=evaluate(m)['publication_to_detection_seconds'];self.assertEqual(r['sample'],1);self.assertEqual(r['p95'],30)
        self.assertEqual(r['unverified_publication'],4)
    def test_future_publication_excluded_from_latency(self):
        m=fixture();m['control'][0].update(publication_verified=True,published_at=NOW+30)
        r=evaluate(m)['publication_to_detection_seconds'];self.assertEqual(r['invalid_time_order'],1);self.assertIsNone(r['p50'])
    def test_false_real_evidence_rejected(self):
        m=fixture();m['kind']='observational'
        with self.assertRaises(ValueError):evaluate(m)
    def test_inconsistent_recommendation_rejected(self):
        m=fixture();m['results'][1]['recommended']=True
        with self.assertRaises(ValueError):evaluate(m)
    def test_window_enforced(self):
        m=fixture();m['results'][0]['detected_at']=NOW+3600
        with self.assertRaises(ValueError):evaluate(m)
    def test_no_private_ids_in_output(self):
        m=fixture();m['control'][0]['id']='PRIVATE_SENTINEL';m['results'][0]['id']='PRIVATE_SENTINEL'
        self.assertNotIn('PRIVATE_SENTINEL',str(evaluate(m)))
