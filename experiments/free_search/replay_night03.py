"""Replay sanitized night03 valuation and timing evidence with outbound blocked."""
def main():
    from .network_guard import OutboundGuard
    guard=OutboundGuard()
    with guard.isolated():
        import json
        from pathlib import Path
        from .drivetrain_audit import same_drive_scenario
        from .public_holdout import evaluate_public_holdout
        from .valuation_sensitivity import audit_sensitivity
        from .stage_timing import stage_timing
        f=json.loads(Path(__file__).with_name('NIGHT_PEERS_20261008_03.json').read_text());d=f['result']
        scenario=same_drive_scenario(d['target'],d['new_peers'],d['target_drive'],d['drive_evidence'],d['as_of'])
        assert scenario['outcome']['sample_count']==6
        assert scenario['outcome']['reference_price']=='12999.25'
        assert f['initial_same_drive']['outcome']['status']=='unknown'
        assert f['initial_same_drive']['outcome']['sample_count']==4
        same={x['listing_id'] for x in scenario['audit']['rows'] if x['relation']=='same_source_label'}
        audit=audit_sensitivity(d['target'],[p for p in d['new_peers'] if p['listing_id'] in same],d['as_of'])
        assert audit['region_summary']['unknown']==2
        assert audit['region_summary']['known_threshold_flips']=={'5':0,'10':0,'15':0}
        cohort=evaluate_public_holdout(d['target_ids'],d['targets'],d['peer_ids'],d['all_peers'],d['as_of'])
        assert cohort['status_counts']=={'estimated':3,'unknown':47}
        assert cohort['planned_targets']==50 and cohort['prepared_peers']==97
        events={k:{'at':d['timing']['events'][k],'proof':proof} for k,proof in [('source_observed','public_snapshot'),('details_ready','public_details_parsed'),('assessment_ready','local_assessment')]}
        timing=stage_timing(events,environment='local',assessment_status='estimated')
        assert timing['durations']['publication_to_detection_seconds'] is None
        assert timing['events']['telegram_delivered'] is None
        assert not scenario['ready_for_delivery'] and not cohort['ready_for_delivery']
    print(json.dumps({'replay':'PASS','cohort':cohort['status_counts'],'same_drive_samples':6,'source_identity_audit_replayed':False,'reason':'raw VIN/HTML excluded; parser tested with synthetic identities','network_guard':guard.summary()}))

if __name__=='__main__':main()
