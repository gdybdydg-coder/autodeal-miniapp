"""Owner-only sorted RIA worker. Production DB is read-only; no provider client.

Uses the existing Mini App filters and /stop flag, local durable checkpoints and
at-most-once Telegram attempts. Credentials never reach the browser subprocess.
"""
import argparse
import hashlib
import json
import os
import random
import signal
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from backend.listing_card import listing_caption
from backend.models import Filters, Search, User, SourceCache, MonitorWatch
from experiments.free_search.owner_local_bridge import child_environment, atomic_json
from experiments.free_search.owner_sorted_photo import SortedTelegramPhoto, notification
from experiments.free_search import owner_peer_delivery as peer
from experiments.free_search.owner_pilot import PublicPage, _page, _filters
from experiments.free_search.owner_public_mean import browser_range_quote
from experiments.free_search.owner_price_rule import evaluate
from experiments.free_search.visible_adapter import parse_visible_facts
from experiments.free_search.filter_gate import PublicEvidence, gate
from decimal import Decimal


def source_url(db, filters):
    """Only cached dictionary IDs, never a request to a paid API."""
    def resolve(route, names):
        if not names:
            return []
        digest = hashlib.sha256(json.dumps([route, {}], sort_keys=True).encode()).hexdigest()
        row = db.get(SourceCache, digest)
        if row is None:
            # Public search checkbox IDs observed 2026-11-01; not paid lookups.
            public={'states':{'Вінницька':1,'Житомирська':2,'Тернопільська':3,'Хмельницька':4,
                'Львівська':5,'Чернігівська':6,'Харківська':7,'Сумська':8,'Рівненська':9,'Київська':10,
                'Дніпропетровська':11,'Донецька':13,'Запорізька':14,'Івано-Франківська':15,
                'Кіровоградська':16,'Волинська':18,'Полтавська':20,'Закарпатська':22,
                'Черкаська':24,'Чернівецька':25},
                'categories/1/marks':{'Audi':6},'categories/1/marks/6/models':{'A6':49}}
            if route in public and all(n in public[route] for n in names):
                return [public[route][n] for n in names]
            raise ValueError('dictionary_missing:' + route)
        values = []
        for name in names:
            found = {int(i['value']) for i in row.payload['items']
                     if i['name'].strip().casefold() == name.strip().casefold()}
            if len(found) != 1:
                raise ValueError('dictionary_name_unknown:' + name)
            values.append(found.pop())
        return values
    q = dict(indexName='auto,order_auto,newauto_search', category_id=1,
             order_by=7, abroad=2, custom=1, page=0, countpage=50, currency=1)
    if filters.brand:
        brand = resolve('categories/1/marks', [filters.brand])[0]
        q['marka_id[0]'] = brand
        if filters.model:
            q['model_id[0]'] = resolve(f'categories/1/marks/{brand}/models', [filters.model])[0]
    elif filters.model:
        raise ValueError('model_without_brand')
    for i, region in enumerate(resolve('states', [r.removesuffix(' область') for r in filters.regions])):
        q[f'state[{i}]'], q[f'city[{i}]'] = region, 0
    for field, low, high in [('price','price_ot','price_do'),('year','s_yers[0]','po_yers[0]'),('mileage','raceFrom','raceTo')]:
        span = getattr(filters, field)
        for value, key in [(span.from_, low),(span.to, high)]:
            if value is not None:
                if value != int(value):
                    raise ValueError('fractional_filter_not_supported')
                q[key] = int(value)
    for field, route, key in [('body','categories/1/bodystyles','bodystyle'),('fuel','type','type'),('transmission','categories/1/gearboxes','gearbox')]:
        for i, value in enumerate(resolve(route, getattr(filters,field))):
            q[f'{key}[{i}]'] = value
    return 'https://auto.ria.com/uk/search/?' + urlencode(q)


def current(engine, owner):
    with Session(engine) as db:
        user = db.get(User, owner)
        if not user or not user.ready:
            return []
        result = []
        for search, watch in db.execute(select(Search, MonitorWatch).join(
                MonitorWatch, MonitorWatch.search_id == Search.id).where(
                Search.user_id == owner, Search.enabled.is_(True))):
            try:
                f = Filters.model_validate(search.filters)
                if f.fingerprint() != search.fingerprint:
                    raise ValueError('filter_fingerprint_mismatch')
                url = source_url(db, f)
            except ValueError:
                print(json.dumps(dict(event='invalid_search_filter',search_id=search.id)),flush=True)
                continue
            result.append(dict(id=search.id, name=search.name, filters=f.canonical(),
                fingerprint=search.fingerprint, epoch=watch.epoch, url=url))
        return result


def search_key(s):
    return f"{s['id']}-{s['epoch']}-{s['fingerprint']}"


def qualify(raw, filters, now):
    page = PublicPage(raw['listing_id'], raw['url'], raw['html'], raw['received_at'])
    row = _page(page, now, 300)
    visible = parse_visible_facts(page.html, page.listing_id)
    evidence = PublicEvidence(listing_id=page.listing_id, publication_proven=False, active=True,
        category=visible.category, price_usd=Decimal(str(row['price'])), region=visible.region,
        year=row['year'], body=row['body'], fuel=row['fuel'], transmission=row['transmission'],
        mileage_km=row['mileage_km'], brand=row['brand'], model=row['model'])
    blocked = [b for b in gate(evidence, _filters(filters)).blockers if b != 'publication_not_proven']
    # Explicit requested fields must be proven; missing values never pass.
    for field in ('body','fuel','transmission'):
        if filters.get(field) and not row.get(field):
            blocked.append(field + '_unknown')
    for field, actual in [('year',row['year']),('mileage',row['mileage_km'])]:
        if any(v is not None for v in filters.get(field,{}).values()) and actual is None:
            blocked.append(field + '_unknown')
    if blocked:
        raise ValueError('filter_mismatch:' + ','.join(blocked))
    quote = browser_range_quote(raw['capture'],page,now)
    rule = evaluate(quote['lower_usd'],row['price'],filters.get('minDiscount',15))
    if not rule['eligible']:
        raise ValueError('below_saved_discount')
    def dollars(value):
        return '$' + f'{Decimal(str(value)):,.2f}'.rstrip('0').rstrip('.').replace(',', ' ').replace('.', ',')
    def percent(value):
        return format(Decimal(str(value)).normalize(),'f').replace('.', ',')
    reference = Decimal(rule['reference_usd'])
    saving = reference - Decimal(str(row['price']))
    benefit = f"{Decimal(rule['discount_percent']):.1f}".rstrip('0').rstrip('.').replace('.', ',')
    pricing = [f"📊 Діапазон AUTO.RIA: {dollars(quote['lower_usd'])}–{dollars(quote['upper_usd'])}",
               f"Орієнтир (нижня межа − 5%): {dollars(reference)}",
               f"🔥 Вигода: {benefit}% ({dollars(saving)} нижче орієнтира)",
               f"Поріг підписки: {percent(rule['required_discount_percent'])}%",
               "Технічний стан потребує перевірки."]
    text = listing_caption(row['brand'], row['model'], row['year'], row['fuel'],
                           row['transmission'], row['mileage_km'], visible.region,
                           row['price'], pricing)
    return text, {**quote, **rule}


class Store:
    def __init__(self, path, owner):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS identity(owner INTEGER PRIMARY KEY);'
            'CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY,state TEXT);'
            'CREATE TABLE IF NOT EXISTS jobs(key TEXT,id TEXT,url TEXT,first_seen REAL,state TEXT,reason TEXT,PRIMARY KEY(key,id));'
            'CREATE TABLE IF NOT EXISTS detail_delays(id TEXT PRIMARY KEY, next_at REAL NOT NULL);'
            'CREATE TABLE IF NOT EXISTS peer_previews(key TEXT,id TEXT,payload TEXT,PRIMARY KEY(key,id));'
            'CREATE TABLE IF NOT EXISTS sent(id TEXT PRIMARY KEY,state TEXT,message_id INTEGER,evidence TEXT);')
        rows = self.db.execute('SELECT owner FROM identity').fetchall()
        if rows and rows != [(owner,)]:
            self.db.close()
            raise ValueError('owner_state_mismatch')
        self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(owner,)); self.db.commit()

    def accept(self, key, report, keep_previews=False):
        if not report.get('complete') or not report.get('checkpoint'):
            raise ValueError('incomplete_scan')
        with self.db:
            for c in report['newly_observed']:
                self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)',
                    (key,c['id'],c['url'],time.time(),'pending',''))
                if keep_previews and c.get('preview'):
                    self.db.execute('INSERT OR REPLACE INTO peer_previews VALUES(?,?,?)',
                        (key,c['id'],json.dumps(c,ensure_ascii=False)))
            self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)',
                (key,json.dumps(report['checkpoint'])))


def main():
    args = argparse.ArgumentParser()
    args.add_argument('action', choices=['inspect','once','run'])
    args.add_argument('--state-dir', required=True)
    a = args.parse_args()
    owner = int(os.environ['AUTODEAL_OWNER_ID'])
    if owner <= 0 or str(owner) != os.environ.get('SUBSCRIPTION_EXPECTED_ADMIN_ID'):
        raise ValueError('owner_identity_mismatch')
    engine = create_engine(os.environ['AUTODEAL_OWNER_DATABASE_URL'],pool_size=1,max_overflow=0,
        connect_args={'connect_timeout':5,'options':'-c statement_timeout=5000 -c default_transaction_read_only=on'})
    root = Path(a.state_dir).resolve(); root.mkdir(parents=True,exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: (root/'stop').write_text('platform_shutdown', encoding='utf-8'))
    if a.action == 'inspect':
        atomic_json(root/'searches.json',current(engine,owner)); print('owner_searches_saved'); return
    lock = root/'worker.lock'
    handle = lock.open('x'); handle.write(str(os.getpid())); handle.flush()
    store = Store(root/'state.sqlite3',owner)
    recipients = [owner]
    extra = os.environ.get('AUTODEAL_SECOND_CHAT_ID')
    if extra:
        if not extra.isdecimal() or int(extra) <= 0 or int(extra) == owner:
            raise ValueError('invalid_second_recipient')
        recipients.append(int(extra))
    stores = {owner: store}
    for uid in recipients[1:]:
        stores[uid] = Store(root/f'recipient-{uid}.sqlite3', uid)
    transports = {uid: SortedTelegramPhoto(os.environ['AUTODEAL_OWNER_TELEGRAM_TOKEN'], uid)
                  for uid in recipients}
    node = os.environ.get('AUTODEAL_OWNER_NODE') or shutil.which('node')
    if not node: raise ValueError('node_missing')
    def browser(url, group=None, ident=None, catalogue=False):
        probe_file=root/'cache'/'access-comparison-v1.json'
        compare=group and os.environ.get('AUTODEAL_COMPARE_HTTP')=='1' and not probe_file.exists()
        ledger=root/'cache'/'transport.json'
        if ledger.exists():
            hold=json.loads(ledger.read_text(encoding='utf-8')).get('hold_until',0)
            if hold>time.time():raise ValueError('source_hold_until:'+str(int(hold)))
        output=root/'browser-result.json'; state=root/'browser-state.json'
        output.unlink(missing_ok=True); state.unlink(missing_ok=True)
        cmd=[node,str(REPO/'experiments/free_search/sorted_monitor.mjs'),'--url',url,
             '--output',str(output),'--cache-dir',str(root/'cache'),'--daily-limit','2400']
        if catalogue: cmd += ['--peer-catalogue','true']
        elif ident: cmd += ['--detail-id',ident]
        else:
            if compare: cmd += ['--compare-http','true']
            checkpoints = {}
            for search in group:
                key = search_key(search)
                row = stores[search['recipient']].db.execute('SELECT state FROM checkpoints WHERE key=?', (key,)).fetchone()
                checkpoints[key] = json.loads(row[0]) if row else None
            atomic_json(state, checkpoints)
            cmd += ['--state', str(state), '--max-pages', '10', '--grouped-heads', 'true']
        p=subprocess.run(cmd,cwd=REPO,env=child_environment(os.environ),capture_output=True,
            timeout=1500,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if compare and probe_file.exists():
            metric('access_comparison', search_id=group[0]['id'], **json.loads(probe_file.read_text(encoding='utf-8')))
        if p.returncode:
            # Fixed parser diagnostics only, no credentials or request tokens.
            error=p.stderr.decode('utf-8',errors='replace')[:500]
            if error.strip()=='source_denied_or_challenge' and ledger.exists():
                saved=json.loads(ledger.read_text(encoding='utf-8'))
                metric('source_pause',until=saved.get('hold_until'),http_status=(saved.get('last_failure') or {}).get('status'),
                       stage='detail' if ident else 'search',cause=saved.get('hold_reason'))
            raise ValueError('browser_failed:'+error)
        return json.loads(output.read_text(encoding='utf-8'))
    def metric(event, **values):
        print(json.dumps(dict(event=event,at=time.time(),**{k:v for k,v in values.items() if k not in ('ids','reason')})),flush=True)
        with (root/'timings.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(dict(event=event,at=time.time(),**values),ensure_ascii=False)+'\n')
    try:
        # Recently reviewed public examples use the same owner ledger as live alerts.
        for packet in json.loads(os.environ.get('OWNER_REVIEWED_PEERS','[]')):
            if not 0 <= time.time()-packet['at'] <= 3600:continue
            match=next((s for s in current(engine,owner) if s['id']==packet['search'] and s['fingerprint']==packet['fingerprint']),None)
            if match:
                subject=packet['subject']
                try:
                    card,evidence=peer.make_card(subject,packet['peers'],match['filters'])
                    state,mid=peer.deliver(store.db,transports[owner],owner,subject['id'],card,evidence)
                    metric('owner_peer_delivery',listing_id=subject['id'],state=state,message_id=mid)
                except ValueError as error:
                    metric('owner_peer_skipped',cause=str(error)[:80])
        # Restarting the worker must not turn a five-minute schedule into a burst.
        status_file=root/'status.json'
        if a.action=='run' and status_file.exists():
            prior=json.loads(status_file.read_text(encoding='utf-8'))
            prior['pid']=os.getpid()
            if prior.get('cadence')!='owner_fast_v1':
                prior['next_poll']=min(prior.get('next_poll',time.time()),prior.get('at',time.time())+120)
                ledger=root/'cache'/'transport.json'
                if ledger.exists():prior['next_poll']=max(prior['next_poll'],json.loads(ledger.read_text(encoding='utf-8')).get('hold_until',0))
            atomic_json(status_file,prior)
            metric('schedule_restored', next_poll=prior.get('next_poll'), configured_chats=len(recipients))
            ledger=root/'cache'/'transport.json'
            if ledger.exists():
                transport_state=json.loads(ledger.read_text(encoding='utf-8'))
                if transport_state.get('hold_until',0)>time.time():
                    failure=transport_state.get('last_failure') or {}
                    cause=transport_state.get('hold_reason')
                    # A previous denial can remain in the ledger after an unrelated HTTP failure.
                    denial=cause=='source_denied_or_challenge'
                    metric('source_pause',cause=cause,until=transport_state['hold_until'],
                           http_status=failure.get('status') if denial else None,
                           captcha=failure.get('visible_captcha') if denial else None,
                           valid_content=failure.get('valid_content') if denial else None)
            while time.time()<prior.get('next_poll',0) and not (root/'stop').exists():
                time.sleep(1)
        last_polled = {}
        last_detail = {}
        while not (root/'stop').exists():
            cycle_started=time.time()
            searches = [dict(search, recipient=uid) for uid in recipients for search in current(engine, uid)]
            statuses=[]
            groups = {}
            for search in searches:
                groups.setdefault(search['url'], []).append(search)
            # Reserve navigation budget for detail pages and rotate independent searches.
            for url, group in sorted(groups.items(), key=lambda item: last_polled.get(item[0], 0))[:2]:
                if (root/'stop').exists() or time.time() - cycle_started >= 60:
                    break
                poll_started = time.time()
                last_polled[url] = poll_started
                atomic_json(root/'status.json', dict(at=poll_started, pid=os.getpid(), phase='collecting',
                    current_search=group[0]['id'], searches=statuses, paid_api_calls=0,
                    next_poll=cycle_started+120, cadence='owner_fast_v1'))
                try:
                    results = browser(url, group=group)
                except Exception as exc:
                    # Only emit fixed parser diagnostics, never browser stack traces or URLs.
                    code = str(exc).removeprefix('browser_failed:').partition('\n')[0].partition(':')[0]
                    cause = code if re.fullmatch('[a-z_0-9]{1,60}', code) else 'browser_error'
                    for search in group:
                        statuses.append(dict(search_id=search['id'], state='blocked', reason=str(exc)[:500]))
                        metric('poll_error', search_id=search['id'], reason=str(exc)[:500], cause=cause)
                    continue
                for search in group:
                    key = search_key(search)
                    result = results[key]
                    if not result['complete']:
                        metric('poll_error', search_id=search['id'], reason=result['reason'], cause=result['reason'])
                        continue
                    stores[search['recipient']].accept(key, result, search['recipient']==owner)
                    new_ids = [card['id'] for card in result['newly_observed']]
                    metric('poll', search_id=search['id'], started_at=poll_started,
                           duration=time.time()-poll_started, pages=result['pages_read'], new_ids=new_ids,
                           ids=[c['id'] for c in result['observed_cards']],
                           unclassified_ids=[c['id'] for c in result['unclassified_after_overlap']])
                    statuses.append(dict(search_id=search['id'], pages=result['pages_read'],
                        new=len(new_ids), total=result['pages'][0]['total'], state='checked'))
            details = {}
            failed_details = {}
            for recipient_store in stores.values():
                with recipient_store.db:
                    recipient_store.db.execute('DELETE FROM detail_delays WHERE next_at<=?',(time.time(),))
            # Discover both searches before slow detail/valuation work.
            for s in sorted(searches, key=lambda search: last_detail.get(search_key(search), 0)):
                uid = s['recipient']
                recipient_store = stores[uid]
                key=search_key(s)
                for ident,url,first_seen in recipient_store.db.execute(
                    "SELECT id,url,first_seen FROM jobs WHERE key=? AND state='pending' "
                    "AND id NOT IN (SELECT id FROM detail_delays) ORDER BY first_seen DESC,rowid ASC LIMIT 3",(key,)).fetchall():
                    if (root/'stop').exists() or time.time()-cycle_started>=85: break
                    last_detail[key] = time.time()
                    state,reason='rejected',''
                    try:
                        if recipient_store.db.execute('SELECT 1 FROM sent WHERE id=?',(ident,)).fetchone():
                            raise ValueError('previous_send_attempt')
                        if time.time()-first_seen>1800: raise ValueError('candidate_expired')
                        if not any(search_key(v)==key for v in current(engine,uid)):
                            raise ValueError('search_stopped_or_changed')
                        if uid==owner:
                            stored=recipient_store.db.execute('SELECT payload FROM peer_previews WHERE key=? AND id=?',(key,ident)).fetchone()
                            if not stored:raise ValueError('peer_preview_missing')
                            subject=peer.preview_subject(json.loads(stored[0]))
                            pool=peer.pool_for(subject,root/'cache',lambda u:browser(u,catalogue=True))
                            card,evidence=peer.make_card(subject,peer.reference(subject,pool),s['filters'])
                            if not any(search_key(v)==key for v in current(engine,uid)):raise ValueError('search_stopped_or_changed')
                            state,mid=peer.deliver(recipient_store.db,transports[uid],uid,ident,card,evidence)
                            with recipient_store.db:
                                recipient_store.db.execute('UPDATE jobs SET state=? WHERE key=? AND id=?',(state,key,ident))
                            metric('owner_peer_delivery',listing_id=ident,state=state,message_id=mid)
                            continue
                        if ident not in details:
                            if ident in failed_details:
                                raise ValueError(failed_details[ident])
                            try:
                                details[ident] = browser(url, ident=ident)
                            except ValueError as error:
                                failed_details[ident] = str(error)
                                raise
                        raw = details[ident]
                        text,evidence=qualify(raw,s['filters'],time.time())
                        if not any(search_key(v)==key for v in current(engine,uid)):
                            raise ValueError('search_stopped_or_changed')
                        with recipient_store.db:
                            recipient_store.db.execute('INSERT INTO sent VALUES(?,?,?,?)',(ident,'attempting',None,json.dumps(evidence)))
                        response=transports[uid](uid,notification(raw,text))
                        message=response.get('result',{})
                        state='sent' if response.get('ok') is True and type(message.get('message_id')) is int else 'uncertain'
                        with recipient_store.db:
                            recipient_store.db.execute('UPDATE sent SET state=?,message_id=? WHERE id=?',(state,message.get('message_id'),ident))
                    except Exception as exc:
                        reason=str(exc)[:500]
                        # Keep unsent candidates pending on temporary source access failures.
                        # Their original first_seen and expiry are never reset.
                        if reason.startswith(('source_hold_until:','browser_failed:')) and not reason.startswith('browser_failed:listing_gone'):
                            state='pending'
                    with recipient_store.db:
                        recipient_store.db.execute('UPDATE jobs SET state=?,reason=? WHERE key=? AND id=?',(state,reason,key,ident))
                        if state=='pending':
                            recipient_store.db.execute('INSERT OR REPLACE INTO detail_delays VALUES(?,?)',(ident,time.time()+300))
                    cause=reason.removeprefix('browser_failed:').partition(':')[0].strip()
                    metric('candidate',search_id=s['id'],listing_id=ident,state=state,reason=reason,
                        cause=cause if re.fullmatch(r'[A-Za-z0-9_.]{1,64}',cause) else 'other',
                        first_seen=first_seen,discovery_to_decision_seconds=time.time()-first_seen)
                    if state=='pending':break
            # Start-to-start target, rather than work time plus a fixed sleep.
            delay=max(10,random.randint(110,120)-(time.time()-cycle_started))
            with store.db:
                store.db.execute("DELETE FROM peer_previews WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.key=peer_previews.key AND j.id=peer_previews.id AND j.state='pending' AND j.first_seen>?)",(time.time()-1800,))
            ledger=root/'cache'/'transport.json'
            if ledger.exists():
                hold=json.loads(ledger.read_text(encoding='utf-8')).get('hold_until',0)
                delay=max(delay,int(hold-time.time())+1)
            atomic_json(root/'status.json',dict(at=time.time(),pid=os.getpid(),searches=statuses,paid_api_calls=0,
                phase='waiting',next_poll=time.time()+delay,
                cadence='owner_fast_v1',
                sent=store.db.execute("SELECT count(*) FROM sent WHERE state='sent'").fetchone()[0]))
            atomic_json(root/'deliveries.json',[dict(id=row[0],state=row[1],message_id=row[2])
                for row in store.db.execute('SELECT id,state,message_id FROM sent').fetchall()])
            metric('cycle',duration=time.time()-cycle_started,next_poll=time.time()+delay)
            if a.action=='once': break
            for _ in range(int(delay)+1):
                if (root/'stop').exists(): break
                time.sleep(1)
    finally:
        for recipient_store in stores.values():
            recipient_store.db.close()
        engine.dispose();handle.close();lock.unlink()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        safe=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('dictionary_', 'filter_', 'owner_', 'model_', 'fractional_')) else None
        print(json.dumps({'state':'blocked','error_type':type(exc).__name__,'reason':safe,
            'location':[(Path(f.filename).name,f.name,f.lineno) for f in frames[-3:]]}));sys.exit(1)
