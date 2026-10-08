"""Replay frozen control evidence, including errors/unknowns, with no IO."""
def main():
    from .network_guard import OutboundGuard
    guard=OutboundGuard()
    with guard.isolated():
        import json
        from pathlib import Path
        from collections import Counter
        from .scoped_control_audit import scoped_controls
        from .valuation import estimate
        d=json.loads(Path(__file__).with_name('NIGHT_CONTROL_20261008_04.json').read_text());ids=[c['id'] for c in d['manifest']['candidates']]
        for name,first in [('cycle1',2),('cycle2',23)]:
            cycle=d[name]
            a=scoped_controls(ids,cycle['observations'],control_query=cycle['control_query'],collector_queries=cycle['queries'],frozen_at=d['manifest']['frozen_at'],collector_started_at=d['receipts'][first]['started_at'],independent_selection=True,control_source='legacy_hour',collector_source='public_search',expected_pages=[0,1,2],successful_pages=[p['page_index'] for p in cycle['pages']])
            assert a['controls']==18 and a['observed_controls']==10
            assert a['bounded_control_detection_fraction'] is None and a['whole_market_recall'] is None
            assert a['comparison_blockers']==['scope_not_equivalent']
        peers=[p for p in d['public_peer_records'] if p['listing_id'] not in set(ids)];counts=Counter()
        for row in d['inline']['rows']:
            if row['error']:counts['error']+=1;continue
            out=estimate(row['details'],peers,row['valuation']['as_of']).as_dict()
            assert out['status']==row['valuation']['status'] and out['sample_count']==0
            counts[out['status']]+=1
            assert not row['ready_for_delivery']
        assert dict(counts)=={'unknown':17,'error':1}
        assert d['supplemental_currency_evidence'][0]['same_currency_visible_amount']=='22000'
        assert d['supplemental_currency_evidence'][0]['jsonld_currency']=='UAH'
        assert d['retention']['retained']==60 and all(d['retention']['same_hash_by_page'])
    print(json.dumps({'replay':'PASS','controls':18,'outcomes':dict(counts),'recall':None,'delivery':None,'raw_HTML_currency_replay':False,'network_guard':guard.summary()}))

if __name__=='__main__':main()
