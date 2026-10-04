"""Bounded, owner-only live OLX observation canary; never charges AUTO.RIA.

This first live stage collects real public HTML, checks current searches and
records reasons. Unproven publication/price/valuation never becomes a deal.
Only an owner-requested diagnostic summary goes to Telegram. This is not a
general OLX rollout or permission to replay baseline cars.
"""
import asyncio
import copy
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import billing, paid_source_access, telegram_setup
from .models import Filters, Search, SourceProbe, User
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.html_snapshot import parse_search_snapshot, clean_url
from experiments.olx_offline.integration import filters_from_backend
from experiments.olx_offline.pipeline import filtered
from experiments.olx_offline.market import estimate_usd

STATE = 'olx-owner-canary-20261004-v1'
NOTICE = STATE + '-notice'
INTERVAL = 300
MAX_REQUESTS = 40
MAX_BYTES = 80 * 1024 * 1024
MAX_HTML = 4 * 1024 * 1024
URL = 'https://www.olx.ua/uk/transport/legkovye-avtomobili/?currency=UAH&search%5Border%5D=created_at%3Adesc'
log = logging.getLogger('uvicorn.error')


def enabled(settings, now=None):
    now = time.time() if now is None else now
    return (settings.olx_owner_canary_enabled and settings.admin_telegram_id > 0
            and settings.live and now < settings.olx_owner_canary_until)


def searches(engine, settings, *, pinned=None, now=None):
    """Same current purchase rule as RIA; administrator role grants nothing."""
    now = time.time() if now is None else now
    if not enabled(settings, now) or not paid_source_access.strict(engine):
        return []
    with Session(engine) as db:
        owner = db.get(User, settings.admin_telegram_id)
        if not owner or not owner.ready or not paid_source_access.allowed(db, owner.id, now):
            return []
        query = select(Search).where(Search.user_id == owner.id, Search.enabled.is_(True))
        if pinned is not None:
            query = query.where(Search.id.in_(pinned))
        rows = []
        for row in db.scalars(query.order_by(Search.id)):
            try:
                filters = Filters.model_validate(row.filters)
                bridge = filters_from_backend(filters.canonical(), currency='USD')
            except (ValueError, TypeError):
                continue
            rows.append({'sid': row.id, 'fingerprint': filters.fingerprint(),
                         'filters': bridge['filters'], 'only_deals': filters.onlyDeals,
                         'threshold': filters.minDiscount})
        return rows


def initialize(engine, settings, now):
    current = searches(engine, settings, now=now)
    if not current:
        return False
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE)
        if row:
            return True
        db.add(SourceProbe(id=STATE, status='active', checked_at=now, requests=0,
            result={'started_at': now, 'until': settings.olx_owner_canary_until,
                    'search_ids': [r['sid'] for r in current], 'next_at': 0,
                    'bytes': 0, 'budget_bytes': 0, 'seen': [], 'lease': '', 'lease_until': 0,
                    'cycles': 0, 'stage': 'owner_observation', 'car_sends': 0}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return True


def claim(engine, settings, now):
    token = uuid.uuid4().hex
    with Session(engine) as db:
        changed = db.execute(update(SourceProbe).where(SourceProbe.id == STATE,
            SourceProbe.status == 'active',
            SourceProbe.result['lease_until'].as_float() <= now,
            SourceProbe.result['next_at'].as_float() <= now).values(checked_at=now))
        if not changed.rowcount:
            db.rollback(); return None
        row = db.get(SourceProbe, STATE)
        data = copy.deepcopy(row.result)
        if now >= min(data['until'], settings.olx_owner_canary_until) or row.requests >= MAX_REQUESTS or data['bytes'] >= MAX_BYTES:
            row.status = 'finished'; db.commit(); return None
        data.update(lease=token, lease_until=now+120, next_at=now+INTERVAL)
        row.result = data
        db.commit()
        return token, data


def reserve(engine, token, settings, now):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if not row or row.status != 'active' or row.result.get('lease') != token or row.result['lease_until'] <= now:
            return False
        if not enabled(settings, now) or row.requests >= MAX_REQUESTS or row.result.get('budget_bytes', 0) + MAX_HTML > MAX_BYTES:
            row.status = 'finished'; db.commit(); return False
        row.requests += 1
        data = copy.deepcopy(row.result)
        data['budget_bytes'] = data.get('budget_bytes', 0) + MAX_HTML
        row.result = data
        db.commit(); return True


def public_fetch(url):
    """One bounded GET, no cookies/auth/redirects/retries or internal endpoints."""
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname != 'www.olx.ua' or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('Only official public OLX pages')
    if not (url == URL or parts.path.startswith('/d/uk/obyavlenie/') and parts.path.endswith('.html') and not parts.query):
        raise ValueError('Unobserved source path')
    deadline = time.monotonic() + 20
    with httpx.Client(timeout=httpx.Timeout(5, connect=5), follow_redirects=False,
                      headers={'User-Agent': 'AutoDeal-Research/0.3 (bounded owner canary)'}) as client:
        with client.stream('GET', url) as response:
            if response.status_code != 200:
                return response.status_code, b'', False
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Bounded OLX request deadline')
                body.extend(chunk[:MAX_HTML+1-len(body)])
                if len(body) > MAX_HTML:
                    return 200, bytes(body[:MAX_HTML]), True
            return 200, bytes(body), False


def permitted(engine, settings, state):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE)
        if not row or row.status != 'active':
            return []
    return searches(engine, settings, pinned=state['search_ids'])


def tick(engine, settings, fetch=public_fetch, sender=telegram_setup.call, now=None):
    clock = time.time if now is None else lambda: now
    now = clock()
    if not enabled(settings, now):
        return {'status': 'disabled'}
    if not initialize(engine, settings, now):
        return {'status': 'waiting_for_paid_ready_search'}
    owned = claim(engine, settings, now)
    if owned is None:
        with Session(engine) as db:
            row = db.get(SourceProbe, STATE)
            return {'status': 'paused_or_busy', 'canary_state': row.status if row else None}
    token, state = owned
    result = {'status': 'observed', 'pages': 0, 'details': 0, 'new_ids': 0,
              'eligible': 0, 'filter_candidates': 0, 'filter_unresolved': 0,
              'excluded': 0, 'valuation_unconfirmed': 0, 'car_sends': 0,
              'bytes': 0, 'catalogue_complete': False}

    def get(url):
        if not permitted(engine, settings, state) or not reserve(engine, token, settings, clock()):
            raise ValueError('current_access_or_budget_required')
        code, body, truncated = fetch(url)
        result['bytes'] += len(body)
        if code != 200:
            result['http_status'] = code
            result['status'] = 'source_blocked' if code in (401, 403, 429) else 'source_unavailable'
            raise ValueError('HTTP_' + str(code))
        return body, truncated

    try:
        if not permitted(engine, settings, state):
            raise ValueError('current_paid_ready_search_required')
        body, truncated = get(URL)
        page = parse_search_snapshot(body, fetched_at=int(clock()), truncated=truncated)
        result['pages'] = 1
        result['page_truncated'] = page['summary']['download_truncated']
        result['cards'] = len(page['listings'])
        known = set(state['seen'])
        fresh = [car for car in page['listings'] if car['id'] not in known]
        fresh.sort(key=lambda car: car.get('observed_search_reason') != 'organic')
        result['new_ids'] = len(fresh) if state['cycles'] else 0
        state['seen'] = list(dict.fromkeys(state['seen'] + [car['id'] for car in page['listings']]))[-5000:]
        # The initial sample is a baseline, never old-ad messages. Two details
        # provide evidence of exclusions/fields; unknown value stays unresolved.
        for card in fresh[:2]:
            time.sleep(4) if fetch is public_fetch else None
            body, truncated = get(card['url'])
            parsed = parse_detail_snapshot(body, fetched_at=int(clock()), truncated=truncated)
            car = parsed['listing']
            if car['id'] != card['id'] or car['url'] != clean_url(card['url']):
                raise ValueError('detail_identity_mismatch')
            result['details'] += 1
            if parsed['summary']['download_truncated']:
                result['filter_unresolved'] += 1; continue
            if car['eligibility_review']['status'] != 'allowed':
                result['excluded'] += 1; continue
            result['eligible'] += 1
            current = permitted(engine, settings, state)
            matches = [filtered(car, row['filters']) for row in current]
            result['filter_candidates'] += any(value is True for value in matches)
            result['filter_unresolved'] += any(value is None for value in matches)
            assessment = estimate_usd(car, [], int(now), comparable_sources=('olx',))
            if assessment['status'] != 'experimental_estimate':
                result['valuation_unconfirmed'] += 1
            # Do not send this car: public HTML does not establish original
            # seller currency, first publication, and independent market peers.
            # Customer deal/threshold policy remains unchanged.
        result['hold_reason'] = 'publication_original_currency_and_market_unconfirmed'
    except Exception as exc:
        if result['status'] == 'observed':
            result['status'] = 'technical_hold'
        result['error_type'] = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc) in {'current_paid_ready_search_required', 'current_access_or_budget_required'}:
            result['hold_reason'] = str(exc)
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if row.result.get('lease') != token:
            return {'status': 'lease_lost'}
        data = copy.deepcopy(row.result)
        data.update(seen=state['seen'], cycles=data['cycles']+1,
                    bytes=data['bytes']+result['bytes'], last=result,
                    lease='', lease_until=0)
        row.result, row.checked_at = data, now
        if result['status'] == 'source_blocked':
            row.status = 'source_blocked'
        db.commit()
    log.info('OLX owner canary %s', json.dumps(result, ensure_ascii=False))
    send_notice(engine, settings, sender, now)
    return result


def status_text(row):
    if not row:
        return 'OLX-тест ще не запускався.'
    last = row.result.get('last', {})
    status = 'finished' if row.status == 'active' and time.time() >= row.result['until'] else row.status
    label = {'active': 'Увімкнено', 'finished': 'Тест завершено',
             'paused_by_owner': 'Вимкнено тобою', 'source_blocked': 'OLX відмовив у доступі'}.get(status, 'Потребує перевірки')
    until = datetime.fromtimestamp(row.result['until'], ZoneInfo('Europe/Kyiv')).strftime('%H:%M')
    return ('🧪 AutoDeal — OLX лише для тебе\n'
        f"Стан: {label} · автоматична зупинка до {until} (Київ)\nПеревірок: {row.result.get('cycles', 0)} · запитів OLX: {row.requests}/{MAX_REQUESTS}\n"
        f"Карток у останній видачі: {last.get('cards', 0)} · деталей: {last.get('details', 0)}\n"
        f"Дозволено за описом: {last.get('eligible', 0)} · відсіяно: {last.get('excluded', 0)}\n"
        f"Кандидатів за фільтром: {last.get('filter_candidates', 0)} · фільтр потребує даних: {last.get('filter_unresolved', 0)}\n"
        'Вигідність і точна новизна ще не підтверджені: ці авто не відправляємо як вигідні.\n'
        'Платних AUTO.RIA-запитів для OLX: 0.\n/olx_status — стан · /olx_stop — вимкнути лише OLX.')


def send_notice(engine, settings, sender, now):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE)
        if not row or row.status != 'active' or not searches(engine, settings, pinned=row.result['search_ids'], now=now):
            return
        if db.get(SourceProbe, NOTICE):
            return
        text = status_text(row)
        state = copy.deepcopy(row.result)
        db.add(SourceProbe(id=NOTICE, status='sending', checked_at=now, requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback(); return
    # Recheck after durable attempt reservation. /stop cannot be bypassed.
    if not permitted(engine, settings, state):
        response = {'not_attempted': True}
    else:
        try:
            response = sender(settings.bot_token, 'sendMessage', {'chat_id': settings.admin_telegram_id, 'text': text})
        except Exception:
            response = {'uncertain': True}
    response = response if isinstance(response, dict) else {'uncertain': True}
    receipt = response.get('result') if isinstance(response.get('result'), dict) else {}
    accepted = response.get('ok') is True and type(receipt.get('message_id')) is int
    with Session(engine) as db:
        notice = db.get(SourceProbe, NOTICE)
        notice.status = 'accepted' if accepted else 'not_attempted' if response.get('not_attempted') else 'uncertain' if response.get('uncertain') else 'rejected'
        notice.result = {'telegram_api_accepted': accepted,
                         'message_id': receipt.get('message_id') if accepted else None}
        status = notice.status
        db.commit()
    log.info('OLX owner canary Telegram receipt %s', json.dumps({'accepted': accepted, 'status': status}))


def handle(engine, settings, event):
    message = event.get('message', {})
    text = message.get('text', '')
    token = text.split()[0] if isinstance(text, str) and text.split() else ''
    command, _, mention = token.partition('@')
    if command not in ('/olx_status', '/olx_stop'):
        return None
    uid = (message.get('from') or {}).get('id')
    chat = message.get('chat') or {}
    if (type(uid) is not int or uid <= 0 or uid != settings.admin_telegram_id or chat.get('type') != 'private' or chat.get('id') != uid
            or (message.get('from') or {}).get('is_bot') or mention and mention.casefold() != telegram_setup.BOT_USERNAME.casefold()):
        return {'ok': True}
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE, with_for_update=True)
        if command == '/olx_stop':
            if row is None:
                row = SourceProbe(id=STATE, status='paused_by_owner', checked_at=time.time(), requests=0,
                    result={'started_at': time.time(), 'until': settings.olx_owner_canary_until,
                            'search_ids': [], 'next_at': 0, 'bytes': 0, 'budget_bytes': 0,
                            'seen': [], 'lease': '', 'lease_until': 0, 'cycles': 0,
                            'stage': 'owner_observation', 'car_sends': 0})
                db.add(row)
            else:
                row.status = 'paused_by_owner'
            db.commit()
        reply = '⏸ OLX вимкнено. AUTO.RIA працює за своїми налаштуваннями.' if command == '/olx_stop' else status_text(row)
        return billing.message(uid, reply)


async def run(engine, settings, stop):
    while not stop.is_set() and enabled(settings):
        try:
            result = await asyncio.to_thread(tick, engine, settings)
            if (result.get('status') == 'source_blocked' or
                    result.get('canary_state') in {'finished', 'paused_by_owner', 'source_blocked'}):
                break
        except Exception as exc:
            log.error('OLX owner canary unavailable (%s)', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
