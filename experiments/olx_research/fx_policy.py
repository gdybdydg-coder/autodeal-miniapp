"""Proposed offline FX policy. Injected transports only; never reads env/secrets.

One selection is shared by a whole comparison cohort. This is not a trading
quote. Provider failures and a switch to a bank-derived midpoint are explicit.
"""
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import sqlite3

from experiments.olx_offline.fx import (KYIV, amount, instant, nbu_url,
    parse_nbu_quote, DECIMAL_PRECISION)

MONO_URL = 'https://api.monobank.ua/bank/currency'
POLICY = 'olx-research-fx-v1'


def privat_url(day):
    return 'https://api.privatbank.ua/p24api/exchange_rates?json&date=' + day.strftime('%d.%m.%Y')


def _json(body):
    if not isinstance(body, (str, bytes)) or not 0 < len(body) <= 262144:
        raise ValueError('fx_payload_size')
    return json.loads(body, parse_float=Decimal)


@dataclass(frozen=True)
class Quote:
    rate: Decimal
    source: str
    kind: str
    effective_date: date
    fetched_at: datetime
    published_at: datetime | None = None
    buy: Decimal | None = None
    sell: Decimal | None = None

    def validate(self, now):
        current = instant(now)
        today = current.astimezone(KYIV).date()
        if (not isinstance(self.rate, Decimal) or not self.rate.is_finite()
                or not Decimal('10') <= self.rate <= Decimal('200')):
            return 'fx_rate_anomaly'
        if self.kind == 'nbu_official':
            if self.source not in (nbu_url(self.effective_date), privat_url(self.effective_date)):
                return 'fx_source_invalid'
            if self.buy is not None or self.sell is not None:
                return 'fx_official_with_spread'
        elif self.kind == 'bank_derived_midpoint':
            if self.source != MONO_URL or self.published_at is None:
                return 'fx_source_invalid'
            if (not isinstance(self.buy, Decimal) or not isinstance(self.sell, Decimal)
                    or not self.buy.is_finite() or not self.sell.is_finite()
                    or not 0 < self.buy <= self.sell
                    or self.rate != (self.buy+self.sell)/2
                    or (self.sell-self.buy)/self.rate > Decimal('.10')):
                return 'fx_bank_spread_invalid'
            if self.effective_date != instant(self.published_at).astimezone(KYIV).date():
                return 'fx_date_conflict'
        else:
            return 'fx_type_unsupported'
        # No implicit carry into Monday, holidays or an arbitrary next weekday.
        weekend = today.weekday() in (5, 6)
        days = (today-self.effective_date).days
        if days < 0:
            return 'fx_future_date'
        if days > (today.weekday()-4 if weekend else 0):
            return 'fx_effective_date_expired'
        age = (current-instant(self.fetched_at)).total_seconds()
        if age < 0:
            return 'fx_future_fetch'
        if age > (86400 if weekend else 21600):
            return 'fx_cache_expired'
        if self.published_at is not None:
            source_age = (current-instant(self.published_at)).total_seconds()
            if source_age < 0 or source_age > (72*3600 if weekend else 24*3600):
                return 'fx_source_timestamp_expired'
        return None

    def payload(self):
        return {'rate':str(self.rate), 'source':self.source, 'kind':self.kind,
                'effective_date':self.effective_date.isoformat(),
                'fetched_at':instant(self.fetched_at).isoformat(),
                'published_at':instant(self.published_at).isoformat() if self.published_at else None,
                'buy':str(self.buy) if self.buy is not None else None,
                'sell':str(self.sell) if self.sell is not None else None,
                'units':'UAH per 1 USD', 'policy':POLICY,
                'transaction_channel':'not_established' if self.kind=='bank_derived_midpoint' else 'official_reference_not_transaction_rate'}

    @property
    def basis(self):
        p=self.payload(); p.pop('fetched_at')
        return hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()

    @classmethod
    def restore(cls, p):
        if p.get('units') != 'UAH per 1 USD' or p.get('policy') != POLICY:
            raise ValueError('fx_units_or_policy')
        return cls(Decimal(p['rate']),p['source'],p['kind'],date.fromisoformat(p['effective_date']),
                   instant(p['fetched_at']),instant(p['published_at']) if p.get('published_at') else None,
                   Decimal(p['buy']) if p.get('buy') is not None else None,
                   Decimal(p['sell']) if p.get('sell') is not None else None)


def parse_quote(provider, body, now):
    at=instant(now); today=at.astimezone(KYIV).date()
    if provider=='nbu':
        q=parse_nbu_quote(body,requested_date=today,fetched_at=at)
        result=Quote(q.uah_per_usd,q.source,'nbu_official',q.effective_date,at)
    elif provider=='privat_nbu':
        p=_json(body)
        if (not isinstance(p,dict) or p.get('bank')!='PB' or p.get('baseCurrency')!=980
                or p.get('baseCurrencyLit')!='UAH' or p.get('date')!=today.strftime('%d.%m.%Y')):
            raise ValueError('fx_privat_header')
        rows=p.get('exchangeRate')
        if not isinstance(rows,list): raise ValueError('fx_rows')
        rows=[r for r in rows if isinstance(r,dict) and r.get('currency')=='USD']
        if len(rows)!=1 or rows[0].get('baseCurrency')!='UAH': raise ValueError('fx_pair')
        buy,sell=amount(rows[0].get('purchaseRateNB')),amount(rows[0].get('saleRateNB'))
        if buy!=sell: raise ValueError('fx_official_rates_disagree')
        result=Quote(buy,privat_url(today),'nbu_official',today,at)
    elif provider=='monobank_mid':
        p=_json(body)
        if not isinstance(p,list): raise ValueError('fx_rows')
        rows=[r for r in p if isinstance(r,dict) and r.get('currencyCodeA')==840 and r.get('currencyCodeB')==980]
        if len(rows)!=1 or type(rows[0].get('date')) is not int: raise ValueError('fx_pair_or_timestamp')
        r=rows[0]; buy,sell=amount(r.get('rateBuy')),amount(r.get('rateSell'))
        published=instant(r['date'])
        result=Quote((buy+sell)/2,MONO_URL,'bank_derived_midpoint',published.astimezone(KYIV).date(),at,published,buy,sell)
    else:
        raise ValueError('fx_provider_unknown')
    reason=result.validate(at)
    if reason: raise ValueError(reason)
    return result


class RateBook:
    """Explicit local durable shared cache; one bounded selection per cohort.

    Fresh selection is retained 5 min, including failure. Each provider is called
    at most once in that interval; re-opening the SQLite file preserves cooldown.
    Transport must return (HTTP status, body) and enforce its own timeout/body cap.
    No transport is installed by this module or by the replay CLI.
    """
    def __init__(self,path):
        if '://' in str(path): raise ValueError('Local SQLite path required')
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS research_fx (id INTEGER PRIMARY KEY, selected_at REAL, payload TEXT, trace TEXT)')
        self.db.commit()

    def close(self): self.db.close()

    def select(self,now,transport):
        at=instant(now); seconds=at.timestamp(); trace=[]; cached=None
        row=self.db.execute('SELECT selected_at,payload,trace FROM research_fx WHERE id=1').fetchone()
        if row:
            try: cached=Quote.restore(json.loads(row[1])) if row[1] else None
            except (TypeError,ValueError,KeyError): cached=None
            if 0 <= seconds-row[0] < 300:
                cached=cached if cached and cached.validate(at) is None else None
                return cached, [{'event':'shared_cooldown_cache','selected_at':row[0]}]+json.loads(row[2])
        urls={'nbu':nbu_url(at.astimezone(KYIV).date()),'privat_nbu':privat_url(at.astimezone(KYIV).date()),'monobank_mid':MONO_URL}
        selected=None
        for provider,url in urls.items():
            try:
                code,body=transport(url)
                if code!=200: raise ValueError('http_'+str(code))
                candidate=parse_quote(provider,body,at)
                if cached and cached.validate(at) is None and abs(candidate.rate/cached.rate-1)>Decimal('.15'):
                    raise ValueError('fx_jump_over_15_percent')
                selected=candidate
                trace.append({'provider':provider,'status':'selected','kind':candidate.kind,'source':url})
                break
            except (ValueError,TypeError,KeyError,ArithmeticError,TimeoutError,OSError) as exc:
                trace.append({'provider':provider,'status':'unavailable','reason':str(exc)[:100] if isinstance(exc,ValueError) else type(exc).__name__})
        if selected is None and cached and cached.validate(at) is None:
            selected=cached;trace.append({'event':'fallback_saved_quote','kind':cached.kind})
        if selected is None: trace.append({'event':'dependent_uah_work_pending'})
        # Keep an older quote for later valid fallback; do not refresh its fetched_at.
        keep=selected or cached
        self.db.execute('INSERT OR REPLACE INTO research_fx VALUES(1,?,?,?)',
                        (seconds,json.dumps(keep.payload()) if keep else None,json.dumps(trace)))
        self.db.commit()
        return selected,trace


def normalize(raw,quote,now,*,amount_basis='source_display'):
    """Preserve observed input and unknown seller-origin currency distinctly."""
    currency=raw.get('input_currency',raw.get('currency'))
    value=raw.get('input_amount',raw.get('price'))
    p={'status':'pending','reason':None,'input_amount':str(value) if value is not None else None,
       'input_currency':currency,'amount_basis':raw.get('amount_basis',amount_basis),
       'seller_original_currency':raw.get('seller_original_currency'),
       'usd_amount':None,'fx':None,'fx_basis':None,'usd_range_from_fx':None}
    try: original=amount(value,canonical='input_amount' in raw)
    except (ValueError,ArithmeticError):p['reason']='amount_invalid';return p
    p['input_amount']=str(original)
    if currency=='USD':p.update(status='ready',reason='already_usd',usd_amount=str(original));return p
    if currency!='UAH':p['reason']='currency_unsupported';return p
    reason=quote.validate(now) if isinstance(quote,Quote) else 'fx_missing'
    if reason:p['reason']=reason;return p
    with localcontext() as ctx:
        ctx.prec=DECIMAL_PRECISION
        p.update(status='ready',reason='converted_uah',usd_amount=str(original/quote.rate),fx=quote.payload(),fx_basis=quote.basis)
        if quote.kind=='bank_derived_midpoint':
            p['usd_range_from_fx']={'low':str(original/quote.sell),'high':str(original/quote.buy)}
    return p
