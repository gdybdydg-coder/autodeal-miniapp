import math
from unittest import TestCase
from experiments.free_search.evidence_audit import Timeline, control_overlap, polling_projection, public_only_valuation
from experiments.free_search.network_guard import OutboundGuard


class EvidenceAuditTests(TestCase):
    def test_unknown_publication_never_becomes_detection(self):
        r=Timeline('local_public_probe', detected_at=100, details_ready_at=106).report()
        self.assertIsNone(r['seconds']['publication_to_acceptance'])
        self.assertIsNone(r['seconds']['publication_to_source'])
        self.assertEqual(r['seconds']['detection_to_details'], 6)
        self.assertIsNone(r['seconds']['details_to_valuation'])

    def test_real_queue_stage_is_not_poll_interval(self):
        r=Timeline('render_paid_production', detected_at=100, valuation_ready_at=103,
                   queued_at=110, send_started_at=120, telegram_accepted_at=120.2).report()
        self.assertEqual(r['seconds']['queue_wait'],10)
        self.assertEqual(r['seconds']['telegram_acceptance'],.2)
        self.assertEqual(r['seconds']['detection_to_acceptance'],20.2)
        self.assertIsNone(r['seconds']['detection_to_details'])
        self.assertFalse(r['phone_push_or_read_proven'])

    def test_noncausal_and_nonfinite_times_rejected(self):
        with self.assertRaises(ValueError):
            Timeline('synthetic', detected_at=20, details_ready_at=19).report()
        for invalid in (math.nan, math.inf, True, -1, 'today'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Timeline('synthetic', detected_at=invalid).report()

    def test_control_keeps_misses_and_empty_unknown(self):
        r=control_overlap(['1','2','2'],['2','3'],independent_basis='preexisting_production_log')
        self.assertEqual(r['not_observed'],['1'])
        self.assertEqual(r['sample_overlap_fraction'],.5)
        self.assertIsNone(r['whole_market_recall'])
        self.assertIsNone(control_overlap([],[],independent_basis='independent_public_sample')['sample_overlap_fraction'])
        with self.assertRaises(ValueError):
            control_overlap(['1'],['1'],independent_basis='collector_own_output')

    def test_projection_charges_every_page_and_detail(self):
        r=polling_projection(60,[1000,2000],daily_details=10,detail_body_bytes=500)
        self.assertEqual(r['feed_requests_per_day'],2880)
        self.assertEqual(r['mean_poll_wait_seconds'],30)
        self.assertAlmostEqual(r['decoded_body_gb_per_day'],.004325)
        self.assertIsNone(r['publication_to_ready_seconds'])
        self.assertFalse(r['production_change_authorized'])
        for invalid in (0, -1, math.nan, math.inf, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                polling_projection(invalid,[100])

    def test_paid_cache_and_undated_details_refused(self):
        with self.assertRaises(ValueError): public_only_valuation({'acquisition_basis':'paid_cache'})
        p={'acquisition_basis':'fresh_public_html_no_paid_cache','paid_api_requests':0,
           'telegram_requests':0,'finished_at':100,'details':[{'details':{'availability':'active'}}]}
        with self.assertRaises(ValueError): public_only_valuation(p)
        p['paid_api_requests']=1
        with self.assertRaises(ValueError): public_only_valuation(p)

    def test_cold_start_unknown_and_network_fenced(self):
        detail={'listing_id':'1','availability':'active','brand':'Example','model':'A',
                'year':2020,'price':'5000','currency':'USD'}
        p={'acquisition_basis':'fresh_public_html_no_paid_cache','paid_api_requests':0,
           'telegram_requests':0,'finished_at':101,
           'details':[{'details':detail,'body_received_at':100}]}
        guard=OutboundGuard()
        with guard.isolated(): r=public_only_valuation(p)
        self.assertEqual(r['estimated'],0)
        self.assertEqual(r['unknown'],1)
        self.assertEqual(guard.attempted,0)
        self.assertFalse(r['production_approved'])
