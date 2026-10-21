from unittest import TestCase
from experiments.free_search.owner_handoff_timings import handoff_timings


class HandoffTimingsTests(TestCase):
    def test_stage_durations_include_wait_and_do_not_claim_publication_or_send(self):
        result=handoff_timings(100,117,160,163)
        self.assertEqual(result['first_observed_to_detail'],17)
        self.assertEqual(result['detail_to_browser_capture'],43)
        self.assertEqual(result['browser_capture_to_qualification'],3)
        self.assertEqual(result['first_observed_to_qualification'],63)
        self.assertIsNone(result['publication_to_discovery'])
        self.assertIsNone(result['telegram_acceptance'])

    def test_clock_regression_and_invalid_values_rejected(self):
        for values in [(100,99,110,120),(100,110,109,120),(100,110,120,119),
                       (100,110,float('nan'),120),(True,110,120,130)]:
            with self.assertRaises(ValueError):handoff_timings(*values)
