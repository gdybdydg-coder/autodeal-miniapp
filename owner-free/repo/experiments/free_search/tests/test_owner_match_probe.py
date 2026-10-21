from copy import deepcopy
import unittest
from backend.models import Filters
from experiments.free_search.owner_match_probe import validate_snapshot
from experiments.free_search.owner_budget import Denied


class SnapshotTests(unittest.TestCase):
    def snapshot(self):
        f=Filters(brand='Audi',model='A6').canonical()
        return dict(owner_identity_verified=True,owner_confirmed_purchase=True,owner_ready=True,
                    checked_at=100,expires_at=2000,searches=[dict(search_id=1,epoch='epoch',
                    filters=f,fingerprint=Filters.model_validate(f).fingerprint())])

    def test_verified_recent_snapshot(self):
        self.assertEqual(len(validate_snapshot(self.snapshot(),101)),1)

    def test_stale_or_changed_filters_fail_closed(self):
        with self.assertRaises(Denied): validate_snapshot(self.snapshot(),1001)
        data=self.snapshot()
        data['searches'][0]['filters']['model']='A4'
        with self.assertRaises(Denied): validate_snapshot(data,101)

    def test_stopped_or_expired_owner_rejected(self):
        for field,value in (('owner_ready',False),('expires_at',100),('owner_confirmed_purchase',False)):
            data=self.snapshot();data[field]=value
            with self.assertRaises(Denied): validate_snapshot(data,101)
