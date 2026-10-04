"""Offline USD normalization with explicit NBU provenance; never performs HTTP.

One caller-owned quote is shared across a batch. No fallback rate, .env, production
database, or timer is used. The dated endpoint avoids tomorrow's advertised rate.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
import json
import re
import sqlite3
from zoneinfo import ZoneInfo

KYIV = ZoneInfo('Europe/Kyiv')
NBU_ENDPOINT = 'https://bank.gov.ua/NBU_Exchange/exchange_site'
DECIMAL_PRECISION = 50


def instant(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Timezone-aware timestamp required')
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        return instant(datetime.fromisoformat(value.replace('Z', '+00:00')))
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, timezone.utc)
    raise ValueError('Timestamp required')


def day(value):
    if isinstance(value, datetime):
        raise ValueError('An effective calendar date is required')
    return value if isinstance(value, date) else date.fromisoformat(value)


def nbu_url(effective_date):
    """Documented date-range endpoint restricted to a single explicit day/USD."""
    d = day(effective_date).strftime('%Y%m%d')
    return f'{NBU_ENDPOINT}?start={d}&end={d}&valcode=usd&sort=exchangedate&order=desc&json'


def amount(value, *, canonical=False):
    """Positive finite decimal, with explicit grouping; ambiguous 1,234 is held.

    Floating-point inputs are rejected: JSON parsers should use parse_float=Decimal
    or preserve the source decimal string. Currency symbols belong in another field.
    """
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ValueError('Invalid amount type')
    text = str(value).strip().replace('\u00a0', ' ').replace('\u202f', ' ')
    if len(text) > 64 or not text:
        raise ValueError('Invalid amount length')
    if ' ' in text:
        if not re.fullmatch(r'\d{1,3}(?: \d{3})+(?:[.,]\d{1,8})?', text):
            raise ValueError('Invalid grouping')
        text = text.replace(' ', '')
    if ',' in text and '.' in text:
        decimal, grouping = (',', '.') if text.rfind(',') > text.rfind('.') else ('.', ',')
        if not re.fullmatch(r'\d{1,3}(?:' + re.escape(grouping) + r'\d{3})+' + re.escape(decimal) + r'\d{1,8}', text):
            raise ValueError('Invalid mixed separators')
        text = text.replace(grouping, '').replace(decimal, '.')
    elif ',' in text or '.' in text:
        separator = ',' if ',' in text else '.'
        if text.count(separator) > 1:
            if not re.fullmatch(r'\d{1,3}(?:' + re.escape(separator) + r'\d{3}){2,}', text):
                raise ValueError('Invalid grouping')
            text = text.replace(separator, '')
        else:
            # Canonical Decimal values are unambiguous; human 1,234 / 1.234 are not.
            if not canonical and not isinstance(value, Decimal) and re.fullmatch(r'\d{1,3}[.,]\d{3}', text):
                raise ValueError('Ambiguous separator')
            text = text.replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d{1,8})?', text):
        raise ValueError('Malformed decimal')
    result = Decimal(text)
    if not result.is_finite() or result <= 0 or len(result.as_tuple().digits) > 38:
        raise ValueError('Amount must be positive, finite and bounded')
    return result


def _plain(value):
    return format(value, 'f')


@dataclass(frozen=True)
class FXQuote:
    uah_per_usd: Decimal
    effective_date: date
    fetched_at: datetime
    source: str
    calculated_on: date | None = None

    def __post_init__(self):
        if (not isinstance(self.uah_per_usd, Decimal) or not self.uah_per_usd.is_finite()
                or self.uah_per_usd <= 0 or len(self.uah_per_usd.as_tuple().digits) > 38
                or abs(self.uah_per_usd.as_tuple().exponent) > 8):
            raise ValueError('Positive bounded Decimal rate required')
        if self.effective_date != day(self.effective_date) or self.source != nbu_url(self.effective_date):
            raise ValueError('Dated NBU provenance required')
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.tzinfo is None:
            raise ValueError('Aware fetched_at required')
        if self.calculated_on and self.calculated_on > self.effective_date:
            raise ValueError('Calculation cannot follow effective date')

    def as_dict(self):
        return {'uah_per_usd': _plain(self.uah_per_usd),
                'source': self.source, 'effective_date': self.effective_date.isoformat(),
                'fetched_at': self.fetched_at.isoformat(),
                'calculated_on': self.calculated_on.isoformat() if self.calculated_on else None,
                'direction': 'UAH per 1 USD'}


def parse_nbu_quote(payload, *, requested_date, fetched_at):
    """Parse the official exchange_site schema, including weekend effective dates.

    JSON float tokens are read as Decimal. Do not substitute calcdate for exchangedate.
    A malformed or missing quote raises ValueError and must remain pending upstream.
    """
    wanted = day(requested_date)
    fetched = instant(fetched_at)
    if isinstance(payload, (str, bytes)):
        if len(payload) > 262144:
            raise ValueError('NBU payload exceeds budget')
        payload = json.loads(payload, parse_float=Decimal)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError('Exactly one dated USD row required')
    row = payload[0]
    if (row.get('cc') != 'USD' or type(row.get('r030')) is not int or row.get('r030') != 840
            or type(row.get('units')) is not int or row.get('units') != 1):
        raise ValueError('Expected one US dollar, code 840')
    try:
        effective = datetime.strptime(row['exchangedate'], '%d.%m.%Y').date()
        calculated = datetime.strptime(row['calcdate'], '%d.%m.%Y').date() if row.get('calcdate') else None
        rate = amount(row['rate_per_unit'])
        total_rate = amount(row['rate'])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError('Malformed NBU row') from exc
    if effective != wanted or (calculated and calculated > effective) or rate != total_rate:
        raise ValueError('NBU date/rate mismatch')
    return FXQuote(rate, effective, fetched, nbu_url(effective), calculated)


def quote_problem(quote, now, *, max_cache_age=86400):
    if not isinstance(quote, FXQuote):
        return 'fx_missing'
    current = instant(now)
    if not isinstance(max_cache_age, (int, float)) or isinstance(max_cache_age, bool) or not 0 < max_cache_age <= 86400:
        raise ValueError('Explicit FX cache age must be between 0 and 86400 seconds')
    if (not isinstance(quote.uah_per_usd, Decimal) or not quote.uah_per_usd.is_finite()
            or quote.uah_per_usd <= 0 or quote.source != nbu_url(quote.effective_date)):
        return 'fx_invalid'
    today = current.astimezone(KYIV).date()
    if quote.effective_date > today:
        return 'fx_future_date'
    if quote.effective_date < today:
        return 'fx_wrong_effective_date'
    age = (current - instant(quote.fetched_at)).total_seconds()
    if age < 0:
        return 'fx_future_fetch'
    if age > max_cache_age:
        return 'fx_stale_cache'
    if quote.calculated_on and quote.calculated_on > quote.effective_date:
        return 'fx_invalid'
    return None


def normalize_price(raw, quote, now, *, max_cache_age=86400):
    """Return serializable amount/provenance; never mutate input or double-convert.

    Original fields take precedence on repeated calls. Division uses 50 significant
    decimal digits without cents rounding; source amount and divisor are retained.
    Downstream comparisons must use Decimal, not float. EUR/unknown remains pending.
    """
    if not isinstance(raw, dict):
        raise ValueError('A price mapping is required')
    use_original = 'original_amount' in raw or 'original_currency' in raw
    value = raw.get('original_amount') if use_original else raw.get('price')
    currency = raw.get('original_currency') if use_original else raw.get('currency')
    currency = currency.strip().upper() if isinstance(currency, str) else None
    result = {'status': 'pending', 'reason': None,
              'original_amount': str(value) if value is not None else None,
              'original_currency': currency, 'usd_amount': None, 'fx': None,
              'conversion_precision': None}
    try:
        original = amount(value, canonical=use_original)
    except (ValueError, InvalidOperation):
        result['reason'] = 'amount_invalid'
        return result
    result['original_amount'] = _plain(original)
    if currency == 'USD':
        result.update(status='ready', reason='already_usd', usd_amount=_plain(original))
        return result
    if currency != 'UAH':
        result['reason'] = 'currency_unsupported' if currency else 'currency_unknown'
        return result
    problem = quote_problem(quote, now, max_cache_age=max_cache_age)
    if problem:
        result['reason'] = problem
        return result
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        converted = original / quote.uah_per_usd
    result.update(status='ready', reason='converted_uah', usd_amount=_plain(converted),
                  fx=quote.as_dict(), conversion_precision=DECIMAL_PRECISION)
    return result


def display_price(normalized):
    """Round only presentation, never the stored comparison value."""
    if normalized.get('status') != 'ready':
        return 'Ціна в USD потребує перевірки'
    with localcontext() as ctx:
        ctx.prec = 60
        rounded = Decimal(normalized['usd_amount']).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    prefix = '≈ ' if normalized.get('fx') else ''
    return f'{prefix}{rounded:,.0f} $'.replace(',', ' ')


class FXCache:
    """Explicit isolated durable cache, shared by all listings; no automatic fetch."""
    def __init__(self, path):
        if '://' in str(path) or str(path) == ':memory:':
            raise ValueError('Explicit local cache file required')
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS olx_fx_quotes (effective_date TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

    def put(self, quote):
        if not isinstance(quote, FXQuote) or quote.source != nbu_url(quote.effective_date):
            raise ValueError('A dated NBU quote is required')
        previous = self.get(quote.effective_date)
        if previous and previous.fetched_at > quote.fetched_at:
            return False
        with self.db:
            self.db.execute('INSERT INTO olx_fx_quotes VALUES (?,?) ON CONFLICT(effective_date) DO UPDATE SET payload=excluded.payload',
                            (quote.effective_date.isoformat(), json.dumps(quote.as_dict())))
        return True

    def get(self, effective_date):
        row = self.db.execute('SELECT payload FROM olx_fx_quotes WHERE effective_date=?', (day(effective_date).isoformat(),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            return FXQuote(Decimal(data['uah_per_usd']), day(data['effective_date']),
                           instant(data['fetched_at']), data['source'],
                           day(data['calculated_on']) if data.get('calculated_on') else None)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None

    def close(self):
        self.db.close()
