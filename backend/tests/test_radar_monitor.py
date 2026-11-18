"""Real existing monitor/valuation/delivery exercised with offline HTTP fixtures."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.billing_models import Entitlement
from backend.models import (Delivery, Filters, MonitorFeed, MonitorJob, MonitorMatch, MonitorMembership,
                            MonitorSeen, MonitorWatch, Search, User)
from backend.monitor import Monitor, active_members, reset_watch
from backend.radar_catalog import import_catalog
from backend.radar_discovery import plan
from backend.radar_models import Radar, RadarMigration, RadarSearch, MIGRATION_VERSION
from backend.radar_monitor import sync_expired
from backend.radar_service import (RadarCreate, RadarStatus, create, remove, set_status)
from backend.tests.test_monitor import p, drain, wake, details, searches
from backend.tests.test_ria_ai_price import enable


@pytest.fixture
def radar(p, monkeypatch):
    with Session(p.engine) as db:
        db.get(Search, 1).enabled = False
        reset_watch(db, 1, False)
        db.add(RadarMigration(version=MIGRATION_VERSION, applied_at=p.clock[0]))
        import_catalog(db, {"brands": [
            {"id": 84, "name": "Volkswagen", "models": [{"id":30,"name":"Golf"},{"id":31,"name":"Passat"}]},
            {"id": 9, "name": "BMW", "models": [{"id":32,"name":"320"}]}],
            "regions": [{"id":4,"name":"Хмельницька область"},{"id":1,"name":"Вінницька область"}]})
        db.commit()
    p.quotes = enable(p, monkeypatch)
    return p


def add(p, uid, *, category=4, percent=10, regions=None, models=None, brands=None, active=True):
    with Session(p.engine) as db:
        if db.get(User, uid) is None:
            db.add(User(id=uid, ready=True))
        db.merge(Entitlement(user_id=uid, expires_at=p.clock[0]+86400, updated_at=p.clock[0]))
        db.flush()
        payload = RadarCreate(name="Радар", brands=brands or [{"brand_id":84,"model_ids":[30] if models is None else models}],
            price_category=category, region_ids=[4] if regions is None else regions,
            year_from=2010, year_to=2020, min_profit=percent)
        value = create(db, uid, payload)
        if active:
            value = set_status(db, uid, value["id"], RadarStatus(active=True,revision=value["revision"]))
        db.commit()
        return value


def group_count(p):
    with Session(p.engine) as db:
        return len({m.feed_id for _,_,m in active_members(db)})


def test_shared_adjacent_prices_use_one_discovery_one_valuation_and_correct_users(radar):
    p=radar
    add(p,111,category=3)
    add(p,222,category=4)
    drain(p)
    assert group_count(p)==1
    p.calls.clear()
    p.ads.update({"124":p.clock[0]+1,"125":p.clock[0]+1})
    p.prices.update({"124":9999,"125":10000})
    wake(p)
    drain(p)
    assert len(searches(p))==1
    assert {(uid,car.source_id) for uid,car in p.sent}=={(111,"124"),(222,"125")}
    assert sorted(p.quotes)==["124","125"]
    assert len(details(p,"124"))==len(details(p,"125"))==1
    assert all(car.pipeline["region_id"]==4 for _,car in p.sent)
    assert not [path for path,_ in p.calls if path in {"states","categories/1/marks","categories/1/marks/84/models"}]


def test_overlap_users_and_multiple_same_owner_radars_do_not_duplicate_notifications(radar):
    p=radar
    add(p,111,percent=5)
    add(p,111,percent=10)
    add(p,222,percent=10)
    add(p,333,percent=50)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    drain(p)
    assert {(uid,car.source_id) for uid,car in p.sent}=={(111,"124"),(222,"124")}
    assert p.quotes==["124"] and len(details(p,"124"))==1
    p.runner=Monitor(p.engine,p.runner.settings,p.runner.search_factory,p.runner.sender)
    wake(p)
    drain(p)
    assert len(p.sent)==2


def test_paused_radars_make_no_requests_and_keep_configuration(radar):
    p=radar
    value=add(p,111,active=False)
    drain(p)
    assert p.calls==[] and p.sent==[]
    with Session(p.engine) as db:
        assert db.get(Radar,value["id"]).status=="paused"
        assert list(db.scalars(select(Search).join(RadarSearch).where(Search.enabled)))==[]


def test_expiry_then_renewal_between_ticks_requires_manual_resume(radar):
    p=radar
    value=add(p,111)
    with Session(p.engine) as db:
        row=db.get(Radar,value["id"])
        row.active_until=p.clock[0]+1
        db.commit()
    p.clock[0]+=2
    # Entitlement already renewed, but the old activation has elapsed.
    with Session(p.engine) as db:
        db.get(Entitlement,111).expires_at=p.clock[0]+86400
        db.commit()
    drain(p)
    assert p.calls==[]
    with Session(p.engine) as db:
        row=db.get(Radar,value["id"])
        assert row.status=="subscription_expired"
        assert not list(active_members(db))
        set_status(db,111,row.id,RadarStatus(active=True,revision=row.revision))
        db.commit()
    drain(p)
    assert group_count(p)==1


def test_continuous_renewal_observed_before_expiry_keeps_active(radar):
    p=radar
    value=add(p,111)
    with Session(p.engine) as db:
        old=db.get(Radar,value["id"]).active_until
        db.get(Entitlement,111).expires_at=old+86400
        db.commit()
    assert sync_expired(p.engine)==1
    with Session(p.engine) as db:
        row=db.get(Radar,value["id"])
        assert row.status=="active" and row.active_until==old+86400


def test_expiry_after_discovery_prevents_provider_evaluation(radar):
    p=radar
    value=add(p,111)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    assert p.runner.tick()
    with Session(p.engine) as db:
        db.get(Entitlement,111).expires_at=p.clock[0]-1
        db.commit()
    drain(p)
    assert not details(p,"124") and not p.quotes and not p.sent


def test_pause_after_evaluation_before_send_cancels_queued_delivery(radar):
    p=radar
    value=add(p,111)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    for _ in range(6):
        p.runner.tick()
    from backend.worker import enqueue
    enqueue(p.engine,require_provider_range=True)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)) is not None
        row=db.get(Radar,value["id"])
        set_status(db,111,row.id,RadarStatus(active=False,revision=row.revision))
        db.commit()
    p.runner.deliver_tick()
    assert not p.sent


def test_delete_removes_only_derived_execution_and_preserves_receipts(radar):
    p=radar
    value=add(p,111)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        child=list(db.scalars(select(RadarSearch.search_id)))
        row=db.get(Radar,value["id"])
        remove(db,111,row.id,row.revision)
        db.commit()
    with Session(p.engine) as db:
        assert not list(db.scalars(select(RadarSearch)))
        assert not list(db.scalars(select(Search).where(Search.id.in_(child))))
        for cls in (MonitorSeen,MonitorMatch,MonitorMembership,MonitorWatch):
            assert not list(db.scalars(select(cls).where(cls.search_id.in_(child))))
        assert db.scalar(select(Delivery)).state=="sent"
        assert db.get(Search,1) is not None  # Unrelated old configuration preserved.


def test_multi_brand_and_model_branches_stay_inside_one_slot(radar):
    p=radar
    value=add(p,111,brands=[{"brand_id":84,"model_ids":[30,31]},{"brand_id":9,"model_ids":[32]}])
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(Radar).where(Radar.user_id==111))))==1
        children=list(db.scalars(select(Search).join(RadarSearch).where(RadarSearch.radar_id==value["id"])))
        assert {(s.filters["brand"],s.filters["model"]) for s in children}=={("Volkswagen","Golf"),("Volkswagen","Passat"),("BMW","320")}


def test_pagination_restart_shared_discovery_retains_all_listings(radar):
    p=radar
    add(p,111)
    add(p,222)
    drain(p)
    p.calls.clear()
    p.ads.update({str(n):p.clock[0]+1 for n in range(1000,1057)})
    wake(p)
    assert p.runner.tick()
    p.runner=Monitor(p.engine,p.runner.settings,p.runner.search_factory,p.runner.sender)
    drain(p)
    for _ in range(120):
        p.runner.deliver_tick()
    assert len(p.quotes)==57 and len(set(p.quotes))==57
    assert {params["page"] for params in searches(p)}=={0,1}
    assert len(p.sent)==114


def test_separate_radar_feeds_and_evaluation_both_progress_during_large_page_backlog(radar):
    p=radar
    add(p,111,models=[30])
    add(p,222,models=[31])
    drain(p)
    assert group_count(p)==2  # Distinct models must not broaden into all models.
    p.calls.clear()
    p.ads.update({str(n):p.clock[0]+1 for n in range(1000,1300)})
    wake(p)
    started=p.clock[0]
    for _ in range(8):
        p.runner.tick()
        p.clock[0]+=.01
    with Session(p.engine) as db:
        feeds={m.feed_id for _,_,m in active_members(db)}
        assert all(db.get(MonitorFeed,key).checked_at>=started for key in feeds)
        assert db.scalar(select(MonitorJob).where(MonitorJob.state=="checked")) is not None
    assert {params["model_id[0]"] for params in searches(p)}=={30,31}


def test_unpriced_cards_are_never_radar_profit_alerts(radar,monkeypatch):
    p=radar
    add(p,111)
    enable(p,monkeypatch,response={"statisticData":[]})
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    drain(p)
    assert not p.sent


def test_valid_peer_valuation_cannot_queue_or_send_radar_when_ai_flag_is_off(radar):
    from dataclasses import replace
    from backend.models import Car, Listing
    from backend.tests.test_valuation import sample
    from backend.valuation import estimate
    from backend.worker import deliver_one, eligible, enqueue, fresh
    p=radar
    value=add(p,111,category=3)
    candidate,peers=sample(8500)
    rating=estimate(candidate,peers,now=p.clock[0])
    car=Car(source="auto_ria",source_id=candidate["id"],url=candidate["url"],
        brand=candidate["brand"],model=candidate["model"],region=candidate["region"],
        body=candidate["body"],fuel=candidate["fuel"],transmission=candidate["transmission"],
        year=candidate["year"],mileage=candidate["mileage"],price=candidate["price_usd"],
        market=rating["market"],comparables=rating["comparables"],observed_at=candidate["observed_at"],
        valuation_evidence=rating["valuation_evidence"],pipeline={"region_id":candidate["region_id"]})
    with Session(p.engine) as db:
        search=db.scalar(select(Search).join(RadarSearch).where(RadarSearch.radar_id==value["id"]))
        watch=db.get(MonitorWatch,search.id)
        listing=Listing(source=car.source,source_id=car.source_id,car=car.model_dump(mode="json"))
        db.add(listing)
        db.flush()
        db.add(MonitorMatch(search_id=search.id,listing_id=listing.id,epoch=watch.epoch,fingerprint=search.fingerprint))
        db.commit()
        assert fresh(car,p.clock[0],db)  # Real valid legacy proof, not a malformed fixture.
        assert not eligible(db,111,listing,p.clock[0],require_provider_range=False,require_confirmed_deal=False)
        listing_id=listing.id
    enqueue(p.engine,now=p.clock[0],require_provider_range=False,require_confirmed_deal=False)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)) is None
        db.add(Delivery(user_id=111,listing_id=listing_id,state="pending"))
        db.commit()
    settings=replace(p.runner.settings,ria_ai_price_enabled=False,ria_confirmed_deals_only=False)
    assert deliver_one(p.engine,settings,p.runner.sender,now=p.clock[0])=="cancelled"
    assert not p.sent


def test_monitor_does_not_create_radar_matches_from_valid_peer_rating(radar):
    from dataclasses import replace
    from backend.models import Listing
    from backend.monitor import source_filters
    from backend.radar_monitor import parameters
    from backend.tests.test_valuation import sample
    from backend.valuation import estimate
    p=radar
    value=add(p,111,category=3)
    candidate,peers=sample(8500)
    rating=estimate(candidate,peers,now=p.clock[0])
    assert rating["market"]==10000
    with Session(p.engine) as db:
        search=db.scalar(select(Search).join(RadarSearch).where(RadarSearch.radar_id==value["id"]))
        watch=db.get(MonitorWatch,search.id)
        _,resolved=parameters(db,Filters.model_validate(search.filters))
        evidence={"candidate":candidate,"rating":rating,
                  "filters":{source_filters(search.filters).fingerprint():resolved}}
        db.add(MonitorJob(source_id=candidate["id"],first_seen=p.clock[0],state="pending"))
        db.add(MonitorSeen(search_id=search.id,source_id=candidate["id"],epoch=watch.epoch,
                           first_seen=p.clock[0],state="pending"))
        db.commit()
    p.runner.settings=replace(p.runner.settings,ria_ai_price_enabled=False,ria_confirmed_deals_only=False)
    assert p.runner.claim()
    try:
        p.runner.complete(candidate["id"],evidence,"checked")
    finally:
        p.runner.release("idle")
    with Session(p.engine) as db:
        assert db.scalar(select(Listing)) is None
        assert db.scalar(select(MonitorMatch)) is None
        assert db.get(MonitorJob,candidate["id"]).state=="checked"


def test_delivery_uses_official_region_id_not_cosmetic_region_label(radar):
    from backend.models import Listing
    p=radar
    add(p,111)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    for _ in range(6):
        p.runner.tick()
    with Session(p.engine) as db:
        row=db.scalar(select(Listing).where(Listing.source_id=="124"))
        assert row.car["pipeline"]["region_id"]==4
        row.car={**row.car,"region":"Інша назва тієї самої області"}
        db.commit()
    p.runner.deliver_tick()
    assert [(uid,car.source_id) for uid,car in p.sent]==[(111,"124")]


@pytest.mark.parametrize("flag",["damage","technical_condition"])
def test_damage_markers_do_not_filter_radar_or_add_telegram_warnings(radar,flag):
    from backend.ria_market_range import evidence_valid
    from backend.tests.test_informational_alerts import alter_details
    from backend.worker import TelegramSender, fresh
    p=radar
    add(p,111)
    def mutate(data):
        if flag=="technical_condition":
            data["technicalCondition"]={"id":3}
        else:
            data["autoInfoBar"][flag]=True
    alter_details(p,mutate)
    drain(p)
    p.ads["124"]=p.clock[0]+1
    wake(p)
    drain(p)
    assert [(uid,car.source_id) for uid,car in p.sent]==[(111,"124")]
    car=p.sent[0][1]
    assert car.market==13537.5 and car.valuation_evidence["condition_notices"]==[flag]
    assert evidence_valid(car,p.clock[0]) and fresh(car,p.clock[0],require_provider_range=True)
    text,_=TelegramSender.card(car)
    assert "ремонт" not in text and "пошкоджен" not in text
    # The condition is already present in the normal listing details. It
    # causes neither a separate request nor a peer-comparison fallback.
    assert p.quotes==["124"] and len(details(p,"124"))==1
    assert all(not any(key in params for key in ("damage","onRepairParts","technicalCondition[0]"))
        for params in searches(p))


def item(**kwargs):
    filters=Filters(brand="Volkswagen",model="Golf",price={"from":5000,"to":9999.999999999998},year={"from":2010,"to":2020},**kwargs)
    return (SimpleNamespace(filters=filters.canonical()),None,None)


def test_planner_does_not_fill_gaps_or_merge_different_models():
    first=item()
    second=(SimpleNamespace(filters={**first[0].filters,"price":{"from":15000,"to":19999.999999999996}}),None,None)
    assert len(plan([first,second]))==2
    model=(SimpleNamespace(filters={**first[0].filters,"model":"Passat"}),None,None)
    assert len(plan([first,model]))==2


def test_planner_unions_regions_only_when_other_dimensions_equal():
    first=item(region=["Хмельницька область"])
    second=item(region=["Вінницька область"])
    grouped=plan([first,second])
    assert len(grouped)==1
    assert set(grouped[0]["filters"].regions)=={"Хмельницька область","Вінницька область"}
    all_country=item(region=[])
    assert plan([first,second,all_country])[0]["filters"].regions==[]


def test_planner_unions_adjacent_years_but_preserves_year_gaps():
    base=item(region=["Хмельницька область"])[0].filters
    def years(low,high):
        return (SimpleNamespace(filters={**base,"year":{"from":low,"to":high}}),None,None)
    merged=plan([years(2010,2015),years(2016,2020)])
    assert len(merged)==1
    assert (merged[0]["filters"].year.from_,merged[0]["filters"].year.to)==(2010,2020)
    assert len(plan([years(2010,2015),years(2017,2020)]))==2


def test_planner_preserves_exact_price_year_region_union_without_cross_products():
    import itertools
    import math
    base=item()[0].filters
    # A complete price/region rectangle can merge, whereas its neighbour in
    # another year band must not introduce cars from a missing region or year.
    rows=[]
    for low,high,start,end,region in [
        (5000,10000,2010,2015,"Хмельницька область"),
        (10000,15000,2010,2015,"Хмельницька область"),
        (5000,10000,2010,2015,"Вінницька область"),
        (10000,15000,2010,2015,"Вінницька область"),
        (15000,20000,2016,2020,"Хмельницька область")]:
        filters={**base,"price":{"from":low,"to":math.nextafter(float(high),-math.inf)},
                 "year":{"from":start,"to":end},"region":[region]}
        rows.append((SimpleNamespace(filters=filters),None,None))
    groups=plan(rows)
    assert len(groups)==2
    original=[Filters.model_validate(row[0].filters) for row in rows]
    merged=[group["filters"] for group in groups]
    def contains(filters,price,year,region):
        return (filters.price.from_<=price<=filters.price.to
            and filters.year.from_<=year<=filters.year.to
            and (not filters.regions or region in filters.regions))
    for price,year,region in itertools.product(
        (4999,5000,9999.99,10000,14999.99,15000,19999.99,20000),
        (2009,2010,2015,2016,2020,2021),
        ("Хмельницька область","Вінницька область","Інша область")):
        assert any(contains(f,price,year,region) for f in original)==any(
            contains(f,price,year,region) for f in merged)
    assert {id(row) for group in groups for row in group["rows"]}=={id(row) for row in rows}
    assert [group["filters"].fingerprint() for group in groups]==[
        group["filters"].fingerprint() for group in plan(list(reversed(rows)))]


def test_planner_scale_preserves_each_member_once_and_is_deterministic():
    # Many overlapping users reduce to six adjacent price rectangles, never
    # thousands of copies or a new provider request for each subscription.
    from backend.radar_monitor import PRICE_BOUNDS
    import math
    rows=[]
    for n in range(2400):
        low,high=PRICE_BOUNDS[n%6+1]
        base=item()[0].filters
        base={**base,"price":{"from":low,"to":math.nextafter(float(high),-math.inf) if high else None}}
        rows.append((SimpleNamespace(filters=base,number=n),None,None))
    grouped=plan(rows)
    assert len(grouped)==1
    assert sorted(r[0].number for r in grouped[0]["rows"])==list(range(2400))
    assert grouped[0]["filters"].price.from_==0 and grouped[0]["filters"].price.to is None
    assert plan(list(reversed(rows)))[0]["filters"].fingerprint()==grouped[0]["filters"].fingerprint()


@pytest.mark.parametrize("category",range(1,7))
def test_all_price_categories_are_half_open_before_valuation(radar,category):
    from backend.radar_monitor import parameters,PRICE_BOUNDS
    from backend.ria_search import matches
    p=radar
    value=add(p,111,category=category)
    with Session(p.engine) as db:
        search=db.scalar(select(Search).join(RadarSearch).where(RadarSearch.radar_id==value["id"]))
        filters=Filters.model_validate(search.filters)
        wire,ids=parameters(db,filters)
        low,high=PRICE_BOUNDS[category]
        def candidate(price):
            return {"brand_id":84,"model_id":30,"region_id":4,"year":2017,"price_usd":price}
        assert matches(candidate(low),filters,ids)
        if low:
            assert not matches(candidate(low-.01),filters,ids)
        if high:
            assert wire["price_do"]==high  # Provider query retains boundary coverage.
            assert matches(candidate(high-.01),filters,ids)
            assert not matches(candidate(high),filters,ids)
        else:
            assert "price_do" not in wire and matches(candidate(1000000),filters,ids)


@pytest.mark.parametrize("blocked",["frozen_page","quota_exceeded"])
def test_grouping_does_not_replace_frozen_windows_or_bypass_backoff(radar,blocked):
    from backend.radar_discovery import merge
    from backend.monitor import source_filters
    p=radar
    add(p,111,category=3)
    add(p,222,category=4)
    with Session(p.engine) as db:
        for search,_,member in active_members(db):
            db.add(MonitorFeed(id=member.feed_id,filters=source_filters(search.filters).canonical(),
                started_at=member.started_at,cursor=member.started_at,status="watching",next_poll=p.clock[0]+300))
        db.flush()
        feed=db.scalar(select(MonitorFeed))
        if blocked=="frozen_page":
            feed.context={"page":1,"slices":[{"after":1,"before":2,"members":[]}]}
        else:
            feed.status="quota_exceeded"
        db.commit()
    assert p.runner.claim()
    try:
        with Session(p.engine) as db:
            before={m.search_id:m.feed_id for m in db.scalars(select(MonitorMembership))}
            merge(p.runner,db)
            db.commit()
            assert {m.search_id:m.feed_id for m in db.scalars(select(MonitorMembership))}==before
    finally:
        p.runner.release("idle")


def test_resume_does_not_rewind_healthy_shared_feed_to_dormant_cursor(radar):
    p=radar
    first=add(p,111)
    add(p,222)
    drain(p)
    wake(p,600)
    drain(p)
    with Session(p.engine) as db:
        row=db.get(Radar,first["id"])
        paused=set_status(db,111,row.id,RadarStatus(active=False,revision=row.revision))
        db.commit()
    wake(p,600)
    drain(p)
    with Session(p.engine) as db:
        member=db.scalar(select(MonitorMembership).join(Search,Search.id==MonitorMembership.search_id).where(Search.user_id==222))
        healthy_cursor=db.get(MonitorFeed,member.feed_id).cursor
        row=db.get(Radar,first["id"])
        set_status(db,111,row.id,RadarStatus(active=True,revision=row.revision))
        db.commit()
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")
    with Session(p.engine) as db:
        active={m.feed_id for _,_,m in active_members(db)}
        assert len(active)==1
        assert db.get(MonitorFeed,next(iter(active))).cursor>=healthy_cursor
