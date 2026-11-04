"""R6-082: layered cache listing, selective rebuild and derived-only purge.

The assertions that matter are the destructive ones:

* purging a derived layer drops the pointer and **keeps the source bytes** — the
  file is still readable and its raw layer is untouched;
* `raw` cannot be purged through this API at all;
* a rebuild reuses the structure layer, which is checked by comparing the structure
  pointer's signature before and after;
* an active pin is reported instead of being silently dropped, and the pinned
  version stays readable until it expires.
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


class CacheApiTests(unittest.TestCase):
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

    def listing(self) -> dict:
        response = self.request('GET', '/api/v2/cache?limit=10')
        self.assertEqual(response.status, 200, response.value)
        return response.value

    def item(self) -> dict:
        return self.listing()['items'][0]

    def pointer(self, layer: str):
        with self.app.store.connect() as db:
            return db.execute('SELECT p.version_id, v.signature FROM layer_pointers p '
                              'JOIN layer_versions v ON v.version_id=p.version_id '
                              "WHERE p.file_id=? AND p.layer=? AND p.scope='shared'",
                              (self.file_id, layer)).fetchone()

    # -------------------------------------------------------------------- list
    def test_listing_reports_every_layer_and_the_shard_size(self):
        item = self.item()
        self.assertEqual(item['fileId'], self.file_id)
        self.assertEqual(set(item['layers']), {'structure', 'rules', 'search'})
        for layer in ('structure', 'rules', 'search'):
            self.assertEqual(item['layers'][layer]['state'], 'complete', layer)
            self.assertGreater(item['layers'][layer]['rows'], 0, layer)
        self.assertEqual(item['coverage'], 'search')
        self.assertGreater(item['shardBytes'] or 0, 0)
        self.assertFalse(item['missing'])

    def test_listing_can_filter_by_layer_and_state(self):
        response = self.request('GET', '/api/v2/cache?layer=rules&state=complete')
        self.assertEqual(response.status, 200, response.value)
        self.assertEqual(len(response.value['items']), 1)
        response = self.request('GET', '/api/v2/cache?layer=rules&state=failed')
        self.assertEqual(len(response.value['items']), 0)

    def test_listing_requires_admin(self):
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf('10.0.0.9')}
        response = self.app.handle('GET', '/api/v2/cache', headers, b'', '10.0.0.9')
        self.assertEqual(response.status, 403, response.value)

    # ------------------------------------------------------------------- purge
    def test_purge_removes_the_pointer_and_keeps_the_source_bytes(self):
        before = self.log.read_bytes()
        response = self.request('POST', '/api/v2/cache/purge',
                                {'fileId': self.file_id, 'layers': ['rules', 'search'],
                                 'confirm': 'purge-derived'})
        self.assertEqual(response.status, 200, response.value)
        self.assertTrue(response.value['rawKept'])
        self.assertEqual(len(response.value['removed']), 2)
        self.assertTrue(self.log.is_file())
        self.assertEqual(self.log.read_bytes(), before, '排版缓存不能改动原文')
        self.assertIsNone(self.pointer('rules'))
        self.assertIsNotNone(self.pointer('structure'))
        item = self.item()
        self.assertEqual(item['layers']['structure']['state'], 'complete')
        self.assertIn(item['layers']['rules']['state'], ('pending', 'building', 'stale'))
        # The raw layer is the access record and must survive a derived purge.
        with self.app.store.connect() as db:
            raw = db.execute('SELECT COUNT(*) AS n FROM layer_pointers '
                             "WHERE file_id=? AND layer='raw'", (self.file_id,)).fetchone()
        self.assertEqual(raw['n'], 1)

    def test_purge_refuses_raw_and_the_derived_only_confirmation(self):
        raw = self.request('POST', '/api/v2/cache/purge',
                           {'fileId': self.file_id, 'layers': ['raw'], 'confirm': 'purge-derived'})
        self.assertEqual(raw.status, 400, raw.value)
        self.assertEqual(raw.value['error']['code'], 'raw_not_purgeable')
        no_confirm = self.request('POST', '/api/v2/cache/purge',
                                  {'fileId': self.file_id, 'layers': ['rules']})
        self.assertEqual(no_confirm.status, 400, no_confirm.value)
        self.assertEqual(no_confirm.value['error']['code'], 'confirmation_required')
        self.assertIsNotNone(self.pointer('rules'), 'a rejected purge must not change anything')

    def test_purge_reports_active_pins_instead_of_dropping_them(self):
        pointer = self.pointer('rules')
        pin = self.app.cache.registry.pin(pointer['version_id'], owner=self.actor['id'])
        response = self.request('POST', '/api/v2/cache/purge',
                                {'fileId': self.file_id, 'layers': ['rules'],
                                 'confirm': 'purge-derived'})
        self.assertEqual(response.status, 200, response.value)
        self.assertTrue(response.value['pinned'])
        self.assertEqual(response.value['pinned'][0]['layer'], 'rules')
        self.assertTrue(response.value['pinned'][0]['refs'])
        item = self.item()
        self.assertTrue(item['layers']['rules'].get('pins'))
        # The pinned version was marked stale but not deleted: it is still readable.
        versions = self.app.cache.registry.versions(file_id=self.file_id, layer='rules')
        self.assertTrue(any(row['version_id'] == pointer['version_id'] and
                            row['state'] in ('stale', 'complete') for row in versions))
        self.app.cache.registry.unpin(pointer['version_id'], pin['token'])

    # ----------------------------------------------------------------- rebuild
    def test_rebuild_queues_a_job_and_reuses_the_structure_layer(self):
        structure_before = self.pointer('structure')['signature']
        response = self.request('POST', '/api/v2/cache/rebuild',
                                {'fileId': self.file_id, 'layers': ['rules', 'search'],
                                 'actionId': 'act-cache-1'})
        self.assertEqual(response.status, 200, response.value)
        job = response.value['item']
        self.assertEqual(job['kind'], 'index')
        self.assertIn(job['state'], ('queued', 'running', 'reading', 'indexed'))
        self.app.index_jobs.run_now(job['id'])
        self.assertEqual(self.pointer('structure')['signature'], structure_before,
                         '重建规则层不得重新分帧')
        item = self.item()
        self.assertEqual(item['layers']['rules']['state'], 'complete')

    def test_rebuild_of_an_unknown_file_is_a_404(self):
        response = self.request('POST', '/api/v2/cache/rebuild',
                                {'fileId': 'f_does_not_exist', 'layers': ['rules']})
        self.assertEqual(response.status, 404, response.value)

    def test_rebuild_requires_a_known_layer(self):
        response = self.request('POST', '/api/v2/cache/rebuild',
                                {'fileId': self.file_id, 'layers': ['nope']})
        self.assertEqual(response.status, 400, response.value)
        self.assertEqual(response.value['error']['code'], 'invalid_layer')


if __name__ == '__main__':
    unittest.main()
