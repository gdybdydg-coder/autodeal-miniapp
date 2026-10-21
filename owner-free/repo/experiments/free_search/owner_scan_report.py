"""Read-only summary of local live evidence; no source or Telegram calls."""
import json
from pathlib import Path
import sqlite3
import time


def summarize(workspace, now=None):
    workspace=Path(workspace)
    now=time.time() if now is None else now
    reports=[json.loads(line) for line in (workspace/'live-scan-20261020.jsonl').read_text().splitlines() if line.strip()]
    receipts=[r for report in reports for r in report['receipts']]
    durations=sorted(r['completed_at']-r['started_at'] for r in receipts
                     if r.get('status')==200 and r.get('error') is None)
    def percentile(p):
        if not durations:return None
        rank=(len(durations)-1)*p
        lo=int(rank);hi=min(lo+1,len(durations)-1)
        return durations[lo]+(durations[hi]-durations[lo])*(rank-lo)
    with sqlite3.connect((workspace/'night-20261020.owner-observations.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        scans=[dict(id=r[0],phase=r[1],baseline=bool(r[2]),elapsed_seconds=r[4]-r[3],reason=r[5],unique_ids=r[6])
               for r in db.execute('SELECT id,phase,baseline,started,last_at,reason,(SELECT count(*) FROM scan_seen WHERE scan_id=scans.id) FROM scans ORDER BY id')]
        new=[dict(listing_id=r[0],first_seen=r[1],age_seconds=now-r[1],publication_at=None)
             for r in db.execute('SELECT id,first_seen FROM observations WHERE baseline=0 ORDER BY first_seen,id')]
    return dict(as_of=now,scans=scans,new_observations=new,
                successful_http_seconds=dict(n=len(durations),p50=percentile(.5),p95=percentile(.95),max=max(durations,default=None)),
                publication_to_discovery_seconds=None,discovery_to_telegram_seconds=None,
                whole_market_recall=None,budget=reports[-1]['budget'],
                paid_calls=sum(r['paid_calls'] for r in reports),telegram_calls=sum(r['telegram_calls'] for r in reports))


if __name__=='__main__':
    print(json.dumps(summarize(Path(__file__).resolve().parents[3]),ensure_ascii=False,indent=2))
