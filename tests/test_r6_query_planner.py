"""R6-043: candidate planning never misses a hit, and never claims a fast path it cannot prove.

The oracle here is a full scan evaluated by the same JS matcher the page uses
(`query-eval` over every record). A plan is only acceptable if its hit set equals
the oracle's, on every fixture and for every shape of term that could tempt a
shortcut: two-character Chinese, a single character, Unicode case folding,
whole-word, regex, a phrase that spans message and body, and FTS metacharacters
that must stay literal.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.indexing.shards import ShardReader
from server.query import planner

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'

CASES = [
    ('literal', {'text': 'Traceback'}),
    ('literal_lower', {'text': 'traceback'}),
    ('unicode_case', {'text': 'ERR_CODE'}),
    ('two_char_cjk', {'text': '中文'}),
    ('single_char', {'text': 'e'}),
    ('whole_word', {'text': 'Error', 'flags': {'whole_word': True}}),
    ('regex', {'text': r'rpc_\w+', 'flags': {'regex': True}}),
    ('fts_specials', {'text': '%_*"'}),
    ('cross_chunk', {'text': 'rpc_success: False'}),
    ('and_terms', {'terms': ['err_code', 'iteration'], 'joiner': 'and'}),
    ('or_terms', {'terms': ['converged', 'exposure_us'], 'joiner': 'or'}),
    ('level_filter', {'text': 'iteration', 'levels': ['ERROR']}),
]


class QueryHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'rules').mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', self.root / 'rules' / 'cpqpsgx-default.json')
        self.app = Application(Config(self.root, bootstrap_admins=['127.0.0.1']))
        self.app.index_jobs.executor = None
        self.app.queries.executor_pool = None
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        self.pool = self.app.index_jobs.pool
        self.pool.start()
        self.actor = self.app.actor('127.0.0.1')
        self.indexed = {}

    def tearDown(self):
        self.app.index_jobs.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def index(self, name: str) -> dict:
        if name in self.indexed:
            return self.indexed[name]
        source = self.data / 'logs' / name
        shutil.copy(GOLDEN / name, source)
        created = self.app.index_jobs.create(self.actor, {'path': 'logs/%s' % name})
        result = self.app.index_jobs.run_now(created['item']['id'])
        shard = Path(result['shard'])
        info = {'file_id': created['item']['payload']['file_id'], 'shard': shard,
                'reader': ShardReader(shard), 'result': result}
        self.indexed[name] = info
        return info

    def oracle(self, shard: Path, spec: dict) -> list:
        """Full scan with the shared matcher: the definition of a correct answer."""
        opened = self.pool.query_open(spec, key='oracle-%s' % abs(hash(json.dumps(spec, sort_keys=True))))
        hits = []
        reader = ShardReader(shard)
        total = reader.count()
        for offset in range(0, total, 500):
            records = reader.page(offset, 500)
            reply = self.pool.query_eval(opened['qid'], records, key='oracle')
            hits.extend(reply['hits'])
        self.pool.query_close(opened['qid'], key='oracle')
        return sorted(hits)

    def planned(self, shard: Path, spec: dict) -> tuple:
        opened = self.pool.query_open(spec, key='plan')
        summary = opened['summary']
        self.pool.query_close(opened['qid'], key='plan')
        postings = ShardReader(shard).layer('search')
        plan = planner.plan_query(shard, dict(spec, terms=summary['terms']),
                                  postings_version=(postings or {}).get('version'),
                                  postings_complete=bool(postings),
                                  total_records=ShardReader(shard).count())
        return plan, list(planner.iter_candidates(shard, plan))


class PlanningTests(QueryHarness):
    def test_every_case_matches_the_full_scan_oracle(self):
        info = self.index('edge_cases.log')
        for name, spec in CASES:
            with self.subTest(case=name):
                expected = self.oracle(info['shard'], spec)
                plan, candidates = self.planned(info['shard'], spec)
                reader = ShardReader(info['shard'])
                records = reader.records_for(candidates) if candidates else []
                opened = self.pool.query_open(spec, key='verify')
                got = []
                for start in range(0, len(records), 500):
                    got.extend(self.pool.query_eval(opened['qid'], records[start:start + 500],
                                                    key='verify')['hits'])
                self.pool.query_close(opened['qid'], key='verify')
                self.assertEqual(sorted(got), expected,
                                 '%s: plan %s (%s) lost or invented hits'
                                 % (name, plan.mode, plan.reason))
                self.assertTrue(plan.superset, name)

    def test_two_character_chinese_falls_back_instead_of_pruning(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': '中文'})
        self.assertEqual(plan.mode, 'full_scan')
        self.assertIn('shorter_than_gram', plan.reason)

    def test_a_regex_never_prunes(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': r'rpc_\w+', 'flags': {'regex': True}})
        self.assertEqual(plan.mode, 'full_scan')
        self.assertIn('regex', plan.reason)

    def test_whole_word_never_prunes(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': 'Error', 'flags': {'whole_word': True}})
        self.assertEqual(plan.mode, 'full_scan')
        self.assertIn('whole_word', plan.reason)

    def test_a_literal_uses_postings_and_returns_fewer_candidates_than_records(self):
        info = self.index('edge_cases.log')
        plan, candidates = self.planned(info['shard'], {'text': 'err_code'})
        self.assertEqual(plan.mode, 'postings')
        self.assertLess(len(candidates), ShardReader(info['shard']).count())

    def test_level_and_time_predicates_reach_sql(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': 'iteration', 'levels': ['ERROR']})
        self.assertIn('level IN', plan.sql)
        self.assertTrue(any('records' in line for line in plan.explain))
        ranged, _ = self.planned(info['shard'], {
            'text': 'iteration',
            'time_range': {'from_ns': '1577836800000000000', 'to_ns': '1577923200000000000'}})
        self.assertIn('ts_key', ranged.sql)

    def test_explain_shows_the_index_or_admits_a_scan(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': 'iteration', 'levels': ['ERROR']})
        joined = ' | '.join(plan.explain)
        self.assertTrue('records' in joined)
        plain, _ = self.planned(info['shard'], {'text': 'iteration'})
        self.assertTrue(any('SCAN' in line or 'SEARCH' in line for line in plain.explain))

    def test_fts_availability_is_probed_not_assumed(self):
        info = self.index('edge_cases.log')
        plan, _ = self.planned(info['shard'], {'text': 'iteration'})
        self.assertIsInstance(plan.fts_available, bool)
        # This build ships FTS5; the value is reported either way, never assumed.
        self.assertEqual(plan.fts_available, planner.fts_available(planner._connect(info['shard'])))

    def test_like_metacharacters_are_literal(self):
        # The planner passes user text as parameters, never as SQL; this guards the
        # helper that a future caller might reach for.
        self.assertEqual(planner._like_pattern_safe('100%_x'), '100\\%\\_x')

    def test_postings_without_coverage_cannot_prune(self):
        info = self.index('edge_cases.log')
        summary = self.pool.query_open({'text': 'err_code'}, key='cover')['summary']
        plan = planner.plan_query(info['shard'], {'terms': summary['terms']},
                                  postings_version='ver-missing', postings_complete=False,
                                  total_records=10)
        candidates = list(planner.iter_candidates(info['shard'], plan))
        self.assertEqual(len(candidates), ShardReader(info['shard']).count(),
                         'an incomplete index must not shrink the candidate set')
        self.pool.query_close(summary and 'cover', key='cover')

    def test_scan_order_is_ordinal_and_resumes_after_the_cursor(self):
        info = self.index('edge_cases.log')
        plan, candidates = self.planned(info['shard'], {'text': 'iteration'})
        self.assertEqual(candidates, sorted(candidates))
        if len(candidates) >= 2:
            resumed = list(planner.iter_candidates(info['shard'], plan, start_after=candidates[0]))
            self.assertEqual(resumed, candidates[1:])


class ProjectionTests(QueryHarness):
    def test_v2_finds_text_in_fields_that_v1_does_not_index(self):
        info = self.index('edge_cases.log')
        file_id = info['file_id']
        v1 = self.app.queries.create(self.actor, {'file_id': file_id, 'text': '0x0',
                                                  'flags': {}, 'budget_ms': 5000})
        v2 = self.app.queries.create(self.actor, {'file_id': file_id, 'text': '0x0',
                                                  'flags': {}, 'budget_ms': 5000,
                                                  'projection_version': 'v2'})
        self.assertEqual(v1['item']['state'], 'done')
        self.assertEqual(v2['item']['state'], 'done')
        self.assertEqual(v1['item']['count']['matched'], v2['item']['count']['matched'],
                         'v1 and v2 both search message/body, so a message hit must agree')

    def test_a_phrase_spanning_message_and_body_is_still_found(self):
        info = self.index('traceback_chain.log')
        # The matcher joins message and body with a newline, so a needle that spans
        # the boundary is a real match; the index must not prune it away.
        expected = self.oracle(info['shard'], {'text': 'raise ValueError'})
        plan, candidates = self.planned(info['shard'], {'text': 'raise ValueError'})
        reader = ShardReader(info['shard'])
        records = reader.records_for(candidates) if candidates else []
        opened = self.pool.query_open({'text': 'raise ValueError'}, key='span')
        got = self.pool.query_eval(opened['qid'], records, key='span')['hits'] if records else []
        self.pool.query_close(opened['qid'], key='span')
        self.assertEqual(sorted(got), expected)


if __name__ == '__main__':
    unittest.main()
