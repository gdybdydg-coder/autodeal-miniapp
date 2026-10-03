import json
import unittest
from experiments.olx_offline.html_snapshot import SearchParser
from experiments.olx_offline.source_dates import observe_source_dates,review_newness


NOW=1791014391
BOUNDARY=1791014200
DATES={'createdTime':'2026-09-08T14:28:20+03:00',
       'lastRefreshTime':'2026-10-03T10:48:48+03:00',
       'pushupTime':'2026-10-03T10:48:48+03:00',
       'validToTime':'2026-10-08T14:28:21+03:00'}


def nodes(ad, *, quoted=True, closed=True, extra=''):
    state={'ad':{'ad':dict(ad,private_contact='PRIVATE_CONTACT',description='PRIVATE_DESCRIPTION',vin='PRIVATE_VIN')}}
    text=json.dumps(json.dumps(state)) if quoted else json.dumps(state)
    html='<script id="olx-init-config">window.__PRERENDERED_STATE__ = '+text+';'+extra
    if closed:html+='</script>'
    p=SearchParser();p.feed(html);p.close()
    return [n for root in p.roots for n in root.nodes()]


class SourceDateTests(unittest.TestCase):
    def observe(self,ad=None,**kw):
        return observe_source_dates(nodes(dict(id=123,**DATES) if ad is None else ad,**kw),expected_id='123',fetched_at=NOW)
    def test_exact_public_contract_and_timezone_without_private_export(self):
        r=self.observe()
        self.assertTrue(r['identity_matches']);self.assertEqual(r['issues'],[])
        self.assertEqual(r['values']['lastRefreshTime']['utc'],'2026-10-03T07:48:48+00:00')
        self.assertNotIn('PRIVATE',json.dumps(r));self.assertFalse(r['publication_semantics_verified'])
        self.assertEqual(self.observe(quoted=False),r)
    def test_refresh_cannot_promote_old_created_time(self):
        r=review_newness(self.observe(),boundary=BOUNDARY,now=NOW)
        self.assertEqual(r['status'],'reported_preexisting');self.assertFalse(r['ready_for_delivery'])
    def test_recent_creation_alone_never_proves_publication(self):
        r=self.observe(dict(id=123,createdTime='2026-10-03T10:57:00+03:00'))
        review=review_newness(r,boundary=BOUNDARY,now=NOW)
        self.assertEqual(review['status'],'publication_unconfirmed');self.assertFalse(review['publication_verified'])
    def test_identity_mismatch_bool_and_missing_date_held(self):
        for ad in (dict(id=456,**DATES),dict(id=True,**DATES),dict(id=123)):
            self.assertEqual(review_newness(self.observe(ad),boundary=BOUNDARY,now=NOW)['status'],'publication_unconfirmed')
    def test_naive_invalid_future_dates_not_accepted(self):
        for value in ('2026-10-03T10:48:48','invalid','2026-10-04T10:48:48+03:00'):
            r=self.observe(dict(id=123,createdTime=value));self.assertNotIn('createdTime',r['values']);self.assertTrue(r['issues'])
    def test_null_pushup_is_unknown_not_a_manufactured_bump(self):
        r=self.observe(dict(id=123,**dict(DATES,pushupTime=None)))
        self.assertNotIn('pushupTime',r['values']);self.assertEqual(r['issues'],[])
    def test_earlier_refresh_conflict_held(self):
        r=self.observe(dict(id=123,**dict(DATES,lastRefreshTime='2026-09-01T10:00:00+03:00')))
        self.assertIn('lastRefreshTime_before_createdTime',r['issues'])
        self.assertEqual(review_newness(r,boundary=BOUNDARY,now=NOW)['status'],'publication_unconfirmed')
    def test_truncated_or_duplicate_state_held_without_eval(self):
        self.assertFalse(self.observe(closed=False)['identity_matches'])
        r=self.observe(extra='window.__PRERENDERED_STATE__ = {};')
        self.assertFalse(r['identity_matches']);self.assertTrue(r['issues'])
    def test_non_json_javascript_is_never_executed(self):
        p=SearchParser();p.feed('<script id="olx-init-config">window.__PRERENDERED_STATE__ = alert("evil");</script>')
        r=observe_source_dates([n for root in p.roots for n in root.nodes()],expected_id='123',fetched_at=NOW)
        self.assertEqual(r['issues'],['invalid_state_json'])


if __name__=='__main__':unittest.main()
