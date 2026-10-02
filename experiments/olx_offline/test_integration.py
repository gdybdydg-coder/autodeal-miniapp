import unittest
from experiments.olx_offline.integration import filters_from_backend,access_gate,cross_source
from experiments.olx_offline.test_pipeline import raw

class IntegrationTests(unittest.TestCase):
    def test_filter_units_regions_aliases(self):
        v=filters_from_backend({'mileage':{'to':150},'region':['Київська','Львівська'],'fuel':['Бензин'],'minDiscount':20},currency='USD')
        self.assertEqual(v['filters']['mileage_km_max'],150000)
        self.assertEqual(v['filters']['fuel'],['petrol'])
        self.assertEqual(v['sources'],('auto_ria',));self.assertEqual(v['min_discount'],20)
        self.assertEqual(len(v['filters']['region']),2)
    def test_numeric_dictionary_not_reused(self):
        with self.assertRaises(ValueError):filters_from_backend({'brand':'84'},currency='USD')
    def test_same_access_policy(self):
        for denied in ('billing_allowed','ready','search_enabled'):
            gates={k:(lambda _:True) for k in ('billing_allowed','ready','search_enabled')};gates[denied]=lambda _:False
            self.assertFalse(access_gate('fixture',**gates))
    def test_cross_source_hints_do_not_merge(self):
        a,b=raw(1),raw(1,source='auto_ria')
        self.assertEqual(cross_source(a,b),'possible_duplicate_keep_both')
        a['vehicle_key']=b['vehicle_key']='synthetic_verified_vehicle'
        b['price']=6000
        self.assertEqual(cross_source(a,b),'same_vehicle_review_price_difference')
