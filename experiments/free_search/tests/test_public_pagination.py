from unittest import TestCase
from experiments.free_search.public_pagination import inspect_page,assess_scan,route_page

BASE='https://auto.ria.com/uk/last/hour/'

def fixture(page=0,size=100,scope='lang_id=4&top=1',terminal=False,underscore=False):
    key='data-search_query' if underscore else 'data-search-query'
    navigation=("<a class='page-link js-next disabled' href='javascript:void(0)'></a>" if terminal else
                f"<a class='page-link js-next' href='{BASE}?page={page+2}'></a>")
    return f"<script {key}='page={page}&countpage={size}&{scope}'></script><div id='pagination'>{navigation}</div>"


class PaginationTests(TestCase):
    def test_observed_size_change_is_incomplete(self):
        pages=[inspect_page(fixture(),BASE),inspect_page(fixture(1),BASE+'?page=2'),
               inspect_page(fixture(2,20,terminal=True),BASE+'?page=3')]
        result=assess_scan(pages)
        self.assertEqual(result['state'],'incomplete')
        self.assertIn('page_size_changed',result['reasons'])
        self.assertIsNone(result['whole_market_recall'])

    def test_missing_defaults_are_not_assumed_equivalent(self):
        a=inspect_page(fixture(),BASE)
        b=inspect_page(fixture(1,scope='lang_id=4&top=1&custom=1&abroad=2',terminal=True),BASE+'?page=2')
        self.assertIn('scope_metadata_changed',assess_scan([a,b])['reasons'])

    def test_both_source_attribute_spellings_and_order(self):
        a=inspect_page(fixture(),BASE)
        b=inspect_page(fixture(1,scope='top=1&lang_id=4',underscore=True,terminal=True),BASE+'?page=2')
        r=assess_scan([a,b]);self.assertEqual(r['state'],'declared_end_observed')
        self.assertFalse(r['atomic_snapshot_proven']);self.assertFalse(r['automatic_request_authorized'])

    def test_short_page_cannot_prove_terminal(self):
        p=inspect_page(fixture(),BASE)
        self.assertIn('source_end_not_observed',assess_scan([p])['reasons'])

    def test_deep_page_and_gap_remain_incomplete(self):
        a=inspect_page(fixture(),BASE);b=inspect_page(fixture(7,20,terminal=True),BASE+'?page=8')
        self.assertIn('page_sequence_gap',assess_scan([a,b])['reasons'])
        self.assertIn('scan_did_not_start_at_head',assess_scan([b])['reasons'])

    def test_no_metadata_or_navigation_is_unknown(self):
        p=inspect_page('<div>empty</div>',BASE)
        self.assertIn('pagination_metadata_missing',p.issues)
        self.assertIn('pagination_navigation_missing',p.issues)
        self.assertEqual(assess_scan([p])['state'],'incomplete')

    def test_conflicting_or_duplicate_metadata(self):
        p=inspect_page(fixture()+fixture(0,20),BASE)
        self.assertIn('conflicting_pagination_metadata',p.issues)
        p=inspect_page(fixture(scope='page=2'),BASE)
        self.assertIn('invalid_pagination_metadata',p.issues)

    def test_bad_next_and_route_mismatch(self):
        p=inspect_page(fixture().replace(BASE+'?page=2','https://evil.invalid/'),BASE)
        self.assertIn('invalid_next_page',p.issues)
        self.assertIn('route_page_mismatch',inspect_page(fixture(1),BASE).issues)

    def test_external_or_ambiguous_routes_denied(self):
        for url in ['http://auto.ria.com/uk/last/hour/',BASE+'?page=0',BASE+'?page=2&page=3',
                    BASE+'?page=2&api_key=x',BASE+'#x',BASE.replace('auto.ria.com','evil.invalid')]:
            with self.subTest(url=url):self.assertIsNone(route_page(url))

    def test_empty_and_oversize(self):
        self.assertEqual(assess_scan([])['state'],'incomplete')
        with self.assertRaises(ValueError):inspect_page('x'*2_500_001,BASE)
