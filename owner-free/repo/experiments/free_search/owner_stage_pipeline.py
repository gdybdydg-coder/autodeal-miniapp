"""Local discovery/detail/range bridge. Qualification is NOT send authorization.

Public pages and CUA captures are persisted with their original timestamps.
Restart never refreshes their freshness or first_seen. No network or production
writes here: collectors supply fetched pages, browser supplies visible captures.
"""
from dataclasses import asdict
import hashlib
from .owner_pilot import PublicPage, _page
from .owner_public_mean import browser_quote
from .owner_price_rule import evaluate
from .visible_adapter import parse_visible_facts


class StagePipeline:
    def __init__(self, queue, observations):
        self.queue, self.observations = queue, observations

    def ingest(self, cards, now):
        accepted=[]
        self.skipped=[]
        for card in cards:
            signal=self.observations.signal(card.listing_id,now)
            if signal['reason']=='eligible':
                try:
                    if self.queue.discover(card.listing_id,card.url,signal['first_seen']):
                        accepted.append(card.listing_id)
                except ValueError as exc:
                    self.skipped.append({'listing_id':card.listing_id,'reason':str(exc)})
        return accepted

    def _current(self, ident, now):
        row=self.queue.get(ident)
        if row is None: raise ValueError('candidate_not_discovered')
        signal=self.observations.signal(ident,now)
        if signal['reason']!='eligible': raise ValueError(signal['reason'])
        if signal['first_seen']!=row['first_seen']: raise ValueError('observation_identity_changed')
        return row

    def detail(self, page, now):
        row=self._current(page.listing_id,now)
        if page.url!=row['url']: raise ValueError('candidate_url_changed')
        record=_page(page,now,300)
        if parse_visible_facts(page.html,page.listing_id).category!='passenger':
            raise ValueError('passenger_category_not_proven')
        self.queue.advance(page.listing_id,'discovered','detail',now,
            {'page':asdict(page),'html_sha256':hashlib.sha256(page.html.encode()).hexdigest()})
        return record

    def _page(self, ident, now):
        evidence=self.queue.evidence(ident,'detail')
        if not evidence: raise ValueError('detail_evidence_missing')
        page=PublicPage(**evidence['page'])
        if (page.listing_id!=ident or page.url!=self.queue.get(ident)['url'] or
                hashlib.sha256(page.html.encode()).hexdigest()!=evidence['html_sha256']):
            raise ValueError('detail_evidence_changed')
        if parse_visible_facts(page.html,page.listing_id).category!='passenger':
            raise ValueError('passenger_category_not_proven')
        return page,_page(page,now,300)

    def capture(self, ident, capture, now):
        self._current(ident,now)
        page,_=self._page(ident,now)
        source=browser_quote(capture,page,now)
        if source.get('basis')!='auto_ria_public_market_range' or 'lower_usd' not in source:
            raise ValueError('public_lower_bound_unverified')
        self.queue.advance(ident,'detail','range',now,{'capture':capture,'source':source})
        return source

    def qualify(self, ident, saved_percent, now):
        self._current(ident,now)
        page,record=self._page(ident,now)
        evidence=self.queue.evidence(ident,'range')
        if not evidence: raise ValueError('range_evidence_missing')
        source=browser_quote(evidence['capture'],page,now)
        if source!=evidence['source']: raise ValueError('range_evidence_changed')
        rule=evaluate(source['lower_usd'],record['price'],saved_percent)
        self.queue.advance(ident,'range','qualified' if rule['eligible'] else 'rejected',now,
            {'rule':rule,'capture_sha256':source['capture_sha256'],
             'send_authorized':False,'reason':'await_current_owner_sender' if rule['eligible'] else 'owner_discount_threshold'})
        return rule

    def expire(self, now, limit=100):
        expired=[]
        for row in self.queue.pending(limit):
            signal=self.observations.signal(row['id'],now)
            if signal['reason']!='eligible':
                self.queue.advance(row['id'],row['stage'],'rejected',now,{'reason':signal['reason']})
                expired.append(row['id'])
        return expired
