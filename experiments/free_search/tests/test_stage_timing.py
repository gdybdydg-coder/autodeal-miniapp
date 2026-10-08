import unittest
from experiments.free_search.stage_timing import stage_timing

class StageTimingTests(unittest.TestCase):
    def run_timing(self, events, status='unknown', environment='local'):
        return stage_timing(events, environment=environment, assessment_status=status)

    def test_first_seen_cannot_be_publication_or_known_id_discovery(self):
        r=self.run_timing({'publication':{'at':10,'proof':'first_seen'},
                           'collector_detected':{'at':11,'proof':'known_id_lookup'},
                           'source_observed':{'at':12,'proof':'public_snapshot'}})
        self.assertIsNone(r['events']['publication'])
        self.assertIsNone(r['events']['collector_detected'])
        self.assertIsNone(r['first_source_availability_at'])
        self.assertEqual(len(r['rejected_evidence']),2)

    def test_unknown_completed_assessment_has_time_but_not_message(self):
        r=self.run_timing({'details_ready':{'at':10,'proof':'public_details_parsed'},
                           'assessment_ready':{'at':20,'proof':'local_assessment'}})
        self.assertEqual(r['durations']['details_to_assessment_seconds'],10)
        self.assertEqual(r['assessment_status'],'unknown')
        self.assertIsNone(r['events']['message_prepared'])
        self.assertIsNone(r['durations']['publication_to_message_seconds'])

    def test_local_render_is_not_delivery(self):
        r=self.run_timing({'telegram_delivered':{'at':30,'proof':'local_render'}})
        self.assertIsNone(r['events']['telegram_delivered'])
        self.assertEqual(r['environment'],'local')

    def test_queue_requires_both_real_events(self):
        r=self.run_timing({'queue_entered':{'at':10,'proof':'queue_receipt'}})
        self.assertIsNone(r['durations']['queue_wait_seconds'])
        r=self.run_timing({'queue_entered':{'at':10,'proof':'queue_receipt'},'queue_released':{'at':16,'proof':'queue_receipt'}})
        self.assertEqual(r['durations']['queue_wait_seconds'],6)

    def test_bad_times_and_reversed_events_fail_closed(self):
        for bad in [True,0,-1,float('nan'),float('inf'),'10']:
            with self.assertRaises(ValueError):self.run_timing({'details_ready':{'at':bad,'proof':'public_details_parsed'}})
        with self.assertRaises(ValueError):self.run_timing({'details_ready':{'at':20,'proof':'public_details_parsed'},'assessment_ready':{'at':10,'proof':'local_assessment'}})

    def test_not_attempted_cannot_have_completion(self):
        with self.assertRaises(ValueError):self.run_timing({'assessment_ready':{'at':20,'proof':'local_assessment'}},status='not_attempted')

    def test_explicit_evidence_durations_keep_environment(self):
        r=self.run_timing({'publication':{'at':10,'proof':'verified_publication'},'collector_detected':{'at':16,'proof':'collector_scan'},'details_ready':{'at':18,'proof':'public_details_parsed'}},environment='render')
        self.assertEqual(r['durations']['publication_to_detection_seconds'],6)
        self.assertEqual(r['durations']['detection_to_details_seconds'],2)
        self.assertEqual(r['environment'],'render')
        self.assertFalse(r['caller_evidence_independently_verified'])
