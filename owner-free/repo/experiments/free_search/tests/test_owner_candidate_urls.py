from unittest import TestCase
from experiments.free_search.owner_candidate_probe import has_detail_url


class CandidateURLTests(TestCase):
    def test_missing_and_foreign_link_not_selected_for_fetch(self):
        valid={'listing_id':'123','url':'https://auto.ria.com/uk/auto_example_model_123.html'}
        self.assertTrue(has_detail_url(valid))
        for url in (None,'https://auto.ria.com/newauto/123','https://example.com/123',
                    'https://auto.ria.com/uk/auto_example_model_999.html'):
            self.assertFalse(has_detail_url({**valid,'url':url}))
