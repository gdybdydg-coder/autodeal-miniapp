from decimal import Decimal
from .evaluation import partition_id,stability,evaluate_holdout
from .test_research import car,peers,EPOCH


def test_partition_independent_of_prices_and_input_order():
    assert partition_id('936199703')==partition_id('936199703')
    rows=[car('a',1000),car('b',99999)]
    first={c['id']:partition_id(c['id']) for c in rows}
    rows[0]['price']='999999'
    assert first=={c['id']:partition_id(c['id']) for c in reversed(rows)}


def test_stability_does_not_relax_minimum_full_sample():
    result=stability(car(),peers(),None,EPOCH)
    assert result['status']=='stable_asking_sample'
    assert len(result['leave_one_out'])==8
    assert all(Decimal(m['max_shift_percent'])<5 for m in result['methods'].values())
    assert stability(car(),peers()[:7],None,EPOCH)['status']=='insufficient'


def test_holdout_never_becomes_reference_or_profit_label():
    target=car('control',6000)
    rows=peers()+[target];split={c['id']:'reference' for c in peers()}|{'control':'holdout'}
    r=evaluate_holdout(rows,split,None,EPOCH)
    assert r['estimated_holdout_count']==1 and r['eligible_coverage']=='1'
    assert r['metrics']['median']['mean_absolute_usd']=='2075.0'
    assert r['false_positive'] is None and r['false_negative'] is None
    assert not r['sale_price_accuracy_verified']
    assert 'control' not in [c['id'] for c in r['rows'][0]['review']['assessment']['used_comparables']]


def test_known_crosspost_cannot_leak_into_training():
    target=car('control',vehicle_key='same')
    ref=peers()+[car('duplicate',vehicle_key='same')]
    r=evaluate_holdout(ref+[target],{c['id']:'reference' for c in ref}|{'control':'holdout'},None,EPOCH)
    assert r['reference_count']==8


def test_missing_holdout_stays_visible_in_denominator():
    r=evaluate_holdout(peers(),{c['id']:'reference' for c in peers()}|{'missing':'holdout'},None,EPOCH)
    assert r['frozen_holdout_count']==1 and r['loaded_holdout_count']==0
    assert r['eligible_coverage'] is None
    assert r['missing_holdout_count']==1
    assert r['frozen_estimate_coverage']=='0'


def test_holdout_denominators_do_not_hide_missing_or_known_ineligible_rows():
    eligible=car('eligible-control',6000)
    ineligible=car('ineligible-control',fuel=None)
    rows=peers()+[eligible,ineligible]
    split={c['id']:'reference' for c in peers()}|{
        eligible['id']:'holdout',ineligible['id']:'holdout','not-loaded':'holdout'}
    result=evaluate_holdout(rows,split,None,EPOCH)
    assert result['frozen_holdout_count']==3
    assert result['loaded_holdout_count']==2
    assert result['missing_holdout_count']==1
    assert result['eligible_holdout_count']==1
    assert result['known_ineligible_holdout_count']==1
    assert result['estimated_holdout_count']==1
    assert result['unestimated_eligible_holdout_count']==0
    assert result['frozen_estimate_coverage']=='0.33333333333333333333333333333333333333333333333333'
    assert result['loaded_estimate_coverage']=='0.5'
    assert result['eligible_coverage']=='1'
    assert result['evaluation_denominators']=={
        'frozen_holdout_ads':3,'loaded_holdout_ads':2,'eligible_loaded_ads':1,
        'estimated_loaded_ads':1,'missing_frozen_ads':1,'known_ineligible_loaded_ads':1,
        'unestimated_eligible_loaded_ads':0}


def test_all_asking_error_methods_use_the_same_comparable_group():
    target=car('control',6000)
    references=peers()
    result=evaluate_holdout(references+[target],
        {c['id']:'reference' for c in references}|{target['id']:'holdout'},None,EPOCH)
    row=result['rows'][0]
    assert row['comparison_group_ids']==[c['id'] for c in row['review']['assessment']['used_comparables']]
    assert set(row['error_against_asking'])==set(row['review']['assessment']['methods'])
    assert row['same_comparable_group_for_all_methods'] is True


def test_mixed_frozen_cohorts_do_not_claim_one_seed():
    rows=peers();provenance=[{'seed':'old','ids':['a']},{'seed':'new','ids':['b']}]
    result=evaluate_holdout(rows,{c['id']:'reference' for c in rows},None,EPOCH,split_provenance=provenance)
    assert result['split_seed'] is None
    assert result['split_provenance']==provenance
