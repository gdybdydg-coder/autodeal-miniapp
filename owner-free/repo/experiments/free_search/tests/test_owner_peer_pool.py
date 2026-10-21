import tempfile
from pathlib import Path
import unittest
from experiments.free_search.owner_peer_cache import PeerCache
from experiments.free_search.owner_peer_pool import pool


class PoolTests(unittest.TestCase):
    def test_stale_subject_cannot_get_estimate_from_fresh_peers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = PeerCache(Path(tmp)/'test.owner-peers.sqlite3')
            try:
                subject = dict(listing_id='1', observed_at=100000, brand='Example', model='Model',
                    year=2022, generation='II', drive='Повний', fuel='Дизель',
                    transmission='Автомат', body='SUV', engine_cc=2000,
                    mileage_km=90000, price=8000, currency='USD')
                for ident in range(2, 8):
                    cache.put(str(ident), 100301, row={**subject, 'listing_id':str(ident),
                        'observed_at':100301, 'price':10000+ident*100})
                result = pool(subject, cache, 100301)
                self.assertEqual(result['estimate']['status'], 'unknown')
                self.assertEqual(result['subject_error'], 'subject_stale_price')
                self.assertIsNone(result['estimate']['reference_price'])
                # Exact 300-second boundary remains permitted; no threshold weakening.
                fresh = {**subject, 'observed_at':100001}
                self.assertEqual(pool(fresh, cache, 100301)['estimate']['status'], 'estimated')
                invalid = {**fresh, 'year':None}
                self.assertEqual(pool(invalid, cache, 100301)['subject_error'],
                                 'subject_core_features_missing')
            finally:
                cache.close()

    def test_missing_optional_fields_do_not_reject_and_robot_is_not_automatic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = PeerCache(Path(tmp)/'test.owner-peers.sqlite3')
            try:
                subject = dict(listing_id='1', observed_at=100000, brand='Example', model='Model',
                    year=2022, generation='II', drive='Повний', fuel=None,
                    transmission='Автомат', body='SUV', engine_cc=2000,
                    mileage_km=90000, price=8000, currency='USD')
                for ident in range(2, 8):
                    cache.put(str(ident), 100000, row={**subject, 'listing_id':str(ident),
                        'fuel':'Дизель', 'transmission':None if ident == 2 else 'Автомат',
                        'price':10000+ident*100})
                cache.put('8', 100000, row={**subject, 'listing_id':'8', 'transmission':'Робот'})
                result = pool(subject, cache, 100001)
                self.assertEqual(result['estimate']['sample_count'], 6)
                self.assertEqual(result['rejected']['8'], 'transmission_mismatch')
            finally:
                cache.close()
    def test_accumulates_across_pages_rejects_drive_staleness_and_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = PeerCache(Path(tmp)/'test.owner-peers.sqlite3')
            try:
                subject = dict(listing_id='1', observed_at=100000, brand='Example', model='Model',
                    year=2022, generation='II', drive='Повний', fuel='Дизель',
                    transmission='Автомат', body='SUV', engine_cc=2000,
                    mileage_km=90000, price=8000, currency='USD', region='Київська область')
                cache.put('1', 100000, row=subject)
                for ident in range(2, 8):
                    row = {**subject, 'listing_id':str(ident), 'price':10000+ident*100}
                    cache.put(str(ident), 100000, row=row)
                cache.put('8', 100000, row={**subject, 'listing_id':'8', 'drive':'Передній'})
                cache.put('9', 1, row={**subject, 'listing_id':'9', 'observed_at':1})
                result = pool(subject, cache, 100001)
                self.assertEqual(len(result['rows']), 6)
                self.assertEqual(result['estimate']['status'], 'estimated')
                self.assertEqual(result['rejected']['8'], 'drive_mismatch')
                self.assertEqual(result['rejected']['9'], 'stale_public_record')
                cache.put('2', 100002, reason='public_detail_conflict')
                self.assertEqual(len(pool(subject, cache, 100003)['rows']), 5)
            finally:
                cache.close()
