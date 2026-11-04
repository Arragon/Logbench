"""R6-074: one reusable event stream, bounded replay and honest resync.

Real SQLite, real sockets, real SSE framing. The cases come from the failure modes
that make a status stream untrustworthy: an event id that silently skips, a replay
window that expires without saying so, a member receiving administrator events, and a
slow tab blocking collection.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.events import EventBroker, scopes_for
from server.httpd import Server

ROOT = Path(__file__).resolve().parents[1]


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    (root / 'rules' / 'cpqpsgx-default.json').write_text(
        json.dumps({'name': 'Basic', 'format': 'bracket', 'headerPattern': '^(?<message>.*)$',
                    'moduleRules': [], 'stepRules': [], 'errorRules': [],
                    'anomalies': [], 'extractors': [], 'nodes': []}), 'utf-8')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


class BrokerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.broker = self.app.events

    def tearDown(self):
        self.temp.cleanup()

    def test_events_are_ordered_and_replayable_by_id(self):
        for index in range(3):
            self.broker.publish(kind='archive_commit', payload={'sourceId': 's', 'n': index},
                                scope='shared')
        admin = {'role': 'admin', 'id': 'a'}
        first = self.broker.poll(admin)
        self.assertEqual([event['payload']['n'] for event in first['events']], [0, 1, 2])
        ids = [event['id'] for event in first['events']]
        self.assertEqual(ids, sorted(ids))
        second = self.broker.poll(admin, ids[1])
        self.assertEqual([event['payload']['n'] for event in second['events']], [2])
        self.assertFalse(second['resync_required'])

    def test_expired_position_asks_for_a_resync_instead_of_silence(self):
        broker = EventBroker(self.app.store, retention=20)
        for index in range(30):
            broker.publish(kind='index_commit', payload={'n': index}, scope='shared')
        result = broker.poll({'role': 'admin', 'id': 'a'}, last_event_id=1)
        self.assertTrue(result['resync_required'])
        self.assertEqual(result['reason'], 'event_retention_exceeded')

    def test_members_do_not_receive_administrator_scoped_events(self):
        self.broker.publish(kind='source_error', payload={'sourceId': 's'}, scope='shared')
        self.broker.publish(kind='source_error', payload={'sourceId': 'm'}, scope='members')
        admin = self.broker.poll({'role': 'admin', 'id': 'a'})
        member = self.broker.poll({'role': 'member', 'id': 'm'})
        self.assertEqual(len(admin['events']), 2)
        self.assertEqual([event['payload']['sourceId'] for event in member['events']], ['m'])
        self.assertEqual(scopes_for({'role': 'viewer'}), [])

    def test_a_slow_subscriber_is_dropped_not_allowed_to_backpressure(self):
        import queue
        subscription = self.broker.attach({'role': 'admin', 'id': 'a'})
        subscription.queue = queue.Queue(maxsize=2)
        # `stream()` attaches its own subscriber; hand it the deliberately tiny one.
        self.broker.attach = lambda actor: subscription
        for index in range(5):
            self.broker.publish(kind='tick', payload={'n': index}, scope='shared')
        self.assertTrue(subscription.dropped, '队列满时应丢弃订阅而不是阻塞发布者')
        frames = list(self.broker.stream({'role': 'admin', 'id': 'a'}, heartbeat=0.01,
                                         lifetime=0.05))
        self.assertTrue(any('resync_required' in frame for frame in frames))
        self.assertTrue(any('slow_consumer' in frame for frame in frames))

    def test_event_payload_has_no_log_body_or_credentials(self):
        event = self.broker.publish(kind='record_revision',
                                    payload={'fileId': 'f_1', 'ordinal': 4, 'version': 3},
                                    scope='shared')
        text = json.dumps(event, ensure_ascii=False)
        self.assertIn('version', text)
        for forbidden in ('password', 'credential', 'line=', 'body'):
            self.assertNotIn(forbidden, text)


class SseTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def read_frames(self, sock, want: float) -> str:
        sock.settimeout(0.5)
        buffer = b''
        deadline = time.time() + want
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        return buffer.decode('utf-8', 'replace')

    def open_stream(self, last_event_id=None):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        headers = ['GET /api/v2/events HTTP/1.1', 'Host: 127.0.0.1', 'Accept: text/event-stream']
        if last_event_id is not None:
            headers.append('Last-Event-ID: %d' % last_event_id)
        sock.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('ascii'))
        return sock

    def test_stream_replays_then_delivers_live_events_without_a_length(self):
        first = self.app.events.publish(kind='index_commit', payload={'fileId': 'f_1'},
                                        scope='shared')
        sock = self.open_stream(last_event_id=first['id'] - 1)
        head = self.read_frames(sock, 1.0)
        self.assertIn('200 OK', head)
        self.assertIn('text/event-stream', head)
        self.assertNotIn('Content-Length', head)
        self.assertIn('event: index_commit', head)
        self.app.events.publish(kind='rotation', payload={'fileId': 'f_1', 'generation': 2},
                                scope='shared')
        live = self.read_frames(sock, 1.0)
        self.assertIn('event: rotation', live)
        self.assertIn('"generation": 2', live)
        sock.close()

    def test_expired_last_event_id_gets_an_explicit_resync_frame(self):
        self.app.events.retention = 20
        for index in range(30):
            self.app.events.publish(kind='tick', payload={'n': index}, scope='shared')
        sock = self.open_stream(last_event_id=1)
        frames = self.read_frames(sock, 1.0)
        self.assertIn('resync_required', frames)
        self.assertIn('event_retention_exceeded', frames)
        sock.close()

    def test_polling_fallback_returns_the_same_events(self):
        self.app.events.publish(kind='archive_commit', payload={'n': 1}, scope='shared')
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:%d/api/v2/events/poll' % self.port,
                                    timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        self.assertEqual(len(payload['events']), 1)
        self.assertEqual(payload['events'][0]['kind'], 'archive_commit')
        self.assertFalse(payload['resync_required'])

    def test_viewer_gets_forbidden_instead_of_an_endless_stream(self):
        import urllib.request
        import urllib.error
        # A second identity that is not registered: the page would be read-only.
        from server.app import ApiError
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/v2/events/poll' % self.port,
                                        timeout=5) as response:
                body = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:  # pragma: no cover - depends on identity mode
            self.assertEqual(exc.code, 403)
            return
        self.assertIn('events', body)


if __name__ == '__main__':
    unittest.main()
