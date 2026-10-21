"""Local owner-only adapter. No provider client, schema migration or bot worker.

The caller supplies an existing approved DB connection and Telegram transport.
Secrets never enter browser/intake jobs or snapshots. Production sender retains
current owner, epoch, percentage, independent claim and chat-spacing checks.
"""
import json
import os
from pathlib import Path
import time
import uuid
import threading
from .owner_access import current_searches
from .owner_comparison_delivery import send_comparison
from .owner_automated_review import automated_review


def atomic_json(path,value):
    path=Path(path);temp=path.with_suffix(path.suffix+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x',encoding='utf-8') as out:
            json.dump(value,out,ensure_ascii=False,allow_nan=False);out.flush();os.fsync(out.fileno())
        # Windows readers can briefly deny delete sharing. Keep the old complete
        # lease until replacement succeeds; never fall back to in-place writes.
        for attempt in range(10):
            try:
                os.replace(temp,path);break
            except PermissionError:
                if attempt==9:raise
                time.sleep(.02*(attempt+1))
    finally:
        temp.unlink(missing_ok=True)


def child_environment(env):
    # Allowlist, not a list of today's known secret variable names.
    allowed={'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','LOCALAPPDATA','APPDATA',
             'USERPROFILE','PROGRAMFILES','PROGRAMFILES(X86)','COMSPEC','PATHEXT',
             'HOME','LANG','AUTODEAL_OWNER_STATE_DIR','AUTODEAL_OWNER_BROWSER_BINARIES'}
    return {k:v for k,v in env.items() if k.upper() in allowed}


class LeasePublisher:
    """Keep the bounded access lease alive while one Telegram POST is pending.

    No DB/transport work is done on this thread. Access time is supplied by the
    main thread's actual check and is NEVER advanced by this heartbeat.
    """
    def __init__(self,path,value,*,interval=.5):
        self.path,self.value,self.interval=path,value,interval
        self.halt=threading.Event();self.error=None;self.thread=None

    def start(self):
        atomic_json(self.path,self.value())
        def run():
            while not self.halt.wait(self.interval):
                try:atomic_json(self.path,self.value())
                except Exception as exc:self.error=type(exc).__name__;self.halt.set()
        self.thread=threading.Thread(target=run,name='owner-lease',daemon=True);self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread is not None:self.thread.join(timeout=3)
        if self.thread is not None and self.thread.is_alive():raise RuntimeError('lease_publisher_not_stopped')


class OwnerBridge:
    def __init__(self,engine,settings,pipeline,transport,workspace,*,clock=time.time):
        self.engine,self.settings,self.pipeline=engine,settings,pipeline
        self.transport,self.root,self.clock=transport,Path(workspace),clock

    def refresh(self):
        now=self.clock();searches,reason=current_searches(self.engine,self.settings,now)
        data={'at':now,'status':'readonly_ok' if reason=='eligible' else 'blocked',
          'reason':reason,'access':dict.fromkeys(('ready','entitled','approved'),reason=='eligible'),
          'searches':[{'id':s.search_id,'epoch':s.epoch,'fingerprint':s.fingerprint,
                      'filters':s.filters,'enabled':True} for s in searches]}
        atomic_json(self.root/'comparison-current-access-20261021.json',data)
        if reason=='eligible':
            from .owner_pair_observer import observe_pairs
            try: pairs=observe_pairs(self.engine,self.settings.admin_telegram_id,clock=self.clock)
            except Exception as exc:
                # A reporting failure cannot grant access or cause a send retry.
                pairs={'at':self.clock(),'state':'unavailable','error_type':type(exc).__name__}
            atomic_json(self.root/'owner-service-pairs.json',pairs)
            # Reporting reads paid receipts only; it cannot seed the free queue.
            if self.pipeline.queue.db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_dynamic'").fetchone():
                from .owner_paid_coverage import observe_paid_coverage
                try:coverage=observe_paid_coverage(self.engine,self.settings.admin_telegram_id,
                             self.pipeline.queue,self.pipeline.observations,clock=self.clock)
                except Exception as exc:
                    coverage={'at':self.clock(),'state':'unavailable','error_type':type(exc).__name__}
                atomic_json(self.root/'owner-paid-coverage.json',coverage)
        return data

    def deliver_ready(self,limit=5):
        results=[]
        for row in self.pipeline.queue.pending(limit,stage='qualified'):
            ident=row['id'];file=self.root/'continuous-browser-outbox'/(ident+'.result.json')
            try:
                result=json.loads(file.read_text(encoding='utf-8'))
                if result.get('listing_id')!=ident or result.get('state')!='captured':
                    raise ValueError('browser_result_identity')
                page,_=self.pipeline._page(ident,self.clock())
                review=automated_review(result.get('visible_review'),page,self.clock())
                searches,reason=current_searches(self.engine,self.settings,self.clock())
                current=next((s for s in searches if s.search_id in result.get('search_ids',[])),None)
                if reason!='eligible' or current is None:raise ValueError('current_owner_search_missing')
                receipt=send_comparison(self.engine,self.settings,self.pipeline,ident,current.search_id,
                    self.transport,condition_review=review,enabled=True,clock=self.clock)
                if receipt.get('reason')=='chat_interval':continue
                # Only a durable DB claim authorizes a terminal local delivery.
                state=receipt['state']
                if state in ('sent','uncertain','cancelled'):
                    self.pipeline.queue.advance(ident,'qualified','claimed',self.clock(),{'receipt':receipt})
                    self.pipeline.queue.advance(ident,'claimed',state,self.clock(),{'receipt':receipt})
                else:
                    self.pipeline.queue.advance(ident,'qualified','rejected',self.clock(),{'receipt':receipt})
            except (OSError,ValueError,KeyError,TypeError) as exc:
                receipt={'listing_id':ident,'state':'blocked','reason':str(exc)[:160]}
                self.pipeline.queue.advance(ident,'qualified','rejected',self.clock(),{'receipt':receipt})
            results.append(receipt)
            with (self.root/'owner-service-deliveries.jsonl').open('a',encoding='utf-8') as out:
                out.write(json.dumps(receipt,ensure_ascii=False)+'\n')
        return results
