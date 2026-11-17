"""Use the API's per-user SourceProbe cutoff; keep the existing SQLite queue.

First-seen and head overlap alone cannot prove publication. Rounded source age
labels provide a conservative lower bound; ambiguous/unknown labels fail closed.
"""
from datetime import datetime
import json
import math
import re
import time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from backend.models import SourceProbe


def boundary(db, user_id):
    row = db.get(SourceProbe, f"notification-baseline-{user_id}", populate_existing=True)
    if row is None:
        return None
    value = row.result.get('baseline_at')
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('invalid_notification_baseline')
    return value


def publication_lower_bound(card):
    observed = card.get('publication_observed_at')
    if (type(observed) not in (int, float) or not math.isfinite(observed)
            or not 0 < observed <= time.time() or card.get('promoted') is not False):
        return None
    explicit = card.get('source_add_date')
    if explicit is not None:
        try:
            date = datetime.fromisoformat(explicit.replace('Z','+00:00'))
            if date.tzinfo is None:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', explicit):
                    return None
                early, late = (date.replace(tzinfo=ZoneInfo('Europe/Kyiv'),fold=f) for f in (0,1))
                if early.utcoffset() != late.utcoffset():return None
                date = early
            value = date.timestamp()
            return value if 0 < value <= observed else None
        except (TypeError,ValueError,OverflowError):
            return None
    text = ' '.join((card.get('age_text') or '').casefold().split())
    # Full matches only: an update/refresh label is never publication evidence.
    text = re.sub(r'^додано\s+', '', text)
    match = re.fullmatch(r'(\d{1,5})\s+(секунд[ауи]?|хв(?:илин[ауи]?)?\.?|год(?:ин[ауи]?)?\.?)\s+тому', text)
    if match:
        count, unit = int(match[1]), match[2]
        seconds = 1 if unit.startswith('сек') else 60 if unit.startswith('хв') else 3600
        # Add one whole display unit plus clock/rounding tolerance.
        return observed - (count + 1) * seconds - 60
    if text in ('щойно', 'зараз'):
        return observed - 120
    if text == 'сьогодні':
        return datetime.fromtimestamp(observed, ZoneInfo('Europe/Kyiv')).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
    return None


def allowed(engine, user_id, card):
    with Session(engine) as db:
        cutoff = boundary(db, user_id)
    if cutoff is None:
        return True
    lower = publication_lower_bound(card)
    return lower is not None and cutoff < lower <= time.time()


def candidates(report, cutoff):
    # After an explicit cutoff, include proven post-cutoff cards in the first
    # head too: deployment time must not become a later implicit baseline.
    pool = report['observed_cards'] if cutoff is not None and report.get('reason') == 'head_baseline' else report['newly_observed']
    if cutoff is None:
        return pool
    return [c for c in pool if (lower := publication_lower_bound(c)) is not None and lower > cutoff]


def sync_store(store, cutoff):
    """One atomic local reset on observing the durable server cutoff.

    No sent claims, identity, previews, source budgets or access holds are erased.
    A restart following a crash safely repeats this transaction.
    """
    if cutoff is None:
        return
    row = store.db.execute("SELECT state FROM checkpoints WHERE key='notification-baseline'").fetchone()
    prior = json.loads(row[0])['at'] if row else None
    if prior is not None and prior >= cutoff:
        if prior > cutoff:
            raise ValueError('notification_baseline_moved_backwards')
        return
    with store.db:
        store.db.execute("UPDATE jobs SET state='cancelled',reason='notification_baseline_reset' WHERE state='pending'")
        store.db.execute('DELETE FROM checkpoints')
        store.db.execute("INSERT INTO checkpoints VALUES('notification-baseline',?)", (json.dumps({'at':cutoff}),))
