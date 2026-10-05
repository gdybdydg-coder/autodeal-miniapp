"""Public AUTO.RIA receipts remain isolated from the paid provider channel."""
import socket,sys,unittest
from copy import deepcopy
from pathlib import Path

def deny(*args,**kwargs):raise AssertionError('public_channel_offline_only')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.socket.sendto=deny
socket.create_connection=deny;socket.getaddrinfo=deny
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.test_olx_ria_public_reference import project,NOW
from backend.olx_market import ria_public_reference as public
from backend.olx_market.valuation import screened,estimate

class PublicChannel(unittest.TestCase):
    def test_full_public_receipt_is_screened_on_its_own_channel(self):
        car=project();price,reasons=screened(car,None,NOW)
        self.assertEqual(reasons,[])
        self.assertEqual(price['usd_amount'],'8000')
        self.assertNotIn('ria_reference_evidence',car)

    def test_public_receipt_cannot_masquerade_as_paid_full_info(self):
        car=project();car['ria_reference_evidence']=car.pop('ria_public_reference_evidence')
        self.assertIn('ria_full_info_receipt_unverified',screened(car,None,NOW)[1])

    def test_mixed_receipts_are_held_without_fallback(self):
        car=project();car['ria_reference_evidence']={'method':'auto/info'}
        self.assertIn('ria_reference_channels_ambiguous',screened(car,None,NOW)[1])

    def test_empty_public_marker_does_not_fall_back_to_paid_receipt(self):
        car=project();car['ria_public_reference_evidence']={}
        self.assertIn('ria_public_receipt_unverified',screened(car,None,NOW)[1])

    def test_less_than_eight_full_public_refs_do_not_invent_a_value(self):
        target=project();target.update(source='olx',id='target',url='https://www.olx.ua/d/obyavlenie/test-ID100.html')
        target.pop('ria_public_reference_evidence')
        target.update(vehicle_key='vin-sha256:'+'d'*64,observed_asking_display={'status':'corroborated_display','amount':target['price'],'currency':'USD','description_reviewed_in_full':True,'reasons':[]},field_conflicts=[],price_conflicts=[])
        assessment=estimate(target,[project()],None,NOW,minimum=8)
        self.assertEqual(assessment['sample'],1)
        self.assertEqual(assessment['reasons'],['insufficient_comparables'])
        self.assertIsNone(assessment['reference_usd'])
        self.assertEqual(assessment['methods'],{})
        self.assertNotEqual(assessment['status'],'experimental_asking_estimate')

    def test_public_imports_do_not_load_app_or_paid_transport(self):
        self.assertNotIn('backend.app',sys.modules)
        self.assertNotIn('backend.ria_search',sys.modules)

if __name__=='__main__':unittest.main(verbosity=2)
