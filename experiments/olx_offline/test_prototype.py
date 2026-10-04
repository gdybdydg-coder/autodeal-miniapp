import dataclasses
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from prototype import Store, FixtureTransport, normalize, matches, possible_duplicate


def raw(id='1',**changes):
    return {'id':id,'url':'https://example.invalid/synthetic/'+id,'title':'Synthetic whole car',
            'category':'whole_passenger_car','brand':'Fixture','model':'Model','year':2015,'region':'Київська область',
            'price':5000,'currency':'USD','price_kind':'full','published_at':1001,'publication_verified':True,**changes}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'offline.sqlite';self.db=Store(self.path)
        self.fences=[patch.object(socket.socket,'connect',side_effect=AssertionError('No external requests')),
                     patch.object(socket,'getaddrinfo',side_effect=AssertionError('No DNS'))]
        for fence in self.fences:fence.start()
    def tearDown(self):
        self.db.close();self.tmp.cleanup()
        for fence in self.fences:fence.stop()
    def collect(self,rows,now=1002,**kwargs):
        return self.db.collect(FixtureTransport({None:{'items':rows,'next':None}}),now,**kwargs)
    def baseline(self):self.collect([],1000)

    def test_missing_optional_fields_and_whole_damaged_car_allowed(self):
        car=normalize(raw(condition='whole_car_for_parts'),1002)
        self.assertIsNone(car.fuel);self.assertEqual(car.photos,())
        self.assertTrue(matches(car,{'fuel':['Бензин'],'transmission':['Автомат']}))
        self.assertEqual(car.valuation_status,'profitability_unconfirmed')

    def test_parts_rental_services_and_unknown_category_excluded(self):
        for category in ('part','rental','service',None):
            with self.subTest(category=category):self.assertFalse(matches(normalize(raw(category=category),1002),{}))

    def test_known_mismatch_and_currency_conversion_never_invented(self):
        car=normalize(raw(fuel='Дизель',currency='UAH'),1002)
        self.assertFalse(matches(car,{'fuel':['Бензин']}))
        self.assertIsNone(matches(car,{'currency':'USD','price_max':7000}))

    def test_placeholder_downpayment_and_nonfinite_price(self):
        for changes in ({'price':1},{'price':None},{'price':'NaN'},{'price':True},{'price_kind':'downpayment'},{'currency':'BTC'}):
            with self.subTest(changes=changes):self.assertIsNone(matches(normalize(raw(**changes),1002),{}))

    def test_publication_and_first_seen_not_update_or_bump(self):
        self.baseline();self.collect([raw()],1002);self.collect([raw(updated_at=1100,bumped_at=1100)],1100)
        car=self.db.cached()[0];self.assertEqual(car.first_seen_at,1002);self.assertEqual(car.published_at,1001)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM publications').fetchone()[0],1)
        self.assertEqual(self.db.db.execute('SELECT status FROM publications').fetchone()[0],'fresh_unvalued')

    def test_initial_baseline_unknown_date_and_old_id_new_publication(self):
        self.collect([raw('1',published_at=900)],1000)
        self.collect([raw('1',published_at=1001),raw('2',published_at=None)],1002)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM publications WHERE status='fresh_unvalued'").fetchone()[0],1)
        self.assertEqual(len(self.db.cached()),2)

    def test_atomic_pages_restart_progress_and_overlap(self):
        self.baseline()
        pages={None:{'items':[raw('1')],'next':'P2'},'P2':{'items':[raw('2')],'next':None,'checkpoint':'C1'}}
        result=self.db.collect(FixtureTransport(pages),1002);self.assertEqual(result['calls'],2)
        self.db.close();self.db=Store(self.path)
        transport=FixtureTransport({'C1':{'items':[raw('1')],'next':None,'checkpoint':'C2'}})
        self.db.collect(transport,1100);self.assertEqual(transport.calls,[('C1',1000)])
        self.assertEqual(len(self.db.cached()),2)

    def test_error_page_rolls_back_cursor_and_replays_after_restart(self):
        self.baseline();pages={None:{'items':[raw()],'next':'P2'},'P2':TimeoutError()}
        self.assertEqual(self.db.collect(FixtureTransport(pages),1002)['status'],'retryable')
        self.assertEqual(len(self.db.cached()),0)
        self.assertEqual(self.db.db.execute('SELECT last_success FROM state').fetchone()[0],1000)
        self.db.close();self.db=Store(self.path)
        self.assertEqual(self.collect([raw()],1003)['status'],'committed')

    def test_pagination_cycle_conflict_and_budget_fail_closed(self):
        self.baseline()
        for pages,limit in [({None:{'items':[raw()],'next':'P2'},'P2':{'items':[],'next':'P2'}},20),
                            ({None:{'items':[raw()],'next':'P2'},'P2':{'items':[raw(price=6000)],'next':None}},20),
                            ({None:{'items':[raw()],'next':'P2'}},1)]:
            self.assertEqual(self.db.collect(FixtureTransport(pages),1002,max_pages=limit)['status'],'retryable')
            self.assertEqual(len(self.db.cached()),0)

    def test_possible_cross_platform_duplicate_is_retained(self):
        a=normalize(raw(),1002);b=normalize(raw(),1002,source='auto_ria')
        self.assertEqual(possible_duplicate(a,b),'possible_cross_source_duplicate')
        self.assertEqual(len({(a.source,a.id),(b.source,b.id)}),2)
        self.assertIsNone(possible_duplicate(a,dataclasses.replace(b,year=2016)))

    def test_200_users_shared_load_no_telegram_no_prod_imports(self):
        self.baseline();pages={}
        for p in range(20):
            pages[None if p==0 else str(p)]={'items':[raw(str(p*25+i+1)) for i in range(25)],'next':str(p+1) if p<19 else None}
        transport=FixtureTransport(pages);self.db.collect(transport,1002)
        users=[(uid,{'currency':'USD','price_max':7000},True) for uid in range(200)]
        counts=self.db.fanout(users)
        self.assertEqual((len(transport.calls),counts['comparisons'],counts['matching_unvalued_pairs'],counts['fake_sends']),(20,100000,100000,0))
        self.assertEqual(self.db.fanout([(1,{},False)])['comparisons'],0)
        source=Path(__file__).with_name('prototype.py').read_text()
        self.assertNotIn('from backend',source);self.assertNotIn('import backend',source)


if __name__=='__main__':unittest.main()
