"""R6-046 / R6-047: private upload sessions, quota, sealing and streaming parts.

Real HTTP with binary bodies, a real disk (parts land under the data dir with 0600),
and the real index/query services afterwards. The failures this guards against are
the ones that make a private store dangerous: another principal reading your
session, a retried part appending twice, and a seal that accepts an incomplete file
because the sizes happened to add up.
"""
from __future__ import annotations

import hashlib
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
from server.uploads.sessions import UploadError, UploadStore

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def make_app(root: Path, *, quota: int = 2 * 1024 * 1024 * 1024) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    app.uploads.store = UploadStore(app.store, app.config.data_dir, quota_bytes=quota)
    return app


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.blob = (GOLDEN / 'traceback_chain.log').read_bytes()

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, body=None, ip=None, headers=None):
        ip = ip or self.ip
        base = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        base.update(headers or {})
        data = body if body is not None else (json.dumps(payload).encode() if payload is not None else b'')
        return self.app.handle(method, target, base, data, ip)

    def create_session(self, *, name='trace.log', size=None, chunk=None, ip=None):
        payload = {'name': name, 'size': size if size is not None else len(self.blob)}
        if chunk:
            payload['chunkSize'] = chunk
        return self.request('POST', '/api/v2/uploads', payload, ip=ip)

    def put(self, session_id, part_no, data, digest=True, ip=None):
        headers = {'Content-Type': 'application/octet-stream'}
        if digest:
            headers['X-LogBench-Digest'] = hashlib.sha256(data).hexdigest()
        return self.request('PUT', '/api/v2/uploads/%s/parts/%d' % (session_id, part_no),
                            body=data, ip=ip, headers=headers)

    # ------------------------------------------------------------------ create
    def test_create_reports_expected_parts_and_a_chunk_size(self):
        response = self.create_session(chunk=65536)
        self.assertEqual(response.status, 200)
        item = response.value['item']
        self.assertEqual(item['name'], 'trace.log')
        self.assertEqual(item['chunk_size'], 65536)
        self.assertEqual(item['parts_expected'], 1)
        self.assertEqual(item['state'], 'created')

    def test_names_cannot_choose_a_directory(self):
        for name in ('../escape.log', '/etc/passwd', '.hidden', 'a/b.log', 'x' * 200):
            with self.subTest(name=name):
                response = self.create_session(name=name)
                self.assertEqual(response.status, 400, response.value)
                self.assertIn(response.value['error']['code'], ('INVALID_PATH', 'INVALID_INPUT',
                                                                'invalid_name', 'INVALID_NAME'))

    def test_quota_is_enforced_per_principal(self):
        self.app.uploads.store = UploadStore(self.app.store, self.app.config.data_dir,
                                             quota_bytes=1024)
        first = self.create_session(size=800)
        self.assertEqual(first.status, 200)
        second = self.create_session(size=800)
        self.assertEqual(second.status, 413)
        self.assertEqual(second.value['error']['code'], 'quota_exceeded')

    def test_concurrent_sessions_are_capped_per_principal(self):
        # Bytes have a quota; sessions need their own cap. Three half-finished
        # uploads are cheap in bytes but each holds a directory, a descriptor and
        # a row for its whole lifetime.
        first = self.create_session(name='a.log', size=1024)
        second = self.create_session(name='b.log', size=1024)
        self.assertEqual((first.status, second.status), (200, 200))
        third = self.create_session(name='c.log', size=1024)
        self.assertEqual(third.status, 429, third.value)
        self.assertEqual(third.value['error']['code'], 'too_many_sessions')
        # Finishing one hands the slot back.
        self.request('DELETE', '/api/v2/uploads/' + first.value['item']['session_id'])
        again = self.create_session(name='d.log', size=1024)
        self.assertEqual(again.status, 200, again.value)

    def test_another_principal_cannot_see_the_session(self):
        session_id = self.create_session().value['item']['session_id']
        foreign = self.request('GET', '/api/v2/uploads/' + session_id, ip='10.9.9.9')
        missing = self.request('GET', '/api/v2/uploads/u-0000000000000000', ip='10.9.9.9')
        self.assertEqual(foreign.status, 404)
        self.assertEqual(foreign.value['error'], missing.value['error'])

    # ------------------------------------------------------------------- parts
    def test_parts_are_written_with_private_permissions_and_are_idempotent(self):
        session_id = self.create_session().value['item']['session_id']
        first = self.put(session_id, 1, self.blob)
        self.assertEqual(first.status, 200)
        self.assertFalse(first.value['item']['idempotent'])
        again = self.put(session_id, 1, self.blob)
        self.assertEqual(again.status, 200)
        self.assertTrue(again.value['item']['idempotent'], 'a retried part changes nothing')
        status = self.request('GET', '/api/v2/uploads/' + session_id).value['item']
        self.assertEqual(status['bytes_done'], len(self.blob))
        self.assertEqual(status['resume'], [])

        part_files = list((Path(self.app.config.data_dir) / '.logbench-uploads' / session_id)
                          .glob('*.part'))
        self.assertEqual(len(part_files), 1)
        for path in part_files:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600, 'parts must not be world readable')

    def test_a_digest_mismatch_is_refused(self):
        session_id = self.create_session().value['item']['session_id']
        response = self.request('PUT', '/api/v2/uploads/%s/parts/1' % session_id,
                                body=self.blob,
                                headers={'Content-Type': 'application/octet-stream',
                                         'X-LogBench-Digest': '0' * 64})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'digest_mismatch')

    def test_a_short_non_final_part_is_refused(self):
        blob = b'x' * 200000
        session_id = self.create_session(size=len(blob), chunk=65536).value['item']['session_id']
        response = self.put(session_id, 1, b'x' * 1024)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'part_size_mismatch')

    def test_an_out_of_range_part_is_refused(self):
        session_id = self.create_session().value['item']['session_id']
        response = self.put(session_id, 9, self.blob)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_part')

    def test_resume_lists_the_missing_parts(self):
        blob = b'y' * 200000
        session = self.create_session(size=len(blob), chunk=65536).value['item']
        session_id = session['session_id']
        self.assertEqual(session['parts_expected'], 4)
        self.put(session_id, 1, blob[:65536])
        self.put(session_id, 3, blob[131072:196608])
        status = self.request('GET', '/api/v2/uploads/' + session_id).value['item']
        self.assertEqual(status['resume'], [2, 4])
        self.assertEqual(status['bytes_done'], 131072)

    def test_parts_are_refused_after_the_session_is_cancelled(self):
        session_id = self.create_session().value['item']['session_id']
        self.assertEqual(self.request('DELETE', '/api/v2/uploads/' + session_id).status, 200)
        response = self.put(session_id, 1, self.blob)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'session_closed')

    # -------------------------------------------------------------------- seal
    def test_seal_refuses_an_incomplete_session(self):
        blob = b'z' * 200000
        session_id = self.create_session(size=len(blob), chunk=65536).value['item']['session_id']
        self.put(session_id, 1, blob[:65536])
        response = self.request('POST', '/api/v2/uploads/%s/commit' % session_id, {})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'parts_missing')
        self.assertIn(2, response.value['error']['details']['missing'])

    def test_seal_assembles_in_order_and_indexes_the_file(self):
        blob = b''.join(b'[2024-01-01 00:00:0%d.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() part%d\n'
                        % (index % 10, index) for index in range(400))
        session = self.create_session(size=len(blob), chunk=65536).value['item']
        session_id = session['session_id']
        for part_no in range(1, session['parts_expected'] + 1):
            start = (part_no - 1) * 65536
            response = self.put(session_id, part_no, blob[start:start + 65536])
            self.assertEqual(response.status, 200, response.value)
        sealed = self.request('POST', '/api/v2/uploads/%s/commit' % session_id, {})
        self.assertEqual(sealed.status, 200, sealed.value)
        item = sealed.value['item']
        self.assertEqual(item['state'], 'sealed')
        self.assertEqual(item['bytes'], len(blob))
        self.assertEqual(Path(item['sealed_path']).read_bytes(), blob, 'assembly preserves bytes')

        job = self.app.index_jobs.run_now(item['job_id'])
        self.assertEqual(job['state'], 'indexed')
        status = self.request('GET', '/api/v2/uploads/' + session_id).value['item']
        self.assertTrue(status['indexed'])
        self.assertEqual(status['file_id'], item['file_id'])

        query = self.request('POST', '/api/v2/queries',
                             {'file_id': item['file_id'], 'text': 'part42', 'flags': {},
                              'budget_ms': 5000})
        self.assertEqual(query.status, 200, query.value)
        self.assertEqual(query.value['item']['state'], 'done')
        self.assertGreater(query.value['item']['count']['matched'], 0)

    def test_seal_is_idempotent(self):
        session_id = self.create_session().value['item']['session_id']
        self.put(session_id, 1, self.blob)
        first = self.request('POST', '/api/v2/uploads/%s/commit' % session_id, {}).value['item']
        second = self.request('POST', '/api/v2/uploads/%s/commit' % session_id, {}).value['item']
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['file_id'], second['file_id'])
        # A retried seal must reuse the index job it already started: minting a
        # second one would re-index the same bytes and double the worker time.
        self.assertEqual(first['job_id'], second['job_id'])

    # --------------------------------------------------------------------- gc
    def test_expired_sessions_release_quota_and_disk(self):
        store = UploadStore(self.app.store, self.app.config.data_dir, quota_bytes=1024 * 1024,
                            ttl=1)
        session = store.create(self.actor['id'], name='old.log', size=1024)
        with self.app.store.connect() as db:
            db.execute('UPDATE upload_sessions SET expires_at=? WHERE id=?',
                       (0, session['session_id']))
        report = store.gc(now=10)
        self.assertIn(session['session_id'], report['expired'])
        self.assertFalse((store.root / session['session_id']).exists())
        self.assertEqual(store.usage(self.actor['id']), 0)
        with self.assertRaises(UploadError) as caught:
            store.get(session['session_id'], self.actor['id'])
        self.assertEqual(caught.exception.code, 'unknown_session')

    def test_cancel_keeps_the_parts(self):
        session_id = self.create_session().value['item']['session_id']
        self.put(session_id, 1, self.blob)
        self.request('DELETE', '/api/v2/uploads/' + session_id)
        parts = list((Path(self.app.config.data_dir) / '.logbench-uploads' / session_id)
                     .glob('*.part'))
        self.assertEqual(len(parts), 1, 'C8: cancel is not delete')


if __name__ == '__main__':
    unittest.main()
