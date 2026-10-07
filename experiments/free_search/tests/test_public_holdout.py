import unittest
from experiments.free_search.public_holdout import evaluate_public_holdout

class PublicHoldoutTests(unittest.TestCase):
    def rows(self):
        target=dict(listing_id='100',brand='A',model='B',year=2016,price=10000,currency='USD',observed_at=1000)
        peers=[dict(target,listing_id=str(i),price=14000+i*100) for i in range(1,7)]
        return target,peers

    def test_all_failures_stay_in_frozen_denominator(self):
        target,peers=self.rows()
        result=evaluate_public_holdout(['100','101'],[target],[str(i) for i in range(1,8)],peers,1000)
        self.assertEqual(result['planned_targets'],2)
        self.assertEqual(result['status_counts'],{'estimated':1,'not_evaluated':1})
        self.assertEqual(result['missing_peer_ids'],['7'])
        self.assertFalse(result['ready_for_delivery'])

    def test_other_holdout_cannot_be_peer(self):
        target,peers=self.rows()
        with self.assertRaisesRegex(ValueError,'holdout_peer_leakage'):
            evaluate_public_holdout(['100','101'],[target],['101'],[dict(target,listing_id='101')],1000)

    def test_known_identity_of_any_holdout_is_excluded(self):
        target,peers=self.rows();other=dict(target,listing_id='101',duplicate_key='known-car')
        for p in peers[:2]:p['duplicate_key']='known-car'
        result=evaluate_public_holdout(['100','101'],[target,other],[p['listing_id'] for p in peers],peers,1000)
        self.assertEqual(result['known_cross_target_duplicates_removed'],2)
        self.assertEqual(result['status_counts'],{'unknown':2})

    def test_paid_annotations_do_not_change_outcome(self):
        target,peers=self.rows();ids=[p['listing_id'] for p in peers]
        before=evaluate_public_holdout(['100'],[target],ids,peers,1000)
        for p in peers:p.update(market=1,paid_average=1,historical_label={'lower_usd':1})
        after=evaluate_public_holdout(['100'],[target],ids,peers,1000)
        self.assertEqual(before,after)

    def test_unplanned_record_rejected(self):
        target,peers=self.rows()
        with self.assertRaises(ValueError):evaluate_public_holdout(['100'],[target],['1'],peers,1000)

    def test_missing_targets_still_require_valid_clock(self):
        with self.assertRaises(ValueError):evaluate_public_holdout(['100'],[],['1'],[],float('nan'))

    def test_price_qualifier_in_title_survives_projection(self):
        target,peers=self.rows();target['title']='Перший внесок за авто'
        result=evaluate_public_holdout(['100'],[target],[p['listing_id'] for p in peers],peers,1000)
        self.assertEqual(result['rows'][0]['reason'],'subject_price_ambiguous')
