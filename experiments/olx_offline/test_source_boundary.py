"""Explicit source incompleteness cannot advance the collection checkpoint."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.olx_offline.html_snapshot import parse_search_snapshot
from experiments.olx_offline.pipeline import Pipeline
from experiments.olx_offline.test_html_snapshot import CARD
from experiments.olx_offline.test_pipeline import NOW, raw


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.sqlite'
        self.p = Pipeline(self.path)
        self.net = patch('socket.socket', side_effect=AssertionError('No network'))
        self.net.start()

    def tearDown(self):
        self.p.db.close()
        self.tmp.cleanup()
        self.net.stop()

    def restart(self):
        self.p.db.close()
        self.p = Pipeline(self.path)

    def checkpoint(self):
        return self.p.db.execute('SELECT cursor,complete,last_success FROM checkpoint').fetchone()

    def test_truncated_terminal_html_is_saved_and_resumable_after_restart(self):
        body = ('<html>'+CARD+'<div data-testid="l-card" id="456">').encode()
        parsed = parse_search_snapshot(body, fetched_at=NOW, truncated=True)
        self.assertTrue(parsed['summary']['download_truncated'])
        page = dict(items=parsed['listings'], next=None,
                    collection_complete=parsed['summary']['collection_complete'])
        for delta in (0, 10):
            calls = []
            def fetch(cursor):
                calls.append(cursor)
                return page
            result = self.p.collect(fetch, NOW+delta, page_budget=5, row_budget=100)
            self.assertEqual(result, dict(status='incomplete', pages=1, rows=1,
                                          reason='source_incomplete'))
            self.assertEqual(calls, [None])
            self.assertEqual(self.checkpoint(), (None, 0, None))
            self.assertEqual(len(self.p.cars()), 1)
            self.assertEqual(self.p.db.execute('SELECT count(*) FROM page_cursors').fetchone()[0], 0)
            self.restart()

    def test_partial_second_page_resumes_its_cursor_and_later_completes(self):
        calls = []
        pages = {None: dict(items=[raw('1')], next='two', collection_complete=False),
                 'two': dict(items=[raw('2')], next=None, collection_complete=False)}
        def fetch(cursor):
            calls.append(cursor)
            return pages[cursor]
        result = self.p.collect(fetch, NOW, page_budget=5, row_budget=100)
        self.assertEqual(calls, [None, 'two'])
        self.assertEqual(result['reason'], 'source_incomplete')
        self.assertEqual(self.checkpoint(), ('two', 0, None))
        self.assertEqual({car['id'] for car in self.p.cars()}, {'1', '2'})
        self.restart()
        calls.clear()
        self.p.collect(fetch, NOW+10, page_budget=5, row_budget=100)
        self.assertEqual(calls, ['two'])
        self.assertEqual(self.checkpoint(), ('two', 0, None))
        self.restart()
        pages['two'] = dict(items=[raw('2'), raw('3')], next=None, collection_complete=True)
        calls.clear()
        result = self.p.collect(fetch, NOW+20, page_budget=5, row_budget=100)
        self.assertEqual(calls, ['two'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.checkpoint(), (None, 1, NOW+20))
        self.assertEqual({car['id'] for car in self.p.cars()}, {'1', '2', '3'})

    def test_truncated_nonterminal_page_does_not_follow_visible_next_link(self):
        body = ('<html>'+CARD+'<a href="?page=2">2</a>'
                '<div data-testid="l-card" id="456">').encode()
        parsed = parse_search_snapshot(body, fetched_at=NOW, truncated=True)
        page = dict(items=parsed['listings'], next='p2',
                    page_complete=not parsed['summary']['download_truncated'],
                    collection_complete=False)
        for delta in (0, 10):
            calls = []
            def fetch(cursor):
                calls.append(cursor)
                if cursor is not None:
                    self.fail('Truncated page advanced to p2')
                return page
            result = self.p.collect(fetch, NOW+delta, page_budget=5, row_budget=100)
            self.assertEqual(result, dict(status='incomplete', pages=1, rows=1,
                                          reason='page_incomplete'))
            self.assertEqual(calls, [None])
            self.assertEqual(self.checkpoint(), (None, 0, None))
            self.assertEqual(len(self.p.cars()), 1)
            self.assertEqual(self.p.db.execute('SELECT count(*) FROM page_cursors').fetchone()[0], 0)
            self.restart()

    def test_incomplete_nonterminal_page_preserves_prior_cursor_and_success_watermark(self):
        self.p.collect(lambda _: dict(items=[raw('old')], next=None), NOW,
                       page_budget=1, row_budget=10)
        pages = {None: dict(items=[raw('1')], next='p2', page_complete=True,
                            collection_complete=False),
                 'p2': dict(items=[raw('2')], next='p3', page_complete=False,
                            collection_complete=False)}
        calls = []
        def fetch(cursor):
            calls.append(cursor)
            return pages[cursor]
        result = self.p.collect(fetch, NOW+10, page_budget=5, row_budget=100)
        self.assertEqual(calls, [None, 'p2'])
        self.assertEqual(result['reason'], 'page_incomplete')
        self.assertEqual(self.checkpoint(), ('p2', 0, NOW))
        self.assertEqual({car['id'] for car in self.p.cars()}, {'old', '1', '2'})
        self.restart()
        calls.clear()
        self.p.collect(fetch, NOW+20, page_budget=5, row_budget=100)
        self.assertEqual(calls, ['p2'])
        self.assertEqual(self.checkpoint(), ('p2', 0, NOW))

    def test_page_complete_must_be_boolean(self):
        result = self.p.collect(lambda _: dict(items=[raw('1')], next='p2',
            page_complete='false'), NOW, page_budget=5, row_budget=10)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'ValueError')
        self.assertEqual(self.p.cars(), [])
        self.assertEqual(self.checkpoint(), (None, 0, None))

    def test_partial_next_run_preserves_an_existing_success_watermark(self):
        self.p.collect(lambda _: dict(items=[raw('1')], next=None), NOW,
                       page_budget=1, row_budget=10)
        result = self.p.collect(lambda _: dict(items=[raw('2')], next=None,
            collection_complete=False), NOW+10, page_budget=1, row_budget=10)
        self.assertEqual(result['reason'], 'source_incomplete')
        self.assertEqual(self.checkpoint(), (None, 0, NOW))
        self.assertEqual(len(self.p.cars()), 2)

    def test_generic_page_without_completeness_flag_remains_compatible(self):
        result = self.p.collect(lambda _: dict(items=[raw('1')], next=None), NOW,
                                page_budget=5, row_budget=10)
        self.assertEqual(result, dict(status='complete', pages=1, rows=1, reason=None))
        self.assertEqual(self.checkpoint(), (None, 1, NOW))

    def test_invalid_completeness_contract_does_not_save_or_claim_completion(self):
        result = self.p.collect(lambda _: dict(items=[raw('1')], next=None,
            collection_complete='false'), NOW, page_budget=5, row_budget=10)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'ValueError')
        self.assertEqual(self.p.cars(), [])
        self.assertEqual(self.checkpoint(), (None, 0, None))
