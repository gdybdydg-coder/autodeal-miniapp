"""Synthetic source+ID evaluation contracts, no production imports or I/O."""
from copy import deepcopy
import hashlib
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError('evaluation_prelaunch_network_forbidden')


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
socket.create_connection = denied
socket.getaddrinfo = denied
if hasattr(socket.socket, 'sendmsg'):
    socket.socket.sendmsg = denied
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.test_olx_ria_reference_prelaunch import NOW, raw_info, project
from backend.olx_market.evaluation import stability, evaluate_holdout


def olx_card(source_id, *, key='synthetic-olx'):
    car = deepcopy(project(raw_info()))
    car.update(source='olx', id=source_id, url='https://www.olx.ua/d/obyavlenie/fixture-ID'+source_id+'.html',
        vehicle_key='vin-sha256:'+hashlib.sha256(key.encode()).hexdigest(),
        observed_asking_display={'status':'corroborated_display','amount':'8075','currency':'USD',
            'description_reviewed_in_full':True,'reasons':[]},
        price_review={'reasons':['full_price_unconfirmed']},
        field_conflicts=[], research_field_conflicts=[], price_conflicts=[])
    car.pop('ria_reference_evidence')
    return car


class EvaluationIdentityPrelaunch(unittest.TestCase):
    def test_leave_one_out_omits_exactly_one_source_and_id(self):
        refs = [project(raw_info(i)) for i in range(1, 8)]
        refs.append(olx_card(refs[0]['id'], key='independent-second-platform-car'))
        target = olx_card('900000001', key='target')
        review = stability(target, refs, None, NOW)
        self.assertEqual(review['assessment']['sample'], 8)
        self.assertEqual(len(review['leave_one_out']), 8)
        identities = {(r['omitted_source'],r['omitted_id']) for r in review['leave_one_out']}
        self.assertEqual(identities, {(c['source'],c['id']) for c in refs})
        self.assertTrue(all(r['status']=='experimental_asking_estimate' for r in review['leave_one_out']))
        self.assertEqual(review['status'], 'stable_asking_sample')

    def test_ambiguous_legacy_id_collision_fails_closed(self):
        refs = [project(raw_info(i)) for i in range(1, 9)]
        control = olx_card(refs[0]['id'], key='distinct-control')
        split = {c['id']:'reference' for c in refs}
        split[control['id']] = 'holdout'
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            evaluate_holdout(refs+[control], split, None, NOW)

    def test_namespaced_frozen_roles_keep_colliding_ids_independent(self):
        refs = [project(raw_info(i)) for i in range(1, 9)]
        control = olx_card(refs[0]['id'], key='distinct-control')
        split = {(c['source'],c['id']):'reference' for c in refs}
        split[('olx',control['id'])] = 'holdout'
        split[('olx','999999999')] = 'holdout'
        result = evaluate_holdout(refs+[control], split, None, NOW)
        self.assertEqual(result['reference_count'], 8)
        self.assertEqual(result['frozen_holdout_count'], 2)
        self.assertEqual(result['loaded_holdout_count'], 1)
        self.assertEqual(result['missing_holdout_count'], 1)
        self.assertEqual(result['estimated_holdout_count'], 1)
        self.assertEqual(result['rows'][0]['target_source'], 'olx')
        self.assertEqual(len(result['rows'][0]['comparison_group_keys']), 8)

    def test_unambiguous_legacy_split_remains_compatible(self):
        refs = [project(raw_info(i)) for i in range(1, 9)]
        control = olx_card('900000001', key='control')
        split = {c['id']:'reference' for c in refs}|{control['id']:'holdout'}
        result = evaluate_holdout(refs+[control], split, None, NOW)
        self.assertEqual(result['frozen_holdout_count'], 1)
        self.assertEqual(result['estimated_holdout_count'], 1)
        self.assertEqual(result['rows'][0]['comparison_group_ids'], [c['id'] for c in refs])

    def test_json_source_colon_id_membership_supports_colliding_ids(self):
        refs = [project(raw_info(i)) for i in range(1, 9)]
        control = olx_card(refs[0]['id'], key='distinct-control')
        split = {c['source']+':'+c['id']:'reference' for c in refs}
        split['olx:'+control['id']] = 'holdout'
        result = evaluate_holdout(refs+[control], split, None, NOW)
        self.assertEqual(result['reference_count'], 8)
        self.assertEqual(result['frozen_holdout_count'], 1)
        self.assertEqual(result['estimated_holdout_count'], 1)

    def test_duplicate_legacy_and_namespaced_aliases_fail_closed(self):
        c = olx_card('900000001')
        split = {c['id']:'holdout',('olx',c['id']):'holdout'}
        with self.assertRaises(ValueError):
            evaluate_holdout([c], split, None, NOW)

    def test_no_production_app_import(self):
        self.assertNotIn('backend.app', sys.modules)


if __name__ == '__main__':
    unittest.main(verbosity=2)
