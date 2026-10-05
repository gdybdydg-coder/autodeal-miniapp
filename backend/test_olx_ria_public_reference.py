"""Isolated public-HTML adapter contracts; synthetic cards are not live proof."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError('ria_public_test_network_forbidden')


# Fence precedes all project imports, including collection-time imports.
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
socket.create_connection = denied
socket.getaddrinfo = denied
if hasattr(socket.socket, 'sendmsg'):
    socket.socket.sendmsg = denied
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.olx_market import ria_public_reference as public
from backend.olx_market import ria_reference as api

NOW = 1791227200
SID = '40123456'
URL = 'https://auto.ria.com/auto_skoda_octavia_' + SID + '.html'
VIN = 'TMBAB1234A1234567'
DESCRIPTION = 'Продаю цілим, на ходу. Не на розбір. Українська реєстрація.'
PROVENANCE = {'role': 'reference', 'frozen_commit': 'b' * 40, 'frozen_at': NOW - 20,
    'search_params': {'category_id': 1, 'marka_id[0]': 70, 'model_id[0]': 652}}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def fixture(*, generation='II поколение/A5 (FL), 1.6D MT (105 к.с.)',
            description=DESCRIPTION, currency='USD', schema_price=8000,
            visible_price='8 000 $', engine='Дизель, 1.6 л', drive='Передний',
            canonical=URL, visible_vin=VIN, active=True, unknown_power=False,
            visible_description=None, technical='Полностью неповрежденное', paint=None):
    if unknown_power:
        generation = 'II поколение/A5 (FL)'
    vehicle = {'@type': 'Vehicle', '@id': URL, 'url': URL, 'mainEntityOfPage': URL,
        'name': 'Skoda Octavia 2011', 'brand': {'name': 'Skoda'}, 'model': 'Octavia',
        'productionDate': 2011, 'bodyType': 'Легковые', 'description': description,
        'vehicleIdentificationNumber': VIN, 'fuelType': engine.partition(',')[0],
        'vehicleEngine': {'fuelType': engine.partition(',')[0]},
        'vehicleTransmission': 'Ручная / Механика',
        'mileageFromOdometer': {'unitCode': 'KMT', 'value': 250000},
        'offers': {'price': schema_price, 'priceCurrency': currency,
                   'availability': 'https://schema.org/InStock'}}
    labels = {'basicInfoTitle': 'Skoda Octavia 2011', 'basicInfoPrice': visible_price,
        'descCharacteristicsValue': 'Универсал • 5 дверей • 5 мест',
        'descGenerationBaseValue': generation, 'descEngineEngine': engine,
        'descTransmissionTransmission': 'Ручная / Механика',
        'descDriveTypeDriveType': drive, 'basicInfoTableMainInfo0': '250 тыс. км',
        'descTechStateText': technical}
    if paint is not None:
        labels['descPaintConditionValue'] = paint
    templates = [{'id': ident, 'isHide': False,
                  'elements': [{'type': 'Text', 'content': text}]}
                 for ident, text in labels.items()]
    templates.append({'id': 'descDescription', 'isHide': False,
        'component': {'expandableText': {'description': {'content': description}}}})
    page = {'status': 200, 'templates': templates, 'ldJSON': vehicle,
        'additionalParams': {'autoId': int(SID), 'isActive': active, 'link': URL,
            'title': 'Skoda Octavia 2011', 'prices': {'USD': '8 000'}}}
    state = {'page': {'structures': {'/auto_skoda_octavia_' + SID + '.html/': page}}}
    raw = '<html><head><link rel="canonical" href="' + canonical + '"></head><body>'
    raw += '<script>window.__PINIA__ = ' + json.dumps(state, ensure_ascii=False) + ';</script>'
    raw += '<script type="application/ld+json">' + json.dumps(vehicle, ensure_ascii=False) + '</script>'
    raw += ''.join('<div id="' + ident + '">' + html.escape(text) + '</div>' for ident, text in labels.items())
    raw += '<div id="descDescription"><span class="common-text ws-pre-wrap">' + html.escape(
        description if visible_description is None else visible_description) + '</span></div>'
    raw += '<span id="badgesVin">' + html.escape(visible_vin) + '</span></body></html>'
    data = raw.encode()
    receipt = {'source': 'auto_ria_public_html', 'id': SID, 'url': URL,
        'reserved_commit': 'c' * 40, 'started_at': iso(NOW - 10), 'completed_at': iso(NOW),
        'http_status': 200, 'curl_exit': 0, 'body_bytes': len(data),
        'body_sha256': hashlib.sha256(data).hexdigest(), 'captcha_indicator': False,
        'source_hold': None, 'get_calls': 1, 'paid_api_calls': 0, 'redirects_followed': 0}
    return data, receipt


def project(**kwargs):
    data, receipt = fixture(**kwargs)
    return public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)


def rewrite(data, receipt, before, after):
    changed = data.replace(before.encode(), after.encode())
    receipt = deepcopy(receipt)
    receipt.update(body_bytes=len(changed), body_sha256=hashlib.sha256(changed).hexdigest())
    return changed, receipt


def catalog_fixture():
    rows = [{'id': 3133, 'name': 'II поколение/A5', 'nameMobile': 'II поколение/A5',
        'slangWord': 'II поколение/A5', 'link': '/car/skoda/octavia/a5/'},
        {'id': 3607, 'name': 'II поколение/A5 (FL)', 'nameMobile': 'II поколение/A5 (FL)',
         'slangWord': 'II поколение/A5 (FL)', 'link': '/car/skoda/octavia/a5-fl/'}]
    state = {'catalog': {'catalog': {'rightRelinkPanel': {'generationsNew': {'items': rows}}}}}
    data = ('<html><link rel="canonical" href="https://auto.ria.com/car/skoda/octavia/">'
        '<script>window.__PINIA__ = ' + json.dumps(state) + ';</script>' + ''.join(
        '<a href="' + row['link'] + '">' + row['name'] + '</a>' for row in rows) + '</html>').encode()
    receipt = fixture()[1]
    receipt.pop('id')
    receipt.update(url='https://auto.ria.com/car/skoda/octavia/', body_bytes=len(data),
                   body_sha256=hashlib.sha256(data).hexdigest())
    return data, receipt


class PublicReferenceContracts(unittest.TestCase):
    def test_public_receipt_explicit_fields_and_no_api_impersonation(self):
        car = project()
        self.assertEqual(public.reasons(car, NOW), [])
        self.assertEqual((car['generation_variant'], car['power_hp'], car['engine_cc']), ('FL', 105, 1600))
        self.assertEqual(car['currency'], 'USD')
        self.assertTrue(car['vehicle_identity_verified'])
        self.assertFalse(car['identity_review']['distinct_photos_reviewed'])
        self.assertIn('ria_full_info_receipt_unverified', api.reasons(car))
        self.assertNotIn('ria_reference_evidence', car)

    def test_sanitization_no_vin_contact_or_complete_description(self):
        description = DESCRIPTION + ' Телефон +380 67 123 45 67.'
        car = project(description=description)
        serialized = json.dumps(car)
        for private in (VIN, description, '+380 67 123 45 67'):
            self.assertNotIn(private, serialized)
        self.assertEqual(car['vehicle_key'], 'vin-sha256:' + hashlib.sha256(VIN.encode()).hexdigest())

    def test_a5_and_year_do_not_imply_variant(self):
        car = project(generation='II поколение/A5, 1.6D MT (105 к.с.)')
        self.assertIsNone(car['generation_variant'])
        self.assertIn('ria_public_generation_variant_pending', public.reasons(car, NOW))

    def test_unknown_power_not_filled_from_16d(self):
        car = project(unknown_power=True)
        self.assertIsNone(car['power_hp'])
        self.assertIn('ria_public_power_hp_pending', public.reasons(car, NOW))

    def test_unknown_drive_stays_pending(self):
        car = project(drive='Не указано')
        self.assertIsNone(car['drive_type'])
        self.assertIn('ria_public_drive_type_pending', public.reasons(car, NOW))

    def test_three_part_visible_engine_label_preserves_explicit_fuel_and_volume(self):
        car = project(engine='Дизель, 1.6 л, (105 к.с. / 77 кВт)')
        self.assertEqual((car['fuel'], car['engine_cc'], car['power_hp']), ('diesel', 1600, 105))
        self.assertEqual(public.reasons(car, NOW), [])

    def test_fuel_only_label_preserves_fuel_but_does_not_invent_volume(self):
        car = project(engine='Дизель')
        self.assertEqual(car['fuel'], 'diesel')
        self.assertIsNone(car['engine_cc'])
        self.assertIn('ria_public_engine_cc_pending', public.reasons(car, NOW))

    def test_declared_fractional_power_must_round_to_explicit_catalog_integer(self):
        car = project(engine='Дизель, 1.6 л, (104.72 к.с. / 77 кВт)')
        self.assertEqual(car['power_hp'], 105)
        self.assertEqual(car['source_attribute_evidence']['visible_power_hp'], '104.72')
        self.assertEqual(car['source_attribute_evidence']['power_consistency_policy'],
                         'round_explicit_hp_to_source_catalog_integer-v1')
        with self.assertRaisesRegex(ValueError, 'power_attribute_conflict'):
            project(engine='Дизель, 1.6 л, (110 к.с. / 81 кВт)')

    def test_explicit_engine_power_can_replace_missing_catalog_power(self):
        car = project(unknown_power=True, engine='Дизель, 1.6 л, (105 к.с. / 77 кВт)')
        self.assertEqual(car['power_hp'], 105)
        fractional = project(unknown_power=True, engine='Дизель, 1.6 л, (104.72 к.с. / 77 кВт)')
        self.assertIsNone(fractional['power_hp'])

    def test_raw_receipt_binding_enforced(self):
        data, receipt = fixture()
        with self.assertRaisesRegex(ValueError, 'transport_receipt'):
            public.from_public_html(data + b' ', SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_source_status_paid_calls_captcha_redirect_and_source_hold(self):
        data, receipt = fixture()
        for field, value in (('http_status', 403), ('paid_api_calls', 1),
                ('captcha_indicator', True), ('source_hold', 'captcha'), ('redirects_followed', 1)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'transport_receipt'):
                public.from_public_html(data, SID, NOW, PROVENANCE,
                                        {**receipt, field: value}, now=NOW)

    def test_stale_and_future_observations_rejected(self):
        data, receipt = fixture()
        for now in (NOW - 1, NOW + public.MAX_AGE + 1):
            with self.subTest(now=now), self.assertRaisesRegex(ValueError, 'stale_or_future'):
                public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=now)

    def test_freeze_is_before_fetch_not_after_price_reading(self):
        data, receipt = fixture()
        with self.assertRaisesRegex(ValueError, 'split_not_frozen'):
            public.from_public_html(data, SID, NOW, {**PROVENANCE, 'frozen_at': NOW - 1}, receipt, now=NOW)

    def test_reference_collection_not_budget_or_region_capped(self):
        data, receipt = fixture()
        for key in ('price_ot', 'price_do', 'state[0]', 'city', 'region'):
            p = deepcopy(PROVENANCE)
            p['search_params'][key] = 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'capped'):
                public.from_public_html(data, SID, NOW, p, receipt, now=NOW)

    def test_canonical_id_mismatch_and_inactive_listing(self):
        for kwargs, code in (({'canonical': URL.replace(SID, '40123457')}, 'canonical'),
                             ({'active': False}, 'active_state')):
            data, receipt = fixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, code):
                public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_usd_only_and_price_conflict(self):
        for kwargs in ({'currency': 'UAH'}, {'schema_price': 8001}, {'visible_price': '1 000 $'}):
            data, receipt = fixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'price_or_active'):
                public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_full_visible_description_cannot_be_missing_or_shortened(self):
        data, receipt = fixture(visible_description='Продаю.')
        with self.assertRaisesRegex(ValueError, 'full_description'):
            public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_dismantling_and_uncleared_rejected_but_negation_allowed(self):
        for description in ('Продаю авто під розбір.', 'Авто не розмитнений.'):
            with self.subTest(description=description), self.assertRaisesRegex(ValueError, 'not_eligible'):
                project(description=description)
        self.assertEqual(public.reasons(project(description=DESCRIPTION), NOW), [])

    def test_initial_payment_not_full_asking(self):
        with self.assertRaisesRegex(ValueError, 'initial_payment'):
            project(description='Продаю цілим. Ціна — перший внесок, решта у кредит.')

    def test_damage_condition_preserved_not_forbidden(self):
        car = project(description=DESCRIPTION + ' На лобовому склі тріщина.')
        self.assertEqual(car['research_condition'], 'running_reported_damage:windshield_crack')
        self.assertEqual(car['eligibility_review']['status'], 'allowed')

    def test_description_nonrunning_claim_cannot_enter_running_cohort(self):
        car = project(description=DESCRIPTION + ' Авто не на ходу.')
        self.assertEqual(car['research_condition'], 'not_running')

    def test_unclassified_engine_or_body_repair_holds_market_but_not_whole_car(self):
        for description in ('Потрібен ремонт двигуна.', 'Требует кузовного ремонта.'):
            with self.subTest(description=description):
                car = project(description=DESCRIPTION + ' ' + description)
                self.assertIsNone(car['research_condition'])
                self.assertIn('ria_public_research_condition_pending', public.reasons(car, NOW))
                self.assertEqual(car['eligibility_review']['status'], 'allowed')
        car = project(description=DESCRIPTION + ' Не потребує ремонту двигуна.')
        self.assertEqual(car['research_condition'], 'seller_declared_running')

    def test_repair_claims_in_both_word_orders_cannot_enter_running_cohort(self):
        for description in ('Двигун потребує ремонту.', 'Кузов потребує ремонту.',
                            'Двигатель требует ремонта.', 'Кузов требует ремонта.',
                            'Потрібен ремонт двигуна.', 'Потребує ремонту кузова.',
                            'Двигун потребує капітального ремонту.'):
            with self.subTest(description=description):
                car = project(description=DESCRIPTION + ' ' + description)
                self.assertIsNone(car['research_condition'])
                self.assertIn('ria_public_research_condition_pending', public.reasons(car, NOW))

    def test_negated_and_completed_historical_engine_body_repairs_are_not_current(self):
        for description in ('Двигун не потребує ремонту.', 'Кузов не потребує ремонту.',
                            'Двигатель не требует ремонта.', 'Не потребує ремонту двигуна.',
                            'Двигун відремонтовано.', 'Кузов відремонтовано.',
                            'Двигун потребував ремонту, але його відремонтовано.',
                            'Двигатель требовал ремонта, но отремонтирован.'):
            with self.subTest(description=description):
                car = project(description=DESCRIPTION + ' ' + description)
                self.assertEqual(car['research_condition'], 'seller_declared_running')
                self.assertEqual(car['condition_evidence']['unsupported_condition_flags'], [])

    def test_complete_own_car_good_condition_claim_when_attribute_is_missing(self):
        for claim in (
            'В технически и визуально хорошем состоянии, пригнан из Европы.',
            'Предлагаю свою Шкоду в отличном состоянии, которая полностью обслужена и не требует вложений.',
        ):
            with self.subTest(claim=claim):
                car = project(description=claim, technical='')
                self.assertEqual(car['research_condition'], 'seller_declared_running')
                self.assertEqual(car['condition_evidence']['basis'],
                                 'corroborated_complete_public_own_description_claim')
                self.assertEqual(car['condition_evidence']['own_claim_sha256'], hashlib.sha256(claim.encode()).hexdigest())
                self.assertFalse(car['condition_evidence']['independently_verified'])
                self.assertNotIn(claim, json.dumps(car))

    def test_negated_historical_and_other_car_condition_claims_are_not_own_current_claims(self):
        for claim in (
            'Авто не в технически и визуально хорошем состоянии.',
            'Раніше авто в технически и визуально хорошем состоянии.',
            'У друга авто в технически и визуально хорошем состоянии.',
            'Другое авто в технически и визуально хорошем состоянии.',
            'Предлагаю чужую Шкоду в отличном состоянии, которая полностью обслужена и не требует вложений.',
            'Предлагаю свою Шкоду не в отличном состоянии, которая полностью обслужена и не требует вложений.',
            'Предлагаю свою Шкоду в отличном состоянии, которая не полностью обслужена и требует вложений.',
            'Двигатель работает ровно, без посторонних звуков и дыма.',
        ):
            with self.subTest(claim=claim):
                car = project(description=claim, technical='')
                self.assertIsNone(car['research_condition'])
                self.assertIn('ria_public_research_condition_pending', public.reasons(car, NOW))

    def test_postfix_historical_and_other_car_claims_are_not_current_own_condition(self):
        for claim in (
            'В технически и визуально хорошем состоянии была предыдущая машина.',
            'В технически и визуально хорошем состоянии было до аварии.',
            'В технически и визуально хорошем состоянии, была предыдущая машина.',
            'В технически и визуально хорошем состоянии, пригнан из Европы, но это была предыдущая машина.',
            'В технически и визуально хорошем состоянии, пригнан из Европы, таким было до аварии.',
            'Предлагаю свою Шкоду в отличном состоянии, которая полностью обслужена и не требует вложений была раньше.',
        ):
            with self.subTest(claim=claim):
                car = project(description=claim, technical='')
                self.assertIsNone(car['research_condition'])
                self.assertIn('ria_public_research_condition_pending', public.reasons(car, NOW))

    def test_own_good_condition_claim_does_not_override_body_or_engine_conflicts(self):
        claim = 'В технически и визуально хорошем состоянии.'
        for conflict in ('Но есть моменты по кузову.', 'Є нюанси по кузову.',
                         'Двигун потребує ремонту.', 'Кузов потребує ремонту.'):
            with self.subTest(conflict=conflict):
                car = project(description=claim + ' ' + conflict, technical='')
                self.assertIsNone(car['research_condition'])

    def test_nonempty_unknown_paint_and_repaired_technical_label_are_not_clean(self):
        claim = 'В технически и визуально хорошем состоянии.'
        for kwargs in ({'technical': '', 'paint': 'Неисправленные следы использования'},
                       {'technical': '', 'paint': 'Неизвестная окраска'},
                       {'technical': 'Профессионально отремонтированные повреждения'}):
            with self.subTest(kwargs=kwargs):
                car = project(description=claim, **kwargs)
                self.assertIsNone(car['research_condition'])

    def test_source_description_damage_stays_separate_from_clean_claim(self):
        claim = 'В технически и визуально хорошем состоянии. На лобовому склі тріщина.'
        car = project(description=claim, technical='')
        self.assertEqual(car['research_condition'], 'running_reported_damage:windshield_crack')

    def test_visible_attribute_conflict_not_trusted(self):
        data, receipt = fixture()
        data, receipt = rewrite(data, receipt, '<div id="descDriveTypeDriveType">Передний</div>',
                                '<div id="descDriveTypeDriveType">Полный</div>')
        with self.assertRaisesRegex(ValueError, 'visible_state_attribute_conflict'):
            public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_hidden_or_duplicate_visible_price_rejected(self):
        for change in ('<div id="basicInfoPrice" hidden>',
                       '<div id="basicInfoPrice">8 000 $</div><div id="basicInfoPrice">'):
            data, receipt = fixture()
            data, receipt = rewrite(data, receipt, '<div id="basicInfoPrice">', change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'basicInfoPrice_unverified'):
                public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_masked_or_missing_vin_does_not_prove_independence(self):
        data, receipt = fixture(visible_vin='')
        car = public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)
        self.assertIsNone(car['vehicle_key'])
        self.assertFalse(car['vehicle_identity_verified'])

    def test_public_projection_receipt_covers_normalized_fields(self):
        car = project()
        for field, value in (('power_hp', 102), ('research_condition', 'seller_declared_running_x'),
                             ('vehicle_key', 'vin-sha256:' + 'a' * 64)):
            with self.subTest(field=field):
                c = deepcopy(car)
                c[field] = value
                self.assertIn('ria_public_receipt_unverified', public.reasons(c, NOW))

    def test_exact_public_catalog_id_corroborates_base_a5(self):
        data, receipt = catalog_fixture()
        dictionary = public.generation_dictionary_from_public_html(data, receipt, now=NOW)
        raw, r = fixture(generation='II поколение/A5, 1.6D MT (105 к.с.)')
        car = public.from_public_html(raw, SID, NOW, PROVENANCE, r, now=NOW,
                                      generation_dictionary=dictionary)
        self.assertEqual(car['generation_variant'], 'pre_FL')
        self.assertEqual(car['source_attribute_evidence']['generation_id'], 3133)
        self.assertEqual(public.reasons(car, NOW), [])

    def test_catalog_hidden_conflict_or_missing_id_not_dictionary_proof(self):
        for before, after in (('href="/car/skoda/octavia/a5/"', 'hidden href="/car/skoda/octavia/a5/"'),
                              ('id": 3133', 'id": 9999'),
                              ('>II поколение/A5</a>', '>III поколение/A7</a>')):
            data, receipt = catalog_fixture()
            data, receipt = rewrite(data, receipt, before, after)
            with self.subTest(before=before), self.assertRaisesRegex(ValueError, 'dictionary_uncorroborated'):
                public.generation_dictionary_from_public_html(data, receipt, now=NOW)

    def test_forged_or_stale_catalog_dictionary_not_used(self):
        data, receipt = catalog_fixture()
        dictionary = public.generation_dictionary_from_public_html(data, receipt, now=NOW)
        raw, r = fixture()
        for field, value in (('checked_at', NOW - public.MAX_AGE - 1), ('binding', 'a' * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'dictionary_unverified'):
                public.from_public_html(raw, SID, NOW, PROVENANCE, r, now=NOW,
                    generation_dictionary={**dictionary, field: value})

    def test_catalog_receipt_captcha_field_conflict_is_not_ignored(self):
        data, receipt = catalog_fixture()
        receipt['captcha_challenge_indicator'] = True
        with self.assertRaisesRegex(ValueError, 'catalog_receipt'):
            public.generation_dictionary_from_public_html(data, receipt, now=NOW)

    def test_malformed_source_type_is_sanitized_failure(self):
        data, receipt = fixture()
        data, receipt = rewrite(data, receipt, '"offers": {"price": 8000,',
                                '"offers": {"price": null,')
        with self.assertRaises(ValueError):
            public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)
        with self.assertRaisesRegex(ValueError, 'transport_receipt'):
            public.from_public_html(data, SID, NOW, PROVENANCE,
                                    {**receipt, 'reserved_commit': None}, now=NOW)

    def test_saved_real_public_card_and_catalog_receipts_are_distinct(self):
        path = Path(__file__).resolve().parents[1] / 'experiments/olx_research/owner-launch-20261005/ria-public-sanitized.json'
        if not path.exists():
            self.skipTest('saved real fixture is not bundled in this checkout')
        fixture = json.loads(path.read_text())
        self.assertEqual(fixture['dataset_kind'], 'saved_real')
        car = fixture['listing']
        self.assertEqual(car['id'], '40106586')
        self.assertEqual((car['power_hp'], car['engine_cc'], car['fuel_subtype']), (115, 1600, 'propane'))
        self.assertTrue(car['vehicle_identity_verified'])
        self.assertEqual(car['source_attribute_evidence']['generation_id'], 3133)
        self.assertEqual(public.reasons(car, fixture['evaluated_at']),
                         ['ria_public_reference_split_unverified'])
        self.assertEqual(car['reference_provenance']['role'], 'source_feasibility')
        self.assertIn('ria_full_info_receipt_unverified', api.reasons(car))
        self.assertFalse(car['identity_review']['distinct_photos_reviewed'])

    def test_no_production_app_or_transport(self):
        project()
        self.assertNotIn('backend.app', sys.modules)
        self.assertNotIn('backend.ria_search', sys.modules)

    def test_malformed_nested_admission_evidence_fails_closed(self):
        for field, value, expected in (
            ('ria_public_reference_evidence', [], 'ria_public_receipt_unverified'),
            ('reference_provenance', [], 'ria_public_reference_split_unverified'),
            ('eligibility_review', [], 'ria_public_asking_or_eligibility_pending'),
            ('vehicle_key', [], 'ria_public_vehicle_identity_pending'),
        ):
            with self.subTest(field=field):
                car = project()
                car[field] = value
                self.assertIn(expected, public.reasons(car, NOW))
        self.assertEqual(public.reasons(None, NOW), ['ria_public_listing_invalid'])

    def test_rebinding_cannot_make_missing_frozen_role_or_bad_count_admissible(self):
        for field, value in (('role', None), ('frozen_commit', None), ('frozen_at', NOW + 1)):
            with self.subTest(field=field):
                car = project()
                car['reference_provenance'][field] = value
                car['ria_public_reference_evidence']['binding'] = public.binding(car)
                self.assertIn('ria_public_reference_split_unverified', public.reasons(car, NOW))
        car = project()
        car['ria_public_reference_evidence']['paid_api_calls'] = 1
        car['ria_public_reference_evidence']['binding'] = public.binding(car)
        self.assertIn('ria_public_receipt_unverified', public.reasons(car, NOW))

    def test_public_metadata_receipt_tampering_is_bound(self):
        car = project()
        car['ria_public_reference_evidence']['body_sha256'] = 'a' * 64
        self.assertIn('ria_public_receipt_unverified', public.reasons(car, NOW))

    def test_two_vehicle_schemas_or_same_id_conflicting_price_fail_closed(self):
        data, receipt = fixture()
        end = data.index(b'</script>', data.index(b'type="application/ld+json"')) + len(b'</script>')
        start = data.index(b'<script type="application/ld+json">')
        data, receipt = rewrite(data, receipt, '</body>', data[start:end].decode() + '</body>')
        with self.assertRaisesRegex(ValueError, 'schema_ambiguous_or_conflicting'):
            public.from_public_html(data, SID, NOW, PROVENANCE, receipt, now=NOW)

    def test_source_feasibility_observation_never_becomes_reference_admission(self):
        data, receipt = fixture()
        car = public.from_public_html(data, SID, NOW,
            {**PROVENANCE, 'role': 'source_feasibility'}, receipt, now=NOW)
        self.assertEqual(public.reasons(car, NOW), ['ria_public_reference_split_unverified'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
