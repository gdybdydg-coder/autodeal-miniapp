"""Wildcard discovery uses the ordinary provider pipeline and recipient guards."""
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.billing_models import Entitlement
from backend.models import (Delivery, Filters, MonitorFeed, MonitorMembership,
                            MonitorWatch, Search, User)
from backend.monitor import active_members, source_filters
from backend.radar_discovery import merge, plan
from backend.radar_models import Radar, RadarBrand, RadarModel, RadarSearch
from backend.radar_service import (RadarCreate, RadarEdit, RadarStatus, config,
                                  create, edit, set_status)
from backend.tests.test_monitor import p, drain, wake, details, searches
from backend.tests.test_radar_monitor import radar, add, group_count
from backend.worker import enqueue


def all_brands(p, uid=111):
    with Session(p.engine) as db:
        if db.get(User, uid) is None:
            db.add(User(id=uid, ready=True))
        db.merge(Entitlement(user_id=uid, expires_at=p.clock[0]+86400, updated_at=p.clock[0]))
        db.flush()
        value = create(db, uid, RadarCreate(name='Усі марки', all_brands=True, brands=[],
            price_category=4, region_ids=[4], year_from=2010, year_to=2020, min_profit=10))
        value = set_status(db, uid, value['id'], RadarStatus(active=True, revision=value['revision']))
        db.commit()
        return value


def varied_candidates(p):
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def call(key, path, params):
            data = fetch(key, path, params)
            if path == 'info':
                sid = params['auto_id']
                if sid in ('125', '127'):
                    data.update(markId=9, modelId=32, markName='BMW', modelName='320')
                elif sid == '126':
                    # A provider brand absent from this test's local catalog
                    # still belongs to an explicitly unrestricted radar.
                    data.update(markId=777, modelId=778, markName='Інша марка', modelName='Інша модель')
            return data
        source.fetch = call
        return source
    p.runner.search_factory = source_factory


def deliver_pending(p, cycles=8):
    for _ in range(cycles):
        p.runner.deliver_tick()
        p.clock[0] += 2


def test_all_and_specific_radars_share_one_query_without_broadening_recipients(radar):
    p = radar
    all_value = all_brands(p)
    add(p, 111)  # A second matching radar must not duplicate this owner's car.
    golf = add(p, 222)
    bmw = add(p, 333, brands=[{'brand_id':9, 'model_ids':[32]}])
    varied_candidates(p)
    drain(p)
    assert group_count(p) == 1
    with Session(p.engine) as db:
        binding = list(db.scalars(select(RadarSearch).where(RadarSearch.radar_id == all_value['id'])))
        assert len(binding) == 1
        assert db.get(Search, binding[0].search_id).filters['brand'] == ''
        assert db.scalar(select(RadarBrand).where(RadarBrand.radar_id == all_value['id'])) is None
        assert db.scalar(select(RadarModel).where(RadarModel.radar_id == all_value['id'])) is None
        assert config(db, db.get(Radar, golf['id']))['brands'][0]['brand_id'] == 84
        assert config(db, db.get(Radar, bmw['id']))['brands'][0]['brand_id'] == 9
    p.calls.clear()
    p.ads.update({sid:p.clock[0]+1 for sid in ('124', '125', '126')})
    wake(p)
    drain(p)
    deliver_pending(p)
    assert len(searches(p)) == 1
    assert 'marka_id[0]' not in searches(p)[0] and 'model_id[0]' not in searches(p)[0]
    assert {(uid,car.source_id) for uid,car in p.sent} == {
        (111,'124'), (111,'125'), (111,'126'), (222,'124'), (333,'125')}
    assert len(p.sent) == 5
    assert sorted(p.quotes) == ['124','125','126']
    assert all(len(details(p,sid)) == 1 for sid in ('124','125','126'))
    assert not [path for path,_ in p.calls if path in ('states','categories/1/marks') or path.endswith('/models')]


def test_narrowing_all_brands_cancels_old_epoch_and_preserves_other_users(radar):
    p = radar
    value = all_brands(p)
    add(p,222)
    varied_candidates(p)
    drain(p)
    p.ads.update({'124':p.clock[0]+1,'125':p.clock[0]+1})
    wake(p)
    for _ in range(6):
        p.runner.tick()
    enqueue(p.engine, require_provider_range=True)
    with Session(p.engine) as db:
        assert list(db.scalars(select(Delivery).where(Delivery.user_id==111)))
        row = db.get(Radar,value['id'])
        before = config(db,row)
        value = edit(db,111,row.id,RadarEdit(name=row.name,all_brands=False,
            brands=[{'brand_id':9,'model_ids':[32]}],price_category=row.price_category,
            region_ids=before['region_ids'],year_from=row.year_from,year_to=row.year_to,
            min_profit=row.min_profit,revision=row.revision))
        assert not value['all_brands']
        db.commit()
    deliver_pending(p)
    assert [(uid,car.source_id) for uid,car in p.sent] == [(222,'124')]
    with Session(p.engine) as db:
        old = list(db.scalars(select(Delivery).where(Delivery.user_id==111)))
        assert old and all(row.state=='cancelled' for row in old)
    # Edits start a new observation boundary; the unchanged user's first
    # receipt stays deduplicated even if the source lists that ID again.
    p.ads.update({'124':p.clock[0]+1,'127':p.clock[0]+1})
    wake(p)
    drain(p)
    deliver_pending(p)
    assert {(uid,car.source_id) for uid,car in p.sent} == {(222,'124'),(111,'127')}
    assert len(p.sent) == 2


def test_pausing_all_brand_radar_restores_specific_discovery_without_cursor_rewind(radar):
    p = radar
    value = all_brands(p)
    add(p,222)
    drain(p)
    wake(p,120)
    drain(p)
    with Session(p.engine) as db:
        member = db.scalar(select(MonitorMembership).join(Search,Search.id==MonitorMembership.search_id)
                           .where(Search.user_id==222))
        cursor = db.get(MonitorFeed,member.feed_id).cursor
        epoch = db.get(MonitorWatch,member.search_id).epoch
        row = db.get(Radar,value['id'])
        set_status(db,111,row.id,RadarStatus(active=False,revision=row.revision))
        db.commit()
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release('idle')
    with Session(p.engine) as db:
        members = active_members(db)
        assert len(members)==1 and members[0][0].user_id==222
        search,watch,member = members[0]
        feed = db.get(MonitorFeed,member.feed_id)
        assert feed.filters['brand']=='Volkswagen' and feed.filters['model']=='Golf'
        assert feed.cursor>=cursor and watch.epoch==epoch


def test_wildcard_absorption_waits_for_frozen_page_to_finish(radar):
    p = radar
    all_brands(p)
    add(p,222)
    with Session(p.engine) as db:
        for search,_,member in active_members(db):
            db.add(MonitorFeed(id=member.feed_id,filters=source_filters(search.filters).canonical(),
                started_at=member.started_at,cursor=member.started_at,status='watching'))
        db.flush()
        db.scalar(select(MonitorFeed)).context={'page':1,'clock':'published','slices':[{'after':1,'before':2,'members':[]}]}
        old={m.search_id:m.feed_id for m in db.scalars(select(MonitorMembership))}
        db.commit()
    assert p.runner.claim()
    try:
        with Session(p.engine) as db:
            merge(p.runner,db)
            db.commit()
            assert {m.search_id:m.feed_id for m in db.scalars(select(MonitorMembership))}==old
    finally:
        p.runner.release('idle')


def test_planner_absorbs_only_existing_wildcards_with_identical_other_dimensions():
    base = Filters(price={'from':10000,'to':14999.999999999998},year={'from':2010,'to':2020},region=['Київська']).canonical()
    def item(**changes):
        return (SimpleNamespace(filters={**deepcopy(base),**changes}),None,None)
    wildcard = item(brand='',model='')
    specific = item(brand='BMW',model='320')
    other = item(brand='Volkswagen',model='Golf')
    rows = [wildcard,specific,other]
    saved = deepcopy([row[0].filters for row in rows])
    grouped = plan(rows)
    assert len(grouped)==1 and len(grouped[0]['rows'])==3
    assert grouped[0]['filters'].brand==grouped[0]['filters'].model==''
    assert [row[0].filters for row in rows]==saved
    assert len(plan([specific,other]))==2
    for change in ({'price':{'from':15000,'to':19999.999999999996}},
                   {'year':{'from':2015,'to':2020}}, {'region':['Вінницька']}):
        assert len(plan([wildcard,item(brand='BMW',model='320',**change)]))==2
