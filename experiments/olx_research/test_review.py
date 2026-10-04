import pytest
from .review import build
from .test_research import car,peers,users,EPOCH


def test_review_emits_provenance_unknown_and_restart_without_real_users(tmp_path):
    fake=[{**u,'id':'FAKE-'+u['id']} for u in users()]
    cars=[car()]+peers()
    raw={'mode':'isolated_research','dataset_kind':'synthetic','listings':cars,'users':fake,'now':EPOCH,
         'split':{c['id']:('holdout' if c['id']=='target' else 'reference') for c in cars}}
    result=build(raw,tmp_path/'local.db')
    assert result['actual_telegram_calls']==0 and len(result['local_previews']['prepared'])==18
    assert build(raw,tmp_path/'local.db')['local_previews']['prepared']==[]
    raw['users'][0]['id']='unlabelled-user'
    with pytest.raises(ValueError):build(raw,tmp_path/'blocked.db')
