"""Pure OLX test-card rendering; no network, Telegram adapter or access grant.

The caller supplies the saved canonical listing and its corresponding assessment.
Presentation never turns an unknown assessment into a deal, invents an FX rate,
or imports the AUTO.RIA valuation formula. A future transport must preserve the
complete text: when it exceeds Telegram's photo-caption limit, send the text
separately rather than cutting off the valuation or currency qualifications.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from html import escape
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .fx import FXQuote, day, instant, quote_problem


KYIV = ZoneInfo('Europe/Kyiv')
METHOD_LABELS = {
    'lower_quartile': 'Нижній квартиль цін зіставних пропозицій',
    'robust_median': 'Медіана зіставних пропозицій після відсіву викидів',
    'weighted_similar': 'Зважена медіана найближчих аналогів',
}
FEATURE_LABELS = {
    'fuel': {'petrol': 'Бензин', 'diesel': 'Дизель', 'lpg': 'Газ',
             'gas/petrol': 'Газ / бензин', 'gas_petrol': 'Газ / бензин',
             'hybrid': 'Гібрид', 'electric': 'Електро'},
    'transmission': {'manual': 'Механічна', 'automatic': 'Автоматична',
                     'robot': 'Роботизована', 'robotized': 'Роботизована',
                     'cvt': 'Варіатор', 'tiptronic': 'Типтронік',
                     'reduction_gear': 'Одноступенева'},
    'body': {'hatchback': 'Хетчбек', 'sedan': 'Седан', 'wagon': 'Універсал',
             'suv': 'Позашляховик', 'coupe': 'Купе', 'minivan': 'Мінівен'},
}
UNKNOWN_LABELS = {
    'insufficient_comparables': 'Недостатньо придатних аналогів.',
    'missing_comparison_attributes': 'Недостатньо характеристик для зіставлення.',
    'detail_refresh_required': 'Потрібні актуальні деталі оголошення.',
    'eligibility_not_allowed': 'Потрібна перевірка придатності оголошення.',
    'full_price_unconfirmed': 'Повна ціна автомобіля не підтверджена.',
    'stale_or_future_observation': 'Дата даних потребує перевірки.',
    'valuation_conflict': 'Дані оцінки суперечливі.',
}


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if len(str(value)) > 128:
        return None
    try:
        result = Decimal(value)
    except (ValueError, InvalidOperation):
        return None
    if (not result.is_finite() or len(result.as_tuple().digits) > 60
            or abs(result.as_tuple().exponent) > 60):
        return None
    return result


def _positive(value):
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _money(value):
    with localcontext() as context:
        context.prec = 100
        result = value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f'${result:,.0f}'.replace(',', ' ')


def _original(value):
    integer, separator, fraction = format(value, 'f').partition('.')
    fraction = fraction.rstrip('0')
    grouped = f'{int(integer):,}'.replace(',', ' ')
    return grouped + (',' + fraction if separator and fraction else '')


def _text(value, limit=80):
    if not isinstance(value, str) or not value.strip():
        return None
    # Control characters are not seller-visible formatting instructions.
    clean = ' '.join(value.split())
    return clean[:limit] + ('…' if len(clean) > limit else '')


def _https_parts(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or re.search(r'[\x00-\x20\x7f\\]', value)):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return None
    except ValueError:
        return None
    return parts


def listing_button(value):
    """Only an official HTTPS OLX listing, with tracking/fragment removed."""
    parts = _https_parts(value)
    if (parts is None or parts.hostname not in ('olx.ua', 'www.olx.ua')
            or not re.fullmatch(r'/(?:d/)?(?:(?:uk|ru)/)?obyavlenie/[^/]+\.html', parts.path)):
        return None
    return {'text': '🔗 Відкрити оголошення',
            'url': urlunsplit(('https', parts.hostname, parts.path, '', ''))}


def _photo(values):
    if not isinstance(values, (list, tuple)):
        return None
    for value in values[:30]:
        parts = _https_parts(value)
        if (parts is not None and parts.hostname.endswith('.olxcdn.com')
                and parts.path not in ('', '/')):
            return urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))
    return None


def _price_proof(car, now):
    proof = car.get('usd_price')
    if not isinstance(proof, dict):
        return None, None, None
    original = _positive(proof.get('original_amount'))
    currency = proof.get('original_currency')
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        currency = None
    usd = _positive(proof.get('usd_amount'))
    if (proof.get('status') != 'ready' or original is None or usd is None
            or currency != car.get('currency') or original != _positive(car.get('price'))):
        return None, original, currency
    fx = proof.get('fx')
    if currency == 'USD':
        return (usd if fx is None and usd == original else None), original, currency
    if currency != 'UAH' or not isinstance(fx, dict):
        return None, original, currency
    try:
        quote = FXQuote(_positive(fx.get('uah_per_usd')), day(fx.get('effective_date')),
                        instant(fx.get('fetched_at')), fx.get('source'),
                        day(fx['calculated_on']) if fx.get('calculated_on') else None)
        if now is not None and quote_problem(quote, now):
            return None, original, currency
    except (ValueError, TypeError, OverflowError):
        return None, original, currency
    with localcontext() as context:
        context.prec = 100
        expected = original / quote.uah_per_usd
    if abs(usd - expected) > max(abs(expected), Decimal(1)) * Decimal('1e-24'):
        return None, original, currency
    return usd, original, currency


def _assessment(assessment, usd):
    if not isinstance(assessment, dict):
        return None
    if assessment.get('status') != 'experimental_estimate' or usd is None:
        return None
    reference = _positive(assessment.get('reference_usd'))
    spread = assessment.get('range_usd')
    low = _positive(spread.get('low')) if isinstance(spread, dict) else None
    high = _positive(spread.get('high')) if isinstance(spread, dict) else None
    supplied_discount = _decimal(assessment.get('discount_percent'))
    sample = assessment.get('sample')
    dates = assessment.get('data_checked_at')
    if (assessment.get('currency') != 'USD' or assessment.get('method') not in METHOD_LABELS
            or reference is None or low is None or high is None or high < low
            or supplied_discount is None or type(sample) is not int or not 2 <= sample <= 10**6
            or not isinstance(dates, dict) or type(dates.get('oldest')) is not int
            or type(dates.get('newest')) is not int
            or not 0 < dates['oldest'] <= dates['newest']):
        return None
    with localcontext() as context:
        context.prec = 100
        discount = (reference - usd) / reference * 100
    if abs(discount - supplied_discount) > max(abs(discount), Decimal(1)) * Decimal('1e-20'):
        return None
    try:
        oldest = datetime.fromtimestamp(dates['oldest'], KYIV).date().isoformat()
        newest = datetime.fromtimestamp(dates['newest'], KYIV).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return reference, low, high, discount, oldest, newest


def render_card(car, assessment=None, *, now=None):
    """Return existing test-sender text/photo/button shape plus HTML parse mode.

    ``now`` optionally rechecks the supplied FX proof against the rendering time.
    The queue remains responsible for eligibility, freshness, filters, payment,
    /stop and duplicate checks immediately before transport. This is no gate.
    """
    if not isinstance(car, dict) or car.get('source') != 'olx':
        raise ValueError('An explicit canonical OLX listing is required')
    if now is not None and (type(now) is not int or now <= 0):
        raise ValueError('A positive explicit rendering timestamp is required')
    title = _text(car.get('title'), 120) or 'Автомобіль'
    sections = ['🚘 <b>' + escape(title) + '</b>', '🟠 <b>OLX · AutoDeal</b>']
    usd, original, currency = _price_proof(car, now)
    displayed = car.get('observed_asking_display')
    display_only = (car.get('original_seller_currency_verified') is False
                    or isinstance(displayed, dict)
                    and displayed.get('original_seller_currency_verified') is False)
    if usd is None:
        sections.append('💰 Ціна в USD потребує перевірки')
    else:
        label = ('💰 Перерахунок показаної ціни OLX: ' if currency == 'UAH'
                 else '💰 Показана ціна OLX: ') if display_only else '💰 Ціна: '
        sections.append(label + ('≈ ' if currency == 'UAH' else '') + _money(usd))
    if original is not None and isinstance(currency, str):
        label = 'На сторінці OLX: ' if display_only else 'Початкова ціна: '
        sections.append(label + escape(_original(original) + ' ' + currency))
    if display_only:
        sections.append('⚠️ Початкова ціна/валюта продавця не підтверджені.')
    if usd is not None and currency == 'UAH':
        fx = car['usd_price']['fx']
        sections.append('Курс НБУ: ' + escape(fx['uah_per_usd']) + ' UAH за 1 USD · '
                        + escape(fx['effective_date']))
        sections.append('Джерело курсу: ' + escape(fx['source']))

    features = []
    year = car.get('year')
    if type(year) is int and 1886 <= year <= 2100:
        features.append(f'📅 Рік: {year}')
    mileage = car.get('mileage_km')
    if type(mileage) is int and 0 <= mileage <= 10**7:
        features.append(f'🛣️ Пробіг: {mileage:,} км'.replace(',', ' '))
    for key, label in (('body', '🚗 Кузов'), ('fuel', '⛽️ Пальне'),
                       ('transmission', '⚙️ Коробка'), ('region', '📍 Область'),
                       ('locality', '📍 Місто')):
        value = _text(car.get(key), 60)
        if value:
            value = FEATURE_LABELS.get(key, {}).get(value.casefold(), value)
            features.append(label + ': ' + escape(value))
    engine = car.get('engine_cc')
    if type(engine) is int and 0 < engine <= 20000:
        features.append(f'Двигун: {engine} см³')
    if features:
        sections.append('\n'.join(features))

    reviewed = _assessment(assessment, usd)
    if reviewed is None:
        sections.append('ℹ️ <b>Вигідність не підтверджена</b>')
        reason = assessment.get('reason') if isinstance(assessment, dict) else None
        if isinstance(assessment, dict) and assessment.get('status') == 'experimental_estimate':
            reason = 'valuation_conflict'
        if reason in UNKNOWN_LABELS:
            sections.append(UNKNOWN_LABELS[reason])
    else:
        reference, low, high, discount, oldest, newest = reviewed
        with localcontext() as context:
            context.prec = 100
            shown_discount = format(abs(discount).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.')
        sections.append('📊 Орієнтир цін пропозицій: ≈ ' + _money(reference))
        sections.append('Середній діапазон цін аналогів: ' + _money(low) + '–' + _money(high))
        sections.append(('📉 Нижче орієнтира: ' if discount > 0 else '📈 Вище орієнтира: '
                         if discount < 0 else 'Відхилення від орієнтира: ') + shown_discount + '%')
        sections.append('Вибірка: ' + str(assessment['sample']) + ' оголошень; відомі дублікати вилучено')
        sections.append(METHOD_LABELS[assessment['method']])
        sections.append('Дані аналогів: ' + oldest + (' — ' + newest if newest != oldest else '')
                        + ' · Europe/Kyiv')
        sections.append('⚠️ Експериментальна оцінка. Це ціни пропозицій, а не фактичних продажів; '
                        'діапазон не гарантує ціну цього авто.')
        reasons = assessment.get('reasons', [])
        if 'crossposts_without_verified_vehicle_identity_may_remain' in reasons:
            sections.append('Міжплатформні дублікати без підтвердженого збігу авто можуть залишатися.')
        if 'repair_severity_and_cost_not_verified' in reasons:
            sections.append('⚠️ Вартість і обсяг ремонту не підтверджені.')
    review = car.get('eligibility_review')
    if not isinstance(review, dict) or review.get('status') != 'allowed':
        sections.append('⚠️ Потрібна перевірка придатності оголошення.')
    sections.append('/stop — вимкнути сповіщення')
    text = '\n\n'.join(sections)
    return {'text': text, 'photo': _photo(car.get('photos')),
            'button': listing_button(car.get('url')), 'parse_mode': 'HTML'}
