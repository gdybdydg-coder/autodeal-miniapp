import unittest
from experiments.free_search.owner_budget import public_url,Denied

class SourceAssetTests(unittest.TestCase):
    def test_only_opted_in_observable_static_script_route(self):
        url='https://auto.ria.com/vue3-prod/static/index-VJw18PKi.js'
        with self.assertRaises(Denied):public_url(url)
        public_url(url,allow_source_assets=True)
        for bad in (url+'?key=x',url+'#x',url.replace('auto.ria.com','developers.ria.com'),
                    url.replace('index-VJw18PKi','../secret'),
                    'https://auto.ria.com/api/average/123',
                    'https://auto.ria.com/vue3-prod/static/config.json'):
            with self.assertRaises(Denied):public_url(bad,allow_source_assets=True)
