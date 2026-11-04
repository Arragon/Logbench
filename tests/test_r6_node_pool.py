"""R6-035: the Node pool is exercised with real child processes.

Nothing is mocked: `subprocess` is the implementation, so a kill test really
kills, a timeout test really waits, and a protocol mismatch test really starts a
wrong worker and disposes of it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.indexing.node_pool import MAX_PAYLOAD, PROTOCOL, NodePool, PoolError

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


class NodePoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = NodePool(ENTRY, workers=2, handshake_timeout=15, request_timeout=20)

    def tearDown(self):
        self.pool.close(drain=False)

    def test_handshake_reports_protocol_and_reuses_workers(self):
        info = self.pool.start()
        self.assertEqual(info['protocol'], PROTOCOL)
        self.assertEqual(len(self.pool.workers), 2)
        first = self.pool.snapshot()
        for _ in range(3):
            self.pool.parse('job-a', 1, b'[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() hello\n')
        second = self.pool.snapshot()
        self.assertEqual(second['stats']['spawned'], first['stats']['spawned'],
                         'requests must reuse the pool, not spawn per call')
        self.assertGreaterEqual(second['stats']['requests'], 3)

    def test_framing_matches_the_shared_framer(self):
        self.pool.start()
        raw = (GOLDEN / 'edge_cases.log').read_bytes()
        reply = self.pool.parse('job-edge', 1, raw, eof=True,
                                profile=None)
        self.assertEqual(reply['type'], 'batch')
        self.assertTrue(reply['records'])
        # The browser framer numbers physical lines; the pool must agree.
        self.assertEqual(reply['records'][0]['lines']['first'], 1)
        self.assertEqual(reply['records'][0]['record_id'], 'job-edge:0')
        self.assertIsInstance(reply['records'][0]['ts_ns'], (str, type(None)))

    def test_a_second_chunk_continues_the_same_framer(self):
        self.pool.start()
        text = ''.join('[2020-01-01 00:00:0%d.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() line %d\n' % (i % 10, i)
                       for i in range(20))
        raw = text.encode()
        split = raw.index(b'\n', len(raw) // 2) + 1
        first = self.pool.parse('job-stream', 1, raw[:split])
        second = self.pool.parse('job-stream', 1, raw[split:], eof=True, offset=split)
        ordinals = [record['ordinal'] for record in first['records'] + second['records']]
        self.assertEqual(ordinals, list(range(20)), 'ordinals continue across chunks')
        self.assertEqual(len({record['record_id'] for record in first['records'] + second['records']}), 20)

    def test_oversized_payload_is_refused_before_it_is_sent(self):
        self.pool.start()
        with self.assertRaises(PoolError) as caught:
            self.pool.parse('job-big', 1, b'x' * (MAX_PAYLOAD + 1))
        self.assertEqual(caught.exception.code, 'payload_too_large')

    def test_a_hung_worker_is_killed_and_the_pool_recovers(self):
        self.pool.start()
        target = self.pool.workers[0]
        with self.assertRaises(PoolError) as caught:
            self.pool.sleep(5000, timeout=0.4)
        self.assertEqual(caught.exception.code, 'worker_timeout')
        self.assertFalse(target.alive(), 'the hung worker must actually be dead')

        # The pool replaces the worker and keeps serving.
        reply = self.pool.parse('job-after', 2, b'[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() ok\n')
        self.assertEqual(reply['type'], 'batch')
        self.assertGreaterEqual(self.pool.snapshot()['stats']['restarted'], 1)

    def test_a_killed_child_is_replaced_on_the_next_request(self):
        self.pool.start()
        victim = self.pool.workers[1]
        victim.process.kill()
        victim.process.wait(timeout=5)
        self.assertFalse(victim.alive())
        reply = self.pool.parse('job-kill', 3, b'[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() alive\n')
        self.assertEqual(reply['type'], 'batch')
        self.assertTrue(self.pool.workers[1].alive())

    def test_a_wrong_protocol_worker_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / 'fake.cjs'
            fake.write_text("process.stdin.on('data', () => {"
                            "process.stdout.write(JSON.stringify({type:'ready',protocol:'other/9'})+'\\n');});",
                            'utf-8')
            pool = NodePool(fake, workers=1)
            try:
                with self.assertRaises(PoolError) as caught:
                    pool.start()
                self.assertEqual(caught.exception.code, 'protocol_mismatch')
                self.assertEqual(pool.workers, [], 'a mismatched worker must not stay in the pool')
            finally:
                pool.close(drain=False)

    def test_close_drains_with_an_acknowledgement(self):
        self.pool.start()
        result = self.pool.close(drain=True)
        self.assertEqual(len(result['closed']), 2)
        self.assertTrue(all(item['ack'] for item in result['closed']))
        self.assertEqual(self.pool.workers, [])

    def test_missing_node_is_reported_not_crashed(self):
        pool = NodePool(ENTRY, workers=1, node_bin='/nonexistent/node')
        self.assertFalse(pool.available)
        with self.assertRaises(PoolError) as caught:
            pool.start()
        self.assertEqual(caught.exception.code, 'node_missing')

    def test_concurrent_callers_on_one_worker_get_their_own_reply(self):
        """The regression this prevents: one stream, two callers, crossed replies.

        Before serialisation, two threads parsing different jobs on the same worker
        could each receive the other's `batch` — a file would be indexed with zero
        records while another got duplicated. Distinct payload sizes make the
        mismatch obvious.
        """
        self.pool.start()
        line = b'[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() t%d\n'
        jobs = {'lane-%d' % index: line * (index + 1) * 20 for index in range(6)}
        results = {}

        def run(job, data):
            results[job] = self.pool.parse(job, 1, data, eof=True, offset=len(data))

        threads = [threading.Thread(target=run, args=(job, data)) for job, data in jobs.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(results), len(jobs))
        for job, reply in results.items():
            self.assertEqual(reply['job'], job, 'a reply must carry the job that asked for it')
            self.assertEqual(reply['id'], '%s:%d' % (job, len(jobs[job])), job)
            self.assertEqual(reply['consumed'], len(jobs[job]))
            self.assertTrue(reply['records'], job)
            self.assertEqual({record['file_id'] for record in reply['records']}, {job})


if __name__ == '__main__':
    unittest.main()
