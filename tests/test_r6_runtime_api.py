"""R6-083: runtime status, live budget changes and redacted diagnostics.

The point of these assertions is that the page cannot show a number the process is
not actually using:

* raising `parse_slots` spawns real Node workers, and lowering it stops idle ones;
* `query_slots` reaches the query gate, so the concurrency limit changes without a
  restart;
* a rejected or conflicting budget edit changes nothing and writes no audit row;
* the diagnostics export is a whitelist — the test looks for the data path and the
  log file name in the body and expects not to find them.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
SEED = ROOT / 'rules' / 'cpqpsgx-default.json'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(SEED, root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    return app


class RuntimeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        shutil.copy(GOLDEN / 'edge_cases.log', self.data / 'logs' / 'edge.log')
        self.ip = '127.0.0.1'
        self.file_id = self.index('logs/edge.log')

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, ip=None):
        ip = ip or self.ip
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, headers, body, ip)

    def index(self, path: str) -> str:
        created = self.request('POST', '/api/v2/index-jobs', {'path': path})
        self.assertEqual(created.status, 200, created.value)
        item = created.value['item']
        self.app.index_jobs.run_now(item['id'])
        return item['payload']['file_id']

    def budget(self) -> dict:
        response = self.request('GET', '/api/v2/runtime/budget')
        self.assertEqual(response.status, 200, response.value)
        return response.value

    def put_budget(self, value: dict, revision=None):
        revision = self.budget()['revision'] if revision is None else revision
        return self.request('PUT', '/api/v2/runtime/budget',
                            {'value': value, 'expectedRevision': revision})

    # ------------------------------------------------------------------ status
    def test_status_reports_real_pools_queue_and_resources(self):
        response = self.request('GET', '/api/v2/runtime')
        self.assertEqual(response.status, 200, response.value)
        body = response.value
        pool = body['pools']['parse']
        self.assertGreaterEqual(pool['workers'], 1)
        self.assertEqual(pool['budget_slots'], body['budget']['effective']['parse_slots'])
        self.assertTrue(pool['available'])
        self.assertEqual(body['pools']['query']['gate']['slots'],
                         body['budget']['effective']['query_slots'])
        self.assertGreater(body['resources']['rss_bytes'] or 0, 0)
        self.assertGreater(body['resources']['disk']['total_bytes'] or 0, 0)
        self.assertIsNone(body['resources']['disk']['latency_ms'],
                          'disk latency must not be sampled unless asked')
        kinds = body['jobs']['counts']['by_kind']
        self.assertIn('index', kinds)
        job = body['jobs']['queue'][0]
        for key in ('id', 'kind', 'state', 'source', 'file', 'retryable', 'cancellable'):
            self.assertIn(key, job)
        self.assertEqual(job['kind'], 'index')
        self.assertEqual(job['file'], 'edge.log')

    def test_disk_probe_is_opt_in_and_measured(self):
        response = self.request('GET', '/api/v2/runtime?probe=disk')
        self.assertEqual(response.status, 200, response.value)
        latency = response.value['resources']['disk']['latency_ms']
        self.assertIsNotNone(latency)
        self.assertGreaterEqual(latency, 0.0)

    def test_status_requires_admin(self):
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf('10.0.0.9')}
        for target in ('/api/v2/runtime', '/api/v2/runtime/budget', '/api/v2/runtime/diagnostics'):
            response = self.app.handle('GET', target, headers, b'', '10.0.0.9')
            self.assertEqual(response.status, 403, target)

    # ------------------------------------------------------------------ budget
    def test_raising_parse_slots_spawns_real_workers(self):
        before = len(self.app.index_jobs.pool.workers)
        response = self.put_budget({'parse_slots': before + 1})
        self.assertEqual(response.status, 200, response.value)
        body = response.value
        self.assertEqual(body['applied']['parse']['workers'], before + 1)
        self.assertEqual(len(self.app.index_jobs.pool.workers), before + 1)
        status = self.request('GET', '/api/v2/runtime').value
        self.assertEqual(status['pools']['parse']['workers'], before + 1)

    def test_lowering_parse_slots_stops_idle_workers(self):
        self.put_budget({'parse_slots': 3})
        self.assertEqual(len(self.app.index_jobs.pool.workers), 3)
        # history_slots may not exceed parse_slots, so shrinking the pool has to
        # shrink the history class with it; the API enforces that rather than
        # accepting a budget whose own halves disagree.
        response = self.put_budget({'parse_slots': 1, 'history_slots': 1})
        self.assertEqual(response.status, 200, response.value)
        self.assertEqual(len(self.app.index_jobs.pool.workers), 1)
        self.assertEqual(self.app.index_jobs.pool.workers[0].index, 0)

    def test_shrinking_without_shrinking_history_is_rejected(self):
        response = self.put_budget({'parse_slots': 1})
        self.assertEqual(response.status, 400, response.value)
        self.assertIn('历史补采', response.value['error']['message'])

    def test_query_slots_reach_the_gate_and_the_upload_limits(self):
        response = self.put_budget({'query_slots': 3, 'upload_slots': 5,
                                    'per_principal_uploads': 3})
        self.assertEqual(response.status, 200, response.value)
        self.assertEqual(self.app.queries.gate.snapshot()['slots'], 3)
        self.assertEqual(self.app.uploads.store.max_active_global, 5)
        self.assertEqual(self.app.uploads.store.max_active_per_principal, 3)

    def test_budget_edit_is_audited_with_before_and_after(self):
        response = self.put_budget({'query_slots': 3})
        self.assertEqual(response.status, 200, response.value)
        with self.app.store.connect() as db:
            row = db.execute("SELECT actor,detail FROM audit WHERE action='runtime.budget' "
                             "ORDER BY at DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        detail = json.loads(row['detail'])
        self.assertEqual(detail['before']['query_slots'], 2)
        self.assertEqual(detail['after']['query_slots'], 3)
        self.assertEqual(row['actor'], self.app.actor(self.ip)['id'])

    def test_stale_revision_is_a_conflict_and_changes_nothing(self):
        stale = self.put_budget({'query_slots': 4}, revision=99)
        self.assertEqual(stale.status, 409, stale.value)
        self.assertEqual(self.budget()['effective']['query_slots'], 2)
        self.assertEqual(self.app.queries.gate.snapshot()['slots'], 2)
        with self.app.store.connect() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM audit WHERE action='runtime.budget'"
                               ).fetchone()['n']
        self.assertEqual(count, 0)

    def test_impossible_budget_is_rejected_with_the_reason(self):
        response = self.put_budget({'parse_slots': 6, 'query_slots': 6, 'total_cpu': 8})
        self.assertEqual(response.status, 400, response.value)
        self.assertEqual(response.value['error']['code'], 'invalid_budget')
        self.assertIn('总 CPU', response.value['error']['message'])

    def test_budget_survives_a_restart(self):
        self.put_budget({'query_slots': 3})
        reopened = Application(Config(self.root, bootstrap_admins=['127.0.0.1']))
        try:
            self.assertEqual(reopened.runtime.base.query_slots, 3)
            self.assertEqual(reopened.queries.gate.snapshot()['slots'], 3)
        finally:
            reopened.index_jobs.close()
            reopened.queries.close()

    # ------------------------------------------------------------- diagnostics
    def test_diagnostics_export_is_redacted(self):
        response = self.request('GET', '/api/v2/runtime/diagnostics')
        self.assertEqual(response.status, 200)
        body = response.stream.read().decode('utf-8')
        payload = json.loads(body)
        self.assertEqual(payload['schema'], 'logbench.runtime/1')
        self.assertNotIn('edge.log', body)
        self.assertNotIn(str(self.data), body)
        self.assertIn('redaction', payload)
        self.assertNotIn('logs', json.dumps(payload['jobs']))
        self.assertNotIn('Content-Type', response.headers.get('content-type', ''))


if __name__ == '__main__':
    unittest.main()
