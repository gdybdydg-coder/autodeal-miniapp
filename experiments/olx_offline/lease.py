"""Pure cross-run lease transitions; caller MUST publish via GitHub non-force CAS.

Construct commit with the EXACT read branch head as parent. Only the successful
non-force ref update owns the lease. A failed race never permits doing the work.
This module has no GitHub token, connection, timer or background executor.
"""
from copy import deepcopy
from .run_guard import HARD_STOP


def claim(state, token, now, *, seconds=1800):
    if not isinstance(token,str) or len(token)<16:
        raise ValueError('Unique per-run token required')
    if type(now) is not int or now>=HARD_STOP or state.get('stop') is not False:
        raise ValueError('Night window closed or STOP set')
    if type(seconds) is not int or not 0<seconds<=1800:
        raise ValueError('Lease at most 30 minutes')
    lease=state.get('lease')
    if lease and lease['expires_at']>now:
        raise ValueError('Another run owns the lease')
    result=deepcopy(state)
    result['lease']={'token':token,'claimed_at':now,'expires_at':min(now+seconds,int(HARD_STOP))}
    return result


def owns(state, token, now):
    lease=state.get('lease') or {}
    return (state.get('stop') is False and now<HARD_STOP and lease.get('token')==token
            and lease.get('claimed_at',now+1)<=now<lease.get('expires_at',0))


def release(state, token):
    if (state.get('lease') or {}).get('token')!=token:
        raise ValueError('Cannot release another run lease')
    result=deepcopy(state);result['lease']=None
    return result
