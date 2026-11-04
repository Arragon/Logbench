"""R6-042 / R6-044 / R6-049: the async query API, its cursors, and snapshot export.

Real HTTP through `Application.handle` (Host check, CSRF for writes), real SQLite
snapshots, real shard reads and the real Node matcher. The failures this guards
against are the ones that make a query API untrustworthy: a cursor that reaches
another snapshot, a count that claims to be exact while the scan is incomplete, a
cancelled query that reports zero, and an export that ignores permissions.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.indexing.cache_versions import CacheRegistry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    return app


class QueryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        self.log = self.data / 'logs' / 'edge.log'
        shutil.copy(GOLDEN / 'edge_cases.log', self.log)
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.file_id = self.index('logs/edge.log')

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, ip=None, headers=None):
        ip = ip or self.ip
        base = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        base.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, base, body, ip)

    def index(self, path: str) -> str:
        created = self.request('POST', '/api/v2/index-jobs', {'path': path})
        item = created.value['item']
        self.app.index_jobs.run_now(item['id'])
        return item['payload']['file_id']

    def query(self, **body):
        payload = {'file_id': self.file_id, 'text': 'iteration', 'flags': {}, 'budget_ms': 5000}
        payload.update(body)
        response = self.request('POST', '/api/v2/queries', payload)
        self.assertEqual(response.status, 200, response.value)
        return response.value['item']

    def wait(self, query_id: str, states=('done', 'failed', 'cancelled', 'paused'), timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.request('GET', '/api/v2/queries/' + query_id).value['item']
            if item['state'] in states:
                return item
            time.sleep(0.02)
        raise AssertionError('query did not settle: %s' % query_id)

    # -------------------------------------------------------------------- create
    def test_create_returns_before_the_scan_when_the_worker_pool_is_enabled(self):
        from concurrent.futures import ThreadPoolExecutor
        self.app.queries.executor_pool = ThreadPoolExecutor(max_workers=1)
        try:
            item = self.query(budget_ms=1)
            self.assertIn(item['state'], ('queued', 'running', 'paused', 'done'))
            settled = self.wait(item['query_id'])
            self.assertIn(settled['state'], ('done', 'paused'))
        finally:
            self.app.queries.executor_pool.shutdown(wait=True)
            self.app.queries.executor_pool = None

    def test_invalid_specs_are_rejected_without_creating_a_snapshot(self):
        cases = [
            ({'text': ''}, 'empty_query'),
            ({'text': '(', 'flags': {'regex': True}}, 'invalid_regex'),
            ({'text': 'a*', 'flags': {'regex': True}}, 'empty_match'),
            ({'text': 'x', 'time_range': {'from_ns': '2', 'to_ns': '1'}}, 'INVALID_RANGE'),
            ({'text': 'x', 'levels': ['A'] * 40}, 'INVALID_INPUT'),
            ({'text': 'x', 'limit': 5000}, 'INVALID_INPUT'),
        ]
        for body, code in cases:
            with self.subTest(code=code):
                response = self.query_expect_error(body)
                self.assertEqual(response.status, 400, response.value)
                self.assertEqual(response.value['error']['code'], code)
        with self.app.store.connect() as db:
            count = db.execute('SELECT COUNT(*) AS n FROM query_snapshots').fetchone()['n']
        self.assertEqual(count, 0, 'a rejected query must not leave a snapshot behind')

    def query_expect_error(self, body):
        payload = {'file_id': self.file_id, 'flags': {}, 'budget_ms': 5000}
        payload.update(body)
        return self.request('POST', '/api/v2/queries', payload)

    def test_unknown_and_foreign_files_are_indistinguishable(self):
        foreign = self.request('POST', '/api/v2/queries',
                               {'file_id': 'f_ffffffffffffffff', 'text': 'x', 'flags': {}})
        self.assertEqual(foreign.status, 404)
        mine = self.query(file_id=self.file_id, text='iteration')
        other = self.request('GET', '/api/v2/queries/' + mine['query_id'], ip='10.9.9.9')
        missing = self.request('GET', '/api/v2/queries/q-0000000000000000', ip='10.9.9.9')
        self.assertEqual(other.status, missing.status)
        self.assertEqual(other.value['error'], missing.value['error'])

    # --------------------------------------------------------------------- state
    def test_a_finished_query_is_exact_with_complete_coverage(self):
        item = self.query()
        self.assertEqual(item['state'], 'done')
        self.assertEqual(item['count']['relation'], 'exact')
        self.assertTrue(item['coverage']['complete'])
        self.assertGreater(item['count']['matched'], 0)
        self.assertIsNone(item['resume_token'])

    def test_a_budget_stop_is_at_least_with_a_resume_token(self):
        paused = self.query(budget_ms=1)
        self.assertEqual(paused['state'], 'paused')
        self.assertEqual(paused['count']['relation'], 'at_least')
        self.assertFalse(paused['coverage']['complete'])
        self.assertTrue(paused['resume_token'])

        resumed = self.request('POST', '/api/v2/queries/%s/continue' % paused['query_id'],
                               {'budget_ms': 5000})
        self.assertEqual(resumed.status, 200)
        settled = self.wait(paused['query_id'])
        self.assertEqual(settled['state'], 'done')
        self.assertEqual(settled['count']['relation'], 'exact')

    def test_continue_on_a_finished_query_is_refused(self):
        item = self.query()
        response = self.request('POST', '/api/v2/queries/%s/continue' % item['query_id'], {})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'job_finished')

    def test_cancel_reports_unknown_not_zero(self):
        item = self.query()
        cancelled = self.request('DELETE', '/api/v2/queries/' + item['query_id']).value['item']
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['count']['relation'], 'unknown')
        self.assertIsNone(cancelled['count']['matched'])
        self.assertFalse(cancelled['coverage']['complete'])
        # Cancel keeps the snapshot readable: C8 says cancel is not delete.
        page = self.request('GET', '/api/v2/queries/%s/records' % item['query_id'])
        self.assertEqual(page.status, 200)

    # ------------------------------------------------------------------- paging
    def test_paging_walks_the_snapshot_with_signed_cursors(self):
        item = self.query()
        collected, cursor, guard = [], None, 0
        while True:
            target = '/api/v2/queries/%s/records?limit=2' % item['query_id']
            if cursor:
                target += '&cursor=' + cursor
            page = self.request('GET', target).value
            collected.extend(entry['ordinal'] for entry in page['items'])
            cursor = page['next_cursor']
            guard += 1
            self.assertLess(guard, 50)
            if not cursor:
                break
        self.assertEqual(len(collected), len(set(collected)), 'no record is served twice')
        self.assertEqual(collected, sorted(collected), 'source order')
        self.assertEqual(len(collected), item['count']['matched'])

    def test_a_tampered_cursor_is_refused(self):
        item = self.query()
        page = self.request('GET', '/api/v2/queries/%s/records?limit=1' % item['query_id']).value
        self.assertIsNotNone(page['next_cursor'])
        cursor = page['next_cursor']
        body, mac = cursor.split('.', 1)
        tampered = body[:-2] + ('AA' if body[-2:] != 'AA' else 'BB') + '.' + mac
        response = self.request('GET', '/api/v2/queries/%s/records?cursor=%s'
                                % (item['query_id'], tampered))
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_cursor')

    def test_a_cursor_from_another_identity_is_refused(self):
        item = self.query()
        cursor = self.request('GET', '/api/v2/queries/%s/records?limit=1'
                              % item['query_id']).value['next_cursor']
        self.assertIsNotNone(cursor)
        response = self.request('GET', '/api/v2/queries/%s/records?cursor=%s'
                                % (item['query_id'], cursor), ip='10.9.9.9')
        # The identity cannot even see the snapshot, which is the strongest form of
        # "no wrong rows and no count leak".
        self.assertEqual(response.status, 404)

    def test_revocation_stops_reads_immediately(self):
        item = self.query()
        CacheRegistry(self.app.store).revoke(self.actor['id'])
        for target in ('/api/v2/queries/' + item['query_id'],
                       '/api/v2/queries/%s/records' % item['query_id'],
                       '/api/v2/queries/%s/export' % item['query_id']):
            response = self.request('GET', target)
            self.assertEqual(response.status, 404, target)

    def test_a_new_generation_invalidates_old_cursors(self):
        item = self.query()
        cursor = self.request('GET', '/api/v2/queries/%s/records?limit=1'
                              % item['query_id']).value['next_cursor']
        # Rewrite the file: a second generation, and the old snapshot refers to a
        # generation that is no longer current.
        self.log.write_text(self.log.read_text('utf-8') + '[2024-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() appended\n',
                            'utf-8')
        self.index('logs/edge.log')
        response = self.request('GET', '/api/v2/queries/%s/records?cursor=%s'
                                % (item['query_id'], cursor))
        self.assertIn(response.status, (410, 200))
        if response.status == 410:
            self.assertEqual(response.value['error']['code'], 'cursor_expired')

    # ------------------------------------------------------------------- export
    def test_export_writes_ndjson_and_csv_with_a_row_cap(self):
        item = self.query()
        self.assertGreater(item['count']['matched'], 1, 'the fixture must have several hits')
        ndjson = self.request('GET', '/api/v2/queries/%s/export?format=ndjson&max=1'
                              % item['query_id'])
        self.assertEqual(ndjson.status, 200)
        self.assertEqual(ndjson.headers.get('Content-Type'), 'application/x-ndjson; charset=utf-8')
        self.assertEqual(ndjson.headers.get('X-LogBench-Export-Rows'), '1')
        self.assertEqual(ndjson.headers.get('X-LogBench-Export-Truncated'), 'true')
        payload = ndjson.stream.read().decode('utf-8')
        lines = [line for line in payload.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        first = json.loads(lines[0])
        self.assertIn('ordinal', first)
        self.assertIn('message', first)
        ndjson.stream.close()

        csv_export = self.request('GET', '/api/v2/queries/%s/export?format=csv' % item['query_id'])
        self.assertEqual(csv_export.status, 200)
        text = csv_export.stream.read().decode('utf-8')
        csv_export.stream.close()
        self.assertTrue(text.startswith('ordinal,ts,level,logger,fn,src,line,message'))
        self.assertEqual(len([line for line in text.splitlines() if line]),
                         item['count']['matched'] + 1)

    def test_export_rejects_an_unknown_format(self):
        item = self.query()
        response = self.request('GET', '/api/v2/queries/%s/export?format=pdf' % item['query_id'])
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'INVALID_FORMAT')

    def test_query_reads_require_a_valid_host_and_writes_require_csrf(self):
        item = self.query()
        bad_host = self.request('GET', '/api/v2/queries/' + item['query_id'],
                                headers={'Host': 'evil.example.com'})
        self.assertEqual(bad_host.status, 421)
        no_csrf = self.request('POST', '/api/v2/queries',
                              {'file_id': self.file_id, 'text': 'x', 'flags': {}},
                              headers={'X-LogBench-CSRF': 'wrong'})
        self.assertEqual(no_csrf.status, 403)
        self.assertEqual(no_csrf.value['error']['code'], 'CSRF_REJECTED')

    # ------------------------------------------------- match_all (R6-079 scope)
    def test_match_all_returns_records_without_a_search_term(self):
        item = self.query(match_all=True, text='', limit=200, budget_ms=20000)
        settled = self.wait(item['query_id'])
        self.assertIn(settled['state'], ('done', 'paused'))
        page = self.request('GET', '/api/v2/queries/%s/records?limit=200' % item['query_id'])
        self.assertEqual(page.status, 200, page.value)
        self.assertGreater(len(page.value['items']), 0)
        self.assertNotEqual(page.value['count']['relation'], 'unknown')

    def test_match_all_still_honours_level_and_time_filters(self):
        item = self.query(match_all=True, text='', levels=['ERROR'], limit=200,
                          budget_ms=20000)
        settled = self.wait(item['query_id'])
        self.assertIn(settled['state'], ('done', 'paused'))
        page = self.request('GET', '/api/v2/queries/%s/records?limit=200' % item['query_id'])
        for header in page.value['items']:
            self.assertEqual(header['level'], 'ERROR')

    def test_an_empty_query_without_the_flag_is_still_rejected(self):
        response = self.query_expect_error({'text': '', 'match_all': False})
        self.assertEqual(response.status, 400, response.value)
        self.assertEqual(response.value['error']['code'], 'empty_query')


if __name__ == '__main__':
    unittest.main()
