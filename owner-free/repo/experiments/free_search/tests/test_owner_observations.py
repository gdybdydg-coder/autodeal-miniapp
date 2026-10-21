import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import PublicCard


def card(sid='1', added=90):
    return PublicCard(sid, f'https://auto.ria.com/uk/auto_fixture_{sid}.html',
                      added, None, None, False, (), ())


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.owner-observations.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_retains_baseline_and_republication_identity(self):
        db = Observations(self.path)
        self.assertTrue(db.ingest([card()], 100)['cold_start'])
        db.close()
        db = Observations(self.path)
        try:
            r = db.ingest([card(added=110), card('2', 115)], 120)
            self.assertFalse(r['cold_start'])
            self.assertEqual(r['changed_add_date'], 1)
            self.assertEqual(r['source_date_after_baseline'], 1)
            self.assertEqual(r['publication_proven'], 0)
            self.assertEqual(r['ready_for_delivery'], 0)
            self.assertEqual(db.ingest([card('2', 115)], 125)['first_seen'], 0)
        finally:
            db.close()

    def test_overflow_rolls_back_entire_page_and_cursor(self):
        db = Observations(self.path, capacity=2)
        try:
            db.ingest([card()], 100)
            with self.assertRaisesRegex(ValueError, 'capacity'):
                db.ingest([card('2', 110), card('3', 110)], 120)
            self.assertEqual(db.db.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
            self.assertEqual(db.db.execute('SELECT last_observed FROM control').fetchone()[0], 100)
        finally:
            db.close()

    def test_unknown_old_future_and_promoted_are_not_new_publications(self):
        db = Observations(self.path)
        try:
            db.ingest([card()], 100)
            r = db.ingest([card('2', None), card('3', 90), card('4', 200),
                           replace(card('5', 110), issues=('promoted',))], 120)
            self.assertEqual(r['source_date_after_baseline'], 0)
            self.assertEqual(r['unknown_add_date'], 1)
            with self.assertRaisesRegex(ValueError, 'clock_regression'):
                db.ingest([card()], 119)
        finally:
            db.close()

    def test_foreign_database_untouched(self):
        import sqlite3
        connection = sqlite3.connect(self.path)
        connection.execute('CREATE TABLE customer_data(id INTEGER)')
        connection.close()
        with self.assertRaisesRegex(ValueError, 'foreign_database'):
            Observations(self.path)

