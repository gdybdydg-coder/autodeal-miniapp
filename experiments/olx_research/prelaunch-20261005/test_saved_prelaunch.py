"""Saved live Oct5 detail receipts; offline replay, never Telegram/readiness proof."""
import json
from pathlib import Path
import socket

# Fence before importing project code. Never import app or load .env here.
def deny(*a,**k):raise AssertionError('saved_prelaunch_network_forbidden')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.socket.sendto=deny;socket.getaddrinfo=deny
from backend.olx_market.fx_policy import normalize
from backend.olx_market.candidates import filter_reasons
from backend.olx_market.valuation import estimate
from backend.olx_market.observations import enrich
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
P=Path(__file__).parent

def test_current_three_real_details_have_no_fabricated_market_or_discount():
    cars=json.loads((P/'fresh-details-sanitized.json').read_text())
    e=json.loads((P/'saved-real-evaluation.json').read_text())
    now=e['checked_at']
    assert len(cars)==3 and {c['id'] for c in cars}=={'936658970','936768428','933340614'}
    filters={'currency':'USD','region':['вінницька','тернопільська','хмельницька','чернівецька'],'price_min':1000,'price_max':7000}
    for car in cars:
        price=normalize(car,None,now)
        assert price['status']=='ready' and price['usd_amount']==car['price'] and price['fx'] is None
        assert filter_reasons(car,filters,None,now)['match']
        assert car['market_reference_usd'] is None and car['benefit_percent'] is None
        a=estimate(car,cars,None,now,minimum=8)
        assert a['reference_usd'] is None and not a['methods']
    damaged=next(c for c in cars if c['id']=='936658970')
    assert damaged['research_condition']=='running_reported_damage:body_dents+windshield_crack'
    assert e['technical_ready'] is False and e['asking_estimates']==0
    assert e['frozen_holdout']==3 and e['estimated_holdout']==0
    assert e['paid_ria_calls']==0 and e['telegram_calls']==0
    assert e['selected_method'] is None

def test_actual_source_receipts_stay_within_new_packet_and_cannot_be_old_budget():
    r=json.loads((P/'source-receipt.json').read_text())
    plan=json.loads((P/'source-plan.json').read_text())
    assert r['reservation_commit']=='072c02e545771eee9aea3ebc74807bde71031a41'
    assert r['actual_gets']==3 and r['actual_bytes']==4714020
    assert r['actual_gets']<=plan['budget']['olx_get_cap']
    assert r['actual_bytes']<=plan['budget']['byte_cap']
    assert [c['url'] for c in r['calls']]==[c['url'] for c in plan['requests']]
    assert all(c['http']==200 and c['parsed'] is True for c in r['calls'])
    assert r['source_hold'] is None and r['retry_count']==0
    assert r['fx_calls']==r['paid_ria_calls']==r['telegram_calls']==0
