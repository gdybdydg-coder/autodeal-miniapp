"""Offline contracts for truthful, safe OLX customer-card presentation."""
from copy import deepcopy
from decimal import Decimal
import socket
import unittest
from unittest.mock import patch

from experiments.olx_offline.cards import render_card
from experiments.olx_offline.fx import normalize_price, parse_nbu_quote
from experiments.olx_offline.pipeline import canonical, estimate
from experiments.olx_offline.test_pipeline import raw, comps, NOW
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.test_observed_price_provenance import page, AD, NOW as OBSERVED_NOW


URL = 'https://www.olx.ua/d/uk/obyavlenie/volkswagen-golf-IDAB123.html'
PHOTO = 'https://ireland.apollo.olxcdn.com/v1/files/photo/image;s=1000x700'


def listing(**changes):
    defaults = dict(url=URL, photos=[PHOTO])
    defaults.update(changes)
    return canonical(raw('123', **defaults), NOW)


def valued(car=None):
    car = car or listing()
    return car, estimate(car, comps(), NOW)


class CardTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('No network from renderer'))
        self.network.start()

    def tearDown(self):
        self.network.stop()

    def test_saved_market_assessment_and_native_style_without_provider_or_competitor_claim(self):
        car, assessment = valued()
        card = render_card(car, assessment, now=NOW)
        self.assertEqual(card['parse_mode'], 'HTML')
        self.assertIn('🚘 <b>', card['text'])
        self.assertIn('OLX · AutoDeal', card['text'])
        self.assertIn('Орієнтир цін пропозицій: ≈ $9 938', card['text'])
        self.assertIn('Середній діапазон цін аналогів: $9 938–$10 213', card['text'])
        self.assertIn('Вибірка: 12 оголошень', card['text'])
        self.assertIn('Нижній квартиль', card['text'])
        self.assertIn('Дані аналогів: 2026-10-02 · Europe/Kyiv', card['text'])
        self.assertIn('Експериментальна оцінка', card['text'])
        self.assertIn('а не фактичних продажів', card['text'])
        self.assertIn('Міжплатформні дублікати', card['text'])
        self.assertNotIn('оцінка AUTO.RIA', card['text'])
        self.assertNotIn('Bull', card['text'])
        self.assertEqual(card['photo'], PHOTO)
        self.assertEqual(card['button']['url'], URL)
        self.assertIn('/stop', card['text'])

    def test_title_and_known_features_are_html_escaped(self):
        car = listing(title='<b>Golf & "ціна"</b>', fuel='<script>', locality='Київ & область')
        text = render_card(car)['text']
        self.assertIn('&lt;b&gt;Golf &amp; &quot;ціна&quot;&lt;/b&gt;', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('Київ &amp; область', text)
        self.assertNotIn('<script>', text)

    def test_actual_parser_feature_keys_have_ukrainian_customer_labels(self):
        for transmission, label in (('robotized', 'Роботизована'), ('tiptronic', 'Типтронік'),
                                    ('reduction_gear', 'Одноступенева')):
            with self.subTest(transmission=transmission):
                text = render_card(listing(fuel='gas_petrol', transmission=transmission))['text']
                self.assertIn('Пальне: Газ / бензин', text)
                self.assertIn('Коробка: ' + label, text)
                self.assertNotIn('gas_petrol', text)
                self.assertNotIn(transmission, text)

    def test_missing_photo_and_optional_features_preserve_text_and_button(self):
        car = listing(photos=[], fuel=None, transmission=None, mileage_km=None)
        card = render_card(car)
        self.assertIsNone(card['photo'])
        self.assertEqual(card['button']['url'], URL)
        self.assertIn('Ціна: $7 000', card['text'])
        self.assertIn('Вигідність не підтверджена', card['text'])

    def test_uah_original_exact_rate_source_and_day_preserved_without_double_conversion(self):
        quote = parse_nbu_quote([{'cc': 'USD', 'r030': 840, 'units': 1,
                                 'exchangedate': '02.10.2026', 'rate_per_unit': Decimal('42.5'),
                                 'rate': Decimal('42.5')}], requested_date='2026-10-02', fetched_at=NOW)
        car = listing(price='297500', currency='UAH')
        car['usd_price'] = normalize_price(car, quote, NOW)
        original = deepcopy(car)
        text = render_card(car, now=NOW)['text']
        self.assertIn('Ціна: ≈ $7 000', text)
        self.assertIn('Початкова ціна: 297 500 UAH', text)
        self.assertIn('Курс НБУ: 42.5 UAH за 1 USD · 2026-10-02', text)
        self.assertIn('bank.gov.ua/NBU_Exchange/exchange_site?start=20261002&amp;end=20261002', text)
        self.assertEqual(car, original)

    def test_observed_usd_display_is_not_original_seller_currency_proof(self):
        ad = deepcopy(AD)
        ad['price']['displayValue'] = '2 350 $'
        ad['price']['regularPrice']['value'] = 105508
        car = parse_detail_snapshot(page(ad=ad, visible='2 350 $', offer_amount=105508),
                                    fetched_at=OBSERVED_NOW, truncated=False)['listing']
        self.assertFalse(car['observed_asking_display']['original_seller_currency_verified'])
        text = render_card(car, now=OBSERVED_NOW)['text']
        self.assertIn('Показана ціна OLX: $2 350', text)
        self.assertIn('Початкова ціна/валюта продавця не підтверджені', text)
        self.assertIn('Вигідність не підтверджена', text)
        self.assertNotIn('Початкова ціна:', text)
        self.assertNotIn('💰 Ціна:', text)

    def test_observed_uah_display_conversion_retains_explicit_qualified_basis(self):
        quote = parse_nbu_quote([{'cc': 'USD', 'r030': 840, 'units': 1,
                                 'exchangedate': '04.10.2026', 'rate_per_unit': Decimal('42.5'),
                                 'rate': Decimal('42.5')}], requested_date='2026-10-04', fetched_at=OBSERVED_NOW)
        car = parse_detail_snapshot(page(), fetched_at=OBSERVED_NOW,
                                    truncated=False, fx_quote=quote)['listing']
        text = render_card(car, now=OBSERVED_NOW)['text']
        self.assertIn('Перерахунок показаної ціни OLX: ≈ $600', text)
        self.assertIn('На сторінці OLX: 25 500 UAH', text)
        self.assertIn('Початкова ціна/валюта продавця не підтверджені', text)
        self.assertIn('Курс НБУ: 42.5 UAH за 1 USD · 2026-10-04', text)
        self.assertNotIn('Початкова ціна:', text)
        # A later missing or stale quote cannot leave the USD conversion visible.
        expired = render_card(car, now=OBSERVED_NOW + 86400)['text']
        self.assertIn('Ціна в USD потребує перевірки', expired)
        self.assertNotIn('≈ $600', expired)
        self.assertIn('На сторінці OLX: 25 500 UAH', expired)

    def test_wrong_date_and_arithmetic_conflict_never_show_normalized_price(self):
        quote = parse_nbu_quote([{'cc': 'USD', 'r030': 840, 'units': 1,
                                 'exchangedate': '02.10.2026', 'rate_per_unit': Decimal('42'),
                                 'rate': Decimal('42')}], requested_date='2026-10-02', fetched_at=NOW)
        car = listing(price='294000', currency='UAH')
        car['usd_price'] = normalize_price(car, quote, NOW)
        text = render_card(car, now=NOW + 86400)['text']
        self.assertIn('Ціна в USD потребує перевірки', text)
        self.assertNotIn('Ціна: ≈ $', text)
        car['usd_price']['usd_amount'] = '123456'
        self.assertIn('Ціна в USD потребує перевірки', render_card(car, now=NOW)['text'])

    def test_unknown_valuation_and_conflicting_discount_are_not_fake_deals(self):
        car, assessment = valued()
        unknown = dict(status='profitability_unconfirmed', reason='insufficient_comparables')
        text = render_card(car, unknown)['text']
        self.assertIn('Вигідність не підтверджена', text)
        self.assertIn('Недостатньо придатних аналогів', text)
        self.assertNotIn('Нижче орієнтира', text)
        assessment['discount_percent'] = '99'
        text = render_card(car, assessment)['text']
        self.assertIn('Вигідність не підтверджена', text)
        self.assertNotIn('99%', text)

    def test_negative_discount_is_higher_price_not_profitable_claim(self):
        car, assessment = valued(listing(price='11000'))
        text = render_card(car, assessment)['text']
        self.assertIn('Вище орієнтира', text)
        self.assertNotIn('Нижче орієнтира', text)

    def test_only_safe_official_listing_urls_no_tracking_or_credentials(self):
        valid = listing(url=URL + '?tracking=secret#fragment')
        self.assertEqual(render_card(valid)['button']['url'], URL)
        for url in ('https://example.invalid/car/1', 'https://www.olx.ua.example.com/d/uk/obyavlenie/car.html',
                    'https://evil@www.olx.ua/d/uk/obyavlenie/car.html',
                    'https://www.olx.ua:444/d/uk/obyavlenie/car.html',
                    'https://www.olx.ua/profile/user', 'http://www.olx.ua/d/uk/obyavlenie/car.html',
                    'https://www.olx.ua\\@example.com/d/uk/obyavlenie/car.html',
                    'https://www.olx.ua/d/uk/obyavlenie/car.html\n'):
            with self.subTest(url=url):
                malformed = listing()
                malformed['url'] = url
                self.assertIsNone(render_card(malformed)['button'])

    def test_photo_cdn_allowlist_and_text_fallback(self):
        invalid = ['https://olxcdn.com.evil.test/photo.jpg', 'http://ireland.apollo.olxcdn.com/photo.jpg',
                   'https://evil@ireland.apollo.olxcdn.com/photo.jpg',
                   'https://ireland.apollo.olxcdn.com:444/photo.jpg', 'https://example.com/photo.jpg']
        self.assertIsNone(render_card(listing(photos=invalid))['photo'])
        self.assertEqual(render_card(listing(photos=invalid + [PHOTO]))['photo'], PHOTO)

    def test_non_olx_listing_cannot_be_mislabeled(self):
        car = listing()
        car['source'] = 'auto_ria'
        with self.assertRaises(ValueError):
            render_card(car)

    def test_unsafe_or_incomplete_numeric_proofs_do_not_crash_or_show_deal(self):
        car, assessment = valued()
        for value in ('NaN', 'Infinity', '-1', True, 7000.0, '1e100000'):
            with self.subTest(value=value):
                broken = deepcopy(car)
                broken['usd_price']['usd_amount'] = value
                text = render_card(broken, assessment)['text']
                self.assertIn('Ціна в USD потребує перевірки', text)
                self.assertIn('Вигідність не підтверджена', text)

    def test_full_message_retains_qualifications_instead_of_caption_slice(self):
        car, assessment = valued()
        car['title'] = 'Довга назва ' * 100
        text = render_card(car, assessment)['text']
        self.assertIn('Вибірка:', text)
        self.assertIn('Дані аналогів:', text)
        self.assertTrue(text.endswith('/stop — вимкнути сповіщення'))
        self.assertLess(len(text), 4096)


if __name__ == '__main__':
    unittest.main()
