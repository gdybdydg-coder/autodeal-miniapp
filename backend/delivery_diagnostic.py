"""Bounded Telegram response categories; never retain raw errors or credentials."""
import time

from .models import SourceProbe

REASONS = {
    'wrong type of the web page content': 'photo_content_type',
    'failed to get http url content': 'photo_fetch_failed',
    'wrong file identifier/http url specified': 'photo_url_invalid',
    'webpage_curl_failed': 'photo_fetch_failed',
    'webpage_media_empty': 'photo_empty',
    'image_process_failed': 'photo_processing_failed',
    'photo_invalid_dimensions': 'photo_dimensions',
    'file is too big': 'photo_size',
    'chat not found': 'chat_not_found',
    'bot was blocked': 'bot_blocked',
    'user is deactivated': 'user_deactivated',
}


def summary(result, method):
    value = result.get('description')
    description = value.lower() if isinstance(value, str) else ''
    code = result.get('error_code')
    message = result.get('result')
    accepted = (result.get('ok') is True and isinstance(message, dict)
                and type(message.get('message_id')) is int)
    return {'method': method if method in {'sendPhoto', 'sendMessage'} else 'unknown',
            'accepted': accepted,
            'error_code': code if type(code) is int and code in {400, 401, 403, 404, 429, 500, 502, 503} else None,
            'reason': ('accepted' if accepted else next(
                (label for pattern, label in REASONS.items() if pattern in description),
                'explicit_rejection' if result.get('ok') is False else 'uncertain'))}


def receipt(db, delivery_id):
    row = db.get(SourceProbe, 'telegram-delivery-result-v1-' + str(delivery_id))
    return row.result if row else None


def record(db, delivery, car, result, logger):
    # Normal successes already have a durable receipt. Record rejections and
    # fallback outcomes, including uncertain outcomes after a rejected photo.
    raw = result.get('_delivery_attempts')
    attempts = []
    if isinstance(raw, list) and 1 <= len(raw) <= 2:
        for item in raw:
            if not isinstance(item, dict):
                continue
            attempts.append({
                'method': item.get('method') if item.get('method') in {'sendPhoto', 'sendMessage'} else 'unknown',
                'accepted': item.get('accepted') is True,
                'error_code': item.get('error_code') if type(item.get('error_code')) is int and
                    item['error_code'] in {400, 401, 403, 404, 429, 500, 502, 503} else None,
                'reason': item.get('reason') if item.get('reason') in
                    {*REASONS.values(), 'accepted', 'explicit_rejection', 'uncertain'} else 'uncertain'})
    attempts = attempts or [summary(result, 'unknown')]
    key = 'telegram-delivery-result-v1-' + str(delivery.id)
    row = db.get(SourceProbe, key)
    if delivery.state == 'sent' and len(attempts) == 1 and row is None:
        return
    if row is None:
        row = SourceProbe(id=key, requests=0)
        db.add(row)
    row.status, row.checked_at = delivery.state, time.time()
    row.result = {'attempts': attempts, 'state': delivery.state}
    # Only normalized categories above, never Telegram's raw description.
    logger.info('Telegram delivery result source_id=%s state=%s attempts=%s',
                car.source_id, delivery.state, attempts)
