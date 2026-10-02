import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from experiments.olx_offline.pipeline import Pipeline, canonical, estimate, filtered, source_selection, message

NOW=1790942400

def raw(i, **changes):
    r=dict(id=str(i),source='olx',url='https://example.invalid/car/'+str(i),title='Volkswagen Golf — синтетичний приклад',
           price=7000,currency='USD',price_kind='full',category='whole_passenger_car',brand='Volkswagen',model='Golf',
           generation='VII',body='hatchback',engine_cc=1400,year=2016,mileage_km=120000,region='Київська',
           locality='Київ',fuel='petrol',transmission='manual',published_at=NOW,publication_verified=True,checked_at=NOW-100,evidence='synthetic')
    r.update(changes);return r

def comps():
    return [canonical(raw('comp'+str(i),price=9800+i*50),NOW-100) for i in range(12)]

def users(n=1):
    return [dict(id=str(i),source='OLX',paid=True,stopped=False,filters={},min_discount=15) for i in range(n)]

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'research.sqlite';self.p=Pipeline(self.path)
        self.net=patch('socket.socket',side_effect=AssertionError('Network forbidden'));self.net.start()
    def tearDown(self):
        self.p.db.close();self.tmp.cleanup();self.net.stop()
    def collect(self,rows):
        return self.p.collect(lambda _:dict(items=rows,next=None),NOW,page_budget=1,row_budget=100)
    def test_pagination_restart_partial(self):
        pages={None:dict(items=[raw(1)],next='two'),'two':dict(items=[raw(2)],next=None)}
        self.assertEqual(self.p.collect(pages.__getitem__,NOW,page_budget=1,row_budget=10)['status'],'incomplete')
        self.p.db.close();self.p=Pipeline(self.path)
        self.assertEqual(self.p.collect(pages.__getitem__,NOW,page_budget=1,row_budget=10)['status'],'complete')
        self.assertEqual(len(self.p.cars()),2)
    def test_baseline_startup_new_and_bump(self):
        self.collect([raw(1,published_at=NOW-100),raw(2),raw(3,publication_verified=False)])
        self.assertEqual(len(self.p.cars(True)),1)
        self.collect([raw(1,published_at=NOW,bumped_at=NOW)])
        self.assertEqual(len(self.p.cars(True)),1)
    def test_queue_overflow_resume_no_duplicate(self):
        self.collect([raw(1),raw(2)])
        self.assertEqual(self.p.enqueue(users(2),comps(),NOW,capacity=1,olx_enabled=True)['overflow'],3)
        self.assertEqual(self.p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True)['accepted'],1)
        self.assertEqual(self.p.enqueue(users(2),comps(),NOW,capacity=10,olx_enabled=True)['queued'],3)
        self.assertEqual(self.p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True)['accepted'],3)
        self.p.db.close();self.p=Pipeline(self.path)
        self.assertEqual(self.p.enqueue(users(2),comps(),NOW,capacity=10,olx_enabled=True)['queued'],0)
    def test_access_rechecked_and_timeout(self):
        self.collect([raw(1)])
        self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)
        self.assertEqual(self.p.deliver_fake(lambda *_:self.fail(),lambda _:False,now=NOW,olx_enabled=True)['denied'],1)
        def timeout(*_):raise TimeoutError()
        self.assertEqual(self.p.deliver_fake(timeout,lambda _:True,now=NOW,olx_enabled=True)['uncertain'],1)
        self.assertEqual(self.p.deliver_fake(lambda *_:self.fail(),lambda _:True,now=NOW,olx_enabled=True)['accepted'],0)
    def test_paid_stop_kill_default_sources(self):
        self.collect([raw(1)])
        u=users(3);u[0]['paid']=False;u[1]['stopped']=True;u[2].pop('source')
        self.assertEqual(self.p.enqueue(u,comps(),NOW,capacity=10,olx_enabled=True)['queued'],0)
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=10)['queued'],0)
        self.assertEqual(source_selection(),('auto_ria',))
    def test_price_and_missing_and_condition(self):
        for kind in ('deposit','monthly','conditional','part',None):
            self.assertEqual(estimate(canonical(raw(1,price_kind=kind),NOW),comps(),NOW)['status'],'profitability_unconfirmed')
        self.assertEqual(estimate(canonical(raw(1,generation=None),NOW),comps(),NOW)['status'],'profitability_unconfirmed')
        self.assertEqual(estimate(canonical(raw(1,condition='damaged'),NOW),comps(),NOW)['status'],'experimental_estimate')
        self.assertEqual(estimate(canonical(raw(1,currency='EUR'),NOW),comps(),NOW)['status'],'profitability_unconfirmed')
    def test_comparables_dedup_stale_and_different_model(self):
        target=canonical(raw(1),NOW)
        self.assertEqual(estimate(target,[comps()[0]]*20,NOW)['sample'],1)
        self.assertEqual(estimate(target,comps(),NOW+31*86400)['sample'],0)
        self.assertEqual(estimate(canonical(raw(1,model='Polo'),NOW),comps(),NOW)['sample'],0)
    def test_optional_filters_and_text_fallback(self):
        c=canonical(raw(1,fuel=None,transmission=None),NOW)
        self.assertTrue(filtered(c,{'fuel':['diesel'],'transmission':['automatic']}))
        self.assertFalse(filtered(c,{'body':['sedan']}))
        self.assertIsNone(message(c)['photo'])
        self.assertIsNone(filtered(c,{'price_max':10000,'currency':'EUR'}))
    def test_failure_backoff_does_not_lose_page(self):
        def fetch(cursor):
            if cursor:raise TimeoutError()
            return dict(items=[raw(1)],next='two')
        result=self.p.collect(fetch,NOW,page_budget=2,row_budget=10)
        self.assertEqual(result['status'],'incomplete');self.assertEqual(len(self.p.cars()),1)
        self.assertEqual(self.p.collect(fetch,NOW+1,page_budget=2,row_budget=10)['status'],'paused')
    def test_source_namespace_and_row_budget(self):
        self.collect([raw(1),raw(1,source='auto_ria')]);self.assertEqual(len(self.p.cars()),2)
        r=self.p.collect(lambda _:dict(items=[raw(2),raw(3)],next=None),NOW,page_budget=1,row_budget=1)
        self.assertEqual(r['reason'],'row_budget');self.assertEqual(len(self.p.cars()),2)
    def test_unknown_publication_can_be_verified_later(self):
        self.collect([raw(1,publication_verified=False)])
        self.assertEqual(len(self.p.cars(True)),0)
        self.collect([raw(1)])
        self.assertEqual(len(self.p.cars(True)),1)
    def test_retry_ceiling(self):
        def fail(_):raise TimeoutError()
        for delta in (0,1000,2000):
            self.p.collect(fail,NOW+delta,page_budget=1,row_budget=1)
        self.assertEqual(self.p.collect(fail,NOW+10000,page_budget=1,row_budget=1)['status'],'paused_manual_review')
    def test_crashed_sending_is_uncertain(self):
        self.p.db.execute("INSERT INTO deliveries VALUES('1','olx','1','sending')");self.p.db.commit()
        self.p.db.close();self.p=Pipeline(self.path)
        self.assertEqual(self.p.db.execute('SELECT status FROM deliveries').fetchone()[0],'uncertain')

if __name__=='__main__':unittest.main()
