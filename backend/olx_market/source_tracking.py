"""Offline public-page observations. First seen is never first publication."""
import re
from urllib.parse import urlsplit
from experiments.olx_offline.html_snapshot import parse_search_snapshot, SearchParser,clean_url


def public_detail_url(value):
    try:
        if not isinstance(value,str):return False
        url=urlsplit(value)
        return bool(url.scheme=='https' and url.hostname in ('www.olx.ua','olx.ua')
                    and clean_url(value) and not (url.username or url.password or url.port or url.query or url.fragment)
                    and url.path.startswith(('/d/uk/obyavlenie/','/d/obyavlenie/')) and url.path.endswith('.html'))
    except (ValueError,TypeError):return False


def same_detail_url(left,right):
    if not public_detail_url(left) or not public_detail_url(right):return False
    # The source exposes both localized routes for the same slug. Host aliases
    # and language alone must not be mistaken for a different car.
    def path(value):return urlsplit(value).path.replace('/d/uk/','/d/',1)
    return path(left)==path(right)


def parse_page(data, *, fetched_at, truncated=False):
    result=parse_search_snapshot(data, fetched_at=fetched_at, truncated=truncated)
    parser=SearchParser();parser.feed(data.decode('utf-8',errors='replace'));parser.close()
    by_id={c['id']:c for c in result['listings']}
    gears={'Механическая':'manual','Автоматическая':'automatic','Вариатор':'cvt',
           'Типтроник':'tiptronic','Роботизированная':'robotized'}
    values={}
    for node in parser.cards:
        id=node.attrs.get('id')
        if id not in by_id:continue
        for n in node.nodes():
            if n.tag!='span' or not n.closed:continue
            text=n.text()
            m=re.fullmatch(r'(\d{4})\s+([\d\s]+)\s+(?:тис|тыс)\.км\.',text)
            observed={'year':int(m[1]),'mileage_km':int(m[2].replace(' ',''))*1000} if m else {}
            if text in gears:observed['transmission']=gears[text]
            for field,value in observed.items():values.setdefault(id,{}).setdefault(field,set()).add(value)
    for id,fields in values.items():
        c=by_id[id]
        for field,observed in fields.items():
            if c.get(field) is not None:observed.add(c[field])
            if len(observed)==1:c[field]=next(iter(observed))
            else:
                c[field]=None
                c.setdefault('field_conflicts',[]).append(field)
    result['summary']['attribute_locale_adapter']='explicit_uk_ru_spans_v1'
    return result


def page_change(previous,current):
    a=[c['id'] for c in previous['listings']];b=[c['id'] for c in current['listings']]
    return {'first_observed_added_ids':sorted(set(b)-set(a)),
            'not_present_in_repeat_ids':sorted(set(a)-set(b)),
            'order_changed':a!=b,'overlap_count':len(set(a)&set(b)),
            'comparison_seconds':current['summary']['fetched_at']-previous['summary']['fetched_at'],
            'both_pages_complete':not previous['summary']['download_truncated'] and not current['summary']['download_truncated'],
            'catalogue_complete':False,'new_publication_count':None,
            'reason':'One moving relevance window cannot establish complete discovery or publication latency'}


def detail_change(previous,current,*,first_seen):
    if type(first_seen) is not int or first_seen<=0:raise ValueError('Persisted first_seen required')
    if previous and (previous.get('source'),previous.get('id'))!=(current.get('source'),current.get('id')):
        raise ValueError('Same source identity required')
    date_obs=current.get('source_date_observations',{})
    valid_dates=date_obs.get('identity_matches') is True and not date_obs.get('issues')
    dates=date_obs.get('values',{}) if valid_dates else {}
    events=[]
    if not previous:events.append('first_observed')
    else:
        if current.get('checked_at',0)<previous.get('checked_at',0):raise ValueError('Observations out of order')
        if current.get('currency')==previous.get('currency'):
            if current.get('price')!=previous.get('price'):events.append('display_price_changed')
        else:events.append('display_currency_changed_not_verified_seller_price_change')
        if any(previous.get(f)!=current.get(f) for f in ('year','mileage_km','engine_cc','fuel','transmission','body','generation','drive_type','power_hp','modification','doors')):
            events.append('characteristics_changed')
        old_date_obs=previous.get('source_date_observations',{})
        old_dates=(old_date_obs.get('values',{}) if old_date_obs.get('identity_matches') is True
                   and not old_date_obs.get('issues') else {})
        for f,event in (('lastRefreshTime','source_refresh_advanced'),('pushupTime','source_pushup_advanced')):
            old=old_dates.get(f,{}).get('epoch');new=dates.get(f,{}).get('epoch')
            if type(old) is int and type(new) is int and new>old:events.append(event)
        a=previous.get('eligibility_review',{}).get('fingerprint');b=current.get('eligibility_review',{}).get('fingerprint')
        if a and b and a!=b:events.append('eligibility_evidence_changed')
    event_kinds=[]
    if 'first_observed' in events:event_kinds.append('first_seen')
    if any(event in events for event in (
            'characteristics_changed','source_refresh_advanced',
            'eligibility_evidence_changed','display_currency_changed_not_verified_seller_price_change')):
        event_kinds.append('update')
    if 'source_pushup_advanced' in events:event_kinds.append('raise')
    if 'display_price_changed' in events:event_kinds.append('reprice')
    created=dates.get('createdTime',{}).get('epoch')
    return {'events':events,'event_kinds':event_kinds,
            'first_seen_at':first_seen,'observed_at':current.get('checked_at'),
            'source_reported_created_at':created,'reported_preexisting':type(created) is int and created<first_seen,
            'source_date_conflicts':date_obs.get('issues',[]),'first_publication_verified':False,
            'publication_latency_seconds':None,'seller_original_price_change_verified':False}
