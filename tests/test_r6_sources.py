"""R6-066: sources, mappings, revision-bound probes and idempotent actions.

Real SQLite, real HTTP (a loopback file server with Range support), real CSRF and
real authorization. The cases are the ones that decide whether the acquisition
centre tells the truth:

* a stale probe cannot enable an edited configuration;
* two mappings cannot write into overlapping target subtrees;
* `enable` queues a job and does not pretend it is running;
* a member sees a summary, not an endpoint or a policy;
* deleting a source keeps what was already collected.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config

BODY = b'2026-11-03 10:00:00 INFO collect sample line\n' * 500


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        return

    def do_HEAD(self):
        self.respond(head=True)

    def do_GET(self):
        self.respond(head=False)

    def respond(self, head=False):
        total = len(BODY)
        header = self.headers.get('Range')
        if header:
            start, _, end = header.replace('bytes=', '').partition('-')
            start, end = int(start), int(end or total - 1)
            end = min(end, total - 1)
            payload = BODY[start:end + 1]
            self.send_response(206)
            self.send_header('ETag', '"sources-1"')
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, total))
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header('ETag', '"sources-1"')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(total))
        self.end_headers()
        if not head:
            self.wfile.write(BODY)


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    (root / 'rules' / 'cpqpsgx-default.json').write_text(
        json.dumps({'name': 'Basic', 'headerPattern': '^(?<message>.*)$',
                    'moduleRules': [], 'stepRules': [], 'errorRules': []}), 'utf-8')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = 'http://127.0.0.1:%d/var/log/app.log' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.ip = '127.0.0.1'
        self.csrf = self.app.csrf(self.ip)

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def call(self, method, path, payload=None, *, ip=None, csrf=True, actor=None):
        headers = {'host': '127.0.0.1'}
        body = b''
        if method in ('POST', 'PATCH', 'DELETE', 'PUT'):
            body = json.dumps(payload or {}).encode()
            headers['content-type'] = 'application/json'
            if csrf:
                headers['x-logbench-csrf'] = self.app.csrf(ip or self.ip)
        return self.app.handle(method, path, headers, body, ip or self.ip)

    def create_source(self, **overrides):
        payload = {
            'adapter': 'http', 'label': 'PSG-A 调试日志',
            'endpoint': overrides.pop('endpoint', self.origin),
            'policy': overrides.pop('policy', {
                'allowedHosts': ['127.0.0.1'], 'allowLoopback': True,
                'allowedPorts': [self.server.server_port]}),
            'mappings': overrides.pop('mappings', [
                {'remoteDir': '/var/log/', 'rootId': 'legacy',
                 'targetSubdir': 'psg-a/debug', 'mode': 'scheduled'}]),
        }
        payload.update(overrides)
        response = self.call('POST', '/api/v2/sources', payload)
        self.assertEqual(response.status, 200, response.value)
        return response.value['item']

    # -------------------------------------------------------------------- cases
    def test_unknown_adapter_is_refused(self):
        response = self.call('POST', '/api/v2/sources',
                             {'adapter': 'telnet', 'label': 'x', 'endpoint': 'telnet://h'})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'unsupported')

    def test_credentials_in_the_endpoint_are_refused(self):
        response = self.call('POST', '/api/v2/sources',
                             {'adapter': 'http', 'label': 'x',
                              'endpoint': 'http://user:pw@127.0.0.1/log'})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'credentials_in_url')
        self.assertNotIn('pw', json.dumps(response.value))

    def test_unknown_policy_field_is_refused_instead_of_ignored(self):
        response = self.call('POST', '/api/v2/sources',
                             {'adapter': 'http', 'label': 'x', 'endpoint': self.origin,
                              'policy': {'command': 'rm -rf /'}})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.value['error']['code'], 'invalid_policy')
        self.assertIn('command', str(response.value['error']['details']['fields']))

    def test_create_stores_a_mapping_and_reports_a_fingerprint(self):
        item = self.create_source()
        self.assertEqual(item['adapter'], 'http')
        self.assertFalse(item['enabled'])
        self.assertEqual(len(item['mappings']), 1)
        self.assertEqual(item['mappings'][0]['targetSubdir'], 'psg-a/debug')
        self.assertEqual(item['mappings'][0]['remoteDir'], '/var/log/')
        self.assertTrue(item['fingerprint'])

    def test_enable_before_a_probe_is_refused(self):
        item = self.create_source()
        response = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                             {'action': 'enable', 'actionId': 'a-1',
                              'expectedRevision': item['revision']})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'probe_required')

    def test_probe_then_enable_queues_a_job_without_claiming_it_ran(self):
        item = self.create_source()
        probe = self.call('POST', '/api/v2/sources/%s/probe' % item['id'], {})
        self.assertEqual(probe.status, 200, probe.value)
        self.assertTrue(probe.value['probe']['ok'], probe.value['probe']['checks'])
        self.assertEqual(probe.value['probe']['capabilities']['rangeRead'], 'supported')

        enabled = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                            {'action': 'enable', 'actionId': 'enable-1'})
        self.assertEqual(enabled.status, 200, enabled.value)
        result = enabled.value['result']
        self.assertTrue(result['enabled'])
        self.assertEqual(result['queued']['state'], 'queued')
        job = self.app.jobs.get({'ip': self.ip, 'id': self.app.namespace(self.ip), 'role': 'admin'},
                                result['queued']['jobId'])
        self.assertEqual(job['kind'], 'collect')
        self.assertEqual(job['state'], 'queued')

    def test_editing_the_configuration_invalidates_the_previous_probe(self):
        item = self.create_source()
        self.call('POST', '/api/v2/sources/%s/probe' % item['id'], {})
        patched = self.call('PATCH', '/api/v2/sources/%s' % item['id'],
                            {'expectedRevision': item['revision'],
                             'policy': {'allowedHosts': ['127.0.0.1'], 'allowLoopback': True,
                                        'allowedPorts': [self.server.server_port],
                                        'appendContract': 'declared',
                                        'generationId': 'gen-2'}})
        self.assertEqual(patched.status, 200, patched.value)
        stale = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                          {'action': 'enable', 'actionId': 'enable-2'})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.value['error']['code'], 'probe_stale')

    def test_stale_revision_conflicts_keep_the_old_state(self):
        item = self.create_source()
        response = self.call('PATCH', '/api/v2/sources/%s' % item['id'],
                             {'expectedRevision': item['revision'] + 5, 'label': 'renamed'})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'revision_conflict')
        current = self.call('GET', '/api/v2/sources/%s' % item['id'])
        self.assertEqual(current.value['item']['label'], 'PSG-A 调试日志')

    def test_overlapping_targets_are_refused(self):
        item = self.create_source()
        for target in ('psg-a/debug', 'psg-a', 'psg-a/debug/sw'):
            response = self.call('POST', '/api/v2/sources/%s/mappings' % item['id'],
                                 {'remoteDir': '/var/log/other/', 'rootId': 'legacy',
                                  'targetSubdir': target})
            self.assertEqual(response.status, 409, target)
            self.assertEqual(response.value['error']['code'], 'target_overlap')

    def test_traversal_and_hidden_targets_are_refused(self):
        item = self.create_source()
        for target in ('../escape', '.hidden', 'a/../../b', ''):
            response = self.call('POST', '/api/v2/sources/%s/mappings' % item['id'],
                                 {'remoteDir': '/var/log/', 'rootId': 'legacy',
                                  'targetSubdir': target})
            self.assertEqual(response.status, 400, target)
            self.assertEqual(response.value['error']['code'], 'invalid_mapping')

    def test_members_see_a_summary_and_cannot_write(self):
        item = self.create_source(acl='members')
        self.app.handle('POST', '/api/v1/admin/users',
                        {'host': '127.0.0.1', 'content-type': 'application/json',
                         'x-logbench-csrf': self.csrf},
                        json.dumps({'ip': '10.1.1.9', 'name': '成员', 'role': 'member',
                                    'enabled': True}).encode(), self.ip)
        listed = self.call('GET', '/api/v2/sources', ip='10.1.1.9')
        self.assertEqual(listed.status, 200, listed.value)
        self.assertEqual(len(listed.value['items']), 1)
        summary = listed.value['items'][0]
        self.assertNotIn('endpoint', summary)
        self.assertNotIn('policy', summary)
        self.assertNotIn('mappings', summary)
        self.assertEqual(summary['mappingCount'], 1)
        self.assertEqual(summary['label'], 'PSG-A 调试日志')

        forbidden = self.call('PATCH', '/api/v2/sources/%s' % item['id'],
                              {'label': 'x'}, ip='10.1.1.9')
        self.assertEqual(forbidden.status, 403)

    def test_deleting_a_source_keeps_collected_bytes(self):
        item = self.create_source()
        with self.app.store.transaction() as db:
            db.execute("INSERT INTO source_files(id,source_id,mapping_id,remote_path,file_id,"
                       "generation,root_id,rel_path,size,evidence,archived_bytes,contiguous_bytes,"
                       "gap_state,sealed,state,last_seen_at,last_error,retry_at) "
                       "VALUES('sf_1',?,?,'/var/log/app.log','f_1',1,'legacy',"
                       "'collected/a.log',100,'{}',100,100,'none',0,'idle',NULL,NULL,NULL)",
                       (item['id'], item['mappings'][0]['id']))
        deleted = self.call('DELETE', '/api/v2/sources/%s' % item['id'], {})
        self.assertEqual(deleted.status, 200, deleted.value)
        self.assertTrue(deleted.value['archiveRetained'])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM source_files').fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM remote_sources').fetchone()['n'], 0)

    def test_action_ids_are_idempotent(self):
        item = self.create_source()
        self.call('POST', '/api/v2/sources/%s/probe' % item['id'], {})
        first = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                          {'action': 'enable', 'actionId': 'once'})
        second = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                           {'action': 'enable', 'actionId': 'once'})
        self.assertEqual(first.status, 200)
        self.assertEqual(first.value['result'], second.value['result'])
        with self.app.store.connect() as db:
            jobs = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind='collect'").fetchone()['n']
            actions = db.execute('SELECT COUNT(*) AS n FROM source_actions').fetchone()['n']
        self.assertEqual(jobs, 1)
        self.assertEqual(actions, 1)

    def test_pause_and_resume_do_not_delete_anything(self):
        item = self.create_source()
        self.call('POST', '/api/v2/sources/%s/probe' % item['id'], {})
        self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                  {'action': 'enable', 'actionId': 'e'})
        paused = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                           {'action': 'pause', 'actionId': 'p'})
        self.assertTrue(paused.value['result']['paused'])
        self.assertTrue(paused.value['result']['archiveRetained'])
        resumed = self.call('POST', '/api/v2/sources/%s/actions' % item['id'],
                            {'action': 'resume', 'actionId': 'r'})
        self.assertFalse(resumed.value['result']['paused'])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM source_files').fetchone()['n'], 0)

    def test_credential_lifecycle_never_returns_the_secret(self):
        created = self.call('POST', '/api/v2/credentials',
                            {'kind': 'token', 'label': 'psg-a token',
                             'value': 'super-secret-token'})
        self.assertEqual(created.status, 200, created.value)
        ref = created.value['item']['ref']
        self.assertNotIn('super-secret-token', json.dumps(created.value))
        listed = self.call('GET', '/api/v2/credentials')
        self.assertEqual(listed.value['items'][0]['ref'], ref)
        self.assertNotIn('super-secret-token', json.dumps(listed.value))
        # A source referencing it blocks deletion.
        self.create_source(credentialRef=ref)
        blocked = self.call('DELETE', '/api/v2/credentials/%s' % ref, {})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.value['error']['code'], 'credential_in_use')

    def test_intake_rows_carry_real_numbers_not_timers(self):
        item = self.create_source()
        payload = self.call('GET', '/api/v2/intake')
        self.assertEqual(payload.status, 200)
        row = payload.value['items'][0]
        self.assertEqual(row['files'], 0)
        self.assertEqual(row['mappingCount'], 1, '列表必须给出映射数，UI 不能显示 undefined')
        self.assertEqual(row['lagBytes'], 0)
        self.assertIsNone(row['lastSeenAt'])
        self.assertIn('adapters', payload.value)
        adapters = {entry['adapter']: entry for entry in payload.value['adapters']}
        self.assertTrue(adapters['http']['available'])
        if not adapters['sftp']['available']:
            self.assertEqual(adapters['sftp']['reason'], 'tool_missing')


if __name__ == '__main__':
    unittest.main()
