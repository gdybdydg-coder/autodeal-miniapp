import unittest
from experiments.free_search.public_detail_crosscheck import crosscheck_details
from experiments.free_search.public_peer_pilot import public_peer_record
from experiments.free_search.owner_pilot import _page,PublicPage
import test_owner_pilot as fixtures


class CharacteristicConflictTests(unittest.TestCase):
    def html(self,drive='Передній',quattro=True):
        p=fixtures.page(brand={'name':'Audi'})
        html=p.html.replace('>II<','>C7/4G (FL) • 3.0 TDI '+('Quattro' if quattro else '')+'<')
        return html.replace('>Передній<','>'+drive+'<')

    def test_quattro_and_two_wheel_drive_are_rejected_without_guessing(self):
        for drive in ('Передній','Задній'):
            html=self.html(drive)
            cross=crosscheck_details(html,'12345678')
            self.assertIn('drive_configuration_conflict',cross['issues'])
            self.assertEqual(cross['drive_text'],drive)
            with self.assertRaisesRegex(ValueError,'public_detail_conflict'):
                public_peer_record(html,'12345678',fixtures.NOW)
            with self.assertRaisesRegex(ValueError,'public_detail_conflict'):
                _page(PublicPage('12345678',fixtures.page().url,html,fixtures.NOW),fixtures.NOW,300)

    def test_consistent_drive_or_no_configuration_claim_not_rejected(self):
        for html in (self.html('Повний'),self.html(quattro=False)):
            self.assertNotIn('drive_configuration_conflict',crosscheck_details(html,'12345678')['issues'])
