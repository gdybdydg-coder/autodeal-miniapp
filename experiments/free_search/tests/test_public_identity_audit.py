import json
import unittest
from experiments.free_search.public_identity_audit import audit_public_identity

class IdentityAuditTests(unittest.TestCase):
    def page(self, ident, vin=None):
        obj={'@type':'Vehicle','url':f'https://auto.ria.com/uk/auto_test_{ident}.html'}
        if vin is not None:obj['vehicleIdentificationNumber']=vin
        return ident,'<script type="application/ld+json">'+json.dumps(obj)+'</script>'

    def test_cross_target_and_peer_duplicates_without_raw_identifier(self):
        a='ABCDEFGH123456789';b='ABCDEFGH123456788'
        r=audit_public_identity([self.page('1',a)],[self.page('2',a),self.page('3',b),self.page('4',b)])
        self.assertEqual(r['source_asserted_cross_target_duplicate_peer_ids'],['2'])
        self.assertEqual(r['source_asserted_peer_duplicate_groups'],[['3','4']])
        self.assertNotIn(a,json.dumps(r));self.assertNotIn(b,json.dumps(r))
        self.assertFalse(r['physical_identity_proven'])

    def test_unknowns_do_not_establish_uniqueness_or_match(self):
        r=audit_public_identity([self.page('1')],[self.page('2','************12345')])
        self.assertEqual(r['unknown_target_ids'],['1'])
        self.assertEqual(r['unknown_peer_ids'],['2'])
        self.assertEqual(r['source_asserted_cross_target_duplicate_peer_ids'],[])
        self.assertFalse(r['physical_identity_proven'])

    def test_identity_mismatch_fails_closed(self):
        with self.assertRaises(ValueError):audit_public_identity([self.page('1')],[('2',self.page('3')[1])])

    def test_multiple_conflicting_source_values_remain_unknown(self):
        first=self.page('2','ABCDEFGH123456789')[1];second=self.page('2','ABCDEFGH123456788')[1]
        r=audit_public_identity([self.page('1')],[('2',first+second)])
        self.assertEqual(r['unknown_peer_ids'],['2'])

    def test_listing_overlap_rejected(self):
        with self.assertRaises(ValueError):audit_public_identity([self.page('1')],[self.page('1')])

    def test_partial_vehicle_node_does_not_erase_known_assertion(self):
        vin='ABCDEFGH123456789'
        html=self.page('2',vin.lower())[1]+self.page('2')[1]
        r=audit_public_identity([self.page('1',vin)],[('2',html)])
        self.assertEqual(r['peer_identifier_known'],1)
        self.assertEqual(r['source_asserted_cross_target_duplicate_peer_ids'],['2'])
