"""Bounded backend contract checks without browser or live external services."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.app import Application, Config
from server.wsgi import WSGI
from server.httpd import Handler
from types import SimpleNamespace


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'rules').mkdir()
        self.profile = {'name': 'Basic', 'headerPattern': '^(?<message>.*)$', 'moduleRules': [], 'stepRules': [], 'errorRules': []}
        (self.root / 'rules' / 'cpqpsgx-default.json').write_text(json.dumps(self.profile), 'utf-8')
        (self.root / 'LogBench.html').write_text('<!doctype html><title>LogBench</title>', 'utf-8')
        self.app = Application(Config(self.root, bootstrap_admins=['127.0.0.1']))
        data = self.app.config.data_dir
        (data / 'machine-a' / 'nested').mkdir(parents=True)
        (data / 'machine-a' / 'run.log').write_text('中文日志\nsecond line\n', 'utf-8')
        (data / 'machine-a' / 'nested' / 'second.log').write_text('2\n', 'utf-8')
        (data / 'shared.log').write_text('root\n', 'utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, target, payload=None, ip='127.0.0.1', headers=None, app=None):
        application = app or self.app
        h = {'Host': 'localhost:50004', 'Content-Type': 'application/json', 'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': application.csrf(ip)}
        h.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        return application.handle(method, target, h, body, ip)

    def good(self, response, status=200):
        self.assertEqual(response.status, status, response.value)
        return response.value

    def create_rule(self, name='Alternative'):
        return self.good(self.request('POST', '/api/v1/rules', {'name': name, 'profile': self.profile}))['item']

    def bind(self, scope, path, rule, revision=0):
        return self.good(self.request('PUT', '/api/v1/bindings', {'scope': scope, 'path': path, 'ruleId': rule, 'expectedRevision': revision}))['item']

    def test_explicit_bootstrap_and_anonymous_read_only(self):
        me = self.good(self.request('GET', '/api/v1/me'))
        self.assertEqual(me['user']['role'], 'admin')
        visitor = self.good(self.request('GET', '/api/v1/me', ip='10.2.3.4'))
        self.assertEqual(visitor['user']['role'], 'viewer')
        self.assertFalse(visitor['capabilities']['rulesWrite'])
        self.assertNotEqual(me['user']['id'], visitor['user']['id'])
        self.assertEqual(self.request('POST', '/api/v1/rules', {'name': 'x', 'profile': self.profile}, ip='10.2.3.4').status, 403)
        self.assertEqual(self.request('DELETE', '/api/v1/files?path=shared.log', ip='10.2.3.4').status, 403)
        self.assertEqual(self.request('GET', '/api/v1/admin/users', ip='10.2.3.4').status, 403)

    def test_user_preferences_are_server_scoped_and_revision_guarded(self):
        a, b = '10.1.1.1', '10.1.1.2'
        saved = self.good(self.request('PUT', '/api/v1/preferences', {'value': {'name': 'private-a', 'userId': b}, 'expectedRevision': 0}, ip=a))
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(self.good(self.request('GET', '/api/v1/preferences', ip=b))['value'], {})
        self.assertEqual(self.good(self.request('GET', '/api/v1/preferences?userId=' + a, ip=b))['value'], {})
        self.assertEqual(self.request('PUT', '/api/v1/preferences', {'value': {}, 'expectedRevision': 0}, ip=a).status, 409)
        self.assertEqual(self.good(self.request('GET', '/api/v1/preferences', ip=a))['value']['name'], 'private-a')

    def test_csrf_origin_and_host_guards(self):
        payload = {'name': 'x', 'profile': self.profile}
        self.assertEqual(self.request('POST', '/api/v1/rules', payload, headers={'X-LogBench-CSRF': ''}).status, 403)
        self.assertEqual(self.request('POST', '/api/v1/rules', payload, headers={'Origin': 'https://evil.test'}).status, 403)
        self.assertEqual(self.request('POST', '/api/v1/rules', payload, headers={'Origin': 'null'}).status, 403)
        self.assertEqual(self.request('POST', '/api/v1/rules', payload, headers={'Sec-Fetch-Site': 'cross-site'}).status, 403)
        self.assertEqual(self.request('GET', '/api/v1/me', headers={'Host': 'evil.test'}).status, 421)
        self.assertEqual(self.request('GET', '/api/v1/me', headers={'Host': 'localhost@evil.test'}).status, 400)

    def test_untrusted_xff_cannot_impersonate_admin(self):
        user = self.good(self.request('GET', '/api/v1/me', ip='10.2.3.4', headers={'X-Forwarded-For': '127.0.0.1'}))['user']
        self.assertEqual(user['ip'], '10.2.3.4')
        self.assertEqual(user['role'], 'viewer')

    def test_trusted_proxy_walks_right_to_left_and_missing_fails(self):
        proxy = Application(Config(self.root, trusted_proxies=['10.0.0.10']))
        result = self.good(self.request('GET', '/api/v1/me', ip='10.0.0.10', app=proxy, headers={'X-Forwarded-For': '127.0.0.1, 10.8.8.8'}))
        self.assertEqual(result['user']['ip'], '10.8.8.8')
        self.assertEqual(result['user']['role'], 'viewer')
        self.assertEqual(self.request('GET', '/api/v1/me', ip='10.0.0.10', app=proxy).status, 400)
        self.assertEqual(self.request('GET', '/api/v1/me', ip='10.0.0.10', app=proxy, headers={'X-Forwarded-For': 'invalid'}).status, 400)

    def test_users_revocation_next_request_and_self_lockout_guard(self):
        item = self.good(self.request('PUT', '/api/v1/admin/users', {'ip': '10.2.3.4', 'name': 'B', 'role': 'admin', 'enabled': True, 'expectedRevision': 0}))['item']
        self.assertEqual(self.good(self.request('GET', '/api/v1/me', ip='10.2.3.4'))['user']['role'], 'admin')
        self.good(self.request('PUT', '/api/v1/admin/users', {**item, 'role': 'viewer', 'expectedRevision': item['revision']}))
        self.assertEqual(self.request('POST', '/api/v1/rules', {'name': 'denied', 'profile': self.profile}, ip='10.2.3.4').status, 403)
        self.assertEqual(self.request('DELETE', '/api/v1/admin/users?ip=127.0.0.1', {'expectedRevision': 1}).status, 409)
        self.good(self.request('PUT', '/api/v1/admin/users', {'ip': '10.2.3.4', 'name': 'B', 'role': 'viewer', 'enabled': False, 'expectedRevision': 2}))
        self.assertEqual(self.request('GET', '/api/v1/files', ip='10.2.3.4').status, 403)

    def test_library_pagination_recursive_search_hidden_paths(self):
        page = self.good(self.request('GET', '/api/v1/files?limit=1'))
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['items'][0]['type'], 'directory')
        self.assertEqual(page['nextCursor'], 1)
        second = self.good(self.request('GET', '/api/v1/files?limit=1&cursor=1'))
        self.assertEqual(second['items'][0]['name'], 'shared.log')
        self.assertIsNone(second['nextCursor'])
        search = self.good(self.request('GET', '/api/v1/files?q=SECOND'))
        self.assertEqual(search['items'][0]['path'], 'machine-a/nested/second.log')
        self.assertEqual(self.request('GET', '/api/v1/files?path=../var').status, 400)
        self.assertEqual(self.request('GET', '/api/v1/files?path=.logbench-trash').status, 400)

    def test_download_binary_stream_range_etag(self):
        response = self.request('GET', '/api/v1/files/content?path=machine-a/run.log')
        self.assertEqual(response.status, 200)
        content = response.stream.read()
        response.close()
        self.assertEqual(len(content), response.length)
        self.assertIn('中文日志'.encode(), content)
        cached = self.request('GET', '/api/v1/files/content?path=machine-a/run.log', headers={'If-None-Match': response.headers['ETag']})
        self.assertEqual(cached.status, 304)
        ranged = self.request('GET', '/api/v1/files/content?path=machine-a/run.log', headers={'Range': 'bytes=0-5'})
        self.assertEqual(ranged.status, 206)
        self.assertEqual(ranged.length, 6)
        self.assertEqual(ranged.stream.read(6), content[:6])
        ranged.close()
        self.assertEqual(self.request('GET', '/api/v1/files/content?path=machine-a/run.log', headers={'Range': 'bytes=999999-'}).status, 416)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink guard')
    def test_symlink_and_traversal_are_denied(self):
        external = self.root / 'secret.txt'
        external.write_text('private', 'utf-8')
        (self.app.config.data_dir / 'leak.log').symlink_to(external)
        (self.app.config.data_dir / 'leak-dir').symlink_to(self.app.config.state_dir, target_is_directory=True)
        self.assertEqual(self.request('GET', '/api/v1/files/content?path=leak.log').status, 403)
        self.assertEqual(self.request('GET', '/api/v1/files?path=leak-dir').status, 403)
        names = [x['name'] for x in self.good(self.request('GET', '/api/v1/files'))['items']]
        self.assertNotIn('leak.log', names)
        for path in ('../secret.txt', '%2e%2e%2fsecret.txt', '/etc/passwd', 'C:%5csecret'):
            self.assertIn(self.request('GET', '/api/v1/files/content?path=' + path).status, (400, 403))
        self.assertEqual(self.request('GET', '/var/logbench.sqlite3').status, 404)
        self.assertEqual(self.request('GET', '/data/shared.log').status, 404)
        self.assertEqual(self.request('POST', '/api/v1/uploads', {}).status, 404)

    def test_shared_file_soft_delete_restore_no_overwrite(self):
        result = self.good(self.request('DELETE', '/api/v1/files?path=shared.log'))['item']
        self.assertFalse((self.app.config.data_dir / 'shared.log').exists())
        self.assertEqual(self.request('GET', '/api/v1/files/content?path=shared.log').status, 404)
        trash = self.good(self.request('GET', '/api/v1/files/trash'))['items']
        self.assertEqual(trash[0]['id'], result['id'])
        (self.app.config.data_dir / 'shared.log').write_text('different', 'utf-8')
        self.assertEqual(self.request('POST', '/api/v1/files/restore', {'id': result['id']}).status, 409)
        (self.app.config.data_dir / 'shared.log').unlink()
        self.good(self.request('POST', '/api/v1/files/restore', {'id': result['id']}))
        self.assertEqual((self.app.config.data_dir / 'shared.log').read_text(), 'root\n')
        self.assertEqual(self.good(self.request('GET', '/api/v1/files/trash'))['items'], [])

    def test_rule_lifecycle_copy_revision_and_restore(self):
        rule = self.create_rule()
        updated = self.good(self.request('PUT', '/api/v1/rules/' + rule['id'], {'name': 'Changed', 'profile': self.profile, 'expectedRevision': 1}))['item']
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['profile']['name'], 'Changed')
        self.assertEqual(self.request('PUT', '/api/v1/rules/' + rule['id'], {'name': 'Stale', 'profile': self.profile, 'expectedRevision': 1}).status, 409)
        copied = self.good(self.request('POST', '/api/v1/rules/' + rule['id'] + '/copy', {}))['item']
        self.assertNotEqual(copied['id'], rule['id'])
        deleted = self.good(self.request('DELETE', '/api/v1/rules/' + rule['id'], {'expectedRevision': 2}))['item']
        self.assertTrue(deleted['deletedAt'])
        self.assertEqual(self.request('GET', '/api/v1/rules/' + rule['id']).status, 404)
        self.assertEqual(len(self.good(self.request('GET', '/api/v1/rules?deleted=1'))['items']), 1)
        self.good(self.request('POST', '/api/v1/rules/' + rule['id'] + '/restore', {'expectedRevision': 3}))
        self.assertEqual(self.good(self.request('GET', '/api/v1/rules/' + rule['id']))['item']['revision'], 4)

    def test_binding_precedence_nearest_folder_and_disabled_rule(self):
        a, b, c = [self.create_rule(name) for name in ('A', 'B', 'C')]
        self.bind('directory', 'machine-a', a['id'])
        self.bind('directory', 'machine-a/nested', b['id'])
        self.bind('file', 'machine-a/nested/second.log', c['id'])
        self.assertEqual(self.good(self.request('GET', '/api/v1/bindings/resolve?path=machine-a/run.log'))['rule']['id'], a['id'])
        self.assertEqual(self.good(self.request('GET', '/api/v1/bindings/resolve?path=machine-a/nested/second.log'))['rule']['id'], c['id'])
        self.good(self.request('DELETE', '/api/v1/bindings', {'scope': 'file', 'path': 'machine-a/nested/second.log', 'expectedRevision': 1}))
        self.assertEqual(self.good(self.request('GET', '/api/v1/bindings/resolve?path=machine-a/nested/second.log'))['rule']['id'], b['id'])
        self.assertEqual(self.request('DELETE', '/api/v1/rules/' + b['id'], {'expectedRevision': 1}).status, 409)
        self.good(self.request('DELETE', '/api/v1/bindings', {'scope': 'directory', 'path': 'machine-a/nested', 'expectedRevision': 1}))
        self.good(self.request('DELETE', '/api/v1/rules/' + b['id'], {'expectedRevision': 1}))
        # Bound rules must be unbound explicitly; removing a child binding restores parent inheritance.
        self.assertEqual(self.good(self.request('GET', '/api/v1/bindings/resolve?path=machine-a/nested/second.log'))['rule']['id'], a['id'])
        self.good(self.request('POST', '/api/v1/rules/' + b['id'] + '/restore', {'expectedRevision': 2}))
        self.bind('directory', 'machine-a/nested', b['id'])
        self.assertEqual(self.good(self.request('GET', '/api/v1/bindings/resolve?path=machine-a/nested/second.log'))['rule']['id'], b['id'])

    def write_mc(self, mapping, admins):
        directory = self.root / 'external-mc' / 'data'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'ip_mapping.json').write_text(json.dumps(mapping), 'utf-8')
        (directory / 'admin_users.json').write_text(json.dumps(admins), 'utf-8')
        return {'identityMode': 'mcchecker', 'mccheckerDirectory': str(directory.parent), 'mccheckerDataDirectory': ''}

    def test_mc_current_ip_priority_legacy_and_revocation(self):
        mapping = [{'ip': '127.0.0.1', 'name': 'Owner'}, {'ip': '10.0.0.2', 'name': 'Shared'}, {'ip': '10.0.0.3', 'name': 'Shared'}, {'ip': '10.0.0.4', 'name': 'Legacy'}]
        admins = [{'ip': '127.0.0.1', 'name': 'Owner'}, {'ip': '10.0.0.2', 'name': 'Shared'}, {'name': 'Legacy'}]
        config = self.write_mc(mapping, admins)
        self.good(self.request('POST', '/api/v1/admin/identity-preview', {'config': config}))
        self.good(self.request('PUT', '/api/v1/admin/config', {'config': config, 'expectedRevision': 1}))
        self.assertEqual(self.good(self.request('GET', '/api/v1/me', ip='10.0.0.2'))['user']['role'], 'admin')
        self.assertEqual(self.good(self.request('GET', '/api/v1/me', ip='10.0.0.3'))['user']['role'], 'member')
        self.assertEqual(self.good(self.request('GET', '/api/v1/me', ip='10.0.0.4'))['user']['role'], 'admin')
        self.assertEqual(self.request('PUT', '/api/v1/admin/users', {'ip': '10.0.0.5', 'name': 'X', 'role': 'admin', 'expectedRevision': 0}).status, 409)
        self.write_mc(mapping, [admins[0]])
        self.assertEqual(self.request('POST', '/api/v1/rules', {'name': 'revoked', 'profile': self.profile}, ip='10.0.0.2').status, 403)
        self.assertEqual(self.good(self.request('GET', '/api/v1/me', ip='10.0.0.4'))['user']['role'], 'member')

    def test_mc_invalid_source_fail_closed_and_no_local_union(self):
        config = self.write_mc([{'ip': '127.0.0.1', 'name': 'Owner'}], [{'ip': '127.0.0.1'}])
        self.good(self.request('PUT', '/api/v1/admin/config', {'config': config, 'expectedRevision': 1}))
        self.write_mc([{'ip': '127.0.0.1', 'name': 'Owner'}], [])
        self.assertEqual(self.good(self.request('GET', '/api/v1/me'))['user']['role'], 'member')
        self.assertEqual(self.request('POST', '/api/v1/rules', {'name': 'x', 'profile': self.profile}).status, 403)
        (self.root / 'external-mc' / 'data' / 'admin_users.json').write_text('broken', 'utf-8')
        self.assertEqual(self.request('GET', '/api/v1/me').status, 503)

    def test_mc_switch_prevents_admin_lockout(self):
        config = self.write_mc([], [])
        self.assertEqual(self.request('PUT', '/api/v1/admin/config', {'config': config, 'expectedRevision': 1}).status, 409)
        self.assertEqual(self.good(self.request('GET', '/api/v1/admin/config'))['config']['identityMode'], 'local')

    def test_audit_and_persistence_across_instances(self):
        created = self.create_rule('Persistent')
        other = Application(Config(self.root))
        self.assertEqual(self.good(self.request('GET', '/api/v1/rules/' + created['id'], app=other))['item']['name'], 'Persistent')
        logs = self.good(self.request('GET', '/api/v1/admin/audit'))['items']
        self.assertTrue(any(item['action'] == 'rule.create' for item in logs))
        self.assertEqual(self.good(self.request('GET', '/api/v1/admin/stats'))['rules'], 2)

    def test_invalid_payloads_are_bounded(self):
        self.assertEqual(self.request('POST', '/api/v1/rules', {'name': 'bad', 'profile': {}}).status, 400)
        self.assertEqual(self.request('GET', '/api/v1/files?limit=10000000').status, 400)
        self.assertEqual(self.request('GET', '/api/v1/files?path=x&path=y').status, 400)
        result = self.app.handle('PUT', '/api/v1/preferences', {'Host': 'localhost', 'X-LogBench-CSRF': self.app.csrf('127.0.0.1'), 'Content-Type': 'application/json'}, b'[]', '127.0.0.1')
        self.assertEqual(result.status, 400)

    def wire(self, request):
        class MemorySocket:
            def __init__(self, data):
                self.input = io.BytesIO(data)
                self.output = bytearray()
            def settimeout(self, value):
                self.timeout = value
            def makefile(self, mode, buffering=-1):
                return self.input
            def sendall(self, data):
                self.output.extend(data)
        transport = MemorySocket(request)
        Handler(transport, ('127.0.0.1', 12345), SimpleNamespace(application=self.app))
        return bytes(transport.output)

    def test_http_transport_request_guards_and_head(self):
        result = self.wire(b'GET /api/v1/me HTTP/1.1\r\nHost: localhost\r\n\r\n')
        head, body = result.split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', head)
        self.assertEqual(json.loads(body)['user']['role'], 'admin')
        head = self.wire(b'HEAD /api/v1/files/content?path=shared.log HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertTrue(head.endswith(b'\r\n\r\n'))
        self.assertIn(b'Content-Length: 5', head)
        for request in (
            b'POST /api/v1/rules HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n',
            b'GET /api/v1/me HTTP/1.1\r\nHost: localhost\r\nHost: evil.test\r\n\r\n',
            b'POST /api/v1/rules HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n',
        ):
            self.assertIn(b'400 Bad Request', self.wire(request))
        self.assertIn(b'HTTP/1.1 413 ', self.wire(b'POST /api/v1/rules HTTP/1.1\r\nHost: localhost\r\nContent-Length: 99999999\r\n\r\n'))

    def test_wsgi_rejects_incomplete_request_body(self):
        status = []
        environ = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/api/v1/rules', 'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': 'localhost', 'CONTENT_LENGTH': '200', 'wsgi.input': io.BytesIO(b'{}')}
        output = b''.join(WSGI(self.app)(environ, lambda s, h: status.append(s)))
        self.assertEqual(status[0], '400 Bad Request')
        self.assertEqual(json.loads(output)['error']['code'], 'INCOMPLETE_BODY')

    def test_wsgi_adapter_streams_and_head_has_no_body(self):
        app = WSGI(self.app)
        status = []
        environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/api/v1/files/content', 'QUERY_STRING': 'path=shared.log', 'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': 'localhost', 'wsgi.input': io.BytesIO()}
        result = b''.join(app(environ, lambda s, h: status.append((s, dict(h)))))
        self.assertEqual(result, b'root\n')
        self.assertEqual(status[0][0], '200 OK')
        self.assertEqual(status[0][1]['X-Content-Type-Options'], 'nosniff')
        environ['REQUEST_METHOD'] = 'HEAD'
        self.assertEqual(b''.join(app(environ, lambda s, h: None)), b'')


if __name__ == '__main__':
    unittest.main(verbosity=2)
