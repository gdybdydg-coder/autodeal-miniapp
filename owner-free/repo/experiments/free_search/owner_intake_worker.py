"""Credential-free child of the local owner supervisor; never starts itself."""
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.free_search.owner_continuous_intake import run_owned
if __name__=='__main__':
    token,pages=sys.argv[1:]
    result=run_owned(token,time.time()+300,max_requests=100,pages=int(pages))
    print(result['reason'])
    raise SystemExit(0 if result['reason'] in ('session_bound_reached','source_retry_wait','batch_deadline','run_request_bound') else 2)
