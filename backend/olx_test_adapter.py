"""Explicit isolated OLX integration; never registered by app/factory or workers.

Only a caller-labelled local SQLite test engine is accepted. The current backend
purchase/search policy is reused without changing its tables, filters or options.
No Settings, environment, HTTP client, AUTO.RIA provider or Telegram sender exists
here. Old searches never opt in automatically. Production launch is separate work.
"""
from dataclasses import dataclass, field
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from . import paid_source_access
from .models import Filters, Search, User
from experiments.olx_offline.integration import filters_from_backend
from experiments.olx_offline.pipeline import filtered
from experiments.olx_offline.cards import render_card


TEST_ENGINE = 'autodeal_olx_isolated_test'


@dataclass
class Switch:
    enabled: bool = False
    opted_searches: set[int] = field(default_factory=set)
    page_budget: int = 2
    row_budget: int = 200
    queue_capacity: int = 1000

    def __post_init__(self):
        for value, limit in ((self.page_budget, 20), (self.row_budget, 1000),
                             (self.queue_capacity, 50000)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError('Explicit bounded OLX test resources required')
        if any(type(sid) is not int or sid <= 0 for sid in self.opted_searches):
            raise ValueError('Explicit existing search IDs required')


class LocalReceipts:
    """In-memory proof of local invocation; never a Telegram delivery receipt."""
    def __init__(self):
        self.messages = []

    def __call__(self, uid, card):
        self.messages.append({'user': str(uid), 'card': card, 'transport': 'local_fake'})
        return True


class IsolatedOLX:
    def __init__(self, engine, pipeline, *, switch=None, clock):
        if (engine.dialect.name != 'sqlite'
                or engine.get_execution_options().get(TEST_ENGINE) is not True):
            raise ValueError('Only an explicitly labelled local SQLite test engine is accepted')
        if not paid_source_access.strict(engine):
            raise ValueError('Confirmed-purchase policy must be enabled before OLX tests')
        self.engine, self.pipeline, self.clock = engine, pipeline, clock
        self.switch = switch or Switch()
        self.receipts = LocalReceipts()
        self.search_holds = {}

    def _searches(self, uid=None):
        """SELECT-only, current purchase + ready + enabled + separate opt-in."""
        if not self.switch.enabled or not self.switch.opted_searches:
            return []
        now = self.clock()
        if uid is None:
            self.search_holds = {}
        with Session(self.engine) as db:
            query = select(Search).join(User, User.id == Search.user_id).where(
                Search.id.in_(self.switch.opted_searches), Search.enabled.is_(True),
                User.ready.is_(True))
            if uid is not None:
                query = query.where(Search.user_id == int(uid))
            result = []
            for search in db.scalars(query.order_by(Search.id)):
                if not paid_source_access.allowed(db, search.user_id, now):
                    continue
                # Validate the same public filter model; do not mutate its JSON.
                try:
                    filters = Filters.model_validate(search.filters).canonical()
                    bridge = filters_from_backend(filters, currency='USD')
                except (ValueError, TypeError):
                    self.search_holds[search.id] = 'stored_filter_mapping_unavailable'
                    continue
                self.search_holds.pop(search.id, None)
                result.append({'id': str(search.user_id), 'search_id': search.id,
                    'search_fingerprint': Filters.model_validate(search.filters).fingerprint(),
                    'source': 'OLX', 'paid': True, 'ready': True, 'enabled': True,
                    'stopped': False, 'filters': bridge['filters'],
                    'min_discount': bridge['min_discount'], 'only_deals': filters['onlyDeals']})
            return result

    def planning(self):
        rows = self._searches()
        groups = {hashlib.sha256(json.dumps(row['filters'], sort_keys=True).encode()).hexdigest()
                  for row in rows}
        return {'eligible_searches': len(rows), 'distinct_filters': len(groups),
                'distinct_recipients': len({row['id'] for row in rows}),
                'held_searches': len(self.search_holds),
                'transport': 'local_fake', 'olx_enabled': self.switch.enabled}

    def current_search(self, uid, car):
        rows = self._searches(uid)
        result = []
        for row in rows:
            comparable = dict(car)
            region = comparable.get('region')
            if isinstance(region, str):
                comparable['region'] = region.casefold().removesuffix(' область').strip()
            if filtered(comparable, row['filters']) is True:
                result.append(row)
        return result

    def allowed(self, uid):
        return bool(self._searches(uid))

    def run_cycle(self, fetch, comparisons, *, before_dispatch=None, fx_quote=None):
        """Full shared fixture intake -> queue -> local fake receipt.

        No eligible search means no source callback. Each page rechecks purchase
        and source switch; access is checked again before queue and transport.
        Unknown market/publication remains visible in saved research decisions.
        """
        try:
            plan = self.planning()
        except SQLAlchemyError:
            return {'status': 'access_temporarily_unavailable', 'phase': 'planning',
                    'source_calls': 0, 'local_receipts': 0}
        if not plan['eligible_searches']:
            return {'status': 'disabled_or_no_eligible_search', 'plan': plan,
                    'source_calls': 0, 'local_receipts': 0}
        calls = 0

        def guarded_fetch(cursor):
            nonlocal calls
            try:
                available = self._searches()
            except SQLAlchemyError as exc:
                raise ValueError('current_access_read_unavailable') from exc
            if not available:
                raise ValueError('current_paid_OLX_search_required')
            calls += 1
            return fetch(cursor)

        comparisons = [car for car in comparisons if car.get('source') == 'olx']
        collected = self.pipeline.collect(guarded_fetch, int(self.clock()),
            page_budget=self.switch.page_budget, row_budget=self.switch.row_budget,
            fx_quote=fx_quote)
        try:
            users = self._searches()
        except SQLAlchemyError:
            return {'status': 'access_temporarily_unavailable', 'phase': 'queue',
                    'plan': plan, 'collection': collected, 'source_calls': calls,
                    'local_receipts': 0}
        queued = self.pipeline.enqueue(users, comparisons, int(self.clock()),
            capacity=self.switch.queue_capacity, olx_enabled=self.switch.enabled)
        if before_dispatch is not None:
            before_dispatch()
        initial = len(self.receipts.messages)
        delivered = self.pipeline.deliver_fake(self.receipts, self.allowed,
            now=self.clock, olx_enabled=lambda: self.switch.enabled,
            current_search=self.current_search,
            renderer=lambda car, assessment: render_card(car, assessment, now=self.clock()))
        return {'status': 'isolated_cycle', 'plan': plan, 'source_calls': calls,
                'collection': collected, 'queue': queued, 'delivery': delivered,
                'local_receipts': len(self.receipts.messages) - initial,
                'actual_OLX_requests': 0, 'actual_Telegram_sends': 0,
                'AUTO_RIA_requests': 0}
