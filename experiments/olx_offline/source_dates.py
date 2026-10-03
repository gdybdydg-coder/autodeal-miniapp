"""Allowlisted dates from an observed public OLX HTML assignment; offline only.

JSON is decoded, JavaScript is NEVER evaluated. Source names are observations,
not an official promise that createdTime is the first publication timestamp.
Seller, contact, VIN, configuration and other payload fields are not returned.
"""
from datetime import datetime, timezone
import json
import re


FIELDS=('createdTime','lastRefreshTime','pushupTime','validToTime')
ASSIGNMENT=re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*')
ISO=re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')


def _instant(value):
    if not isinstance(value,str) or not ISO.fullmatch(value):return None
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        return int(dt.timestamp()) if dt.tzinfo is not None else None
    except (ValueError,OverflowError,OSError):return None


def observe_source_dates(nodes, *, expected_id, fetched_at):
    """Extract one closed, matching ad.ad object from olx-init-config only."""
    if not isinstance(expected_id,str) or not expected_id.isdecimal():
        raise ValueError('Explicit numeric ad identity required')
    if type(fetched_at) is not int or fetched_at<=0:
        raise ValueError('Explicit observation timestamp required')
    scripts=[n for n in nodes if n.tag=='script' and n.closed and n.attrs.get('id')=='olx-init-config']
    result={'origin':'public_html_prerendered_state','path':'ad.ad',
            'identity_matches':False,'values':{},'issues':[],
            'publication_semantics_verified':False,'other_state_exported':False}
    if not scripts:return dict(result,issues=['source_dates_unavailable'])
    if len(scripts)!=1:return dict(result,issues=['ambiguous_state_script'])
    script=''.join(x for x in scripts[0].children if isinstance(x,str))
    matches=list(ASSIGNMENT.finditer(script))
    if len(matches)!=1:return dict(result,issues=['ambiguous_or_missing_state_assignment'])
    try:
        state,_=json.JSONDecoder().raw_decode(script[matches[0].end():])
        if isinstance(state,str):state=json.loads(state)
        ad=state.get('ad',{}).get('ad',{})
    except (ValueError,AttributeError,TypeError,RecursionError):
        return dict(result,issues=['invalid_state_json'])
    if not isinstance(ad,dict) or type(ad.get('id')) not in (str,int) or str(ad['id'])!=expected_id:
        return dict(result,issues=['source_date_identity_mismatch'])
    result['identity_matches']=True
    for key in FIELDS:
        value=ad.get(key)
        if value is None:continue
        epoch=_instant(value)
        if epoch is None or epoch<=0:
            result['issues'].append(key+'_invalid_or_timezone_missing');continue
        if key!='validToTime' and epoch>fetched_at:
            result['issues'].append(key+'_in_future');continue
        result['values'][key]={'iso':value,'utc':datetime.fromtimestamp(epoch,timezone.utc).isoformat(),'epoch':epoch}
    created=result['values'].get('createdTime',{}).get('epoch')
    for key in ('lastRefreshTime','pushupTime'):
        epoch=result['values'].get(key,{}).get('epoch')
        if epoch is not None and created is not None and epoch<created:
            result['issues'].append(key+'_before_createdTime')
    return result


def review_newness(observations, *, boundary, now):
    """A negative evidence gate; never authorizes a new-publication delivery.

    Dates are seller/platform reported. Missing/inconsistent observations stay
    unknown. A refresh after the boundary cannot promote an older createdTime.
    """
    if type(boundary) is not int or type(now) is not int or not 0<boundary<=now:
        raise ValueError('Explicit valid launch boundary and current time required')
    result={'status':'publication_unconfirmed','reasons':[],
            'ready_for_delivery':False,'publication_verified':False,
            'source_semantics_verified':False}
    if not isinstance(observations,dict) or not observations.get('identity_matches'):
        return dict(result,reasons=['source_dates_unavailable_or_wrong_identity'])
    if observations.get('issues'):
        return dict(result,reasons=['source_date_conflict_or_invalid'])
    created=observations.get('values',{}).get('createdTime',{}).get('epoch')
    if type(created) is not int or created<=0 or created>now:
        return dict(result,reasons=['source_created_time_unavailable_or_invalid'])
    if created<boundary:
        return dict(result,status='reported_preexisting',reasons=['source_created_before_boundary'])
    return dict(result,reasons=['first_publication_semantics_unverified'])
