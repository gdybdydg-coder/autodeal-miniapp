"""Small deterministic checks of the many-car end-to-end measurement harness."""
import json
import unittest
from unittest.mock import patch

from experiments.free_search.recovery_benchmark import make_fixture, parse_fixture_detail, run_benchmark, NOW


class RecoveryBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # More than two100-cardpages and everyrecipientrace cohort, with real
        # parser/estimator/durablequeue code, but no network or real recipients.
        cls.normal = run_benchmark(users=20, cars=240, scenario='normal')
        cls.recovery = run_benchmark(users=20, cars=240, scenario='recovery')

    def test_normal_full_many_car_flow_and_pair_dedupe(self):
        report = self.normal
        self.assertEqual(report['stages']['distinct_discovered'], 240)
        self.assertEqual(report['stages']['distinct_details_parsed'], 240)
        self.assertEqual(report['stages']['detail_attempts'], 240)
        self.assertEqual(report['delivery']['accepted_pairs'], 720)
        self.assertEqual(report['delivery']['unique_cars_with_acceptance'], 144)
        self.assertEqual(report['delivery']['duplicate_acceptances'], 0)
        self.assertEqual(report['delivery']['missed_truth_pairs_including_unknown'], 0)
        self.assertEqual(report['delivery']['false_positive_pairs'], 0)

    def test_recovery_really_exercises_faults_in_full_flow(self):
        report = self.recovery
        self.assertEqual(report['stages']['distinct_discovered'], 240)
        self.assertEqual(report['stages']['distinct_details_parsed'], 240)
        self.assertGreater(report['stages']['detail_attempts'], 240)
        self.assertGreater(report['stages']['valuation_attempts'], 240)
        self.assertGreater(report['recovery']['overflow_events'], 0)
        self.assertEqual(report['recovery']['source_backoff_observed'], 1)
        self.assertEqual(report['recovery']['late_low_id_intake'], 1)
        self.assertTrue(report['recovery']['cursor_preserved_on_overflow'])
        self.assertTrue(report['recovery']['stop_then_manual_grant_remained_stopped'])
        self.assertGreater(report['recovery']['manual_access_resolved'], 0)
        for outcome in ('photo_invalid', 'rate_limited', 'known_transient', 'uncertain', 'blocked'):
            self.assertGreater(report['delivery']['sender_outcomes'].get(outcome, 0), 0, outcome)
        self.assertEqual(report['delivery']['ambiguous_pairs_with_more_than_one_attempt'], 0)

    def test_quality_does_not_hide_unknown_or_biased_peer_false_predictions(self):
        quality = self.recovery['stages']['valuation_quality']
        self.assertEqual(quality['total'], 240)
        self.assertGreater(quality['false_positive'], 0)
        self.assertGreater(quality['false_negative'], 0)
        self.assertGreater(quality['unknown_positive'], 0)
        self.assertGreater(quality['unknown_negative'], 0)
        self.assertEqual(quality['missed_positive_total'], quality['false_negative'] + quality['unknown_positive'])
        self.assertEqual(quality['total'], sum(quality[k] for k in
                         ('true_positive', 'false_positive', 'true_negative', 'false_negative', 'unknown')))
        self.assertGreater(self.recovery['stages']['oldest_unprocessed_age_seconds'], 0)

    def test_counterfactual_comparison_honest_and_same_data(self):
        for report in (self.normal, self.recovery):
            baseline = report['baseline_policy_model']
            self.assertIn('not_current_production_execution', baseline['label'])
            self.assertTrue(baseline['same_targets_and_current_details_and_estimator'])
            self.assertEqual(baseline['counts']['discovered'], 200)
            self.assertEqual(baseline['counts']['source_cap_unseen'], 40)
            self.assertGreater(report['delivery']['true_positive_pairs'], baseline['accepted_truth_pairs'])
            self.assertFalse(report['source_coverage_proven'])
            self.assertFalse(report['production_ready'])

    def test_cold_keys_network_and_timing_measurements_are_explicit(self):
        for report in (self.normal, self.recovery):
            self.assertEqual(report['peer_corpus']['initial_paid_cache_rows'], 0)
            self.assertTrue(report['peer_corpus']['created_from_fixture_html_this_run'])
            self.assertEqual(report['peer_corpus']['rows'], 48)
            self.assertEqual(report['outbound']['paid_calls'], 0)
            self.assertEqual(report['outbound']['successful_external_calls'], 0)
            self.assertEqual(report['outbound']['guard']['attempted'], 0)
            self.assertGreater(report['wall_clock_runtime_seconds'], 0)
            self.assertIn('synthetic_clock_not_real_Telegram_or_source_latency', report['limitations'])
            self.assertIsNotNone(report['latency_synthetic_seconds']['publication_to_accepted'])

    def test_fixture_truth_never_leaks_into_estimator_features(self):
        car = make_fixture(200)['targets'][1]
        feature = parse_fixture_detail(car, NOW)
        self.assertNotIn('should_qualify', feature)
        self.assertNotIn('latent_market', feature)
        self.assertNotIn('index', feature)
        changed = dict(car, latent_market=1, should_qualify=not car['should_qualify'])
        self.assertEqual(parse_fixture_detail(changed, NOW), feature)

    def test_duplicate_acceptance_cannot_be_silently_swallowed_by_sender_boundary(self):
        # A fake send-loop bug calls sender twice for one acceptedpair. Pipeline
        # normally catches callbackexceptions asunknown; theharnessmuststillfail.
        from experiments.free_search.durable_pipeline import DurablePipeline
        original = DurablePipeline.send_due
        def duplicated(pipeline, sender, now, **kwargs):
            def faulty(claim, details):
                first = sender(claim, details)
                if first.get('outcome') == 'accepted':
                    sender(claim, details)
                return first
            return original(pipeline, faulty, now, **kwargs)
        with patch.object(DurablePipeline, 'send_due', duplicated):
            with self.assertRaisesRegex(AssertionError, 'duplicate_fixture_acceptance'):
                run_benchmark(users=4, cars=200, scenario='normal')


if __name__ == '__main__':
    unittest.main()
