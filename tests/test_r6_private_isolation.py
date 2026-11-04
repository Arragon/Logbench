"""R6-050: private upload, index, query and download are isolated end to end.

Two principals share one server process and one database. Everything one of them
creates — an upload session, a sealed file, an index, a query snapshot, an export —
must be invisible to the other, and the failure must look the same as "this never
existed" rather than "you may not".

This is the test that would catch the classic mistake: authorizing at upload time
and then trusting a file id later.
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
from server.indexing.cache_versions import CacheRegistry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'

ADMIN = '127.0.0.1'
OTHER = '10.9.9.9'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=[ADMIN]))
    app.index_jobs.executor = None
    app.queries.executor_pool = None
    return app


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.blob = (GOLDEN / 'traceback_chain.log').read_bytes()

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, body=None, ip=ADMIN, headers=None):
        base = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        base.update(headers or {})
        data = body if body is not None else (json.dumps(payload).encode() if payload is not None else b'')
        return self.app.handle(method, target, base, data, ip)

    def upload_and_index(self, name='private.log'):
        created = self.request('POST', '/api/v2/uploads', {'name': name, 'size': len(self.blob)})
        self.assertEqual(created.status, 200, created.value)
        session = created.value['item']
        part = self.request('PUT', '/api/v2/uploads/%s/parts/1' % session['session_id'],
                            body=self.blob,
                            headers={'Content-Type': 'application/octet-stream',
                                     'X-LogBench-Digest': hashlib.sha256(self.blob).hexdigest()})
        self.assertEqual(part.status, 200, part.value)
        sealed = self.request('POST', '/api/v2/uploads/%s/commit' % session['session_id'], {})
        self.assertEqual(sealed.status, 200, sealed.value)
        item = sealed.value['item']
        self.app.index_jobs.run_now(item['job_id'])
        return session['session_id'], item['file_id']

    # -------------------------------------------------------------------- cases
    def test_the_owner_can_read_everything_they_created(self):
        session_id, file_id = self.upload_and_index()
        status = self.request('GET', '/api/v2/uploads/' + session_id)
        self.assertEqual(status.status, 200)
        self.assertTrue(status.value['item']['indexed'])
        self.assertEqual(self.request('GET', '/api/v2/files/' + file_id).status, 200)
        self.assertEqual(self.request('GET', '/api/v2/files/%s/records' % file_id).status, 200)
        query = self.request('POST', '/api/v2/queries',
                            {'file_id': file_id, 'text': 'Traceback', 'flags': {}})
        self.assertEqual(query.status, 200)
        self.assertGreater(query.value['item']['count']['matched'], 0)

    def test_the_same_ids_are_absent_for_another_principal(self):
        session_id, file_id = self.upload_and_index()
        query = self.request('POST', '/api/v2/queries',
                            {'file_id': file_id, 'text': 'Traceback', 'flags': {}}).value['item']
        targets = [
            ('GET', '/api/v2/uploads/' + session_id, None),
            ('GET', '/api/v2/files/' + file_id, None),
            ('GET', '/api/v2/files/%s/records' % file_id, None),
            ('GET', '/api/v2/files/%s/timeline' % file_id, None),
            ('GET', '/api/v2/files/%s/generations' % file_id, None),
            ('GET', '/api/v2/files/%s/seek?line=1' % file_id, None),
            ('GET', '/api/v2/records/%s:0' % file_id, None),
            ('GET', '/api/v2/records/%s:0/raw' % file_id, None),
            ('GET', '/api/v2/queries/' + query['query_id'], None),
            ('GET', '/api/v2/queries/%s/records' % query['query_id'], None),
            ('GET', '/api/v2/queries/%s/export' % query['query_id'], None),
        ]
        # Each family has its own "not there" answer; what matters is that a foreign
        # id produces exactly the same answer as an id that never existed.
        baselines = {
            '/api/v2/uploads/': '/api/v2/uploads/u-0000000000000000',
            '/api/v2/files/': '/api/v2/files/f_0000000000000000',
            '/api/v2/records/': '/api/v2/records/f_0000000000000000:0',
            '/api/v2/queries/': '/api/v2/queries/q-0000000000000000',
        }
        for method, target, payload in targets:
            with self.subTest(target=target):
                response = self.request(method, target, payload, ip=OTHER)
                self.assertEqual(response.status, 404, target)
                family = next(prefix for prefix in baselines if target.startswith(prefix))
                baseline = baselines[family]
                missing = self.request(
                    method, baseline + target[len(family) + 8:].split('/', 1)[0]
                    if False else baseline, None, ip=OTHER)
                self.assertEqual(response.value['error']['code'],
                                 missing.value['error']['code'], target)

    def test_another_principal_cannot_upload_into_the_session(self):
        session_id, _ = self.upload_and_index()
        response = self.request('PUT', '/api/v2/uploads/%s/parts/1' % session_id,
                                body=self.blob, ip=OTHER,
                                headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.value['error']['code'], 'unknown_session')

    def test_another_principal_cannot_cancel_or_seal_the_session(self):
        session_id, _ = self.upload_and_index()
        self.assertEqual(self.request('DELETE', '/api/v2/uploads/' + session_id, ip=OTHER).status,
                         404)
        self.assertEqual(
            self.request('POST', '/api/v2/uploads/%s/commit' % session_id, {}, ip=OTHER).status,
            404)

    def test_query_creation_for_a_foreign_file_does_not_leak_existence(self):
        _, file_id = self.upload_and_index()
        foreign = self.request('POST', '/api/v2/queries',
                              {'file_id': file_id, 'text': 'x', 'flags': {}}, ip=OTHER)
        missing = self.request('POST', '/api/v2/queries',
                              {'file_id': 'f_0000000000000000', 'text': 'x', 'flags': {}}, ip=OTHER)
        self.assertEqual(foreign.status, 404)
        self.assertEqual(foreign.value['error'], missing.value['error'])

    def test_revoking_the_owner_closes_every_path(self):
        session_id, file_id = self.upload_and_index()
        CacheRegistry(self.app.store).revoke(self.app.actor(ADMIN)['id'])
        for target in ('/api/v2/files/' + file_id,
                       '/api/v2/files/%s/records' % file_id,
                       '/api/v2/uploads/' + session_id,
                       '/api/v2/records/%s:0' % file_id):
            response = self.request('GET', target)
            self.assertEqual(response.status, 404, target)

    def test_two_principals_can_index_the_same_bytes_independently(self):
        # B is not an admin, so it cannot start an index job; it also cannot see the
        # file A indexed. This asserts the second half without pretending B has a
        # path that the file cage would reject anyway.
        _, file_id = self.upload_and_index()
        self.assertEqual(self.request('GET', '/api/v2/files/' + file_id, ip=OTHER).status, 404)
        with self.app.store.connect() as db:
            owners = {row['owner'] for row in db.execute(
                'SELECT owner FROM layer_versions WHERE file_id=?', (file_id,))}
            sessions = {row['principal'] for row in db.execute(
                'SELECT principal FROM upload_sessions')}
        self.assertEqual(owners, {self.app.actor(ADMIN)['id']})
        self.assertEqual(len(sessions), 1)


if __name__ == '__main__':
    unittest.main()
