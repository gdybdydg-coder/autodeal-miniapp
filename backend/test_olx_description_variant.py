"""Description-only own-car generation claims, with no source or app I/O.

Synthetic fixtures contain a harmless short seller clause, never raw contacts
or VIN. The optional saved-real assertion reads the original local receipt;
it does not publish that complete seller description or refresh its timestamp.
"""
from html import escape
import hashlib
import json
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError('description_variant_network_forbidden')


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
socket.create_connection = denied
socket.getaddrinfo = denied
if hasattr(socket.socket, 'sendmsg'):
    socket.socket.sendmsg = denied
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.olx_market.observations import enrich
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot


URL = 'https://www.olx.ua/d/obyavlenie/synthetic-ID123.html'
CLAUSE = 'Продам власну Skoda Octavia A5 рестайлінг.'


def observed(description=CLAUSE, *, title='Skoda Octavia A5', generation='',
             state_description=None, source_id=123, source_url=URL,
             active=True, truncated=False, close_description=True,
             technical='', paint='', second_paint=None):
    state = {'ad': {'ad': {'id': source_id, 'url': source_url,
        'status': 'active' if active else 'removed', 'isActive': active,
        'params': [], 'description': description if state_description is None else state_description}}}
    data = ('<script id="olx-init-config">window.__PRERENDERED_STATE__=' +
        json.dumps(state, ensure_ascii=False) + ';</script>' +
        '<p>Покоління: ' + escape(generation) + '</p>' +
        '<p>Технічний стан: ' + escape(technical) + '</p>' +
        '<p>Лакофарбове покриття: ' + escape(paint) + '</p>' +
        ('<p>Лакофарбове покриття: ' + escape(second_paint) + '</p>' if second_paint is not None else '') +
        '<div data-testid="ad_description"><h3>Опис</h3>' + escape(description) +
        ('</div>' if close_description else '')).encode()
    parsed = {'listing': {'id': '123', 'url': URL, 'source': 'olx',
        'brand': 'Skoda', 'model': 'Octavia', 'title': title},
        'summary': {'download_truncated': truncated}}
    return enrich(data, parsed)['listing']


class DescriptionVariant(unittest.TestCase):
    def test_complete_matching_own_car_clause_supplies_fl_without_year_guess(self):
        car = observed()
        self.assertEqual(car.get('generation_variant'), 'FL')
        evidence = car['research_evidence']['generation_variant']
        self.assertEqual(evidence['basis'], 'explicit_own_car_complete_matching_description')
        self.assertFalse(evidence['independently_verified'])
        self.assertEqual(evidence['description_sha256'],
                         hashlib.sha256(('Опис ' + CLAUSE).encode()).hexdigest())
        self.assertNotIn(CLAUSE, str(car))

    def test_pre_fl_own_car_claim_is_explicit(self):
        self.assertEqual(observed('Продам власну Skoda Octavia A5 дорестайлінг.').get(
            'generation_variant'), 'pre_FL')

    def test_incomplete_mismatched_inactive_or_wrong_identity_holds(self):
        for kwargs in ({'truncated': True}, {'close_description': False},
                       {'state_description': 'Інше авто.'}, {'source_id': 124},
                       {'source_url': URL.replace('123', '124')}, {'active': False}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(observed(**kwargs).get('generation_variant'))

    def test_engine_variant_other_car_negation_and_ambiguous_claim_hold(self):
        for description in (
            'Надійна рестайлінгова версія 1.8 TSI.',
            'Інше авто: Skoda Octavia A5 рестайлінг.',
            'Не продаю власну Skoda Octavia A5 рестайлінг.',
            'Продам власну Skoda Octavia A5 не рестайлінг.',
            'Продам власну Skoda Octavia A5 дорестайлінг або рестайлінг.',
            'Продам власну Skoda Octavia A5. Рестайлінгова версія двигуна.',
            'Продам власну Skoda Octavia A5 з двигуном рестайлінгової версії.',
            'Продам власну Skoda Octavia A5 рестайлінгова версія двигуна 1.8 TSI.',
            'Продам власну Skoda Octavia A5 рестайлінг двигуна.',
            'Продам власну Skoda Octavia A5; порівняння з рестайлінгом іншого авто.',
        ):
            with self.subTest(description=description):
                self.assertIsNone(observed(description).get('generation_variant'))

    def test_contradictory_title_or_parameter_is_not_silently_overwritten(self):
        for kwargs in ({'title': 'Skoda Octavia A5 pre-FL'},
                       {'generation': 'дорестайлінг'},
                       {'title': 'Skoda Octavia A5 FL', 'generation': 'дорестайлінг'}):
            with self.subTest(kwargs=kwargs):
                car = observed(**kwargs)
                self.assertIsNone(car.get('generation_variant'))
                self.assertIn('generation_variant', car['research_field_conflicts'])

    def test_negated_or_ambiguous_own_car_claim_drops_existing_fl(self):
        for description in ('Продам власну Skoda Octavia A5 не рестайлінг.',
                            'Продам власну Skoda Octavia A5 дорестайлінг або рестайлінг.',
                            'Продам власну Skoda Octavia A5 FL. Продам власну Skoda Octavia A5 дорестайлінг.'):
            with self.subTest(description=description):
                car = observed(description, title='Skoda Octavia A5 FL')
                self.assertIsNone(car.get('generation_variant'))
                self.assertIn('generation_variant', car['research_field_conflicts'])

    def test_same_claim_corroborates_title_without_publishing_description(self):
        car = observed(title='Skoda Octavia A5 FL')
        self.assertEqual(car.get('generation_variant'), 'FL')
        self.assertNotIn(CLAUSE, str(car))

    def test_family_and_variant_do_not_come_from_a_year(self):
        car = observed('Авто 2012 року.', title='Skoda Octavia 2012')
        self.assertIsNone(car.get('generation'))
        self.assertIsNone(car.get('generation_variant'))

    def test_saved_real_931146600_description_only_claim(self):
        path = Path(__file__).resolve().parents[2] / 'owner-valued-20261005/source/detail-931146600.html'
        if not path.exists():
            self.skipTest('original local saved-real receipt unavailable; synthetic contracts still run')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         '6f897736f01be7297362ce6d37fc53ceb851afef6c676320d74772c72f5dc3ea')
        parsed = parse_detail_snapshot(data, fetched_at=1791184162, truncated=False)
        car = enrich(data, parsed)['listing']
        self.assertEqual(car['id'], '931146600')
        self.assertEqual(car.get('generation_variant'), 'FL')
        self.assertEqual(car['research_evidence']['generation_variant']['description_sha256'],
                         'd910a0ce930ba3a59cf03f4a17d79d9e9227e212797d70a2fc48b826cc348559')
        self.assertIsNone(car.get('vehicle_key'))
        self.assertEqual(car['checked_at'], 1791184162)
        self.assertNotIn('рестайлінг. Надійне', str(car))

    def test_actual_russian_repair_label_is_not_clean_running(self):
        car = observed(technical='На ходу, технически исправна', paint=(
            'Требуется восстановление (рихтовка, покраска, замена деталей, сварка)'))
        self.assertEqual(car.get('research_condition'), 'running_body_repair')

    def test_unknown_negated_or_conflicting_paint_does_not_mean_clean(self):
        for paint in ('Невідомий стан', 'Не требуется восстановление',
                      'Без потреби відновлення', 'На вигляд добре',
                      'Требуется восстановление, але не потрібен ремонт'):
            with self.subTest(paint=paint):
                car = observed(technical='На ходу, технически исправна', paint=paint)
                self.assertIsNone(car.get('research_condition'))
        car = observed(technical='На ходу, технически исправна',
            paint='Как новое, без видимых следов эксплуатации',
            second_paint='Требуется восстановление (рихтовка, покраска, замена деталей, сварка)')
        self.assertIsNone(car.get('research_condition'))
        self.assertIn('Лакофарбове покриття', car['research_field_conflicts'])

    def test_observed_source_paint_enums_keep_separate_running_cohorts(self):
        for paint in ('Як нове, без видимих ​​слідів експлуатації',
                      'Как новое, без видимых следов эксплуатации',
                      'Незначительные следы эксплуатации (мелкие царапины, сколы)',
                      'Незначні сліди експлуатації (дрібні подряпини, сколи)',
                      'Профессионально отремонтированные следы эксплуатации',
                      'Професійно відремонтовані сліди експлуатації'):
            with self.subTest(paint=paint):
                self.assertEqual(observed(technical='На ходу, технически исправна',
                    paint=paint).get('research_condition'), 'seller_declared_running')
        for paint in ('Потрібно відновлення (рихтування, фарбування, заміна деталей, зварювання)',
                      "Не відремонтовані сліди експлуатації (подряпини, вм'ятини і т.д.)",
                      'Не отремонтированные следы эксплуатации (царапины, вмятины и т.д.)',
                      'Требуется восстановление (рихтовка, покраска, замена деталей, сварка)'):
            with self.subTest(paint=paint):
                self.assertEqual(observed(technical='На ходу, технически исправна',
                    paint=paint).get('research_condition'), 'running_body_repair')

    def test_saved_real_936181092_repair_condition(self):
        path = Path(__file__).resolve().parents[2] / 'evidence/olx-night-0300/response-936181092.html'
        if not path.exists():
            self.skipTest('original local saved-real receipt unavailable; synthetic contracts still run')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         'c0d8ec32e717bc46877c2a4908b1ff8ffe2920e0e91af5a52b1b4535ab9c0346')
        parsed = parse_detail_snapshot(data, fetched_at=1791159410, truncated=False)
        car = enrich(data, parsed)['listing']
        self.assertEqual(car['id'], '936181092')
        self.assertEqual(car.get('research_condition'), 'running_body_repair')
        self.assertEqual(car['checked_at'], 1791159410)
        self.assertTrue(car.get('vehicle_key'))

    def test_no_production_app_import(self):
        self.assertNotIn('backend.app', sys.modules)


if __name__ == '__main__':
    unittest.main(verbosity=2)
