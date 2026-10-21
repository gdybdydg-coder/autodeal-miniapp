import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from experiments.free_search.owner_night_details import validate_cohort, run
from experiments.free_search.owner_night_runtime import Coordinator


class FrozenDetailsTests(unittest.TestCase):
    def cohort(self):
        return {'cohort': [
            dict(id=str(i), url=f'https://auto.ria.com/uk/auto_audi_a6_{i}.html',
                 preview_usd='999999' if i == 10 else '1000')
            for i in range(1, 11)]}

    def test_no_price_selection_or_order_change(self):
        doc = self.cohort()
        self.assertEqual(validate_cohort(doc), doc['cohort'])
        self.assertEqual(validate_cohort(doc)[-1]['preview_usd'], '999999')

    def test_duplicate_or_mismatched_identity_denied_before_transport(self):
        doc = self.cohort()
        doc['cohort'][0]['url'] = doc['cohort'][1]['url']
        with self.assertRaisesRegex(ValueError, 'invalid_control_url'):
            validate_cohort(doc)
        doc['cohort'][0] = doc['cohort'][1]
        with self.assertRaisesRegex(ValueError, 'invalid_control_size'):
            validate_cohort(doc)

    def test_restart_claim_preserves_evidence_without_budget_or_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root/'lock.sqlite3'
            lock = Coordinator(lock_path)
            lock.acquire('fixture_worker', 100)
            lock.close()
            (root/'night-control-frozen.json').write_text(json.dumps(self.cohort()))
            (root/'night-control-details.claim').write_text('previous')
            output = root/'night-control-details.json'
            output.write_text('original evidence')
            prefix = 'experiments.free_search.owner_night_details.'
            with patch(prefix+'WORKSPACE', root), patch(prefix+'LOCK', lock_path), \
                 patch(prefix+'OUTPUT', output), patch(prefix+'night_budget') as budget, \
                 patch(prefix+'PublicFetch') as fetch:
                with self.assertRaises(FileExistsError):
                    run('fixture_worker')
                budget.assert_not_called()
                fetch.assert_not_called()
            self.assertEqual(output.read_text(), 'original evidence')

    def test_wrong_lock_denies_before_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = 'experiments.free_search.owner_night_details.'
            with patch(prefix+'LOCK', Path(directory)/'lock.sqlite3'), \
                 patch(prefix+'night_budget') as budget:
                with self.assertRaisesRegex(ValueError, 'executor_lock_required'):
                    run('fixture_worker')
                budget.assert_not_called()
