from dataclasses import replace
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from experiments.free_search.owner_peer_probe import freeze, run
from experiments.free_search.owner_peer_cache import PeerCache
from experiments.free_search.tests.test_owner_observations import card


class FreezeTests(unittest.TestCase):
    def test_fixed_limit_identity_and_order_not_price(self):
        rows = [card(str(i)) for i in range(1, 16)]
        rows.insert(1, card('1'))
        result = freeze(rows, '1')
        self.assertEqual([x[0] for x in result], [str(i) for i in range(2, 12)])

    def test_promoted_and_unresolved_records_not_silent_comparables(self):
        result = freeze([replace(card('1'), promoted=True),
                         replace(card('2'), issues=('missing_price',)), card('3')], '9')
        self.assertEqual([x[0] for x in result], ['3'])

    def test_fresh_cached_subject_and_peer_skip_detail_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp)/'cache.owner-peers.sqlite3'
            cache = PeerCache(cache_path)
            for ident in ('1', '2'):
                cache.put(ident, 90, row=dict(listing_id=ident, observed_at=90,
                    brand='Volkswagen', model='Tiguan', year=2022, fuel='Дизель',
                    transmission='Автомат', mileage_km='95000', price='30000',
                    currency='USD', generation='II', drive='Повний'))
            cache.close()
            with patch('experiments.free_search.owner_peer_probe.PublicFetch') as fetch, \
                    patch('experiments.free_search.owner_peer_probe.time.time', return_value=100), \
                    patch('experiments.free_search.owner_peer_probe.time.sleep'), \
                    patch('experiments.free_search.owner_peer_probe.parse_public_cards', return_value=[card('2')]):
                fetch.return_value.get.return_value = ('<html/>', {'completed_at': 100})
                result = run(Path(tmp)/'ledger.sqlite3',
                    'https://auto.ria.com/uk/car/volkswagen/tiguan/year/2022/',
                    card('1').url, cache_path)
                self.assertEqual(fetch.return_value.get.call_count, 2)
            self.assertEqual(result['cache_hits'], 2)
            self.assertEqual(result['status'], 'bounded_comparison_complete')
            self.assertFalse(result['ready_for_delivery'])
