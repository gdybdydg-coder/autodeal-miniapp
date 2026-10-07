"""Offline, pre-import isolated replay of sanitized night02 public snapshots.

Run: python -m experiments.free_search.replay_night02
No production configuration, raw HTML, credentials or paid cache are needed.
"""
def main():
    from .network_guard import OutboundGuard
    guard=OutboundGuard()
    with guard.isolated():
        import json
        from pathlib import Path
        from .drivetrain_audit import same_drive_scenario
        from .generation_scenarios import unknown_generation_scenarios
        from .public_holdout import evaluate_public_holdout
        from .valuation import estimate
        d=json.loads(Path(__file__).with_name('NIGHT_PEERS_20261008_02.json').read_text())
        a=same_drive_scenario(d['target2016'],d['peers2016'],d['target2016_drive'],d['peers2016_drive'],d['as_of'])
        assert a['outcome']['status']=='unknown' and a['outcome']['sample_count']==4
        b=same_drive_scenario(d['target2022'],d['peers2022'],d['target2022_drive'],d['peers2022_drive'],d['as_of'])
        assert b['outcome']['sample_count']==14 and b['outcome']['reference_price']=='22600.00'
        same={x['listing_id'] for x in b['audit']['rows'] if x['relation']=='same_source_label'}
        g=unknown_generation_scenarios(d['target2022'],[p for p in d['peers2022'] if p['listing_id'] in same],d['as_of'])
        assert g['unconditional_reference_price'] is None
        assert [(x['outcome']['status'],x['outcome']['sample_count']) for x in g['cases']]==[('unknown',2),('estimated',12)]
        c=evaluate_public_holdout(d['target_ids'],d['targets'],d['peer_ids'],d['peers'],d['as_of'])
        assert c['status_counts']=={'estimated':2,'unknown':48}
        assert c['planned_targets']==50 and c['prepared_peers']==72
        assert all(not x['ready_for_delivery'] for x in [a,b,g,c])
    print(json.dumps({'replay':'PASS','nominal_cohort':c['status_counts'],'2016_same_drive':'unknown/min5','2022_generation':'unresolved/conditional_only','network_guard':guard.summary()}))

if __name__=='__main__':main()
