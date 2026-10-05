"""Isolated prelaunch contracts; synthetic API cards are not live evidence.

Run directly: python backend/test_olx_ria_reference_prelaunch.py
The network barrier precedes every project import; app and .env are unused.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError('prelaunch_test_network_forbidden')


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
socket.create_connection = denied
socket.getaddrinfo = denied
if hasattr(socket.socket, 'sendmsg'):
    socket.socket.sendmsg = denied
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.olx_market import ria_reference as ria
from backend.olx_market.valuation import estimate

NOW = 1791200000


def raw_info(i=0, *, description=None, modification='1.9 TDI MT (105 к.с.)'):
    sid = str(40123456 + i)
    return {'USD': 8075, 'VIN': 'TMBAB1234A' + str(1234567 + i),
        'title': 'Skoda Octavia A5 FL 1.9 TDI',
        'linkToView': '/auto_skoda_octavia_' + sid + '.html',
        'markId': 70, 'modelId': 652, 'markName': 'Skoda', 'modelName': 'Octavia',
        'subCategoryName': 'Універсал', 'technicalCondition': {'id': 1},
        'autoInfoBar': {'damage': False, 'onRepairParts': False, 'abroad': False, 'custom': False},
        'autoData': {'autoId': int(sid), 'active': True, 'isSold': False, 'statusId': 0,
            'year': 2008, 'categoryId': 1, 'bodyId': 2, 'fuelId': 2,
            'fuelName': 'Дизель, 1.9 л.', 'gearBoxId': 1,
            'gearboxName': 'Ручна / Механіка', 'generationId': 3607,
            'driveId': 2, 'raceInt': 300, 'modificationId': 123,
            'modificationName': modification,
            'description': description or 'Продаю автомобіль цілим. На ходу, технічно справний. Українська реєстрація.'}}


def project(raw, *, role='reference'):
    sid = str(raw['autoData']['autoId'])
    provenance = {'role': role, 'frozen_commit': 'b' * 40, 'frozen_at': NOW - 1,
        'search_params': {'category_id': 1, 'marka_id[0]': 70, 'model_id[0]': 652}}
    return ria.from_full_info(raw, sid, NOW, provenance)


class ReferencePrelaunch(unittest.TestCase):
    def test_damage_description_cannot_enter_clean_eight_reference_cohort(self):
        target = project(raw_info())
        references = [project(raw_info(i)) for i in range(1, 9)]
        references[0] = project(raw_info(1, description=(
            'Продаю цілим, на ходу. На кузові є вм’ятини після граду. '
            'На лобовому склі тріщина.')))
        self.assertEqual(references[0]['research_condition'],
            'running_reported_damage:body_dents+windshield_crack')
        result = estimate(target, references, None, NOW)
        self.assertEqual(result['sample'], 7)
        self.assertEqual(result['reasons'], ['insufficient_comparables'])
        self.assertIn('mismatch_condition', result['excluded'][0]['reasons'])

    def test_same_explicit_damage_remains_comparable_not_forbidden(self):
        description = 'Продаю цілим, на ходу. Лобове з тріщиною, скло не замінено.'
        target = project(raw_info(description=description))
        references = [project(raw_info(i, description=description)) for i in range(1, 9)]
        result = estimate(target, references, None, NOW)
        self.assertEqual(result['status'], 'experimental_asking_estimate')
        self.assertEqual(result['sample'], 8)
        self.assertEqual(target['research_condition'], 'running_reported_damage:windshield_crack')
        self.assertNotIn(description, str(target))

    def test_negations_and_completed_repairs_are_not_current_damage(self):
        for description in (
            "Без вм'ятин після граду, без тріщини на лобовому.",
            "Немає тріщин на лобовому та немає вм'ятин.",
            'Нет трещин на лобовом, нет вмятин.',
            "Була тріщина на лобовому, скло замінено. Вм'ятини відремонтовані.",
            'Вмятины после града устранены. Лобовое стекло заменено.',
        ):
            with self.subTest(description=description):
                c = project(raw_info(description=description))
                self.assertEqual(c['research_condition'], 'seller_declared_running')

    def test_negated_repair_preserves_damage(self):
        c = project(raw_info(description="Вм'ятини після граду не відремонтовані. Продаю цілим."))
        self.assertEqual(c['research_condition'], 'running_reported_damage:body_dents')

    def test_known_engine_codes_are_not_pooled_for_same_power(self):
        target = project(raw_info(modification='1.9 TDI BKC MT (105 к.с.)'))
        references = [project(raw_info(i, modification=(
            '1.9 TDI BKC MT (105 к.с.)' if i < 8 else '1.9 TDI BXE MT (105 к.с.)')))
            for i in range(1, 9)]
        self.assertEqual(target['modification'], 'engine_code:bkc')
        self.assertEqual(references[-1]['modification'], 'engine_code:bxe')
        result = estimate(target, references, None, NOW)
        self.assertEqual(result['sample'], 7)
        self.assertEqual(result['reasons'], ['insufficient_comparables'])
        self.assertIn('mismatch_modification', result['excluded'][-1]['reasons'])

    def test_trim_names_and_source_catalog_ids_are_not_engine_codes(self):
        labels = ('1.9 TDI MT Ambiente (105 к.с.)', '1.9 TDI MT Elegance (105 к.с.)')
        target = project(raw_info(modification=labels[0]))
        references = []
        for i in range(1, 9):
            raw = raw_info(i, modification=labels[i % 2])
            raw['autoData']['modificationId'] = 200 + i
            references.append(project(raw))
        result = estimate(target, references, None, NOW)
        self.assertEqual(result['status'], 'experimental_asking_estimate')
        self.assertEqual(result['sample'], 8)
        self.assertIsNone(target.get('modification'))
        evidence = target['source_modification_evidence']
        self.assertEqual(evidence['catalog_id'], 123)
        self.assertEqual(evidence['label_sha256'], hashlib.sha256(labels[0].encode()).hexdigest())
        self.assertNotIn(labels[0], str(target))

    def test_missing_explicit_engine_code_cannot_complete_a_known_code_cohort(self):
        target = project(raw_info(modification='1.9 TDI BKC MT (105 к.с.)'))
        refs = [project(raw_info(i, modification='1.9 TDI BKC MT (105 к.с.)'))
                for i in range(1, 9)]
        refs[-1] = project(raw_info(8))
        result = estimate(target, refs, None, NOW)
        self.assertEqual(result['sample'], 7)
        self.assertIsNone(result['reference_usd'])
        self.assertIn('modification_compatibility_pending', result['excluded'][-1]['reasons'])

    def test_unknown_target_code_cannot_be_filled_from_reference_code(self):
        target = project(raw_info())
        refs = [project(raw_info(i, modification='1.9 TDI BKC MT (105 к.с.)'))
                for i in range(1, 9)]
        result = estimate(target, refs, None, NOW)
        self.assertEqual(result['sample'], 0)
        self.assertIsNone(result['reference_usd'])
        self.assertIsNone(target['modification'])

    def test_explicit_labelled_engine_code_is_retained_without_catalog_guess(self):
        c = project(raw_info(modification='1.9 TDI MT; код двигуна: BXE (105 к.с.)'))
        self.assertEqual(c['modification'], 'engine_code:bxe')

    def test_conflicting_explicit_engine_codes_hold_reference(self):
        with self.assertRaisesRegex(ValueError, 'modification_conflict'):
            project(raw_info(modification='1.9 TDI BKC MT; код двигуна: BXE (105 к.с.)'))

    def test_code_does_not_come_from_year_or_description(self):
        c = project(raw_info(description='На ходу, продається цілим. Двигун BKC.'))
        self.assertIsNone(c.get('modification'))

    def test_condition_and_modification_evidence_are_receipt_bound(self):
        raw = raw_info(modification='1.9 TDI BKC MT (105 к.с.)')
        c = project(raw)
        for field, value in (
            ('modification', 'engine_code:bxe'),
            ('source_modification_evidence', {'catalog_id': 999}),
            ('condition_evidence', {'description_sha256': 'f' * 64}),
        ):
            with self.subTest(field=field):
                changed = deepcopy(c)
                changed[field] = value
                self.assertIn('ria_full_info_receipt_unverified', ria.reasons(changed))
        self.assertNotIn(raw['VIN'], str(c))

    def test_project_does_not_import_production_app(self):
        project(raw_info())
        self.assertNotIn('backend.app', sys.modules)


if __name__ == '__main__':
    unittest.main(verbosity=2)
