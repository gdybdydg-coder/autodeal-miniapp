import json
import unittest

from experiments.olx_offline.eligibility import fingerprint, review_listing


def listing(description='Розмитнений, українська реєстрація. Продаю цілим.', **changes):
    value = dict(title='Volkswagen Golf', category='whole_passenger_car',
                 description=description, description_available=True)
    value.update(changes)
    return value


class EligibilityTests(unittest.TestCase):
    def test_whole_vehicle_already_dismantling(self):
        for text in ('Автомобіль у розборі, продаю двигун та кузов окремо.',
                     'Автомобиль в разборе, детали отдельно.'):
            with self.subTest(text=text):
                self.assertEqual(review_listing(listing(text))['status'], 'excluded')
        for text in ('Розмитнений. Автомобіль не у розборі, продаю цілим.',
                     'Растаможен. Автомобиль не в разборе.',
                     'Купив двигун у розборі, встановив, авто продаю цілим.'):
            with self.subTest(text=text):
                self.assertEqual(review_listing(listing(text))['status'], 'allowed')

    def test_required_uncleared_examples(self):
        for text in ('Нерозмитнений, ціна без мита', 'Не розмитнена машина',
                     'Не-розмитнений', 'нерастаможен', 'НЕ РАСТАМОЖЕНА',
                     'Без растаможки', 'Ціна без розмитнення',
                     'Автомобіль ще за кордоном, ціна не включає розмитнення',
                     'Цена не включает растаможку', 'Розмитнення не включено',
                     'Потребує розмитнення'):
            with self.subTest(text=text):
                result = review_listing(listing(text))
                self.assertEqual(result['status'], 'excluded')
                self.assertIn('uncleared_text', result['reasons'])

    def test_required_dismantling_examples(self):
        for text in ('Продається під розбір / по запчастинах', 'На розбір',
                     'Під-розбір', 'ПОД РАЗБОР', 'На разборку', 'На запчастини',
                     'По запчастям', 'Донор', 'Для донора', 'Продаж частинами',
                     'Продается частями', 'Цілий автомобіль на запчастини'):
            with self.subTest(text=text):
                self.assertEqual(review_listing(listing(text))['status'], 'excluded')

    def test_clearance_and_negation_pass(self):
        for text in ('Розмитнений, українська реєстрація, продаю цілим, не на розбір',
                     'Растаможен. Не донор. Не продаю по запчастям',
                     'Розмитнена, не віддаю на запчастини',
                     'Растаможена, на разбор не продается',
                     'Розмитнений. Донор: ні',
                     'Розмитнений, не на запчастини і не донор'):
            with self.subTest(text=text):
                self.assertEqual(review_listing(listing(text))['status'], 'allowed')

    def test_broken_whole_car_is_not_dismantling(self):
        result = review_listing(listing('Не на ходу, потрібен ремонт, продається цілим', customs_status='cleared'))
        self.assertEqual(result['status'], 'allowed')

    def test_nonrunning_without_customs_is_unknown_not_prohibited(self):
        result = review_listing(listing('Не на ходу, потрібен ремонт, продається цілим'))
        self.assertEqual(result['status'], 'allowed')
        self.assertIn('customs_unknown', result['reasons'])
        self.assertNotIn('dismantling_text', result['reasons'])

    def test_origin_does_not_imply_uncleared(self):
        self.assertEqual(review_listing(listing('Пригнаний з Німеччини. Розмитнений.'))['status'], 'allowed')
        result = review_listing(listing('Автомобіль з Німеччини. Продається цілим.'))
        self.assertEqual(result['customs_status'], 'unknown')
        self.assertNotIn('uncleared_text', result['reasons'])

    def test_contradictions_are_held(self):
        for raw in (listing('Нерозмитнений', customs_status='cleared'),
                    listing('Продаю під розбір', title='Не на розбір'),
                    listing('Розмитнений', customs_cleared=False)):
            self.assertEqual(review_listing(raw)['status'], 'needs_review')

    def test_explicit_structured_fields(self):
        for changes in ({'customs_status': 'uncleared'}, {'customs_cleared': False},
                        {'category': 'parts'}, {'sale_mode': 'donor'},
                        {'sale_mode': 'dismantling'}, {'category': 'rental'}):
            self.assertEqual(review_listing(listing('Продаю автомобіль.', **changes))['status'], 'excluded')

    def test_missing_or_partial_description_held(self):
        for changes in ({'description': None}, {'description': ''},
                        {'description_available': False}, {'description_available': 'true'}):
            result = review_listing(listing(**changes))
            self.assertEqual(result['status'], 'needs_review')
            self.assertIn('description_unavailable', result['reasons'])

    def test_relevant_full_description_changes_fingerprint(self):
        first = listing('Розмитнений. ' + 'опис ' * 2000)
        changed = dict(first, description=first['description'] + ' Продам на розбір')
        self.assertNotEqual(fingerprint(first), fingerprint(changed))
        self.assertEqual(review_listing(first)['status'], 'allowed')
        self.assertEqual(review_listing(changed)['status'], 'excluded')
        self.assertEqual(fingerprint(first), fingerprint(dict(first, checked_at=123)))

    def test_description_not_retained_and_evidence_redacted(self):
        text = 'Розмитнений. Телефон +380 99 123 45 67, person@example.test, https://example.test/private. VIN WVWZZZ1KZAW123456. Продаю цілим.'
        serialized = json.dumps(review_listing(listing(text)), ensure_ascii=False)
        for secret in (text, '+380', '123 45 67', 'person@example', 'example.test', 'WVWZZZ1KZAW123456'):
            self.assertNotIn(secret, serialized)
        for evidence in review_listing(listing(text))['evidence']:
            self.assertTrue(evidence['reason']); self.assertTrue(evidence['field'])
            self.assertLessEqual(len(evidence['snippet']), 96)

    def test_negation_does_not_leak_across_clauses(self):
        for text in ('Не працює, на розбір', 'Не їде; донор',
                     'Не тільки на запчастини', 'Не на ходу, продаю частинами'):
            self.assertEqual(review_listing(listing(text))['status'], 'excluded')

    def test_uncertain_negative_clearance_is_not_allowed(self):
        for text in ('Не нерозмитнений', 'Без української реєстрації', 'Розмитнений: ні'):
            self.assertEqual(review_listing(listing(text))['status'], 'needs_review')

    def test_parts_title_held_but_repair_description_passes(self):
        self.assertEqual(review_listing(listing(title='Двигун Volkswagen Golf'))['status'], 'needs_review')
        self.assertEqual(review_listing(listing('Розмитнений. Замінено двигун та капот, продаю автомобіль.'))['status'], 'allowed')

    def test_unrecognized_or_malformed_evidence_held(self):
        for changes in ({'category': None}, {'customs_status': []}, {'description': {}},
                        {'description': 'x' * 131073}, {'customs_cleared': 'false'}):
            self.assertEqual(review_listing(listing(**changes))['status'], 'needs_review')

    def test_clearance_claim_never_marked_independently_verified(self):
        result = review_listing(listing())
        self.assertEqual(result['customs_status'], 'cleared_declared')
        self.assertFalse(result['customs_independently_verified'])

    def test_refusal_to_sell_for_parts_is_not_forbidden(self):
        for text in ('Не пропонувати на розбір. Розмитнений',
                     'Донора не пропонувати', 'Не предлагать на разбор',
                     'Не является донором, растаможен'):
            self.assertEqual(review_listing(listing(text))['status'], 'allowed')

    def test_customs_not_included_clause_variants(self):
        for text in ('Розмитнення не входить у вартість',
                     'Ціна не включає в себе розмитнення',
                     'Цена не включает в себя растаможку'):
            self.assertEqual(review_listing(listing(text))['status'], 'excluded')

    def test_component_price_excluded_and_component_title_held(self):
        for text in ('Продаю Volkswagen Golf. Ціна за деталь',
                     'Ціна лише за двигун', 'Цена только за капот'):
            self.assertEqual(review_listing(listing(text))['status'], 'excluded')
        for title in ('Запчастини для Volkswagen Golf', 'Разборка Volkswagen Golf'):
            self.assertEqual(review_listing(listing(title=title))['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
