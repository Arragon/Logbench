"""R6-054: the logical library over several physical roots.

The invariants: two roots may hold the same relative path without colliding; a
file id never changes because a label or a display name changed; a generation is a
new row and the previous one stays readable; browsing does not read log bytes; an
offline root is reported as offline, not as an empty library; and pagination is
keyset-based so a page boundary cannot duplicate or drop a row.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config

LINE = '2026-01-01 10:00:00.000 [INFO] boot\n'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = make_app(self.base / 'deploy')
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir()
        (self.data / 'logs' / 'app.log').write_text(LINE * 3, 'utf-8')
        self.archive = self.base / 'archive'
        (self.archive / 'logs').mkdir(parents=True)
        (self.archive / 'logs' / 'app.log').write_text(LINE * 5, 'utf-8')
        self.archive_root = self.app.storage.roots.create(
            self.actor, {'label': '归档', 'path': str(self.archive)})['item']['root_id']

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    def request(self, method, target, payload=None):
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(self.ip)}
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, headers, body, self.ip)

    def scan(self, root_id, path=''):
        return self.app.storage.catalog.sync_directory(
            self.app.storage.roots.get(root_id), path)

    # ------------------------------------------------------------- identity
    def test_same_relative_path_in_two_roots_gets_two_ids(self):
        self.scan('legacy', 'logs')
        self.scan(self.archive_root, 'logs')
        items = self.request('GET', '/api/v2/library?root=all&limit=50').value['items']
        same_path = [item for item in items if item['relPath'] == 'logs/app.log']
        self.assertEqual(len(same_path), 2)
        self.assertEqual(len({item['fileId'] for item in same_path}), 2)
        self.assertEqual(len({item['rootId'] for item in same_path}), 2)

    def test_file_id_survives_a_label_change(self):
        self.scan('legacy', 'logs')
        first = self.request('GET', '/api/v2/library').value['items'][0]
        revision = self.app.storage.roots.get('legacy')['revision']
        self.request('PATCH', '/api/v2/storage-roots/legacy',
                     {'label': '新名称', 'expectedRevision': revision})
        second = self.request('GET', '/api/v2/library').value['items'][0]
        self.assertEqual(second['fileId'], first['fileId'])
        self.assertEqual(second['rootLabel'], '新名称')

    def test_new_content_creates_a_generation_and_keeps_the_old_one(self):
        self.scan('legacy', 'logs')
        first = self.request('GET', '/api/v2/library').value['items'][0]
        file_id = first['fileId']
        (self.data / 'logs' / 'app.log').write_text(LINE * 9, 'utf-8')
        self.scan('legacy', 'logs')
        detail = self.request('GET', '/api/v2/library/files/' + file_id).value['item']
        self.assertEqual(detail['fileId'], file_id)
        self.assertEqual(detail['generation'], 2)
        self.assertEqual([row['generation'] for row in detail['generations']], [2, 1])
        self.assertEqual(detail['generations'][1]['state'], 'active')

    # ----------------------------------------------------------- permissions
    def test_browsing_does_not_read_log_content(self):
        secret = self.data / 'logs' / 'locked.log'
        secret.write_text('secret\n', 'utf-8')
        if os.name != 'posix' or os.geteuid() == 0:
            self.skipTest('permission bits are not enforceable for this account')
        os.chmod(secret, 0o000)
        try:
            self.scan('legacy', 'logs')
            listing = self.request('GET', '/api/v2/library?directory=logs')
            tree = self.request('GET', '/api/v2/library/tree?root=legacy&path=logs')
        finally:
            os.chmod(secret, 0o600)
        self.assertEqual(listing.status, 200)
        names = [item['displayName'] for item in listing.value['items']]
        self.assertIn('locked.log', names)
        files = [item for item in tree.value['items'] if item['type'] == 'file']
        self.assertTrue(any(item['name'] == 'locked.log' for item in files))

    def test_private_rows_are_owner_only(self):
        private = self.base / 'private'
        private.mkdir()
        (private / 'mine.log').write_text(LINE, 'utf-8')
        root_id = self.app.storage.roots.create(
            self.actor, {'label': '私有', 'path': str(private), 'kind': 'private_raw'}
        )['item']['root_id']
        self.app.storage.catalog.register_path(private / 'mine.log', owner=self.actor['id'],
                                               scope='private')
        mine = self.request('GET', '/api/v2/library?scope=private').value['items']
        self.assertEqual([item['displayName'] for item in mine], ['mine.log'])
        other_ip = '10.20.30.40'
        headers = {'Host': 'localhost:50004', 'Origin': 'http://localhost:50004',
                   'X-LogBench-CSRF': self.app.csrf(other_ip)}
        theirs = self.app.handle('GET', '/api/v2/library?scope=private', headers, b'', other_ip)
        self.assertEqual(theirs.value['items'], [])
        # The private row must not appear in the shared library either.
        shared = self.request('GET', '/api/v2/library?root=all').value['items']
        self.assertEqual([item for item in shared if item['rootId'] == root_id], [])
        _ = stat

    # ------------------------------------------------------------ pagination
    def test_keyset_pagination_has_no_gaps_or_duplicates(self):
        for index in range(230):
            (self.data / 'logs' / ('p-%03d.log' % index)).write_text(LINE, 'utf-8')
        self.scan('legacy', 'logs')
        seen, cursor, pages = [], None, 0
        while True:
            target = '/api/v2/library?directory=logs&limit=100'
            if cursor:
                target += '&cursor=' + cursor
            page = self.request('GET', target).value
            pages += 1
            seen.extend(item['relPath'] for item in page['items'])
            cursor = page['nextCursor']
            if not cursor:
                break
            self.assertLess(pages, 10)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 231)
        self.assertEqual(sorted(seen), seen)

    def test_cursor_from_another_page_order_is_rejected_when_malformed(self):
        self.scan('legacy', 'logs')
        response = self.request('GET', '/api/v2/library?cursor=not-a-cursor')
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_cursor')

    # ------------------------------------------------------------------ tree
    def test_tree_is_lazy_and_reports_child_counts(self):
        (self.data / 'logs' / 'nested').mkdir()
        (self.data / 'logs' / 'nested' / 'deep.log').write_text(LINE, 'utf-8')
        root = self.request('GET', '/api/v2/library/tree?root=legacy&path=').value
        names = [item['name'] for item in root['items']]
        self.assertEqual(names, ['logs'])
        # The root listing did not descend into logs/nested.
        self.assertFalse(any(item['name'] == 'nested' for item in root['items']))
        deeper = self.request('GET', '/api/v2/library/tree?root=legacy&path=logs').value
        dirs = [item for item in deeper['items'] if item['type'] == 'directory']
        self.assertEqual([item['name'] for item in dirs], ['nested'])
        # Lazy: nothing inside `nested` is registered yet, so the honest number is
        # its direct entry count, not "0 files".
        self.assertEqual(dirs[0]['files'], 0)
        self.assertEqual(dirs[0]['entries'], 1)
        # Expanding it registers the metadata, and the count becomes real.
        expanded = self.request('GET', '/api/v2/library/tree?root=legacy&path=logs/nested').value
        self.assertEqual([item['name'] for item in expanded['items']], ['deep.log'])
        again = self.request('GET', '/api/v2/library/tree?root=legacy&path=logs').value
        nested = [item for item in again['items'] if item['name'] == 'nested'][0]
        self.assertEqual(nested['files'], 1)

    def test_offline_root_keeps_its_rows_and_is_not_an_empty_library(self):
        self.scan('legacy', 'logs')
        before = self.request('GET', '/api/v2/library?root=legacy').value
        self.assertEqual(before['total'], 1)
        (self.data / '.logbench-root.json').unlink()
        after = self.request('GET', '/api/v2/library?root=legacy').value
        self.assertEqual(after['total'], 1)
        self.assertEqual(after['items'][0]['available'], False)
        self.assertEqual(after['roots'][0]['state'], 'offline')
        tree = self.request('GET', '/api/v2/library/tree?root=legacy&path=').value
        self.assertEqual(tree['state'], 'offline')
        self.assertTrue(tree['registrationKept'])

    # ------------------------------------------------------------------ scan
    def test_scan_endpoint_is_admin_only_and_bounded(self):
        for index in range(12):
            (self.data / 'logs' / ('b-%02d.log' % index)).write_text(LINE, 'utf-8')
        viewer_ip = '10.7.7.7'
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(viewer_ip)}
        denied = self.app.handle('POST', '/api/v2/library/scan',
                                 headers, json.dumps({'rootId': 'legacy', 'path': 'logs'}).encode(),
                                 viewer_ip)
        self.assertEqual(denied.status, 403)
        first = self.request('POST', '/api/v2/library/scan',
                             {'rootId': 'legacy', 'path': 'logs', 'limit': 5}).value
        self.assertEqual(first['visited'], 5)
        self.assertIsNotNone(first['nextOffset'])
        second = self.request('POST', '/api/v2/library/scan',
                              {'rootId': 'legacy', 'path': 'logs', 'limit': 5,
                               'offset': first['nextOffset']}).value
        self.assertGreater(second['visited'], 0)

    def test_metadata_exposes_physical_path_only_to_admins(self):
        self.scan('legacy', 'logs')
        file_id = self.request('GET', '/api/v2/library').value['items'][0]['fileId']
        admin = self.request('GET', '/api/v2/library/files/' + file_id).value['item']
        self.assertTrue(admin['physicalPath'])
        viewer_ip = '10.8.8.8'
        headers = {'Host': 'localhost:50004', 'Origin': 'http://localhost:50004'}
        viewer = self.app.handle('GET', '/api/v2/library/files/' + file_id, headers, b'', viewer_ip)
        self.assertIsNone(viewer.value['item']['physicalPath'])


if __name__ == '__main__':
    unittest.main()
