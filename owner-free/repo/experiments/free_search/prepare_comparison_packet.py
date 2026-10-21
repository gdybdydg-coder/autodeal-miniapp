"""Prepare fresh owner-only one-shot, no source or Telegram calls here."""
from pathlib import Path
import sys,json,time,base64,zlib,hashlib
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from experiments.free_search.owner_comparison_delivery import send_comparison
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_stage_pipeline import StagePipeline
from experiments.free_search.owner_card_transport import TelegramCard
from experiments.free_search.owner_session_runtime import session
from experiments.free_search.owner_night_runtime import LOCK

def main():
    root=ROOT.parent
    capture=json.loads((root/sys.argv[1]).read_text(encoding='utf-8'));ident=capture['listing_id']
    review=json.loads((root/sys.argv[2]).read_text(encoding='utf-8'))
    search_id=int(sys.argv[3])
    saved=json.loads((root/'comparison-current-access-20261021.json').read_text(encoding='utf-8'))
    search=next(s for s in saved['searches'] if s['id']==search_id and s['enabled'])
    with session(LOCK,60):
        obs=Observations(root/'night-20261020.owner-observations.sqlite3')
        queue=CandidateQueue(root/'night21-candidates.sqlite3');pipeline=StagePipeline(queue,obs)
        try:
            row=pipeline._current(ident,time.time());page,record=pipeline._page(ident,time.time())
            review.update(listing_id=ident,listing_url=page.url,detail_sha256=hashlib.sha256(page.html.encode()).hexdigest())
            if row['stage']=='detail':pipeline.capture(ident,capture,time.time())
            if queue.get(ident)['stage']=='range':
                rule=pipeline.qualify(ident,search['filters'].get('minDiscount',15),time.time())
                if not rule['eligible']:raise ValueError('owner_discount_threshold')
            modules={name:Path(m.__file__).read_text(encoding='utf-8') for name,m in list(sys.modules.items())
                if name.startswith('experiments.free_search.') and getattr(m,'__file__',None) and m.__file__.endswith('.py')}
            packet={'modules':modules,'ident':ident,'search_id':search_id,'review':review,
                'observations':base64.b64encode(obs.db.serialize()).decode(),
                'candidate':queue.db.execute('SELECT * FROM candidates WHERE id=?',(ident,)).fetchone(),
                'events':queue.db.execute('SELECT * FROM stage_events WHERE candidate=? ORDER BY id',(ident,)).fetchall()}
            packed=base64.b64encode(zlib.compress(json.dumps(packet).encode())).decode()
            bootstrap=(root/'comparison-remote-bootstrap.py').read_text(encoding='utf-8')
            code='PACKET=(\n'+''.join(repr(packed[i:i+3000])+'\n' for i in range(0,len(packed),3000))+')\n'+bootstrap
            (root/'comparison-remote-command.txt').write_text("python - <<'AUTODEAL_COMPARE_END'\n"+code+"\nAUTODEAL_COMPARE_END",encoding='utf-8')
            print(json.dumps({'ready':True,'id':ident,'age':time.time()-row['first_seen'],'packet_chars':len(packed)}))
        finally:queue.close();obs.close()
if __name__=='__main__':main()
