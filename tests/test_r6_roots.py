"""R6-052: storage-root registration, legacy compatibility and probe binding.

The cases that matter are the ones an operator would hit on a real machine:
re-running registration, pointing a second root at a directory the service account
cannot write, registering a directory that contains another root, and trying to
"fix" a moved root by editing its path while files are still registered under it.
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
from server.storage import roots as roots_module


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class RootRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root / 'deploy')
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.data = self.app.config.data_dir

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

    # ------------------------------------------------------------------ legacy
    def test_legacy_root_is_registered_once_and_keeps_data_dir(self):
        first = self.app.storage.roots.ensure_legacy()
        second = self.app.storage.roots.ensure_legacy()
        self.assertEqual(first['root_id'], 'legacy')
        self.assertEqual(second['root_id'], 'legacy')
        self.assertEqual(Path(second['path']), Path(self.data))
        with self.app.store.connect() as db:
            count = db.execute("SELECT count(*) FROM storage_roots WHERE id='legacy'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(second['state'], 'online')

    def test_legacy_root_keeps_existing_bindings_and_rules(self):
        with self.app.store.connect() as db:
            rules = db.execute('SELECT count(*) FROM rules').fetchone()[0]
            bindings = db.execute("SELECT count(*) FROM bindings WHERE scope='default'").fetchone()[0]
        self.assertGreaterEqual(rules, 1)
        self.assertEqual(bindings, 1)
        resolved = self.app.handle('GET', '/api/v1/bindings/resolve?path=any.log',
                                   {'Host': 'localhost:50004'}, b'', self.ip)
        self.assertEqual(resolved.value['source']['scope'], 'default')

    # ------------------------------------------------------------------- create
    def test_register_second_root_outside_deployment(self):
        external = self.root / 'external-share'
        external.mkdir()
        response = self.request('POST', '/api/v2/storage-roots',
                                {'label': '外部共享', 'path': str(external), 'kind': 'shared_raw'})
        self.assertEqual(response.status, 200, response.value)
        item = response.value['item']
        self.assertEqual(item['kind'], 'shared_raw')
        self.assertEqual(item['state'], 'online')
        self.assertTrue(item['volume_id'])
        self.assertTrue(item['sentinel_id'])
        self.assertTrue((external / '.logbench-root.json').is_file())

    def test_duplicate_and_nested_paths_are_refused(self):
        first = self.root / 'share-a'
        first.mkdir()
        self.assertEqual(self.request('POST', '/api/v2/storage-roots',
                                      {'label': 'A', 'path': str(first)}).status, 200)
        duplicate = self.request('POST', '/api/v2/storage-roots',
                                 {'label': 'A2', 'path': str(first)})
        self.assertEqual(duplicate.status, 409)
        self.assertEqual(duplicate.value['error']['code'], 'root_path_duplicate')
        nested = self.root / 'share-a' / 'inner'
        nested.mkdir()
        overlap = self.request('POST', '/api/v2/storage-roots',
                               {'label': 'Inner', 'path': str(nested)})
        self.assertEqual(overlap.status, 409)
        self.assertEqual(overlap.value['error']['code'], 'root_overlap')

    def test_relative_path_is_refused(self):
        response = self.request('POST', '/api/v2/storage-roots',
                                {'label': 'Relative', 'path': 'data/elsewhere'})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_root_path')

    def test_non_admin_cannot_register_or_probe(self):
        viewer_ip = '10.1.2.3'
        external = self.root / 'their-share'
        external.mkdir()
        created = self.request('POST', '/api/v2/storage-roots',
                               {'label': 'Nope', 'path': str(external)}, ip=viewer_ip)
        self.assertEqual(created.status, 403)
        probed = self.request('POST', '/api/v2/storage-roots/legacy/probe', {}, ip=viewer_ip)
        self.assertEqual(probed.status, 403)

    def test_index_root_on_network_filesystem_is_refused(self):
        external = self.root / 'network-index'
        external.mkdir()
        original = roots_module.is_network_fs
        roots_module.is_network_fs = lambda path: True
        try:
            response = self.request('POST', '/api/v2/storage-roots',
                                    {'label': '索引盘', 'path': str(external), 'kind': 'index'})
        finally:
            roots_module.is_network_fs = original
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'root_not_local')

    def test_path_change_with_registered_files_needs_migration(self):
        (self.data / 'logs').mkdir()
        (self.data / 'logs' / 'a.log').write_text('x\n', 'utf-8')
        self.app.storage.catalog.register_path(self.data / 'logs' / 'a.log')
        current = self.app.storage.roots.get('legacy')
        moved = self.root / 'moved-data'
        moved.mkdir()
        response = self.request('PATCH', '/api/v2/storage-roots/legacy',
                                {'path': str(moved), 'expectedRevision': current['revision']})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'root_path_migration_required')
        self.assertEqual(self.app.storage.roots.get('legacy')['path'], str(Path(self.data)))

    def test_patch_uses_revision_and_updates_label(self):
        current = self.app.storage.roots.get('legacy')
        updated = self.request('PATCH', '/api/v2/storage-roots/legacy',
                               {'label': '主数据目录', 'expectedRevision': current['revision']})
        self.assertEqual(updated.status, 200, updated.value)
        self.assertEqual(updated.value['item']['label'], '主数据目录')
        self.assertEqual(updated.value['item']['revision'], current['revision'] + 1)
        stale = self.request('PATCH', '/api/v2/storage-roots/legacy',
                             {'label': '再次修改', 'expectedRevision': current['revision']})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.value['error']['code'], 'REVISION_CONFLICT')

    # -------------------------------------------------------------------- probe
    def test_probe_records_state_and_identity(self):
        external = self.root / 'probe-me'
        external.mkdir()
        self.request('POST', '/api/v2/storage-roots',
                     {'label': 'Probe', 'path': str(external)})
        root = [item for item in self.app.storage.roots.all()
                if item['path'] == str(Path(external).resolve())][0]
        response = self.request('POST', '/api/v2/storage-roots/%s/probe' % root['root_id'], {})
        self.assertEqual(response.status, 200, response.value)
        self.assertTrue(response.value['item']['writable'])
        self.assertEqual(response.value['item']['volume_id'], root['volume_id'])
        self.assertIsNotNone(response.value['item']['free_bytes'])
        with self.app.store.connect() as db:
            rows = db.execute('SELECT * FROM storage_root_probes WHERE root_id=?',
                              (root['root_id'],)).fetchall()
        self.assertTrue(rows)

    def test_probe_refuses_a_changed_device(self):
        external = self.root / 'remounted'
        external.mkdir()
        self.request('POST', '/api/v2/storage-roots', {'label': 'Remount', 'path': str(external)})
        root = [item for item in self.app.storage.roots.all()
                if item['path'] == str(Path(external).resolve())][0]
        with self.app.store.transaction() as db:
            db.execute("UPDATE storage_roots SET volume_id='not-this-device' WHERE id=?",
                       (root['root_id'],))
        response = self.request('POST', '/api/v2/storage-roots/%s/probe' % root['root_id'], {})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'storage_root_offline')
        self.assertEqual(response.value['error']['details']['reason'], 'volume_changed')

    # -------------------------------------------------------------- visibility
    def test_private_root_is_not_listed_in_the_shared_library(self):
        private = self.root / 'private-area'
        private.mkdir()
        (private / 'secret.log').write_text('2026-01-01 00:00:00.000 [INFO] x\n', 'utf-8')
        created = self.request('POST', '/api/v2/storage-roots',
                               {'label': '私有区', 'path': str(private), 'kind': 'private_raw'})
        self.assertEqual(created.status, 200, created.value)
        root_id = created.value['item']['root_id']
        self.app.storage.catalog.sync_directory(self.app.storage.roots.get(root_id), '')
        shared = self.request('GET', '/api/v2/library?scope=shared')
        self.assertEqual([item for item in shared.value['items'] if item['rootId'] == root_id], [])
        sources = self.request('GET', '/api/v2/library/sources')
        labels = {item['rootId']: item['scope'] for item in sources.value['items']}
        self.assertEqual(labels[root_id], 'private')

    def test_admin_only_can_read_the_root_table(self):
        viewer_ip = '10.9.9.9'
        response = self.request('GET', '/api/v2/storage-roots', ip=viewer_ip)
        self.assertEqual(response.status, 403)
        sources = self.request('GET', '/api/v2/library/sources', ip=viewer_ip)
        self.assertEqual(sources.status, 200)
        self.assertIn('items', sources.value)
        self.assertNotIn('path', json.dumps(sources.value))


if __name__ == '__main__':
    unittest.main()
