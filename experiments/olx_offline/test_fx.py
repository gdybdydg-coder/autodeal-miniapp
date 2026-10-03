"""Synthetic arithmetic/clock cases; rates below are NOT current real NBU quotes."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile
import unittest

from experiments.olx_offline.fx import (
    FXCache, FXQuote, amount, display_price, nbu_url, normalize_price, parse_nbu_quote,
)


class FXTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)  # Saturday Kyiv.
        self.day = date(2026, 10, 3)
        self.quote = FXQuote(Decimal('42'), self.day, self.now - timedelta(minutes=1), nbu_url(self.day), date(2026, 10, 2))

    def normalized(self, value='210 000', currency='UAH', quote=None, **kwargs):
        return normalize_price({'price': value, 'currency': currency}, self.quote if quote is None else quote, self.now, **kwargs)

    def test_direction_and_provenance(self):
        result = self.normalized()
        self.assertEqual(result['usd_amount'], '5000')
        self.assertEqual(result['original_amount'], '210000')
        self.assertEqual(result['original_currency'], 'UAH')
        self.assertEqual(result['fx']['direction'], 'UAH per 1 USD')
        self.assertEqual(result['fx']['effective_date'], '2026-10-03')
        self.assertEqual(display_price(result), '≈ 5 000 $')

    def test_usd_does_not_require_rate_or_convert(self):
        result = normalize_price({'price': '5000.25', 'currency': 'USD'}, None, self.now)
        self.assertEqual(result['usd_amount'], '5000.25')
        self.assertIsNone(result['fx'])
        self.assertEqual(display_price(result), '5 000 $')

    def test_no_double_conversion(self):
        first = self.normalized()
        second = normalize_price(first, self.quote, self.now)
        self.assertEqual(first, second)
        # A cache refresh must rederive from the retained seller price.
        refreshed = FXQuote(Decimal('40'), self.day, self.now, nbu_url(self.day))
        self.assertEqual(normalize_price(first, refreshed, self.now)['usd_amount'], '5250')

    def test_canonical_fraction_repeated_without_reinterpretation(self):
        first = self.normalized(value=Decimal('123.456'))
        self.assertEqual(normalize_price(first, self.quote, self.now), first)

    def test_separator_forms(self):
        for value in ('210 000,00', '210\u00a0000.00', '210\u202f000', '210,000.00', '210.000,00', '210000'):
            with self.subTest(value=value):
                self.assertEqual(Decimal(self.normalized(value)['usd_amount']), Decimal('5000'))
        self.assertEqual(amount('1,200,000'), Decimal('1200000'))

    def test_invalid_zero_negative_ambiguous_and_float(self):
        for value in ('0', '-1', 'NaN', 'Infinity', '', None, True, 210000.0, '210,000', '1.234', '21 00', '12,34,56', '1e5', '$5000'):
            with self.subTest(value=value):
                result = self.normalized(value)
                self.assertEqual(result['status'], 'pending')
                self.assertIsNone(result['usd_amount'])

    def test_unknown_and_eur_not_assumed_uah(self):
        for currency, reason in ((None, 'currency_unknown'), ('EUR', 'currency_unsupported'), ('грн', 'currency_unsupported')):
            with self.subTest(currency=currency):
                result = self.normalized(currency=currency)
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['usd_amount'])

    def test_missing_rate_preserves_price_for_retry(self):
        result = normalize_price({'price': '210000', 'currency': 'UAH'}, None, self.now)
        self.assertEqual(result['reason'], 'fx_missing')
        self.assertEqual(result['original_amount'], '210000')
        self.assertIsNone(result['usd_amount'])

    def test_weekend_effective_date_is_valid_even_if_calculated_friday(self):
        result = self.normalized()
        self.assertEqual(result['status'], 'ready')
        sunday = self.now + timedelta(days=1)
        self.assertEqual(normalize_price({'price': '210000', 'currency': 'UAH'}, self.quote, sunday)['reason'], 'fx_wrong_effective_date')

    def test_no_guessed_friday_fallback_on_saturday(self):
        friday = date(2026, 10, 2)
        quote = FXQuote(Decimal('42'), friday, self.now - timedelta(hours=2), nbu_url(friday))
        self.assertEqual(self.normalized(quote=quote)['reason'], 'fx_wrong_effective_date')

    def test_future_rate_and_future_fetch(self):
        tomorrow = self.day + timedelta(days=1)
        future = FXQuote(Decimal('42'), tomorrow, self.now, nbu_url(tomorrow))
        self.assertEqual(self.normalized(quote=future)['reason'], 'fx_future_date')
        future_fetch = FXQuote(Decimal('42'), self.day, self.now + timedelta(seconds=1), nbu_url(self.day))
        self.assertEqual(self.normalized(quote=future_fetch)['reason'], 'fx_future_fetch')

    def test_maximum_cache_age_and_midnight(self):
        self.assertEqual(self.normalized(max_cache_age=59)['reason'], 'fx_stale_cache')
        self.assertEqual(self.normalized(max_cache_age=60)['status'], 'ready')
        with self.assertRaises(ValueError):
            self.normalized(max_cache_age=86401)
        before_kyiv_midnight = datetime(2026, 10, 2, 20, 59, tzinfo=timezone.utc)
        self.assertEqual(normalize_price({'price':'210000','currency':'UAH'}, self.quote, before_kyiv_midnight)['reason'], 'fx_future_date')

    def test_division_does_not_round_to_display_cents(self):
        quote = FXQuote(Decimal('7'), self.day, self.now, nbu_url(self.day))
        result = self.normalized('10000', quote=quote)
        with localcontext() as ctx:
            ctx.prec = 50
            self.assertEqual(Decimal(result['usd_amount']), Decimal('10000') / Decimal('7'))
        self.assertGreater(len(result['usd_amount'].split('.')[1]), 40)
        self.assertEqual(display_price(result), '≈ 1 429 $')

    def test_parse_decimal_and_date_not_calculation_date(self):
        # Official schema, fabricated amount for reproducibility.
        payload = '[{"cc":"USD","r030":840,"units":1,"rate":42.1234,"rate_per_unit":42.1234,"exchangedate":"03.10.2026","calcdate":"02.10.2026"}]'
        quote = parse_nbu_quote(payload, requested_date=self.day, fetched_at=self.now)
        self.assertEqual(quote.uah_per_usd, Decimal('42.1234'))
        self.assertEqual(quote.effective_date, self.day)
        self.assertEqual(quote.calculated_on, date(2026, 10, 2))

    def test_parser_rejects_missing_duplicate_wrong_currency_date_or_unit(self):
        good = {'cc':'USD','r030':840,'units':1,'rate':Decimal('42'),'rate_per_unit':Decimal('42'),'exchangedate':'03.10.2026'}
        cases = [[], [good,good], [dict(good, cc='EUR')], [dict(good, units=100)], [dict(good, exchangedate='02.10.2026')], [dict(good, rate='43')], [dict(good, rate_per_unit='0')], [dict(good, calcdate='04.10.2026')]]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_nbu_quote(payload, requested_date=self.day, fetched_at=self.now)

    def test_cache_survives_restart_and_is_shared_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'isolated-fx.sqlite'
            cache = FXCache(path)
            cache.put(self.quote)
            cache.close()
            restarted = FXCache(path)
            restored = restarted.get(self.day)
            self.assertEqual(restored, self.quote)
            for _ in range(200):
                self.assertEqual(self.normalized(quote=restored)['usd_amount'], '5000')
            self.assertEqual(restarted.db.execute('SELECT count(*) FROM olx_fx_quotes').fetchone()[0], 1)
            self.assertIsNone(restarted.get(self.day + timedelta(days=1)))
            older = FXQuote(Decimal('41'), self.day, self.now-timedelta(hours=1), nbu_url(self.day))
            self.assertFalse(restarted.put(older))
            self.assertEqual(restarted.get(self.day), self.quote)
            restarted.close()

    def test_invalid_provenance_and_naive_clock_rejected(self):
        with self.assertRaises(ValueError):
            FXQuote(Decimal('42'), self.day, self.now, 'https://example.invalid')
        with self.assertRaises(ValueError):
            parse_nbu_quote('[]', requested_date=self.day, fetched_at=datetime(2026,10,3))
        with self.assertRaises(ValueError):
            FXCache(':memory:')


if __name__ == '__main__':
    unittest.main()
