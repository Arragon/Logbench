"""Permanent removal and the cache it must take with it.

The failure this file exists to prevent: a log is destroyed but the index rows
and shard files that describe it survive. The cache then keeps reporting bytes
nobody can read again, and no later rebuild can validate them because the source
is gone. Every test below destroys a file through the public API and then looks
at the database and the disk together.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config

LINE = '2026-01-01 10:00:00.000 [INFO] boot\n'
DEPS = {
    'structure': {'content_sig': 'c1', 'generation': 1, 'framing': 'f1',
                  'runtime': 'py39', 'schema': 'v1', 'encoding': 'utf-8'},
    'rules': {'structure_signature': 's1', 'rule_structure': 'r1',
              'rule_extract': 'e1', 'engine': 'js'},
    'search': {'rules_signature': 'r1', 'projection': 'p1', 'gram_size': 3},
    'raw': {'content_sig': 'c1'},
}


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    return app


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = make_app(self.base / 'deploy')
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir()
        self.file = self.data / 'logs' / 'a.log'
        self.file.write_text(LINE * 4, 'utf-8')
        self.digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.file_id = self.app.storage.catalog.register_path(self.file)['file_id']

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    def request(self, method, target, payload=None, ip=None):
        ip = ip or self.ip
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, headers, body, ip)

    def add_layers(self, layers=('structure', 'rules', 'search'), size=1024):
        """Publish real layer versions with real shard files on disk."""
        registry = self.app.cache.registry
        ids, shards = {}, {}
        for layer in layers:
            shard = self.base / ('shard-%s.bin' % layer)
            shard.write_bytes(b'\0' * 64)
            version = registry.ensure(self.file_id, layer, owner=self.actor['id'],
                                      generation=1, deps=DEPS[layer])
            registry.publish(version['version_id'], rows=4, digest='a' * 64,
                             stats={'bytes': size, 'shard': str(shard)})
            ids[layer] = version['version_id']
            shards[layer] = shard
        return ids, shards

    def trash_the_file(self) -> str:
        deleted = self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        self.assertEqual(deleted.status, 200, deleted.value)
        return deleted.value['item']['id']


class PurgeTests(BaseCase):
    def test_purge_destroys_bytes_and_releases_the_cache(self):
        _, shards = self.add_layers()
        trash_id = self.trash_the_file()
        self.assertFalse(self.file.exists())

        purged = self.request('DELETE', '/api/v2/library/trash/' + trash_id)
        self.assertEqual(purged.status, 200, purged.value)
        self.assertEqual(purged.value['summary']['purged'], 1)
        self.assertEqual(purged.value['summary']['freedBytes'], 3 * 1024)
        self.assertTrue(purged.value['items'][0]['removed'])

        with self.app.store.connect() as db:
            for table in ('layer_versions', 'layer_pointers', 'layer_pins',
                          'layer_events', 'cache_versions', 'trash_entries',
                          'logical_files', 'generations'):
                left = db.execute('SELECT count(*) FROM %s' % table).fetchone()[0]
                self.assertEqual(left, 0, table)
        for shard in shards.values():
            self.assertFalse(shard.exists(), shard)

    def test_purge_leaves_the_raw_layer_of_other_files_alone(self):
        _, shards = self.add_layers()
        other = self.data / 'logs' / 'b.log'
        other.write_text(LINE * 2, 'utf-8')
        other_id = self.app.storage.catalog.register_path(other)['file_id']
        registry = self.app.cache.registry
        kept = registry.ensure(other_id, 'structure', owner=self.actor['id'],
                               generation=1, deps=DEPS['structure'])
        registry.publish(kept['version_id'], rows=2, digest='b' * 64,
                         stats={'bytes': 512, 'shard': str(self.base / 'kept.bin')})
        (self.base / 'kept.bin').write_bytes(b'\0' * 32)

        self.request('DELETE', '/api/v2/library/trash/' + self.trash_the_file())
        with self.app.store.connect() as db:
            remaining = db.execute('SELECT file_id FROM layer_versions').fetchall()
        self.assertEqual([row['file_id'] for row in remaining], [other_id])
        self.assertTrue((self.base / 'kept.bin').exists())

    def test_purge_refuses_while_a_reader_pins_the_version(self):
        ids, _ = self.add_layers()
        trash_id = self.trash_the_file()
        self.app.cache.registry.pin(ids['search'], owner=self.actor['id'], token='tok')
        blocked = self.request('DELETE', '/api/v2/library/trash/' + trash_id)
        self.assertEqual(blocked.status, 409)
        self.assertIn(blocked.value['error']['code'],
                      ('active_references', 'version_pinned'))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM trash_entries').fetchone()[0], 1)

    def test_only_an_owner_or_admin_may_purge(self):
        trash_id = self.trash_the_file()
        response = self.request('DELETE', '/api/v2/library/trash/' + trash_id, ip='10.9.9.9')
        self.assertEqual(response.status, 403)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM trash_entries').fetchone()[0], 1)

    def test_batch_purge_reports_the_missing_entries(self):
        trash_id = self.trash_the_file()
        response = self.request('POST', '/api/v2/library/trash/purge',
                                {'ids': [trash_id, 't-absent']})
        self.assertEqual(response.status, 200, response.value)
        summary = response.value['summary']
        self.assertEqual((summary['requested'], summary['purged'], summary['failed']),
                         (2, 1, 1))
        failure = [item for item in response.value['items'] if not item['ok']][0]
        self.assertEqual(failure['code'], 'trash_not_found')

    def test_restored_entry_cannot_be_purged(self):
        trash_id = self.trash_the_file()
        self.assertEqual(
            self.request('POST', '/api/v2/library/trash/%s/restore' % trash_id).status, 200)
        response = self.request('DELETE', '/api/v2/library/trash/' + trash_id)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'trash_not_purgeable')


class BatchDeleteTests(BaseCase):
    def test_binding_conflict_is_reported_per_file_and_can_be_forced(self):
        self.request('PUT', '/api/v2/bindings',
                     {'scope': 'file', 'rootId': 'legacy', 'path': 'logs/a.log',
                      'ruleId': 'cpqpsgx-default', 'expectedRevision': 0})
        blocked = self.request('POST', '/api/v2/library/files/batch-delete',
                              {'fileIds': [self.file_id]})
        self.assertEqual(blocked.status, 200, blocked.value)
        self.assertEqual(blocked.value['summary']['trashed'], 0)
        self.assertEqual(blocked.value['summary']['bindingsBlocked'], 1)
        self.assertEqual(blocked.value['items'][0]['code'], 'binding_active')
        self.assertTrue(self.file.exists())

        forced = self.request('POST', '/api/v2/library/files/batch-delete',
                             {'fileIds': [self.file_id], 'detachBindings': True})
        self.assertEqual(forced.value['summary']['trashed'], 1, forced.value)
        self.assertFalse(self.file.exists())
        with self.app.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM bindings WHERE path='logs/a.log'")
                .fetchone()[0], 0)

    def test_one_blocked_file_does_not_stop_the_others(self):
        second = self.data / 'logs' / 'b.log'
        second.write_text(LINE * 2, 'utf-8')
        second_id = self.app.storage.catalog.register_path(second)['file_id']
        self.request('PUT', '/api/v2/bindings',
                     {'scope': 'file', 'rootId': 'legacy', 'path': 'logs/a.log',
                      'ruleId': 'cpqpsgx-default', 'expectedRevision': 0})
        response = self.request('POST', '/api/v2/library/files/batch-delete',
                                {'fileIds': [self.file_id, second_id]})
        self.assertEqual(response.value['summary']['trashed'], 1)
        self.assertEqual(response.value['summary']['failed'], 1)
        self.assertTrue(self.file.exists())
        self.assertFalse(second.exists())

    def test_batch_delete_requires_a_non_empty_id_list(self):
        response = self.request('POST', '/api/v2/library/files/batch-delete', {'fileIds': []})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'INVALID_INPUT')


class CacheUsageTests(BaseCase):
    def test_usage_counts_derived_layers_and_ignores_raw(self):
        self.add_layers()
        self.add_layers(layers=('raw',), size=4096)
        usage = self.request('GET', '/api/v2/cache/usage').value
        self.assertEqual(usage['usedBytes'], 3 * 1024)
        self.assertEqual(usage['layers']['structure']['bytes'], 1024)
        self.assertEqual(usage['layers']['rules']['versions'], 1)
        self.assertNotIn('raw', usage['layers'])
        self.assertEqual(usage['quotaBytes'], 1024 ** 3)
        self.assertFalse(usage['overQuota'])
        self.assertEqual(usage['enforcement'], 'warn')

    def test_quota_roundtrip_and_unlimited(self):
        saved = self.request('PUT', '/api/v2/cache/quota', {'quotaBytes': 64 * 1024 * 1024})
        self.assertEqual(saved.status, 200, saved.value)
        self.assertEqual(self.request('GET', '/api/v2/cache/usage').value['quotaBytes'],
                         64 * 1024 * 1024)

        unlimited = self.request('PUT', '/api/v2/cache/quota', {'quotaBytes': None})
        self.assertTrue(unlimited.value['unlimited'])
        usage = self.request('GET', '/api/v2/cache/usage').value
        self.assertTrue(usage['unlimited'])
        self.assertIsNone(usage['quotaBytes'])
        self.assertIsNone(usage['ratio'])

    def test_quota_below_the_floor_is_refused(self):
        response = self.request('PUT', '/api/v2/cache/quota', {'quotaBytes': 1024})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_quota')

    def test_over_quota_is_reported_but_not_enforced(self):
        self.add_layers(size=32 * 1024 * 1024)
        self.request('PUT', '/api/v2/cache/quota', {'quotaBytes': 16 * 1024 * 1024})
        usage = self.request('GET', '/api/v2/cache/usage').value
        self.assertTrue(usage['overQuota'])
        self.assertGreater(usage['ratio'], 1)
        self.assertEqual(usage['enforcement'], 'warn')
        # The quota is a warning: listing the cache still succeeds.
        self.assertEqual(self.request('GET', '/api/v2/cache?limit=10').status, 200)

    def test_viewer_can_read_usage_but_not_change_the_quota(self):
        self.assertEqual(self.request('GET', '/api/v2/cache/usage', ip='10.9.9.9').status, 200)
        response = self.request('PUT', '/api/v2/cache/quota',
                                {'quotaBytes': 2 * 1024 ** 3}, ip='10.9.9.9')
        self.assertEqual(response.status, 403)


class SettingsTests(BaseCase):
    def test_quota_survives_a_restart_and_reuses_the_v3_table(self):
        saved = self.request('PUT', '/api/v2/cache/quota', {'quotaBytes': 128 * 1024 * 1024})
        self.assertEqual(saved.status, 200, saved.value)
        # A second Application over the same data directory is what a restart is:
        # nothing is cached in the process, so this proves the value reached disk.
        again = make_app(self.base / 'deploy')
        try:
            usage = again.cache.usage(again.actor('127.0.0.1'))
            self.assertEqual(usage['quotaBytes'], 128 * 1024 * 1024)
        finally:
            again.index_jobs.close()
            again.queries.close()
        with self.app.store.connect() as db:
            row = db.execute("SELECT value,revision FROM settings "
                             "WHERE key='cache.quotaBytes'").fetchone()
        self.assertEqual(json.loads(row['value']), 128 * 1024 * 1024)
        self.assertGreaterEqual(row['revision'], 1)


if __name__ == '__main__':
    unittest.main()
