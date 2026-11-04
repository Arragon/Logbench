"""R6-036: the pipeline streams, resumes, and refuses to splice generations."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.indexing.node_pool import NodePool
from server.indexing.pipeline import Pipeline, PipelineError
from server.indexing.shards import ShardReader, shard_path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.pool = NodePool(ENTRY, workers=1, request_timeout=30)
        self.pool.start()
        self.pipeline = Pipeline(self.app.store, self.pool, self.root / 'index', chunk_size=64 * 1024)
        self.profile = json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))

    def tearDown(self):
        self.pool.close(drain=False)
        self.temp.cleanup()

    def test_a_golden_file_is_indexed_with_the_same_record_count(self):
        source = GOLDEN / 'edge_cases.log'
        expected = json.loads((GOLDEN / 'expected.json').read_text('utf-8'))
        result = self.pipeline.run('job-golden', source, profile=self.profile)
        self.assertEqual(result['state'], 'indexed')
        self.assertEqual(result['stats']['records'], len(expected['fixtures']['edge_cases.log']['records']))
        reader = ShardReader(Path(result['shard']))
        self.assertEqual(reader.count(), result['stats']['records'])
        self.assertGreater(reader.checkpoint('parsed')['bytes'], 0)

    def test_resume_after_a_crash_neither_loses_nor_duplicates(self):
        source = GOLDEN / 'traceback_chain.log'
        size = source.stat().st_size
        first = self.pipeline.run('job-resume', source, profile=self.profile, max_bytes=size // 2)
        partial = first['stats']['records']
        self.assertGreater(partial, 0)
        self.assertLess(first['stats']['bytes'], size)

        # A new pipeline instance stands in for "the process died and restarted".
        fresh = Pipeline(self.app.store, self.pool, self.root / 'index', chunk_size=64 * 1024)
        second = fresh.run('job-resume-2', source, profile=self.profile)
        reader = ShardReader(Path(second['shard']))
        ordinals = [row['ordinal'] for row in reader.page(0, 2000)]
        self.assertEqual(len(ordinals), len(set(ordinals)), 'no duplicate ordinals after resume')
        self.assertEqual(ordinals, sorted(ordinals))
        self.assertEqual(second['stats']['restarted_from'], first['stats']['bytes'])

        full = Pipeline(self.app.store, self.pool, self.root / 'index2', chunk_size=64 * 1024)
        complete = full.run('job-full', source, profile=self.profile)
        clean = ShardReader(Path(complete['shard'])).page(0, 2000)
        self.assertEqual(reader.count(), len(clean),
                         'resumed index has the same records as a clean run')
        # Not just the same count: the resumed shard must be byte-identical in
        # content, which is what "no loss, no duplication" actually means.
        self.assertEqual(
            [(row['ordinal'], row['lines']['first'], row['lines']['last'], row['message'])
             for row in reader.page(0, 2000)],
            [(row['ordinal'], row['lines']['first'], row['lines']['last'], row['message'])
             for row in clean])

    def test_a_rewrite_between_runs_starts_a_new_generation_and_retires_the_old_one(self):
        source = self.root / 'data' / 'machine.log'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(''.join('[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() line %d\n' % i
                                  for i in range(200)), 'utf-8')
        first = self.pipeline.run('job-gen', source, profile=self.profile)
        source.write_text(source.read_text('utf-8').replace('line 150', 'LINE 150'), 'utf-8')
        second = self.pipeline.run('job-gen-2', source, profile=self.profile)

        # The bytes changed, so the index must not be resumed: a new generation
        # gets its own shard, and the old one is retired rather than reused.
        self.assertEqual(second['generation'], first['generation'] + 1)
        self.assertNotEqual(second['shard'], first['shard'])
        self.assertEqual(second['file_id'], first['file_id'], 'same path keeps the logical id')
        with self.app.store.transaction() as db:
            states = {row['generation']: row['state'] for row in db.execute(
                'SELECT generation,state FROM generations WHERE file_id=?', (first['file_id'],))}
        self.assertEqual(states[first['generation']], 'superseded')
        self.assertEqual(states[second['generation']], 'indexed')
        messages = [row['message'] for row in ShardReader(Path(second['shard'])).page(0, 2000)]
        self.assertIn('LINE 150', '\n'.join(messages))
        self.assertNotIn('line 150', '\n'.join(messages))

    def test_a_truncated_file_does_not_resume_from_the_stale_checkpoint(self):
        source = self.root / 'data' / 'short.log'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(''.join('[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() row %d\n' % i
                                  for i in range(500)), 'utf-8')
        generation = self.pipeline.ensure_generation(source)
        long_run = self.pipeline.run('job-long', source, file_id=generation.file_id,
                                     profile=self.profile, max_bytes=2048)
        self.assertGreater(long_run['stats']['records'], 0)

        source.write_text('truncated\n', 'utf-8')
        short_run = self.pipeline.run('job-trunc', source, file_id=generation.file_id, profile=self.profile)
        self.assertEqual(short_run['generation'], long_run['generation'] + 1)
        self.assertEqual(short_run['stats']['restarted_from'], 0,
                         'a shorter file restarts instead of resuming past its end')
        self.assertEqual(ShardReader(Path(short_run['shard'])).count(), short_run['stats']['records'])

    def test_a_file_rewritten_during_the_run_is_a_generation_conflict(self):
        source = self.root / 'data' / 'live.log'
        source.parent.mkdir(parents=True, exist_ok=True)
        original = ''.join('[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() row %d\n' % i
                           for i in range(3000))
        source.write_text(original, 'utf-8')
        rewritten = []

        def sabotage(event):
            if rewritten:
                return
            rewritten.append(event)
            source.write_text(original.replace('row 2', 'ROW 2'), 'utf-8')

        with self.assertRaises(PipelineError) as caught:
            self.pipeline.run('job-live', source, profile=self.profile, on_progress=sabotage)
        self.assertEqual(caught.exception.code, 'generation_conflict')

    def test_a_file_truncated_during_the_run_is_a_generation_conflict(self):
        source = self.root / 'data' / 'cut.log'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(''.join('[2020-01-01 00:00:00.000] [INFO] [x] [1] [2] [a.py:1] [m] [u] f() row %d\n' % i
                                  for i in range(3000)), 'utf-8')
        cut = []

        def sabotage(event):
            if cut:
                return
            cut.append(event)
            source.write_text('gone\n', 'utf-8')

        with self.assertRaises(PipelineError) as caught:
            self.pipeline.run('job-cut', source, profile=self.profile, on_progress=sabotage)
        self.assertEqual(caught.exception.code, 'generation_conflict')

    def test_a_broken_rule_profile_keeps_structure_and_reports_partial(self):
        source = GOLDEN / 'edge_cases.log'
        result = self.pipeline.run('job-rules', source, profile={'nodes': 'not-a-list', 'extractors': 'nope'})
        reader = ShardReader(Path(result['shard']))
        self.assertGreater(reader.count(), 0, 'records survive a broken profile')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['reason'], 'rules_failed')

    def test_chunks_are_bounded_by_the_configured_size(self):
        source = GOLDEN / 'edge_cases.log'
        small = Pipeline(self.app.store, self.pool, self.root / 'index3', chunk_size=64 * 1024)
        result = small.run('job-chunk', source, profile=self.profile)
        expected_chunks = -(-source.stat().st_size // (64 * 1024))
        self.assertGreaterEqual(result['stats']['chunks'], expected_chunks)

    def test_repeated_runs_reuse_the_same_shard(self):
        source = GOLDEN / 'edge_cases.log'
        first = self.pipeline.run('job-repeat', source, profile=self.profile)
        second = self.pipeline.run('job-repeat-2', source, profile=self.profile)
        self.assertEqual(first['shard'], second['shard'])
        self.assertEqual(first['generation'], second['generation'])


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.pool = NodePool(ENTRY, workers=1)
        self.pipeline = Pipeline(self.app.store, self.pool, self.root / 'index')

    def tearDown(self):
        self.pool.close(drain=False)
        self.temp.cleanup()

    def test_identity_is_stable_for_unchanged_bytes_and_bumps_on_rewrite(self):
        source = self.root / 'data' / 'x.log'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('hello\n', 'utf-8')
        first = self.pipeline.ensure_generation(source)
        second = self.pipeline.ensure_generation(source)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(first.content_sig, second.content_sig)

        source.write_text('hello2\n', 'utf-8')
        third = self.pipeline.ensure_generation(source)
        self.assertEqual(third.generation, first.generation + 1)
        self.assertNotEqual(third.content_sig, first.content_sig)
        self.assertEqual(third.file_id, first.file_id, 'same path keeps the logical id')


if __name__ == '__main__':
    unittest.main()
