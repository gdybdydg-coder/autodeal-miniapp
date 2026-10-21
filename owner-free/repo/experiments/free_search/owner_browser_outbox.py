"""Durable immediate CUA handoff. Publishing never authorizes Telegram."""
import json
import os
from pathlib import Path
from .owner_browser_handoff import digest, next_ticket, accept_capture
from .owner_comparison_batch import matches
from .visible_adapter import parse_visible_facts


class BrowserOutbox:
    def __init__(self, path):
        self.path=Path(path)
        if self.path.is_symlink():raise ValueError('outbox_symlink')
        self.path.mkdir(exist_ok=True)

    def publish(self, work):
        envelope=work['envelope'];ticket=envelope['ticket'];ident=ticket['listing_id']
        if not isinstance(ident,str) or not ident.isascii() or not ident.isdigit():
            raise ValueError('invalid_outbox_identity')
        if envelope['ticket_sha256']!=digest(ticket):raise ValueError('invalid_outbox_digest')
        target=self.path/(ident+'.job.json')
        if target.exists():
            old=json.loads(target.read_text(encoding='utf-8'))
            if (old['envelope']['ticket']['first_seen']!=ticket['first_seen'] or
                old['envelope']['ticket']['detail_sha256']!=ticket['detail_sha256']):
                raise ValueError('outbox_identity_changed')
            return False  # Neither restart nor a later observation refreshes a ticket.
        temporary=target.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as out:
            out.write(json.dumps(work,ensure_ascii=False,allow_nan=False));out.flush();os.fsync(out.fileno())
        os.replace(temporary,target)
        return True

    def recover(self, pipeline, searches, now, deadline):
        added=[]
        for row in pipeline.queue.pending(100,stage='detail'):
            if (self.path/(row['id']+'.job.json')).exists():continue
            try:
                page,record=pipeline._page(row['id'],now);record['listing_id']=row['id']
                selected=[s['id'] for s in searches if matches(record,parse_visible_facts(page.html,row['id']).category,s['filters'])]
                ticket=next_ticket(pipeline,now,deadline,listing_id=row['id'])
                if ticket and selected and self.publish({'envelope':ticket,'search_ids':selected}):added.append(row['id'])
            except ValueError:
                continue  # Stale/changed evidence is not renewed into new evidence.
        return added

    def consume(self, pipeline, searches, now, *, limit=5):
        """Apply captured evidence immediately; qualification is never delivery."""
        if not searches:return []
        db=pipeline.queue.db
        db.execute('CREATE TABLE IF NOT EXISTS browser_outbox_results (id TEXT PRIMARY KEY,processed_at REAL,evidence TEXT)')
        results=[]
        for path in sorted(self.path.glob('*.result.json')):
            ident=path.name.split('.')[0]
            if not ident.isascii() or not ident.isdigit() or db.execute('SELECT 1 FROM browser_outbox_results WHERE id=?',(ident,)).fetchone():continue
            if len(results)>=limit:break
            try:
                result=json.loads(path.read_text(encoding='utf-8'))
                if result.get('listing_id')!=ident:raise ValueError('browser_result_identity')
                if result.get('state')!='captured':
                    result={'listing_id':ident,'state':result.get('state'),'send_authorized':False}
                else:
                    envelope=result['envelope']
                    if envelope['ticket']['listing_id']!=ident:raise ValueError('browser_result_identity')
                    selected=next((s for s in searches if s['id'] in result.get('search_ids',[])),None)
                    if selected is None:continue
                    result=accept_capture(pipeline,envelope,now,research_percent=selected['filters'].get('minDiscount',15))
            except (ValueError,KeyError,TypeError) as exc:
                result={'listing_id':ident,'state':'blocked','reason':str(exc)[:120],'send_authorized':False}
            db.execute('INSERT INTO browser_outbox_results VALUES(?,?,?)',(ident,now,json.dumps(result,default=str)))
            results.append(result)
        return results
