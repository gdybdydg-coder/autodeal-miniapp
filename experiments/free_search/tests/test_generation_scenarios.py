from unittest import TestCase
from experiments.free_search.generation_scenarios import unknown_generation_scenarios

class GenerationScenarioTests(TestCase):
    def test_dominant_group_does_not_fill_target_generation(self):
        subject=dict(listing_id='100',brand='A',model='B',year=2022,price=20000,currency='USD',observed_at=1000)
        peers=[dict(subject,listing_id=str(i),generation='A' if i<=6 else 'B',price=23000+i*100) for i in range(1,10)]
        r=unknown_generation_scenarios(subject,peers,1000)
        self.assertEqual([c['outcome']['status'] for c in r['cases']],['estimated','unknown'])
        self.assertIsNone(r['unconditional_reference_price'])
        self.assertNotIn('generation',subject)
        self.assertFalse(r['target_generation_inferred'])
        self.assertFalse(r['exclude_whole_vehicle'])

    def test_unknown_peers_not_treated_as_common_generation(self):
        subject=dict(listing_id='100',brand='A',model='B',year=2022,price=20000,currency='USD',observed_at=1000)
        peers=[dict(subject,listing_id=str(i)) for i in range(1,7)]
        r=unknown_generation_scenarios(subject,peers,1000)
        self.assertEqual(r['unknown_peer_generation_count'],6)
        self.assertEqual(r['cases'],[])
        self.assertFalse(r['ready_for_delivery'])

    def test_known_target_is_not_reassigned(self):
        with self.assertRaises(ValueError):unknown_generation_scenarios({'generation':'A'},[],1000)
