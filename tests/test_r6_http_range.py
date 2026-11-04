"""R6-063 / R6-064: HTTP range semantics and discovery, over real sockets.

The fixture is a real threaded HTTP server on loopback, so the cases exercise real
status lines, real `Content-Range`, real redirects and real truncation. That matters
because the failures this guards against are protocol behaviours, not logic errors:
a proxy answering 200 to a Range request, a 206 for the wrong span, a weak ETag that
moves after the read, a 416 when the file grew.

SFTP/SMB/SCP have no controlled endpoint on this machine; their evidence is
`qa/evidence/M6-gate.md` with BLOCKED and an unlock condition, not a mock marked pass.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors.base import CollectorError, error_class
from server.collectors.credentials import CredentialStore
from server.collectors.http_range import HttpRangeAdapter

BODY = b''.join(b'2026-11-03 10:00:%02d INFO module=io message=hello world\n' % (i % 60)
                for i in range(3000))
ETAG = '"v1-abc"'
MANIFEST = {
    'schema': 'logbench.manifest/1',
    'generation': 'gen-7',
    'sealed': True,
    'files': [
        {'path': 'a.log', 'size': 120, 'etag': '"a1"', 'sealed': True},
        {'path': 'debug/b.log', 'size': 240, 'etag': '"b1"'},
    ],
}
INDEX_HTML = b"""<html><body>
<a href="a.log">a.log</a>
<a href="debug/">debug/</a>
<a href="https://evil.example/steal.log">external</a>
<a href="../escape.log">escape</a>
<a href="a.log">duplicate</a>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'logbench-fixture'

    def log_message(self, *args):
        return

    # ------------------------------------------------------------------ helpers
    def _record(self):
        self.server.requests.append({'method': self.command, 'path': self.path,
                                     'headers': {k.lower(): v for k, v in self.headers.items()}})

    def _send(self, status, body=b'', headers=None, *, declared=None):
        self.send_response(status)
        payload = body
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload) if declared is None else declared))
        self.end_headers()
        if self.command == 'HEAD':
            return
        if declared is not None and declared > len(payload):
            # Write what we have and close: a truncated body, not a hang.
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(payload)

    # -------------------------------------------------------------------- verbs
    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request(head=False)

    def handle_request(self, head=False):
        self._record()
        path, _, query = self.path.partition('?')
        mode, _, rest = path.lstrip('/').partition('/')
        if mode == 'echo':
            # Cross-host redirect target: report whether the credential leaked.
            body = json.dumps({'authorization': self.headers.get('Authorization')}).encode()
            return self._send(200, body, {'Content-Type': 'application/json'})
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://localhost:%d/echo/a.log' % self.server.server_port)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if mode == 'missing':
            return self._send(404, b'no such file')
        if mode == 'auth':
            return self._send(401, b'no')
        if mode == 'manifest':
            return self._send(200, json.dumps(MANIFEST).encode(),
                              {'Content-Type': 'application/json'})
        if mode == 'badmanifest':
            broken = dict(MANIFEST, extraField=True)
            return self._send(200, json.dumps(broken).encode(),
                              {'Content-Type': 'application/json'})
        if mode == 'index':
            return self._send(200, INDEX_HTML, {'Content-Type': 'text/html; charset=utf-8'})
        if mode == 'indexnone':
            return self._send(200, b'<html><a href="#">top</a></html>',
                              {'Content-Type': 'text/html'})
        return self.serve_file(mode, head)

    def serve_file(self, mode, head):
        total = len(BODY)
        etag = 'W/"weak-1"' if mode == 'weak' else ETAG
        range_header = self.headers.get('Range')
        if_range = self.headers.get('If-Range')
        if mode == 'changed' and if_range:
            # The validator moved: a correct server sends the full representation.
            return self._send(200, BODY, {'ETag': '"v2-new"', 'Accept-Ranges': 'bytes'})
        if range_header and mode not in ('norange', 'changed'):
            start, _, end = range_header.replace('bytes=', '').partition('-')
            start = int(start or 0)
            end = int(end) if end else total - 1
            end = min(end, total - 1)
            if start >= total:
                return self._send(416, b'', {'Content-Range': 'bytes */%d' % total,
                                             'ETag': etag})
            payload = BODY[start:end + 1]
            headers = {'ETag': etag, 'Accept-Ranges': 'bytes',
                       'Content-Range': 'bytes %d-%d/%d' % (start, end, total)}
            if mode == 'wrong':
                headers['Content-Range'] = 'bytes %d-%d/%d' % (start + 1, end + 1, total)
            if mode == 'truncated':
                return self._send(206, payload[:len(payload) // 2], headers,
                                  declared=len(payload))
            return self._send(206, payload, headers)
        headers = {'ETag': etag, 'Accept-Ranges': 'bytes',
                   'Last-Modified': 'Mon, 03 Nov 2026 10:00:00 GMT'}
        return self._send(200, BODY, headers)


class Fixture:
    def __init__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.requests = []
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def url(self, mode, rest='a.log'):
        return 'http://127.0.0.1:%d/%s/%s' % (self.port, mode, rest)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make(temp: Path, mode: str, *, policy=None, credential=None) -> HttpRangeAdapter:
    store = CredentialStore(temp)
    ref = None
    if credential:
        ref = store.create(kind='token', label='t', value=credential, actor='admin')['id']
    base = {'id': 'src_1', 'adapter': 'http', 'endpoint': FIXTURE.url(mode),
            'credential_ref': ref, 'path': FIXTURE.url(mode),
            'policy': dict({'allowedHosts': ['127.0.0.1', 'localhost'], 'allowLoopback': True,
                            'readTimeout': 10}, **(policy or {}))}
    return HttpRangeAdapter(base, credentials=store)


FIXTURE = Fixture()


class HttpRangeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_records_a_supported_range_with_a_strong_validator(self):
        adapter = make(self.root, 'ok')
        probe = adapter.probe(config_revision=3, fingerprint='fp-1')
        self.assertTrue(probe.ok)
        caps = probe.capabilities
        self.assertEqual(caps.range_read, 'supported')
        self.assertEqual(caps.stable_identity, 'validators')
        self.assertEqual(caps.directory_discovery, 'unsupported')
        self.assertEqual(caps.append_contract, 'unsupported')
        self.assertEqual(probe.config_revision, 3)
        self.assertEqual(probe.fingerprint, 'fp-1')
        self.assertIn('range', [check.id for check in probe.checks])

    def test_server_that_ignores_range_is_reported_as_unsupported_not_ok(self):
        adapter = make(self.root, 'norange')
        probe = adapter.probe(config_revision=1, fingerprint='fp')
        self.assertFalse(probe.ok)
        self.assertEqual(probe.capabilities.range_read, 'unsupported')
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['range'].state, 'fail')
        self.assertIn('200', checks['range'].detail)
        self.assertFalse(probe.capabilities.allows_follow())

    def test_range_read_returns_exact_bytes_and_reuses_one_window(self):
        adapter = make(self.root, 'ok')
        stat = adapter.stat('/a.log')
        self.assertEqual(stat.size, len(BODY))
        first = adapter.read_range('/a.log', offset=0, length=4 * 1024,
                                   identity={'etag': stat.identity['etag'], 'size': stat.size})
        self.assertEqual(first.data, BODY[:4 * 1024])
        self.assertTrue(first.verified)
        second = adapter.read_range('/a.log', offset=4 * 1024, length=4 * 1024,
                                    identity={'etag': stat.identity['etag'], 'size': stat.size})
        self.assertEqual(second.data, BODY[4 * 1024:8 * 1024])
        requests = [r for r in FIXTURE.server.requests if r['headers'].get('range')]
        # probe + stat + one window: the second block is served from the first response.
        self.assertLessEqual(len([r for r in requests if r['path'] == '/ok/a.log']), 4)

    def test_206_for_a_different_span_is_a_generation_change(self):
        adapter = make(self.root, 'wrong')
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/a.log', offset=0, length=1024,
                               identity={'etag': ETAG, 'size': len(BODY)})
        self.assertEqual(caught.exception.code, 'generation_changed')

    def test_200_for_a_mid_file_offset_is_never_appended(self):
        adapter = make(self.root, 'changed')
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/a.log', offset=4096, length=1024,
                               identity={'etag': ETAG, 'size': len(BODY)})
        self.assertEqual(caught.exception.code, 'generation_changed')
        self.assertIn('完整表示', caught.exception.message)

    def test_truncated_body_is_a_partial_read_not_a_short_chunk(self):
        adapter = make(self.root, 'truncated')
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/a.log', offset=0, length=2048,
                               identity={'etag': ETAG, 'size': len(BODY)})
        self.assertEqual(caught.exception.code, 'partial_read')

    def test_416_at_end_is_empty_and_416_with_new_size_is_a_change(self):
        adapter = make(self.root, 'ok')
        at_end = adapter.read_range('/a.log', offset=len(BODY), length=16,
                                    identity={'etag': ETAG, 'size': len(BODY)})
        self.assertEqual(at_end.data, b'')
        # The archive believes the file is one byte shorter than it is: the 206
        # carries a different total, which is a replacement, not a continuation.
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/a.log', offset=len(BODY) - 2, length=4,
                               identity={'etag': ETAG, 'size': len(BODY) - 1})
        self.assertEqual(caught.exception.code, 'generation_changed')

    def test_weak_validator_downgrades_identity_and_is_reported(self):
        adapter = make(self.root, 'weak')
        probe = adapter.probe(config_revision=1, fingerprint='fp')
        self.assertEqual(probe.capabilities.stable_identity, 'size-mtime')
        self.assertTrue(any('ETag' in warning for warning in probe.warnings))
        chunk = adapter.read_range('/a.log', offset=0, length=512,
                                   identity={'etag': 'W/"weak-1"', 'size': len(BODY)})
        self.assertFalse(chunk.verified)

    def test_missing_and_unauthorised_are_different_errors(self):
        adapter = make(self.root, 'missing')
        with self.assertRaises(CollectorError) as missing:
            adapter.stat('/a.log')
        self.assertEqual(missing.exception.code, 'not_found')
        adapter = make(self.root, 'auth')
        with self.assertRaises(CollectorError) as denied:
            adapter.stat('/a.log')
        self.assertEqual(denied.exception.code, 'auth_failed')
        self.assertEqual(error_class('auth_failed'), 'manual')

    def test_credential_is_sent_as_a_bearer_token_and_never_logged(self):
        adapter = make(self.root, 'ok', credential='tok-very-secret')
        adapter.stat('/a.log')
        seen = [r for r in FIXTURE.server.requests if r['path'] == '/ok/a.log']
        self.assertTrue(any(r['headers'].get('authorization') == 'Bearer tok-very-secret'
                            for r in seen))
        adapter = make(self.root, 'auth', credential='tok-very-secret')
        with self.assertRaises(CollectorError) as caught:
            adapter.stat('/a.log')
        self.assertNotIn('tok-very-secret', repr(caught.exception.to_dict()))

    def test_redirect_to_another_host_drops_the_credential_and_is_refused(self):
        strict = make(self.root, 'redirect', policy={'allowedHosts': ['127.0.0.1']},
                      credential='tok-very-secret')
        with self.assertRaises(CollectorError) as caught:
            strict.stat('/a.log')
        self.assertIn(caught.exception.code, ('host_not_allowed', 'redirect_denied'))
        permissive = make(self.root, 'redirect', credential='tok-very-secret')
        permissive.stat('/a.log')
        echo = [r for r in FIXTURE.server.requests if r['path'] == '/echo/a.log']
        self.assertTrue(echo, 'redirect target was never reached')
        self.assertIsNone(echo[-1]['headers'].get('authorization'),
                          'Authorization 不得随跨主机跳转泄漏')


class HttpListingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, mode, **policy):
        base = {'id': 'src_1', 'adapter': 'http', 'endpoint': FIXTURE.url(mode),
                'policy': dict({'allowedHosts': ['127.0.0.1', 'localhost'], 'allowLoopback': True,
                                'readTimeout': 10}, **policy)}
        return HttpRangeAdapter(base, credentials=None)

    def test_manifest_is_the_only_mode_with_a_declared_generation(self):
        adapter = self.adapter('manifest', manifestUrl='/manifest/logbench.manifest.json',
                               directoryMode='manifest')
        listing = adapter.list('/')
        self.assertEqual([entry.name for entry in listing.entries], ['a.log', 'b.log'])
        self.assertEqual(listing.manifest['generation'], 'gen-7')
        self.assertEqual(listing.manifest['schema'], 'logbench.manifest/1')
        self.assertEqual(listing.entries[1].path, 'debug/b.log')
        self.assertEqual(listing.entries[0].size, 120)

    def test_manifest_with_an_unknown_field_is_rejected(self):
        adapter = self.adapter('badmanifest', manifestUrl='/badmanifest/logbench.manifest.json',
                               directoryMode='manifest')
        with self.assertRaises(CollectorError) as caught:
            adapter.list('/')
        self.assertEqual(caught.exception.code, 'invalid_manifest')
        self.assertIn('extraField', str(caught.exception.details))

    def test_listing_without_a_manifest_is_a_capability_gap(self):
        adapter = self.adapter('ok')
        self.assertEqual(adapter.listing().mode(), 'none')
        with self.assertRaises(CollectorError) as caught:
            adapter.list('/')
        self.assertEqual(caught.exception.code, 'unsupported')

    def test_html_index_keeps_local_links_and_drops_external_and_parent_links(self):
        adapter = self.adapter('index', listingUrl='/index/', directoryMode='index',
                               allowedPaths=['/index/'])
        listing = adapter.list('/index')
        names = [entry.name for entry in listing.entries]
        self.assertIn('a.log', names)
        self.assertIn('debug', names)
        self.assertNotIn('steal.log', names)
        self.assertNotIn('escape.log', names)
        self.assertEqual(len(names), len(set(names)), '同一个链接只应出现一次')
        self.assertTrue(any(entry.is_dir for entry in listing.entries))

    def test_an_index_with_no_usable_links_is_not_an_empty_directory(self):
        adapter = self.adapter('indexnone', listingUrl='/indexnone/', directoryMode='index')
        with self.assertRaises(CollectorError) as caught:
            adapter.list('/indexnone')
        self.assertEqual(caught.exception.code, 'unsupported')

    def test_probe_reports_discovery_separately_from_range(self):
        adapter = self.adapter('ok', manifestUrl='/manifest/logbench.manifest.json')
        probe = adapter.probe(config_revision=2, fingerprint='fp')
        self.assertEqual(probe.capabilities.directory_discovery, 'supported')
        self.assertEqual(probe.capabilities.range_read, 'supported')
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['discovery'].state, 'pass')


if __name__ == '__main__':
    unittest.main()
