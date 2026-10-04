"""One explicitly authorized OLX sample batch; no customer source integration.

Reuses the public-page adapters, current purchase policy, SourceProbe ledger and
existing Telegram transport. No migrations, RIA API, polling or webhook setup.
"""
import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
from decimal import Decimal
from html import escape

from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import billing, olx_owner_canary as previous, paid_source_access, telegram_setup
from .models import Filters, Search, SourceProbe, User
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.fx import normalize_price, display_price
from experiments.olx_offline.html_snapshot import clean_url, parse_search_snapshot
from experiments.olx_offline.integration import filters_from_backend

STATE = 'olx-owner-batch-20261004-2130-v1'
MAX_ADS = 3
MAX_GETS = 10
MAX_DETAILS = 9
MAX_SECONDS = 600
MAX_BYTES = MAX_GETS * previous.MAX_HTML
LOG = logging.getLogger('uvicorn.error')


def enabled(settings, now=None):
    now = time.time() if now is None else now
    return (settings.olx_owner_batch_enabled and settings.live
            and billing.verified_admin(settings) and now < settings.olx_owner_batch_until)


def searches(engine, settings, state=None):
    now = time.time()
    if not enabled(settings, now) or not paid_source_access.strict(engine):
        return []
    if state and (state['owner_id'] != settings.admin_telegram_id or now >= state['until']):
        return []
    with Session(engine) as db:
        if state:
            batch = db.get(SourceProbe, STATE)
            if not batch or batch.status != 'active':
                return []
        owner = db.get(User, settings.admin_telegram_id)
        if not owner or not owner.ready or not paid_source_access.allowed(db, owner.id, now):
            return []
        q = select(Search).where(Search.user_id == owner.id, Search.enabled.is_(True))
        if state:
            q = q.where(Search.id.in_(state['search_ids']))
        result = []
        for row in db.scalars(q.order_by(Search.id)):
            try:
                f = Filters.model_validate(row.filters)
                mapped = filters_from_backend(f.canonical(), currency='USD')['filters']
            except (ValueError, TypeError):
                continue
            result.append({'sid': row.id, 'filters': mapped, 'fingerprint': f.fingerprint(),
                           'threshold': f.minDiscount, 'only_deals': f.onlyDeals})
        return result


def initialize(engine, settings):
    rows = searches(engine, settings)
    if not rows:
        return False
    now = time.time()
    with Session(engine) as db:
        if db.get(SourceProbe, STATE):
            return True  # Never reset an exhausted, stopped or previous batch.
        db.add(SourceProbe(id=STATE, status='active', checked_at=now, requests=0,
            result={'owner_id': settings.admin_telegram_id, 'search_ids': [x['sid'] for x in rows],
                    'started_at': now, 'until': min(now+MAX_SECONDS, settings.olx_owner_batch_until),
                    'lease': '', 'lease_until': 0, 'attempts': 0, 'accepted': 0,
                    'reserved_bytes': 0, 'received_bytes': 0, 'reviewed': [],
                    'candidates': None, 'reviews': [], 'receipts': []}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return True


def claim(engine):
    now, token = time.time(), uuid.uuid4().hex
    with Session(engine) as db:
        changed = db.execute(update(SourceProbe).where(SourceProbe.id == STATE,
            SourceProbe.status == 'active', SourceProbe.result['lease_until'].as_float() <= now
            ).values(checked_at=now))
        if not changed.rowcount:
            db.rollback()
            return None
        row = db.get(SourceProbe, STATE)
        data = copy.deepcopy(row.result)
        if now >= data['until'] or data['attempts'] >= MAX_ADS or row.requests >= MAX_GETS:
            row.status = 'finished'
            db.commit()
            return None
        data.update(lease=token, lease_until=now+60)
        row.result = data
        db.commit()
        return token, data


def update_state(engine, token, **changes):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if not row or row.result.get('lease') != token:
            return False
        row.result = {**row.result, **changes}
        row.checked_at = time.time()
        db.commit()
        return True


def finish(engine, token, reason):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if row and row.result.get('lease') == token:
            if row.status == 'active':
                row.status = 'finished'
            row.result = {**row.result, 'finish_reason': reason, 'lease': '', 'lease_until': 0}
            row.checked_at = time.time()
            db.commit()


def reserve_get(engine, settings, token, state):
    if not searches(engine, settings, state):
        return False
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if (not row or row.status != 'active' or row.result.get('lease') != token
                or row.result['lease_until'] <= time.time() or row.requests >= MAX_GETS
                or row.result['reserved_bytes'] + previous.MAX_HTML > MAX_BYTES):
            return False
        row.requests += 1
        row.result = {**row.result, 'reserved_bytes': row.result['reserved_bytes'] + previous.MAX_HTML,
                      'lease_until': time.time()+60}
        db.commit()
    return True


def price_proof(car, now, quote=None):
    """Normalize the corroborated displayed asking amount, never invent origin.

    The owner's sample may show the explicit dollar amount on the full OLX page.
    That does not establish the currency originally entered by the seller. The
    regularPrice and displayValue remain distinct evidence. A displayed USD
    amount is not converted again from OLX's alternate UAH display.
    """
    observed = car.get('observed_asking_display', {})
    if (observed.get('status') != 'corroborated_display'
            or observed.get('description_reviewed_in_full') is not True
            or car.get('eligibility_review', {}).get('status') != 'allowed'
            or car.get('field_conflicts') or car.get('price_conflicts')
            or set(car.get('price_review', {}).get('reasons', [])) - {'full_price_unconfirmed'}):
        return None
    try:
        if Decimal(observed['amount']) <= 1 or Decimal(observed['amount']) != Decimal(car['price']):
            return None
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return None
    if observed.get('currency') != car.get('currency'):
        return None
    normalized = normalize_price({'price': observed['amount'], 'currency': observed['currency']}, quote, now)
    if normalized['status'] != 'ready':
        return None
    return {'basis': 'corroborated_full_page_ordinary_sale_display',
            'seller_original_currency': None, 'seller_original_currency_verified': False,
            'display_amount': observed['amount'], 'display_currency': observed['currency'],
            'source_regular_price': observed.get('state_regular_price'), 'usd': normalized}


def matches_sample(car, rows, proof):
    if not proof or not car.get('brand') or not car.get('model') or not car.get('year'):
        return False
    converted = {**car, 'price': proof['usd']['usd_amount'], 'currency': 'USD'}
    return previous.sample_match(converted, rows)


def caption(car, proof):
    name = escape(' '.join(str(car[k]) for k in ('brand', 'model', 'year'))[:180])
    lines = ['🧪 <b>ТЕСТ OLX — лише для власника</b>', '🚘 <b>' + name + '</b>']
    fuel = {'petrol': 'Бензин', 'diesel': 'Дизель', 'gas_petrol': 'Газ / бензин',
            'hybrid': 'Гібрид', 'electric': 'Електро'}.get(car.get('fuel'))
    gear = {'manual': 'Механіка', 'automatic': 'Автомат', 'cvt': 'Варіатор',
            'tiptronic': 'Типтронік', 'robotized': 'Робот'}.get(car.get('transmission'))
    if fuel: lines.append('⛽️ ' + fuel)
    if gear: lines.append('⚙️ ' + gear)
    if car.get('mileage_km') is not None:
        lines.append('🛣️ ' + str(car['mileage_km']//1000) + ' тис. км')
    lines.append('💰 Ціна OLX: <b>' + escape(display_price(proof['usd'])) + '</b>')
    if proof['usd'].get('fx'):
        fx = proof['usd']['fx']
        lines.append('На сторінці: ' + escape(proof['display_amount']) + ' грн; НБУ '
                     + escape(fx['effective_date']) + ': ' + escape(fx['uah_per_usd']) + ' грн/$')
    lines.append('📍 ' + escape(' · '.join(x for x in (car.get('locality'), car.get('region')) if x)[:160]))
    lines += ['', 'Ринкова оцінка: ще не підтверджена.',
              'Це тест отримання та відправлення оголошення.',
              'Приклад чинного оголошення; новизна не підтверджена.',
              '/olx_stop — зупинити лише OLX-тест']
    return '\n'.join(lines)


def prior_attempt(db, car_id, owner_id):
    key = 'olx-owner-car-' + hashlib.sha256((str(owner_id)+':'+car_id).encode()).hexdigest()[:32]
    if db.get(SourceProbe, key):
        return True
    # Includes the previous completed two-card package without resetting it.
    return bool(db.scalar(select(SourceProbe.id).where(
        SourceProbe.id.like(previous.TEST_BATCH+'-%'),
        SourceProbe.result['source_id'].as_string() == car_id,
        SourceProbe.status.in_(('sending', 'accepted', 'uncertain'))).limit(1)))


def send_sample(engine, settings, token, state, car, sender=telegram_setup.call):
    proof = price_proof(car, time.time())
    rows = searches(engine, settings, state)
    if (car.get('source') != 'olx' or car.get('url') not in state.get('test_urls', [])
            or not matches_sample(car, rows, proof)):
        return False
    now = time.time()
    key = 'olx-owner-car-' + hashlib.sha256((str(settings.admin_telegram_id)+':'+car['id']).encode()).hexdigest()[:32]
    with Session(engine) as db:
        # Actual UPDATE serializes claims on both SQLite and PostgreSQL.
        db.execute(update(SourceProbe).where(SourceProbe.id == STATE).values(checked_at=now))
        batch = db.get(SourceProbe, STATE)
        if (not batch or batch.status != 'active' or batch.result.get('lease') != token
                or batch.result['lease_until'] <= now or batch.result['attempts'] >= MAX_ADS
                or prior_attempt(db, car['id'], settings.admin_telegram_id)):
            db.rollback()
            return False
        batch.result = {**batch.result, 'attempts': batch.result['attempts']+1}
        db.add(SourceProbe(id=key, status='sending', checked_at=now, requests=1,
            result={'batch_id': STATE, 'source_id': car['id'], 'attempted_at': now,
                    'price_proof': proof, 'new_publication_verified': False,
                    'profitability_verified': False}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
    response, method = {'not_attempted': True}, None

    def current():
        return matches_sample(car, searches(engine, settings, state), price_proof(car, time.time()))

    try:
        if current():
            chat_response = sender(settings.bot_token, 'getChat', {'chat_id': settings.admin_telegram_id}, timeout=5)
            chat = chat_response.get('result', {}) if isinstance(chat_response, dict) else {}
            chat_ok = (isinstance(chat, dict) and chat_response.get('ok') is True
                       and type(chat.get('id')) is int and chat['id'] == state['owner_id']
                       and chat.get('type') == 'private')
            if chat_ok and current():
                text = caption(car, proof)
                payload = {'chat_id': state['owner_id'], 'parse_mode': 'HTML',
                           'reply_markup': {'inline_keyboard': [[{'text': '🔗 Відкрити OLX-оголошення', 'url': car['url']}]]}}
                method = 'sendPhoto' if car.get('photos') else 'sendMessage'
                if method == 'sendPhoto':
                    payload.update(photo=car['photos'][0], caption=text)
                else:
                    payload.update(text=text, link_preview_options={'is_disabled': True})
                # No blind retries or photo fallback, including on restart.
                response = sender(settings.bot_token, method, payload, timeout=5)
    except Exception:
        response = {'uncertain': True} if method else {'not_attempted': True}
    response = response if isinstance(response, dict) else {'uncertain': True}
    receipt = response.get('result') if isinstance(response.get('result'), dict) else {}
    chat = receipt.get('chat') if isinstance(receipt.get('chat'), dict) else {}
    accepted = (response.get('ok') is True and type(receipt.get('message_id')) is int
                and receipt['message_id'] > 0 and type(chat.get('id')) is int
                and chat['id'] == state['owner_id'] and chat.get('type') == 'private')
    rejected = response.get('ok') is False and response.get('error_code') in (400, 401, 403, 404, 429)
    status = ('accepted' if accepted else 'not_attempted' if response.get('not_attempted')
              else 'rejected' if rejected else 'uncertain')
    saved = {'source_id': car['id'], 'attempted_at': now, 'completed_at': time.time(),
             'status': status, 'accepted': accepted, 'method': method,
             'message_id': receipt.get('message_id') if accepted else None,
             'telegram_error_code': response.get('error_code') if type(response.get('error_code')) is int else None,
             'owner_private_chat_verified': accepted}
    with Session(engine) as db:
        row = db.get(SourceProbe, key)
        row.status, row.result = status, {**row.result, **saved}
        batch = db.get(SourceProbe, STATE, with_for_update=True)
        batch.result = {**batch.result, 'accepted': batch.result['accepted']+int(accepted),
                        'receipts': batch.result['receipts']+[saved]}
        db.commit()
    LOG.info('OLX owner batch receipt %s', json.dumps(saved))
    return accepted


def tick(engine, settings, fetch=previous.public_fetch, sender=telegram_setup.call):
    if not enabled(settings) or not initialize(engine, settings):
        return 'disabled_or_no_paid_search'
    owned = claim(engine)
    if not owned:
        return 'paused_finished_or_busy'
    token, state = owned
    reason = 'candidate_limit'

    def get(url):
        if not reserve_get(engine, settings, token, state):
            raise ValueError('current_access_or_budget_required')
        code, body, truncated = fetch(url)
        state['received_bytes'] += len(body)
        update_state(engine, token, received_bytes=state['received_bytes'])
        if code != 200 or truncated:
            raise ValueError('source_http_'+str(code) if code != 200 else 'source_truncated')
        return body

    try:
        rows = searches(engine, settings, state)
        if state['candidates'] is None:
            url = previous.test_search_url(rows)
            page = parse_search_snapshot(get(url), fetched_at=int(time.time()), truncated=False)
            if page['summary']['download_truncated']:
                raise ValueError('source_truncated')
            cards = sorted(page['listings'], key=lambda c: c.get('observed_search_reason') != 'organic')
            state['candidates'] = [{'id': c['id'], 'url': c['url']} for c in cards[:MAX_DETAILS]]
            update_state(engine, token, candidates=state['candidates'], source_summary=page['summary'])
        state['test_urls'] = [c['url'] for c in state['candidates']]
        for card in state['candidates']:
            with Session(engine) as db:
                batch = db.get(SourceProbe, STATE)
                if batch.result['attempts'] >= MAX_ADS:
                    reason = 'three_attempt_limit'
                    break
                skip = prior_attempt(db, card['id'], state['owner_id'])
            if card['id'] in state['reviewed'] or skip:
                continue
            if fetch is previous.public_fetch:
                time.sleep(4)
            try:
                parsed = parse_detail_snapshot(get(card['url']), fetched_at=int(time.time()), truncated=False)
            except ValueError as exc:
                if str(exc) not in ('source_http_404', 'source_http_410'):
                    raise
                state['reviewed'].append(card['id'])
                state['reviews'].append({'source_id': card['id'], 'held': str(exc)})
                update_state(engine, token, reviewed=state['reviewed'], reviews=state['reviews'])
                continue
            car = parsed['listing']
            if (parsed['summary']['download_truncated'] or car['id'] != card['id']
                    or car['url'] != clean_url(card['url'])):
                raise ValueError('incomplete_or_mismatched_detail')
            state['reviewed'].append(car['id'])
            proof = price_proof(car, time.time())
            match = matches_sample(car, searches(engine, settings, state), proof)
            review = {'source_id': car['id'], 'checked_at': car['checked_at'],
                      'vehicle': {k: car.get(k) for k in ('brand', 'model', 'year', 'region', 'locality')},
                      'eligible': car['eligibility_review']['status'],
                      'eligibility_reasons': car['eligibility_review']['reasons'],
                      'price_ready': bool(proof), 'filter_match': match,
                      'currency': car.get('currency'), 'asking_amount': car.get('price'),
                      'valuation': 'unconfirmed', 'publication': 'unverified'}
            state['reviews'].append(review)
            update_state(engine, token, reviewed=state['reviewed'], reviews=state['reviews'])
            if match:
                send_sample(engine, settings, token, state, car, sender)
    except Exception as exc:
        # Bounded failure is terminal for this package; no hidden retry loop.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            ('source_', 'current_', 'incomplete_')) else type(exc).__name__
    finish(engine, token, reason)
    log_status(engine, settings)
    return reason


def log_status(engine, settings):
    try:
        with Session(engine) as db, db.begin():
            if db.get_bind().dialect.name == 'postgresql':
                db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            row = db.get(SourceProbe, STATE)
            old = db.get(SourceProbe, previous.STATE)
            data = {'batch_id': STATE, 'enabled': enabled(settings),
                    'owner_configuration_verified': billing.verified_admin(settings),
                    'maximum_ads': MAX_ADS, 'maximum_gets': MAX_GETS,
                    'legacy_enabled_flag': settings.olx_owner_canary_enabled,
                    'legacy_deadline': settings.olx_owner_canary_until,
                    'legacy_state': old.status if old else None,
                    'legacy_attempts': None, 'paid_RIA_calls': 0,
                    'status': row.status if row else 'not_started'}
            data['runtime_active'] = bool(row and row.status == 'active' and enabled(settings))
            old_batch = db.get(SourceProbe, previous.TEST_BATCH)
            data['legacy_attempts'] = old_batch.requests if old_batch else 0
            if row:
                data.update({k: v for k, v in row.result.items() if k not in ('owner_id', 'search_ids', 'lease', 'candidates')})
                data['requests'] = row.requests
        LOG.info('OLX owner batch status %s', json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        LOG.warning('OLX owner batch status unavailable (%s)', type(exc).__name__)


def handle(engine, settings, event):
    message = event.get('message', {})
    if not isinstance(message, dict): return None
    words = message.get('text', '').split() if isinstance(message.get('text'), str) else []
    command, _, mention = (words[0] if words else '').partition('@')
    if command not in ('/olx_status', '/olx_stop'): return None
    sender, chat = message.get('from') or {}, message.get('chat') or {}
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return {'ok': True}
    uid = sender.get('id')
    if (not billing.verified_admin(settings) or type(uid) is not int or uid != settings.admin_telegram_id
            or sender.get('is_bot') or chat.get('id') != uid or chat.get('type') != 'private'
            or mention and mention.casefold() != telegram_setup.BOT_USERNAME.casefold()):
        return {'ok': True}
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if not row:
            if command != '/olx_stop' or not settings.olx_owner_batch_enabled:
                return None  # Existing command retains its historical status view.
            row = SourceProbe(id=STATE, status='paused_by_owner', checked_at=time.time(), requests=0,
                result={'owner_id': uid, 'search_ids': [], 'until': settings.olx_owner_batch_until,
                        'attempts': 0, 'accepted': 0, 'lease_until': 0})
            db.add(row)
        if command == '/olx_stop':
            row.status = 'paused_by_owner'
            db.commit()
        reply = ('⏸ OLX-тест зупинено. AUTO.RIA працює за своїми налаштуваннями.'
                 if command == '/olx_stop' else
                 '🧪 OLX — пакет лише для власника\nСтан: ' + row.status
                 + '\nСпроб: ' + str(row.result['attempts']) + '/3'
                 + '\nПрийнято Telegram: ' + str(row.result['accepted'])
                 + '\nРинкова оцінка та новизна не підтверджені.\n/olx_stop — зупинити лише OLX.')
    return billing.message(uid, reply)


async def run(engine, settings, stop):
    while enabled(settings) and not stop.is_set():
        try:
            outcome = await asyncio.to_thread(tick, engine, settings)
            if outcome != 'paused_finished_or_busy':
                break
            with Session(engine) as db:
                row = db.get(SourceProbe, STATE)
                if row and row.status != 'active':
                    break
        except Exception as exc:
            LOG.warning('OLX owner batch halted (%s)', type(exc).__name__)
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
