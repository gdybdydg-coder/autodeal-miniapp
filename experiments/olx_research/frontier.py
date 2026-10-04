"""Persistent local collection frontier; no HTTP, production DB or sender.

All observed candidates are saved before a work cap. An explicit coordinator
claims finite batches. GET retries are bounded; source blocking is persistent.
"""
import hashlib,json,sqlite3,uuid
from .flow import access
from .candidates import select_candidates
from .source_tracking import detail_change,public_detail_url,same_detail_url
from .candidates import filter_reasons


def search_signature(car):
    fields=('title','price','currency','brand','model','year','mileage_km','fuel',
            'transmission','body','engine_cc','field_conflicts')
    return json.dumps({k:car.get(k) for k in fields},sort_keys=True)


class Frontier:
    def __init__(self,path):
        if '://' in str(path):raise ValueError('Explicit isolated SQLite required')
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS research_candidates(
          source TEXT,id TEXT,payload TEXT,state TEXT,attempts INTEGER,next_at INTEGER,
          lease_until INTEGER,token TEXT,reason TEXT,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS research_pages(
          url TEXT,observed_at INTEGER,complete INTEGER,ids TEXT,links TEXT,
          PRIMARY KEY(url,observed_at));
        CREATE TABLE IF NOT EXISTS research_source_holds(source TEXT PRIMARY KEY,reason TEXT);
        CREATE TABLE IF NOT EXISTS research_details(source TEXT,id TEXT,first_seen INTEGER,
          payload TEXT,changes TEXT,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS research_refreshes(source TEXT,id TEXT,payload TEXT,
          PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS research_detail_events(source TEXT,id TEXT,
          observed_at INTEGER,fingerprint TEXT,changes TEXT,
          PRIMARY KEY(source,id,observed_at,fingerprint));
        ''')

    def close(self):self.db.close()

    def record_page(self,url,page):
        summary=page['summary'];now=summary['fetched_at']
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR IGNORE INTO research_pages VALUES(?,?,?,?,?)',
                (url,now,int(not summary['download_truncated']),json.dumps([c['id'] for c in page['listings']]),json.dumps(summary['observed_pagination_links'])))
            for c in page['listings']:
                if c.get('source')!='olx' or not isinstance(c.get('id'),str):raise ValueError('Source identity')
                c={**c,'first_seen_at':now}
                self.db.execute('INSERT OR IGNORE INTO research_candidates VALUES(?,?,?,?,0,0,0,NULL,NULL)',
                                (c['source'],c['id'],json.dumps(c),'pending'))
                old=self.db.execute('SELECT payload,state FROM research_candidates WHERE source=? AND id=?',(c['source'],c['id'])).fetchone()
                previous=json.loads(old['payload'])
                c['first_seen_at']=previous.get('first_seen_at') or previous['checked_at']
                if previous.get('checked_at',0)>=c.get('checked_at',0):continue
                if old['state'] in ('pending','retry'):
                    self.db.execute('UPDATE research_candidates SET payload=? WHERE source=? AND id=?',(json.dumps(c),c['source'],c['id']))
                elif old['state']=='claimed':
                    queued=self.db.execute('SELECT payload FROM research_refreshes WHERE source=? AND id=?',(c['source'],c['id'])).fetchone()
                    if not queued or json.loads(queued[0]).get('checked_at',0)<c['checked_at']:
                        self.db.execute('INSERT OR REPLACE INTO research_refreshes VALUES(?,?,?)',(c['source'],c['id'],json.dumps(c)))
                elif old['state']=='stored':
                    detail=self.db.execute('SELECT payload FROM research_details WHERE source=? AND id=?',(c['source'],c['id'])).fetchone()
                    observed=json.loads(detail[0])['checked_at'] if detail else 0
                    changed=search_signature(previous)!=search_signature(c)
                    self.db.execute('UPDATE research_candidates SET payload=? WHERE source=? AND id=?',(json.dumps(c),c['source'],c['id']))
                    if c['checked_at']>observed and (changed or now-observed>=3600):
                        self.db.execute("UPDATE research_candidates SET state='pending',attempts=0,next_at=?,reason=? WHERE source=? AND id=?",(max(now,observed+60),'search_change' if changed else 'detail_age',c['source'],c['id']))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise

    def claim(self,users,quote,now,*,limit=5,candidate_ids=None):
        if type(limit) is not int or not 1<=limit<=18:raise ValueError('Finite batch required')
        filters=[u['filters'] for u in users if access(u,now)]
        if not filters:return []
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute("UPDATE research_candidates SET state=CASE WHEN attempts<2 THEN 'retry' ELSE 'exhausted' END,next_at=?,token=NULL,reason='interrupted_get' WHERE state='claimed' AND lease_until<=?",(now+60,now))
            rows=self.db.execute("SELECT * FROM research_candidates WHERE state IN ('pending','retry') AND next_at<=? AND attempts<2 AND source NOT IN (SELECT source FROM research_source_holds) ORDER BY rowid",(now,)).fetchall()
            if candidate_ids is not None:
                allowed_ids=set(candidate_ids)
                rows=[r for r in rows if r['id'] in allowed_ids]
            chosen=select_candidates([json.loads(r['payload']) for r in rows],filters,quote,now,limit=limit)
            for r in chosen['reviews']:
                if r['decision']!='selected':
                    self.db.execute('UPDATE research_candidates SET reason=? WHERE source=? AND id=?',(json.dumps(r['reasons']),r['source'],r['id']))
            claimed=[]
            for car in chosen['selected']:
                token=uuid.uuid4().hex
                self.db.execute("UPDATE research_candidates SET state='claimed',attempts=attempts+1,lease_until=?,token=?,reason=NULL WHERE source=? AND id=?",(now+60,token,car['source'],car['id']))
                claimed.append({'token':token,'car':car,'lease_until':now+60})
            self.db.execute('COMMIT');return claimed
        except Exception:self.db.execute('ROLLBACK');raise

    def finish(self,token,status,now,*,car=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute("SELECT * FROM research_candidates WHERE token=? AND state='claimed' AND lease_until>?",(token,now)).fetchone()
            if row is None:raise ValueError('Lease expired or token unknown')
            state='needs_review';reason='parse_or_identity_not_verified';next_at=0
            if status==200 and car is not None:
                original=json.loads(row['payload'])
                if ((car.get('source'),car.get('id'))!=(row['source'],row['id'])
                        or not same_detail_url(car.get('url',''),original.get('url',''))):raise ValueError('Detail identity mismatch')
                old=self.db.execute('SELECT * FROM research_details WHERE source=? AND id=?',(row['source'],row['id'])).fetchone()
                first=old['first_seen'] if old else (original.get('first_seen_at') or original['checked_at'])
                change=detail_change(json.loads(old['payload']) if old else None,car,first_seen=first)
                self.db.execute('INSERT OR REPLACE INTO research_details VALUES(?,?,?,?,?)',(row['source'],row['id'],first,json.dumps(car),json.dumps(change)))
                fingerprint=hashlib.sha256(json.dumps(car,sort_keys=True).encode()).hexdigest()
                self.db.execute('INSERT OR IGNORE INTO research_detail_events VALUES(?,?,?,?,?)',(row['source'],row['id'],car['checked_at'],fingerprint,json.dumps(change)))
                state='stored';reason=None
                queued=self.db.execute('SELECT payload FROM research_refreshes WHERE source=? AND id=?',(row['source'],row['id'])).fetchone()
                if queued:
                    latest=json.loads(queued[0])
                    if latest.get('checked_at',0)>car['checked_at'] and search_signature(latest)!=search_signature(original):
                        state='pending';reason='search_changed_during_detail';next_at=max(now,car['checked_at']+60)
                        self.db.execute('UPDATE research_candidates SET payload=?,attempts=0 WHERE source=? AND id=?',(queued[0],row['source'],row['id']))
                    self.db.execute('DELETE FROM research_refreshes WHERE source=? AND id=?',(row['source'],row['id']))
            elif status in (401,403,429):
                state='source_blocked';reason='http_'+str(status)
                self.db.execute('INSERT OR REPLACE INTO research_source_holds VALUES(?,?)',(row['source'],reason))
            elif status in (404,410):state='unavailable';reason='http_'+str(status)
            elif status is None or status in (408,500,502,503,504):
                state='retry' if row['attempts']<2 else 'exhausted'
                reason='transient_http_'+str(status);next_at=now+60
            self.db.execute('UPDATE research_candidates SET state=?,next_at=?,token=NULL,reason=? WHERE source=? AND id=?',(state,next_at,reason,row['source'],row['id']))
            self.db.execute('COMMIT');return state
        except Exception:self.db.execute('ROLLBACK');raise

    def run_claim(self,claim,current_users,quote,clock,fetch,parse):
        """Injected finite GET; rechecks access/source stop immediately before I/O.

        No default HTTP implementation. Parse must consume the complete body;
        the caller's transport owns timeout and byte limits.
        """
        now=clock();row=self.db.execute("SELECT * FROM research_candidates WHERE token=? AND state='claimed' AND lease_until>?",(claim['token'],now)).fetchone()
        if row is None:raise ValueError('Claim not current')
        candidate=json.loads(row['payload'])
        if not public_detail_url(candidate.get('url')):
            return self.finish(claim['token'],'invalid_url',now)
        blocked=self.db.execute('SELECT 1 FROM research_source_holds WHERE source=?',(row['source'],)).fetchone()
        allowed=any(access(u,now) and filter_reasons(candidate,u['filters'],quote,now)['match'] for u in current_users())
        if blocked or not allowed:
            self.db.execute("UPDATE research_candidates SET state='pending',attempts=attempts-1,token=NULL,reason=? WHERE token=?",('source_paused' if blocked else 'access_changed',claim['token']))
            return 'not_fetched'
        try:
            status,body=fetch(candidate['url'])
            car=parse(body) if status==200 else None
        except (TimeoutError,OSError):status=None;car=None
        except (ValueError,TypeError,KeyError):status='parse_error';car=None
        return self.finish(claim['token'],status,clock(),car=car)

    def snapshot(self):
        counts={r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM research_candidates GROUP BY state')}
        return {'candidate_counts':counts,'observed_pages':self.db.execute('SELECT count(*) FROM research_pages').fetchone()[0],
                'incomplete_page_count':self.db.execute('SELECT count(*) FROM research_pages WHERE complete=0').fetchone()[0],
                'source_holds':[dict(r) for r in self.db.execute('SELECT * FROM research_source_holds')],
                'whole_source_complete':False,'real_telegram_calls':0}
