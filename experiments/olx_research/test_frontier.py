import copy
import json
import pytest
from .frontier import Frontier
from .test_research import car,users,EPOCH
from .source_tracking import detail_change,page_change


def page(cars,complete=True):
    return {'listings':cars,'summary':{'fetched_at':EPOCH,'download_truncated':not complete,'observed_pagination_links':['https://www.olx.ua/uk/transport/legkovye-avtomobili/?page=2']}}


def test_budget_preserves_deferred_candidates_after_restart(tmp_path):
    path=tmp_path/'frontier.db';db=Frontier(path)
    cars=[car(str(i)) for i in range(12)]
    db.record_page('observed page1',page(cars,False));work=db.claim(users(),None,EPOCH,limit=3)
    assert len(work)==3
    for w in work:db.finish(w['token'],200,EPOCH+1,car=w['car'])
    assert db.snapshot()['candidate_counts']=={'pending':9,'stored':3}
    db.close();db=Frontier(path);next_work=db.claim(users(),None,EPOCH+2,limit=3)
    assert len(next_work)==3 and not {x['car']['id'] for x in work}&{x['car']['id'] for x in next_work}
    assert not db.snapshot()['whole_source_complete'];db.close()


def test_retry_is_finite_durable_and_does_not_stall_other_car(tmp_path):
    db=Frontier(tmp_path/'frontier.db');db.record_page('page',page([car('a'),car('b')]))
    work=db.claim(users(),None,EPOCH)
    assert db.finish(work[0]['token'],None,EPOCH+1)=='retry'
    assert db.finish(work[1]['token'],200,EPOCH+1,car=work[1]['car'])=='stored'
    assert db.claim(users(),None,EPOCH+30)==[]
    retry=db.claim(users(),None,EPOCH+62)[0]
    assert db.finish(retry['token'],503,EPOCH+63)=='exhausted'
    assert db.claim(users(),None,EPOCH+1000)==[];db.close()


def test_lease_prevents_concurrent_double_claim_and_recovers_interruption(tmp_path):
    path=tmp_path/'frontier.db';a=Frontier(path);b=Frontier(path)
    a.record_page('page',page([car()]));w=a.claim(users(),None,EPOCH)[0]
    assert b.claim(users(),None,EPOCH+1)==[]
    assert b.claim(users(),None,EPOCH+61)==[] # grace before one final GET retry
    retry=b.claim(users(),None,EPOCH+122)[0]
    with pytest.raises(ValueError):a.finish(w['token'],200,EPOCH+123,car=w['car'])
    b.finish(retry['token'],200,EPOCH+123,car=retry['car']);a.close();b.close()


def test_source_block_persists_and_unpaid_has_no_work(tmp_path):
    path=tmp_path/'frontier.db';db=Frontier(path);db.record_page('page',page([car('a'),car('b')]))
    assert db.claim(users()[-1:],None,EPOCH)==[]
    w=db.claim(users(),None,EPOCH,limit=1)[0]
    assert db.finish(w['token'],429,EPOCH+1)=='source_blocked'
    db.close();db=Frontier(path)
    assert db.claim(users(),None,EPOCH+100)==[]
    assert db.snapshot()['candidate_counts']['pending']==1;db.close()


def test_price_refresh_and_first_seen_do_not_prove_publication():
    before=car();before['source_date_observations']={'identity_matches':True,'issues':[],'values':{'createdTime':{'epoch':EPOCH-86400},'lastRefreshTime':{'epoch':EPOCH-100},'pushupTime':{'epoch':EPOCH-100}}}
    after=copy.deepcopy(before);after['checked_at']=EPOCH+10;after['price']='5900'
    after['source_date_observations']['values']['lastRefreshTime']['epoch']=EPOCH+5
    after['source_date_observations']['values']['pushupTime']['epoch']=EPOCH+5
    first=detail_change(None,before,first_seen=EPOCH)
    assert first['events']==['first_observed'] and first['reported_preexisting']
    assert first['event_kinds']==['first_seen']
    change=detail_change(before,after,first_seen=EPOCH)
    assert set(change['events'])=={'display_price_changed','source_refresh_advanced','source_pushup_advanced'}
    assert change['event_kinds']==['update','raise','reprice']
    assert not change['first_publication_verified'] and change['publication_latency_seconds'] is None


def test_invalid_previous_source_dates_cannot_create_raise_or_update_events():
    before=car();before['source_date_observations']={
        'identity_matches':False,'issues':['source_date_identity_mismatch'],
        'values':{'lastRefreshTime':{'epoch':EPOCH-100},'pushupTime':{'epoch':EPOCH-100}}}
    after=copy.deepcopy(before);after['checked_at']=EPOCH+10
    after['source_date_observations']={
        'identity_matches':True,'issues':[],
        'values':{'lastRefreshTime':{'epoch':EPOCH+5},'pushupTime':{'epoch':EPOCH+5}}}
    change=detail_change(before,after,first_seen=EPOCH)
    assert 'source_refresh_advanced' not in change['events']
    assert 'source_pushup_advanced' not in change['events']
    assert change['event_kinds']==[]


def test_page_shift_is_explicit_without_completeness_claim():
    old=page([car('a'),car('b')]);new=page([car('b'),car('c')]);new['summary']['fetched_at']+=600
    r=page_change(old,new)
    assert r['first_observed_added_ids']==['c'] and r['not_present_in_repeat_ids']==['a']
    assert r['order_changed'] and r['new_publication_count'] is None and not r['catalogue_complete']


def test_current_access_and_source_stop_are_rechecked_before_injected_get(tmp_path):
    db=Frontier(tmp_path/'frontier.db');db.record_page('page',page([car('a'),car('b')]))
    work=db.claim(users(),None,EPOCH)
    assert db.run_claim(work[0],lambda:users()[-1:],None,lambda:EPOCH+1,lambda u:pytest.fail('unpaid GET'),lambda b:b)=='not_fetched'
    db.finish(work[1]['token'],403,EPOCH+1)
    assert db.claim(users(),None,EPOCH+2)==[];db.close()


def test_pending_newer_search_updates_filter_evidence_without_losing_work(tmp_path):
    db=Frontier(tmp_path/'frontier.db');db.record_page('page',page([car('a',6000)]))
    newer=car('a',7000);newer['checked_at']=EPOCH+10
    p=page([newer]);p['summary']['fetched_at']=EPOCH+10;db.record_page('page',p)
    work=db.claim(users(),None,EPOCH+11)[0]
    assert work['car']['price']==7000
    db.finish(work['token'],200,EPOCH+12,car=work['car'])
    stored=db.db.execute('SELECT first_seen,changes FROM research_details').fetchone()
    assert stored['first_seen']==EPOCH
    assert json.loads(stored['changes'])['first_seen_at']==EPOCH
    db.close()


@pytest.mark.parametrize('url',['https://example.com/a.html','https://www.olx.ua:bad/d/uk/obyavlenie/a.html','https://user:pass@www.olx.ua/d/uk/obyavlenie/a.html','/d/uk/obyavlenie/a.html'])
def test_dispatch_rejects_untrusted_detail_url_without_external_io(tmp_path,url):
    db=Frontier(tmp_path/'frontier.db');db.record_page('page',page([car(url=url)]))
    work=db.claim(users(),None,EPOCH)[0]
    result=db.run_claim(work,users,None,lambda:EPOCH+1,lambda u:pytest.fail('untrusted URL'),lambda b:b)
    assert result=='needs_review';db.close()


def test_locale_url_variant_keeps_same_ad_but_different_slug_is_rejected(tmp_path):
    db=Frontier(tmp_path/'frontier.db');c=car();db.record_page('page',page([c]))
    w=db.claim(users(),None,EPOCH)[0];variant={**c,'url':c['url'].replace('/d/uk/','/d/')}
    wrong={**variant,'url':variant['url'].replace('IDexample','IDother')}
    with pytest.raises(ValueError):db.finish(w['token'],200,EPOCH+1,car=wrong)
    assert db.finish(w['token'],200,EPOCH+1,car=variant)=='stored';db.close()


def test_offline_manifest_does_not_erase_other_pending_candidates(tmp_path):
    db=Frontier(tmp_path/'frontier.db');db.record_page('page',page([car('a'),car('b')]))
    w=db.claim(users(),None,EPOCH,candidate_ids={'b'})
    assert len(w)==1 and w[0]['car']['id']=='b'
    assert db.snapshot()['candidate_counts']=={'claimed':1,'pending':1};db.close()


def test_stored_price_change_refreshes_after_cooldown_and_keeps_first_seen(tmp_path):
    db=Frontier(tmp_path/'refresh.db');c=car('a',6000);db.record_page('page',page([c]));w=db.claim(users(),None,EPOCH)[0];db.finish(w['token'],200,EPOCH+1,car=c)
    newer=car('a',5500);newer['checked_at']=EPOCH+20;p=page([newer]);p['summary']['fetched_at']=EPOCH+20;db.record_page('page',p)
    assert db.claim(users(),None,EPOCH+21)==[]
    w=db.claim(users(),None,EPOCH+61)[0];db.finish(w['token'],200,EPOCH+62,car=newer)
    row=db.db.execute('SELECT first_seen,changes FROM research_details').fetchone()
    assert row['first_seen']==EPOCH
    assert 'display_price_changed' in json.loads(row['changes'])['events']
    assert db.db.execute('SELECT count(*) FROM research_detail_events').fetchone()[0]==2
    db.close()


def test_unchanged_search_does_not_refetch_until_hourly_detail_age(tmp_path):
    active=[{**u,'expires_at':EPOCH+7200} for u in users()]
    db=Frontier(tmp_path/'refresh.db');c=car();db.record_page('page',page([c]));w=db.claim(active,None,EPOCH)[0];db.finish(w['token'],200,EPOCH+1,car=c)
    for offset in (10,60,900):
        newer={**c,'checked_at':EPOCH+offset};p=page([newer]);p['summary']['fetched_at']=EPOCH+offset;db.record_page('page',p)
        assert db.claim(active,None,EPOCH+offset)==[]
    newer={**c,'checked_at':EPOCH+3601};p=page([newer]);p['summary']['fetched_at']=EPOCH+3601;db.record_page('page',p)
    assert len(db.claim(active,None,EPOCH+3601))==1;db.close()


def test_new_search_during_lease_is_retained_without_overwriting_claim(tmp_path):
    path=tmp_path/'refresh.db';a=Frontier(path);b=Frontier(path);old=car('a',6000)
    a.record_page('page',page([old]));w=a.claim(users(),None,EPOCH)[0]
    newer=car('a',5500);newer['checked_at']=EPOCH+20;p=page([newer]);p['summary']['fetched_at']=EPOCH+20;b.record_page('page',p)
    assert b.claim(users(),None,EPOCH+21)==[]
    a.finish(w['token'],200,EPOCH+22,car={**old,'checked_at':EPOCH+10});a.close();b.close()
    db=Frontier(path)
    assert db.claim(users()[-1:],None,EPOCH+100)==[]
    pending=db.claim(users(),None,EPOCH+100)[0]
    assert pending['car']['price']==5500
    db.finish(pending['token'],200,EPOCH+101,car={**newer,'checked_at':EPOCH+100})
    assert db.snapshot()['candidate_counts']=={'stored':1};db.close()


def test_same_signature_search_during_lease_preserves_latest_snapshot_without_refetch(tmp_path):
    db=Frontier(tmp_path/'refresh.db');old=car('a',6000)
    db.record_page('page',page([old]));work=db.claim(users(),None,EPOCH)[0]
    newer={**old,'checked_at':EPOCH+20};observed=page([newer]);observed['summary']['fetched_at']=EPOCH+20
    db.record_page('page',observed)
    db.finish(work['token'],200,EPOCH+22,car={**old,'checked_at':EPOCH+21})
    row=db.db.execute('SELECT payload,state FROM research_candidates').fetchone()
    assert row['state']=='stored'
    assert json.loads(row['payload'])['checked_at']==EPOCH+20
    assert db.claim(users(),None,EPOCH+100)==[]
    assert db.db.execute('SELECT count(*) FROM research_detail_events').fetchone()[0]==1
    db.close()


def test_later_detail_supersedes_search_observed_during_fetch(tmp_path):
    db=Frontier(tmp_path/'refresh.db');c=car();db.record_page('page',page([c]));w=db.claim(users(),None,EPOCH)[0]
    newer=car(price=5500);newer['checked_at']=EPOCH+10;p=page([newer]);p['summary']['fetched_at']=EPOCH+10;db.record_page('page',p)
    db.finish(w['token'],200,EPOCH+22,car={**newer,'checked_at':EPOCH+20})
    assert db.claim(users(),None,EPOCH+100)==[];db.close()


def test_drive_and_power_changes_are_characteristic_events():
    previous=car(drive_type='front',power_hp=105)
    current={**previous,'drive_type':'full','power_hp':160,'checked_at':EPOCH+10}
    assert 'characteristics_changed' in detail_change(previous,current,first_seen=EPOCH)['events']
