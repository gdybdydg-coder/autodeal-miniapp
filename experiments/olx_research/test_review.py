import re,subprocess
import pytest
from .review import build
from .export_review import export
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


def test_review_preserves_frozen_partition_provenance(tmp_path):
    fake=[{**u,'id':'FAKE-'+u['id']} for u in users()]
    cars=[car()]+peers()
    provenance={'seed':'explicit-new-cohort','frozen_at':'2026-10-04T22:00:00Z','plan':'examples/test-plan.json'}
    raw={'mode':'isolated_research','dataset_kind':'synthetic','listings':cars,'users':fake,'now':EPOCH,
         'split':{c['id']:('holdout' if c['id']=='target' else 'reference') for c in cars},
         'split_provenance':[provenance]}
    result=build(raw,tmp_path/'provenance.db')
    assert result['holdout']['split_provenance']==[provenance]
    assert result['holdout']['split_seed']=='explicit-new-cohort'


def test_html_explains_missing_eur_pair_instead_of_hiding_price_reason(tmp_path):
    fake=[{**u,'id':'FAKE-'+u['id']} for u in users()]
    eur=car(currency='EUR',price='3500')
    raw={'mode':'isolated_research','dataset_kind':'synthetic','listings':[eur],
         'users':fake,'now':EPOCH,'split':{eur['id']:'holdout'}}
    html=export(build(raw,tmp_path/'eur.db'))
    assert 'Немає перевіреної пари курсів EUR/USD' in html
    assert "['converted_uah','converted_eur']" in html
    script=re.findall(r'<script(?: [^>]*)?>(.*?)</script>',html,re.S)[-1]
    path=tmp_path/'review.js';path.write_text(script)
    assert subprocess.run(['node','--check',path],capture_output=True).returncode==0
