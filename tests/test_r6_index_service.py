"""R6-038/R6-040 groundwork: the index-job API over real HTTP and real SQLite."""
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
from server.indexing.shards import ShardReader
from server.jobs.repository import LeaseError

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None  # tests drive the run explicitly
    return app


class IndexJobApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.service = self.app.index_jobs
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        self.log = self.data / 'logs' / 'edge.log'
        shutil.copy(GOLDEN / 'edge_cases.log', self.log)
        self.ip = '127.0.0.1'

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, ip=None, headers=None):
        ip = ip or self.ip
        h = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
             'Origin': 'http://localhost:50004',
             'X-LogBench-CSRF': self.app.csrf(ip)}
        h.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, h, body, ip)

    def create(self, path='logs/edge.log', ip=None, **extra):
        body = {'path': path}
        body.update(extra)
        return self.request('POST', '/api/v2/index-jobs', body, ip=ip)

    # -------------------------------------------------------------------- tests
    def test_create_returns_a_job_without_indexing_in_the_request(self):
        response = self.create()
        self.assertEqual(response.status, 200)
        item = response.value['item']
        self.assertEqual(item['kind'], 'index')
        self.assertIn(item['state'], ('queued', 'reading'))
        self.assertTrue(response.value['accepted'])
        # Nothing has been parsed yet: creation must not do the work inline. Only
        # the `raw` layer exists, because that is the access record, not an index.
        layers = self.service.registry.versions(file_id=item['payload']['file_id'])
        self.assertEqual([row['layer'] for row in layers], ['raw'])
        self.assertEqual(layers[0]['state'], 'complete')

    def test_running_the_job_publishes_all_three_layers(self):
        item = self.create().value['item']
        result = self.service.run_now(item['id'])
        self.assertEqual(result['state'], 'indexed')
        self.assertEqual(result['reused'],
                         {'structure': False, 'rules': False, 'search': False})
        reader = ShardReader(Path(result['shard']))
        self.assertGreater(reader.count(), 0)
        self.assertEqual(reader.layer('rules')['version'], result['layers']['rules'])
        self.assertEqual(reader.layer('search')['version'], result['layers']['search'])
        detail = self.request('GET', '/api/v2/index-jobs/' + item['id']).value
        self.assertEqual(detail['item']['state'], 'indexed')
        self.assertEqual({row['layer'] for row in detail['layers']},
                         {'raw', 'structure', 'rules', 'search'})
        self.assertTrue(all(row['state'] == 'complete' for row in detail['layers']))

    def test_a_second_job_for_the_same_file_reuses_every_layer(self):
        first = self.create().value['item']
        self.service.run_now(first['id'])
        second = self.create(actionId='reindex-1').value['item']
        result = self.service.run_now(second['id'])
        self.assertEqual(result['reused'],
                         {'structure': True, 'rules': True, 'search': True})
        self.assertEqual(result['layers'], json.loads(json.dumps(
            self.request('GET', '/api/v2/index-jobs/' + first['id']).value['item']['progress']['layers'])))

    def test_action_id_makes_creation_idempotent(self):
        first = self.create(actionId='click-42').value['item']['id']
        second = self.create(actionId='click-42').value['item']['id']
        self.assertEqual(first, second)
        self.assertEqual(len(self.request('GET', '/api/v2/index-jobs').value['items']), 1)

    def test_paths_outside_the_library_are_refused(self):
        for path in ('../etc/passwd', '/etc/passwd', 'logs/../../secret', '.hidden/x', ''):
            response = self.create(path=path)
            self.assertEqual(response.status, 400, path)
            self.assertIn(response.value['error']['code'], ('INVALID_PATH', 'INVALID_INPUT'), path)

    def test_a_missing_file_is_not_found_and_a_directory_is_not_indexed(self):
        missing = self.create(path='logs/nope.log')
        self.assertEqual(missing.status, 404)
        directory = self.create(path='logs')
        self.assertEqual(directory.status, 400)
        self.assertEqual(directory.value['error']['code'], 'INVALID_PATH')

    def test_only_an_admin_can_start_an_index_job(self):
        response = self.create(ip='10.9.9.9')
        self.assertEqual(response.status, 403)
        self.assertEqual(response.value['error']['code'], 'FORBIDDEN')

    def test_creating_requires_csrf(self):
        response = self.request('POST', '/api/v2/index-jobs', {'path': 'logs/edge.log'},
                                headers={'X-LogBench-CSRF': 'nope'})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.value['error']['code'], 'CSRF_REJECTED')

    def test_a_failed_run_marks_the_job_failed_instead_of_indexed(self):
        # A worker that cannot start is the honest way to fail: no shard exists.
        self.service.pool.entry = Path('/nonexistent/server-engine.cjs')
        item = self.create().value['item']
        with self.assertRaises(Exception):
            self.service.run_now(item['id'])
        stored = self.service.repo.get(item['id'])
        self.assertEqual(stored.state, 'failed')
        error = stored.error if isinstance(stored.error, dict) else json.loads(stored.error)
        self.assertEqual(error['code'], 'entry_missing')
        self.assertIsNone(self.service.registry.pointer(
            item['payload']['file_id'], 'structure'))

    def test_a_cancelled_job_cannot_be_run(self):
        item = self.create().value['item']
        self.service.repo.set_state(item['id'], 'cancelled')
        with self.assertRaises(LeaseError) as caught:
            self.service.run_now(item['id'])
        self.assertEqual(caught.exception.code, 'job_finished')

    def test_layers_are_scoped_to_the_caller(self):
        item = self.create().value['item']
        self.service.run_now(item['id'])
        visitor = self.request('GET', '/api/v2/index-jobs/' + item['id'], ip='10.9.9.9')
        self.assertEqual(visitor.status, 404)
        self.assertEqual(visitor.value['error']['code'], 'unknown_job')

    def test_the_background_executor_runs_the_job_to_completion(self):
        from concurrent.futures import ThreadPoolExecutor
        self.service.executor = ThreadPoolExecutor(max_workers=1)
        try:
            item = self.create().value['item']
            deadline = time.time() + 30
            state = item['state']
            while time.time() < deadline:
                state = self.service.repo.get(item['id']).state
                if state in ('indexed', 'failed', 'cancelled'):
                    break
                time.sleep(0.05)
            self.assertEqual(state, 'indexed')
            self.assertTrue(self.service.registry.pointer(item['payload']['file_id'], 'structure'))
        finally:
            self.service.executor.shutdown(wait=True)
            self.service.executor = None

    def test_revocation_makes_published_layers_unreadable(self):
        item = self.create().value['item']
        result = self.service.run_now(item['id'])
        owner = self.service.repo.get(item['id']).principal
        registry = CacheRegistry(self.app.store)
        registry.revoke(owner)
        for layer, version in result['layers'].items():
            self.assertEqual(registry._row(version)['state'], 'revoked', layer)
        self.assertIsNone(registry.pointer(item['payload']['file_id'], 'structure'))


if __name__ == '__main__':
    unittest.main()
