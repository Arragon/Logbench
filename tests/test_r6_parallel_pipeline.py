"""R6-039: parallel indexing with ordered commit, and a bounded buffer.

The interesting failures here are all about *order*, not throughput: a checkpoint
that jumps a gap, a duplicated batch committed twice, a traceback torn by a bad
split. So the tests compare output digests across worker counts instead of
trusting a speed number, and they compare the Python reducer against the
JavaScript one the page uses, because two reductions that disagree is a bug that
only shows up on a slow day.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.indexing.layers import build_rules_layer
from server.indexing.node_pool import NodePool
from server.indexing.ordered import OrderedReducer, ReducerOverflow
from server.indexing.parallel import ParallelIndexer
from server.indexing.pipeline import Pipeline
from server.indexing.shards import ShardReader

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
REDUCER = ROOT / 'src' / 'shared' / 'ordered-reducer.js'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


def trace(schedule, *, max_buffer=64):
    reducer = OrderedReducer(max_buffer=max_buffer)
    committed = []
    for seq in schedule:
        committed.extend(batch['seq'] for batch in reducer.accept({'seq': seq})['commit'])
    return committed


class ReducerTests(unittest.TestCase):
    def test_python_and_javascript_agree_on_every_schedule(self):
        schedules = [
            [0, 1, 2, 3],
            [0, 2, 1, 3],
            [2, 0, 1, 3, 5, 4, 6],
            [1, 2, 3, 0],
            [0, 3, 1, 2, 4],
            [5, 4, 3, 2, 1, 0, 6],
            [0, 1, 1, 2, 0, 3],
        ]
        script = (
            "const {createOrderedReducer}=require(process.argv[1]);"
            "const schedules=JSON.parse(require('fs').readFileSync(0,'utf8'));"
            "const out=schedules.map(order=>{const r=createOrderedReducer({maxBuffer:64});"
            "const t=[];for(const seq of order){t.push(...r.accept({seq}).commit.map(b=>b.seq));}return t;});"
            "process.stdout.write(JSON.stringify(out));"
        )
        completed = subprocess.run(['node', '-e', script, str(REDUCER)],
                                   input=json.dumps(schedules), capture_output=True,
                                   text=True, check=True)
        javascript = json.loads(completed.stdout)
        self.assertEqual([trace(schedule) for schedule in schedules], javascript)

    def test_a_gap_holds_the_checkpoint_until_it_is_filled(self):
        reducer = OrderedReducer(max_buffer=8)
        reducer.accept({'seq': 0, 'boundary': {'bytes': 10}})
        for seq in (2, 3, 4):
            reducer.accept({'seq': seq, 'boundary': {'bytes': (seq + 1) * 10}})
        self.assertEqual(reducer.checkpoint(), {'seq': 0, 'boundary': {'bytes': 10}})
        outcome = reducer.accept({'seq': 1, 'boundary': {'bytes': 20}})
        self.assertEqual([item['seq'] for item in outcome['commit']], [1, 2, 3, 4])
        self.assertEqual(reducer.checkpoint()['seq'], 4)

    def test_the_buffer_is_bounded(self):
        reducer = OrderedReducer(max_buffer=3)
        reducer.accept({'seq': 0})
        reducer.accept({'seq': 5})
        reducer.accept({'seq': 6})
        with self.assertRaises(ReducerOverflow) as caught:
            for seq in range(7, 20):
                reducer.accept({'seq': seq})
        self.assertEqual(caught.exception.code, 'reducer_overflow')

    def test_an_old_token_is_refused_even_if_the_sequence_is_right(self):
        reducer = OrderedReducer(max_buffer=4, token=9)
        outcome = reducer.accept({'seq': 0, 'token': 8})
        self.assertEqual(outcome['dropped'], 'old_token')
        self.assertEqual(reducer.next_seq, 0)
        self.assertTrue(reducer.adopt_token(10))
        self.assertFalse(reducer.adopt_token(4))


class ParallelPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.index_root = self.root / 'index'
        self.profile = json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))
        self.pools = []

    def tearDown(self):
        for pool in self.pools:
            pool.close(drain=False)
        self.temp.cleanup()

    def pool(self, workers=1):
        pool = NodePool(ENTRY, workers=workers, request_timeout=60)
        pool.start()
        self.pools.append(pool)
        return pool

    def index(self, source: Path, *, chunk_size=64 * 1024, name='shard', pool=None):
        pipeline = Pipeline(self.app.store, self.pool(1), self.index_root / name,
                            chunk_size=chunk_size)
        return pipeline.run('job-%s' % name, source, profile=self.profile)

    def test_worker_counts_one_two_four_six_eight_agree(self):
        source = GOLDEN / 'edge_cases.log'
        result = self.index(source)
        shard = Path(result['shard'])
        sequential = build_rules_layer(shard, source_path=source, pool=self.pool(1),
                                       profile=self.profile, version='seq')
        digests = {'sequential': sequential['digest']}
        for lanes in (1, 2, 4, 6, 8):
            indexer = ParallelIndexer(self.app.store, self.pool(lanes), self.index_root,
                                      parse_slots=lanes)
            report = indexer.rebuild_rules_parallel(
                shard, source_path=source, pool=indexer.pool, profile=self.profile,
                version='par%d' % lanes, lanes=lanes)
            self.assertEqual(report['rows'], result['stats']['records'], lanes)
            digests['lanes-%d' % lanes] = report['digest']
        self.assertEqual(len(set(digests.values())), 1,
                         'every worker count must produce byte-identical rules output: %s'
                         % json.dumps(digests))
        rows = ShardReader(shard).rule_rows('par8', 0, 2000)
        self.assertEqual([row['ordinal'] for row in rows], list(range(len(rows))),
                         'committed in source order')

    def test_parallel_files_match_sequential_files(self):
        entries = [{'job': 'f-%d' % index, 'path': str(GOLDEN / name)}
                   for index, name in enumerate(sorted(item.name for item in GOLDEN.glob('*.log')))]
        sequential = ParallelIndexer(self.app.store, self.pool(1), self.index_root / 'seq',
                                     parse_slots=1)
        parallel = ParallelIndexer(self.app.store, self.pool(3), self.index_root / 'par',
                                   parse_slots=3)
        first = sequential.index_files(entries, profile=self.profile)
        second = parallel.index_files(entries, profile=self.profile)
        self.assertEqual(first['failed'], {})
        self.assertEqual(second['failed'], {})
        self.assertEqual(first['records'], second['records'])
        self.assertEqual(first['digest'], second['digest'])

    def test_a_failing_file_does_not_cancel_the_others(self):
        entries = [{'job': 'ok-1', 'path': str(GOLDEN / 'edge_cases.log')},
                   {'job': 'gone', 'path': str(self.root / 'missing.log')},
                   {'job': 'ok-2', 'path': str(GOLDEN / 'traceback_chain.log')}]
        indexer = ParallelIndexer(self.app.store, self.pool(2), self.index_root, parse_slots=2)
        report = indexer.index_files(entries, profile=self.profile)
        self.assertEqual(report['ok'], 2)
        self.assertIn('gone', report['failed'])
        self.assertGreater(report['records'], 0)

    def test_a_traceback_spanning_many_blocks_stays_whole(self):
        source = self.root / 'huge-traceback.log'
        lines = ['[2024-01-01 00:00:00.000] [ERROR] [app] [1] [2] [main.py:10] [run] [u] f() boom']
        lines.append('Traceback (most recent call last):')
        for index in range(4000):
            lines.append('  File "/srv/app/module_%d.py", line %d, in handler' % (index, index))
            lines.append('    value = compute(%d)' % index)
        lines.append('RuntimeError: boom')
        lines.append('[2024-01-01 00:00:01.000] [INFO] [app] [1] [2] [main.py:20] [run] [u] f() after')
        source.write_text('\n'.join(lines) + '\n', 'utf-8')
        self.assertGreater(source.stat().st_size, 3 * 64 * 1024, 'the record spans several blocks')

        result = self.index(source, chunk_size=64 * 1024, name='huge')
        self.assertEqual(result['stats']['records'], 2)
        shard = Path(result['shard'])
        record = ShardReader(shard).page(0, 10)[0]
        self.assertTrue(record['has_trace'])
        self.assertEqual(record['lines']['last'] - record['lines']['first'] + 1, len(lines) - 1)

        sequential = build_rules_layer(shard, source_path=source, pool=self.pool(1),
                                       profile=self.profile, version='huge-seq')
        indexer = ParallelIndexer(self.app.store, self.pool(4), self.index_root, parse_slots=4)
        parallel = indexer.rebuild_rules_parallel(
            shard, source_path=source, pool=indexer.pool, profile=self.profile,
            version='huge-par', lanes=4)
        self.assertEqual(parallel['digest'], sequential['digest'])
        self.assertEqual(parallel['rows'], 2)


class LoadProbeTests(unittest.TestCase):
    def test_the_load_tool_reports_bytes_and_queries_together(self):
        # The ticket forbids "CPU pegged therefore good": the probe must report
        # query latency alongside ingest throughput.
        sys.path.insert(0, str(ROOT / 'tools'))
        import importlib
        probe = importlib.import_module('r6_load')
        self.assertTrue(hasattr(probe, 'measure'))
        report = probe.measure(files=[GOLDEN / 'edge_cases.log'], lanes=[1], samples=1)
        self.assertIn('throughput_mb_s', report)
        self.assertIn('query_p95_ms', report)
        self.assertIn('digests', report)


if __name__ == '__main__':
    unittest.main()
