"""Durable isolated replay. No HTTP implementation, bot token or backend import."""
import json,sqlite3
from decimal import Decimal
from urllib.parse import urlsplit
from .candidates import filter_reasons,select_candidates
from .fx_policy import normalize
from .valuation import estimate
from .observations import asking_price_reasons,enrich
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.html_snapshot import clean_url


def access(user,now):
    return bool(user.get('confirmed_at') is not None and user['confirmed_at']<=now
                and now<user.get('expires_at',0) and user.get('ready') is True
                and user.get('search_enabled') is True and not user.get('stopped'))


class ReplayStore:
    def __init__(self,path):
        if '://' in str(path):raise ValueError('Explicit isolated SQLite path required')
        self.db=sqlite3.connect(path)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS snapshots(source TEXT,id TEXT,payload TEXT,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS previews(uid TEXT,source TEXT,id TEXT,payload TEXT,PRIMARY KEY(uid,source,id));
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,calls INTEGER,bytes_reserved INTEGER,blocked INTEGER,cursor INTEGER);
        CREATE TABLE IF NOT EXISTS detail_attempts(run TEXT,source TEXT,id TEXT,status TEXT,PRIMARY KEY(run,source,id));''')

    def close(self):self.db.close()

    def ingest(self,car):
        if car.get('source')!='olx' or not isinstance(car.get('id'),str):raise ValueError('OLX identity required')
        key=(car['source'],car['id']);row=self.db.execute('SELECT payload FROM snapshots WHERE source=? AND id=?',key).fetchone()
        if row:
            old=json.loads(row[0])
            if old.get('checked_at',0)>car.get('checked_at',0):return False
            if old.get('checked_at')==car.get('checked_at') and old!=car:
                car={**car,'research_field_conflicts':car.get('research_field_conflicts',[])+['same_time_identity']}
        self.db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)',(*key,json.dumps(car)))
        self.db.commit();return True

    def cars(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM snapshots ORDER BY source,id')]

    def collect(self,run_id,cards,users,quote,now,fetch_detail,*,page_complete=False,checkpoint=None,max_calls=9,parser=None):
        """A one-page snapshot never proves the whole source is complete.

        All users share one detail cache and one finite run budget. Partial page
        parsing may preserve verified detail records but cannot advance cursor.
        """
        if type(max_calls) is not int or not 1<=max_calls<=18:raise ValueError('bounded request budget')
        self.db.execute('INSERT OR IGNORE INTO runs VALUES(?,0,0,0,NULL)',(run_id,));self.db.commit()
        filters=[u['filters'] for u in users if access(u,now)]
        old=[(c['source'],c['id']) for c in self.cars() if 0<=now-c.get('checked_at',0)<900]
        old += self.db.execute('SELECT source,id FROM detail_attempts WHERE run=?',(run_id,)).fetchall()
        chosen=select_candidates(cards,filters,quote,now,already_seen=old,limit=max_calls)
        receipts=[]
        for candidate in chosen['selected']:
            try:
                raw_url=candidate.get('url','')
                if not isinstance(raw_url,str):raise ValueError('url_type')
                url=urlsplit(raw_url)
                valid_url=(candidate.get('source')=='olx' and bool(clean_url(raw_url))
                           and not (url.username or url.password or url.port)
                           and url.path.startswith(('/d/uk/obyavlenie/','/d/obyavlenie/'))
                           and url.path.endswith('.html'))
            except (ValueError,TypeError):valid_url=False
            if not valid_url:
                receipts.append({'source':candidate.get('source'),'id':candidate.get('id'),'status':'invalid_detail_url'})
                continue
            row=self.db.execute('SELECT calls,bytes_reserved,blocked FROM runs WHERE id=?',(run_id,)).fetchone()
            if row[0]>=max_calls or row[1]+4194304>67108864 or row[2]:break
            self.db.execute('UPDATE runs SET calls=calls+1,bytes_reserved=bytes_reserved+4194304 WHERE id=?',(run_id,))
            self.db.execute('INSERT INTO detail_attempts VALUES(?,?,?,?)',(run_id,candidate['source'],candidate['id'],'reserved'));self.db.commit()
            status='unknown'
            try:
                code,body=fetch_detail(candidate['url'])
                if code in (401,403,429):
                    self.db.execute('UPDATE runs SET blocked=1 WHERE id=?',(run_id,));status='source_blocked_'+str(code)
                elif code!=200:status='individual_http_'+str(code)
                elif not isinstance(body,bytes) or len(body)>2097152:status='detail_size_invalid'
                else:
                    car=parser(body) if parser else enrich(body,parse_detail_snapshot(body,fetched_at=now,truncated=False))['listing']
                    if (car['id']!=candidate['id'] or car['source']!=candidate['source']
                            or clean_url(car.get('url',''))!=clean_url(candidate['url'])):raise ValueError('identity_mismatch')
                    self.ingest(car);status='stored'
            except (ValueError,TypeError,KeyError,TimeoutError,OSError) as exc:status=type(exc).__name__
            self.db.execute('UPDATE detail_attempts SET status=? WHERE run=? AND source=? AND id=?',(status,run_id,candidate['source'],candidate['id']));self.db.commit()
            receipts.append({'source':candidate['source'],'id':candidate['id'],'status':status})
            if status.startswith('source_blocked'):break
        pending=self.db.execute("SELECT count(*) FROM detail_attempts WHERE run=? AND status!='stored'",(run_id,)).fetchone()[0]
        if (page_complete and checkpoint is not None and not pending
                and not any(r['decision']=='deferred' for r in chosen['reviews'])
                and all(r['status']=='stored' for r in receipts)):
            # Only caller-proven page checkpoint, never a claim of all-source coverage.
            self.db.execute('UPDATE runs SET cursor=? WHERE id=? AND blocked=0',(checkpoint,run_id));self.db.commit()
        return {'selection':chosen['reviews'],'receipts':receipts,'whole_source_complete':False,
                'run':dict(zip(('calls','reserved_bytes','blocked','page_checkpoint'),self.db.execute('SELECT calls,bytes_reserved,blocked,cursor FROM runs WHERE id=?',(run_id,)).fetchone()))}

    def prepare(self,users,quote,now,*,current_access=None):
        """Saves local previews only. There is intentionally no send method."""
        cars=self.cars();report={'simulated_only':True,'actual_telegram_calls':0,'prepared':[],'held':[],'denied_users':[]}
        eligible=[]
        for u in users:
            if access(u,now) and (current_access is None or current_access(u['id'])):eligible.append(u)
            else:report['denied_users'].append(u['id'])
        assessments={c['id']:estimate(c,cars,quote,now) for c in cars} if eligible else {}
        for c in cars:
            price=normalize(c,quote,now);why=asking_price_reasons(c)
            if price['status']!='ready':why.append(price['reason'])
            for u in eligible:
                key=(str(u['id']),c['source'],c['id'])
                f=filter_reasons(c,u['filters'],quote,now)
                if why or not f['match']:
                    report['held'].append({'uid':u['id'],'id':c['id'],'reasons':why+f['reasons']});continue
                if not access(u,now) or current_access is not None and not current_access(u['id']):
                    report['held'].append({'uid':u['id'],'id':c['id'],'reasons':['access_changed']});continue
                a=assessments[c['id']]
                discount=a.get('methods',{}).get('median',{}).get('discount_percent')
                threshold=Decimal(str(u.get('min_discount',15)))
                if not threshold.is_finite():raise ValueError('finite threshold required')
                card={'label':'ЛОКАЛЬНИЙ ПЕРЕГЛЯД OLX — НЕ НАДІСЛАНО','uid':str(u['id']),'source_id':c['id'],
                      'title':c.get('title'),'url':c.get('url'),'photos':c.get('photos',[]),'price':price,
                      'market':a,'new_publication_verified':c.get('publication_verified') is True,
                      'threshold':str(threshold),'threshold_passed_in_experiment':discount is not None and Decimal(discount)>=threshold,
                      'unknown_optional_filters':f['unknown'],'delivery_authorized':False,
                      'message':'Вигідність не підтверджена' if a['status']=='profitability_unconfirmed' else 'Експериментальна оцінка цін пропозицій; точність ще не підтверджена'}
                inserted=self.db.execute('INSERT OR IGNORE INTO previews VALUES(?,?,?,?)',(*key,json.dumps(card))).rowcount
                self.db.commit()
                if inserted:report['prepared'].append(card)
        return report
