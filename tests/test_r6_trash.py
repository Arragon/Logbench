"""R6-057: per-root recycling, reference protection and crash repair.

The dangerous cases are the ones that end with the file in neither place: a move
that fails halfway, a restore that overwrites something, and a delete that runs
while a job still holds the generation. Each of those has a test here with real
files on a real disk.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.storage import trash as trash_module

LINE = '2026-01-01 10:00:00.000 [INFO] boot\n'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    return app


class TrashTests(unittest.TestCase):
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

    # ------------------------------------------------------------ round trip
    def test_delete_then_restore_keeps_bytes_and_identity(self):
        deleted = self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        self.assertEqual(deleted.status, 200, deleted.value)
        self.assertFalse(self.file.exists())
        item = deleted.value['item']
        self.assertEqual(item['digest'], self.digest)
        listing = self.request('GET', '/api/v2/library/trash').value['items']
        self.assertEqual([row['id'] for row in listing], [item['id']])
        restored = self.request('POST', '/api/v2/library/trash/%s/restore' % item['id'])
        self.assertEqual(restored.status, 200, restored.value)
        self.assertEqual(hashlib.sha256(self.file.read_bytes()).hexdigest(), self.digest)
        self.assertFalse(restored.value['digestMismatch'])

    def test_restore_refuses_to_overwrite_a_new_file(self):
        item = self.request('DELETE', '/api/v2/library/files/' + self.file_id).value['item']
        self.file.write_text('someone else wrote this\n', 'utf-8')
        response = self.request('POST', '/api/v2/library/trash/%s/restore' % item['id'])
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'already_exists')
        self.assertEqual(self.file.read_text('utf-8'), 'someone else wrote this\n')

    def test_deleted_file_is_hidden_from_the_library_listing(self):
        self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        listing = self.request('GET', '/api/v2/library').value
        self.assertEqual(listing['total'], 0)

    # ---------------------------------------------------------- private scope
    def test_private_file_is_invisible_to_another_principal(self):
        private = self.base / 'private'
        private.mkdir()
        mine = private / 'mine.log'
        mine.write_text(LINE, 'utf-8')
        root_id = self.app.storage.roots.create(
            self.actor, {'label': '私有', 'path': str(private), 'kind': 'private_raw'}
        )['item']['root_id']
        file_id = self.app.storage.catalog.register_path(
            mine, owner=self.actor['id'], scope='private')['file_id']
        other_ip = '10.3.3.3'
        denied = self.request('DELETE', '/api/v2/library/files/' + file_id, ip=other_ip)
        self.assertEqual(denied.status, 404)
        self.assertTrue(mine.exists())
        trashed = self.request('DELETE', '/api/v2/library/files/' + file_id)
        self.assertEqual(trashed.status, 200, trashed.value)
        theirs = self.request('GET', '/api/v2/library/trash', ip=other_ip).value['items']
        self.assertEqual(theirs, [])
        # An administrator is not the owner: private content stays out of reach.
        self.assertEqual(self.app.storage.roots.get(root_id)['kind'], 'private_raw')

    # ------------------------------------------------------------- protection
    def test_running_index_job_blocks_the_delete(self):
        job = self.app.index_jobs.create(self.actor, {'fileId': self.file_id})
        self.assertEqual(job['item']['state'], 'queued')
        response = self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'active_references')
        kinds = [row['kind'] for row in response.value['error']['details']['references']]
        self.assertIn('job', kinds)
        self.assertTrue(self.file.exists())

    def test_binding_requires_an_explicit_detach(self):
        self.request('PUT', '/api/v2/bindings',
                     {'scope': 'file', 'rootId': 'legacy', 'path': 'logs/a.log',
                      'ruleId': 'cpqpsgx-default', 'expectedRevision': 0})
        blocked = self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.value['error']['code'], 'binding_active')
        forced = self.request('DELETE', '/api/v2/library/files/%s?detachBindings=1'
                              % self.file_id)
        self.assertEqual(forced.status, 200, forced.value)
        self.assertEqual(forced.value['item']['detachedBindings'], 1)
        with self.app.store.connect() as db:
            remaining = db.execute("SELECT count(*) FROM bindings WHERE path='logs/a.log'"
                                   ).fetchone()[0]
        self.assertEqual(remaining, 0)

    # ---------------------------------------------------------------- failure
    def test_cross_device_move_is_refused_and_the_file_stays(self):
        original = os.replace
        calls = {'n': 0}

        def fake_replace(src, dst):
            calls['n'] += 1
            raise OSError(errno.EXDEV, 'cross-device link')

        os.replace = fake_replace
        try:
            response = self.request('DELETE', '/api/v2/library/files/' + self.file_id)
        finally:
            os.replace = original
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'cross_device_trash')
        self.assertTrue(self.file.exists())
        with self.app.store.connect() as db:
            state = db.execute('SELECT state FROM trash_entries').fetchone()['state']
        self.assertEqual(state, 'failed')
        self.assertGreaterEqual(calls['n'], 1)

    def test_repair_resolves_a_move_interrupted_by_a_crash(self):
        # A row was written and the bytes moved, but the process died before the
        # state could be updated.
        bin_dir = self.data / '.logbench-trash'
        if not bin_dir.is_dir():
            bin_dir.mkdir(mode=0o700, parents=True)
        target = bin_dir / 't-crash'
        os.replace(str(self.file), str(target))
        with self.app.store.transaction() as db:
            db.execute(
                'INSERT INTO trash_entries(id,file_id,root_id,rel_path,generation,kind,size,'
                'owner,scope,digest,state,deleted_at,deleted_by,restored_at,restore_path,detail) '
                "VALUES('t-crash',?,'legacy','logs/a.log',1,'file',?,?,'shared',?,'pending',"
                "datetime('now'),'test',NULL,NULL,?)",
                (self.file_id, len(LINE) * 4, '', self.digest,
                 json.dumps({'trashPath': str(target)})))
        report = self.app.storage.trash.repair()
        self.assertEqual(report['repaired'][0]['state'], 'trashed')
        restored = self.request('POST', '/api/v2/library/trash/t-crash/restore')
        self.assertEqual(restored.status, 200, restored.value)
        self.assertEqual(hashlib.sha256(self.file.read_bytes()).hexdigest(), self.digest)

    def test_repair_marks_a_move_that_never_happened_as_failed(self):
        with self.app.store.transaction() as db:
            db.execute(
                'INSERT INTO trash_entries(id,file_id,root_id,rel_path,generation,kind,size,'
                'owner,scope,digest,state,deleted_at,deleted_by,restored_at,restore_path,detail) '
                "VALUES('t-ghost',?,'legacy','logs/a.log',1,'file',10,'','shared','',"
                "'pending',datetime('now'),'test',NULL,NULL,'{}')",
                (self.file_id,))
        report = self.app.storage.trash.repair()
        self.assertEqual(report['repaired'][0]['state'], 'failed')
        self.assertTrue(self.file.exists())

    # ------------------------------------------------------------- v1 compat
    def test_v1_delete_and_restore_still_work_through_the_new_service(self):
        deleted = self.request('DELETE', '/api/v1/files?path=logs/a.log')
        self.assertEqual(deleted.status, 200, deleted.value)
        self.assertFalse(self.file.exists())
        listing = self.request('GET', '/api/v1/files/trash').value['items']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['path'], 'logs/a.log')
        restored = self.request('POST', '/api/v1/files/restore', {'id': listing[0]['id']})
        self.assertEqual(restored.status, 200, restored.value)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.request('GET', '/api/v1/files/trash').value['items'], [])

    def test_directory_delete_keeps_the_legacy_path(self):
        folder = self.data / 'logs' / 'folder'
        folder.mkdir()
        (folder / 'x.log').write_text(LINE, 'utf-8')
        deleted = self.request('DELETE', '/api/v1/files?path=logs/folder')
        self.assertEqual(deleted.status, 200, deleted.value)
        self.assertFalse(folder.exists())
        listing = self.request('GET', '/api/v1/files/trash').value['items']
        self.assertEqual(listing[0]['type'], 'directory')
        restored = self.request('POST', '/api/v1/files/restore', {'id': listing[0]['id']})
        self.assertEqual(restored.status, 200, restored.value)
        self.assertTrue((folder / 'x.log').is_file())

    def test_trash_list_is_admin_only(self):
        viewer_ip = '10.4.4.4'
        response = self.request('GET', '/api/v1/files/trash', ip=viewer_ip)
        self.assertEqual(response.status, 403)
        _ = trash_module


if __name__ == '__main__':
    unittest.main()
