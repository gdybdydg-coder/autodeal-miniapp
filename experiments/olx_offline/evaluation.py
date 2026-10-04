"""Bounded offline evaluation against a separately prepared reference sample.
No fetching, labeling, production DB access or delivery. Outputs aggregate counts only.
The manifest records claimed provenance; software cannot prove human independence.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


def identity(row):
    if row.get('source') not in ('olx','auto_ria') or not isinstance(row.get('id'),str) or not row['id']:
        raise ValueError('Explicit source and stable string ID required')
    return row['source'],row['id']


def unique(rows):
    output={}
    for row in rows:
        key=identity(row)
        if key in output and output[key]!=row:
            raise ValueError('Conflicting duplicate record')
        output[key]=row
    return output


def instant(value):
    return type(value) is int and value>0


def ratio(n,d):return n/d if d else None


def evaluate(manifest):
    kind=manifest.get('kind')
    if kind not in ('synthetic','observational'):raise ValueError('Explicit evidence kind required')
    provenance=manifest.get('control_origin',{})
    if provenance.get('method') not in ('independent_manual','independent_authorized_export','synthetic_holdout'):
        raise ValueError('Independent reference method required')
    if kind=='observational' and provenance['method']=='synthetic_holdout':
        raise ValueError('Synthetic reference is not observational evidence')
    if not provenance.get('reference') or not manifest.get('collector_run_ref') or provenance['reference']==manifest['collector_run_ref']:
        raise ValueError('Reference and collector must have separate provenance')
    start,end=manifest['window']['start'],manifest['window']['end']
    if not instant(start) or not instant(end) or end<=start:raise ValueError('Invalid half-open observation window')
    if type(manifest.get('collector_complete')) is not bool:raise ValueError('Explicit collection completion required')
    control=unique(manifest['control']);results=unique(manifest['results'])
    for c in control.values():
        if c.get('profitable') not in ('yes','no','unknown'):raise ValueError('Explicit independent label required')
        if not instant(c.get('recorded_at')) or not start<=c['recorded_at']<end:
            raise ValueError('Control record outside observation window')
        if c.get('publication_verified') is True and not instant(c.get('published_at')):
            raise ValueError('Verified publication requires timestamp')
    for r in results.values():
        if not instant(r.get('detected_at')) or not start<=r['detected_at']<end:
            raise ValueError('Result outside observation window')
        for field in ('processed','recommended'):
            if type(r.get(field)) is not bool:raise ValueError('Explicit boolean outcome required')
        if r.get('valuation') not in ('estimated','unknown','not_attempted'):raise ValueError('Explicit valuation state required')
        if r.get('filter_match') not in ('yes','no','unknown'):raise ValueError('Explicit filter result required')
        if r['recommended'] and (not r['processed'] or r['valuation']!='estimated' or r['filter_match']!='yes'):
            raise ValueError('Recommendation lacks completed evaluation/filter evidence')
    shared=control.keys() & results.keys()
    quality=dict(tp=0,fp=0,tn=0,fn=0,unknown=0,not_evaluated=0,missing_positive=0,unlabeled_recommendations=0)
    positive_count=sum(c['profitable']=='yes' for c in control.values())
    delays=[];missing_publication=0;invalid_time_order=0
    for key,c in control.items():
        r=results.get(key)
        if r is None:
            quality['missing_positive']+=int(c['profitable']=='yes');continue
        if c.get('publication_verified') is True:
            delay=r['detected_at']-c['published_at']
            if delay>=0:delays.append(delay)
            else:invalid_time_order+=1
        else:missing_publication+=1
        if c['profitable']=='unknown':
            quality['unlabeled_recommendations']+=int(r['recommended']);continue
        if not r['processed'] or r['valuation']=='not_attempted':quality['not_evaluated']+=1;continue
        if r['valuation']=='unknown':quality['unknown']+=1;continue
        key_name=('tp' if c['profitable']=='yes' else 'fp') if r['recommended'] else ('fn' if c['profitable']=='yes' else 'tn')
        quality[key_name]+=1
    delays.sort()
    percentile=lambda q:delays[max(0,math.ceil(q*len(delays))-1)] if delays else None
    quality['precision_labeled']=ratio(quality['tp'],quality['tp']+quality['fp'])
    quality['end_to_end_positive_recall']=ratio(quality['tp'],positive_count)
    quality['positive_control_count']=positive_count
    quality['positive_not_recommended_total']=positive_count-quality['tp']
    return {
        'evidence_kind':kind,'provenance_independence':'declared_not_automatically_verified',
        'window':{zone:{'start':datetime.fromtimestamp(start,timezone.utc).astimezone(ZoneInfo(zone)).isoformat(),
                        'end_exclusive':datetime.fromtimestamp(end,timezone.utc).astimezone(ZoneInfo(zone)).isoformat()} for zone in ('UTC','Europe/Kyiv')},
        'collector_complete':manifest['collector_complete'],'whole_olx_coverage_verified':False,
        'coverage':{'control':len(control),'detected_in_control':len(shared),'missing_from_control':len(control)-len(shared),
                    'control_recall':ratio(len(shared),len(control)),'detected_outside_control':len(results)-len(shared)},
        'pipeline':{'detected':len(results),'processed':sum(r['processed'] for r in results.values()),
                    'estimated':sum(r['valuation']=='estimated' for r in results.values()),
                    'valuation_unknown':sum(r['valuation']=='unknown' for r in results.values()),
                    'filter_match':sum(r['filter_match']=='yes' for r in results.values()),
                    'recommended':sum(r['recommended'] for r in results.values())},
        'quality':quality,
        'publication_to_detection_seconds':{'sample':len(delays),'p50':percentile(.5),'p95':percentile(.95),
                                            'unverified_publication':missing_publication,'invalid_time_order':invalid_time_order},
        'limitations':['Sample recall is not total source coverage','Unknown and missing positives reduce end-to-end recall',
                       'Labels must be reviewed independently; asking prices are not sale prices'],
    }


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',required=True);args=parser.parse_args()
    with Path(args.manifest).open('rb') as f:data=f.read(2*1024*1024+1)
    if len(data)>2*1024*1024:parser.error('Manifest exceeds 2 MiB')
    try:report=evaluate(json.loads(data))
    except (ValueError,KeyError,TypeError) as exc:parser.error(str(exc))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
