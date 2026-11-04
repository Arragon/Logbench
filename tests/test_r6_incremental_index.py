"""R6-073: incremental parsing, provisional revision and count rollback.

Real shard, real Node rule engine, real archive bytes. The properties under test are
the ones that make incremental indexing trustworthy:

* the second pass parses only the new bytes (the archive watermark moved, the parsed
  watermark followed it);
* an unchanged file costs nothing;
* a revised record can **lose** an issue, and the counts say so afterwards.

The revision case is produced the way it happens in practice: the same ordinal is
written again at a higher version (a provisional tail that completed), then the
derived rows are cleared and re-derived.
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
from server.collectors.archive import Archive
from server.indexing.incremental import IncrementalIndexer
from server.indexing.shards import ShardReader, ShardWriter

ROOT = Path(__file__).resolve().parents[1]
# The bracket format the shipped rule profile understands: eight header fields.
# The bracket format the shipped profile understands: eight header fields and a
# message that keeps the `name() key: value` shape the extractor expects.
LINES = (b'[2026-11-03 10:00:01.0000000] [INFO] [APP-A] [1001] [1041] [main.go:42] [100] [0:0:0] boot() stage: ready\n'
         b'[2026-11-03 10:00:02.0000000] [ERROR] [APP-A] [1001] [1041] [client.go:88] [100] [0:0:0] dial() target: psg-a\n'
         b'[2026-11-03 10:00:03.0000000] [INFO] [APP-A] [1001] [1041] [main.go:43] [100] [0:0:0] retry() attempt: 1\n')


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.archive = Archive(self.app)
        self.plan = self.archive.plan(source={'id': 's', 'label': 'PSG-A'},
                                      mapping={'id': 'm', 'root_id': 'legacy',
                                               'target_subdir': 'psg-a'},
                                      remote_path='/var/log/app.log', name='app.log')
        self.archive.register(self.plan, source_id='s', mapping_id='m', size=len(LINES),
                              evidence={'identity': {'id': 'i'}, 'mtimeNs': 1})
        self.archive.append(self.plan['file_id'], 1, offset=0, data=LINES)
        self.file_id = self.plan['file_id']
        self.indexer = IncrementalIndexer(self.app)

    def tearDown(self):
        try:
            self.app.index_jobs.close()
        except Exception:  # noqa: BLE001 - closing a pool twice is harmless
            pass
        self.temp.cleanup()

    # -------------------------------------------------------------------- cases
    def test_first_sync_builds_the_full_index_and_records_all_watermarks(self):
        result = self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        self.assertEqual(result['mode'], 'full')
        self.assertEqual(result['state'], 'indexed')
        self.assertEqual(result['bytes'], len(LINES))
        coverage = self.indexer.coverage(self.file_id, 1)
        self.assertEqual(coverage['unparsedBytes'], 0)
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['stages']['parsed'], len(LINES))
        shard = self.indexer.shard(self.file_id, 1)
        self.assertEqual(ShardReader(shard).count(), 3)

    def test_second_sync_parses_only_the_new_bytes(self):
        self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        extra = (b'[2026-11-03 10:00:04.0000000] [INFO] [APP-A] [1001] [1041] [main.go:44] [100] [0:0:0] tick() count: 2\n'
                 b'[2026-11-03 10:00:05.0000000] [WARNING] [APP-A] [1001] [1041] [main.go:45] [100] [0:0:0] wait() delay: 300\n')
        self.archive.append(self.file_id, 1, offset=len(LINES), data=extra)
        result = self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        self.assertEqual(result['mode'], 'incremental')
        self.assertEqual(result['bytesRead'], len(extra), '只读取新增区间')
        self.assertEqual(result['records'], 5)
        shard = self.indexer.shard(self.file_id, 1)
        self.assertEqual(ShardReader(shard).count(), 5)
        coverage = self.indexer.coverage(self.file_id, 1)
        self.assertEqual(coverage['stages']['parsed'], len(LINES) + len(extra))
        self.assertEqual(coverage['unparsedBytes'], 0)

    def test_unchanged_file_is_not_reparsed(self):
        self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        result = self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        self.assertEqual(result['state'], 'current')
        self.assertNotIn('bytesRead', result)

    def test_coverage_reports_archive_ahead_of_index_as_unparsed(self):
        self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        extra = b'[2026-11-03 10:00:06.0000000] [INFO] [APP-A] [1001] [1041] [main.go:46] [100] [0:0:0] more() count: 3\n'
        self.archive.append(self.file_id, 1, offset=len(LINES), data=extra)
        coverage = self.indexer.coverage(self.file_id, 1)
        self.assertEqual(coverage['unparsedBytes'], len(extra))
        self.assertFalse(coverage['complete'], '归档领先索引时不能宣称完整')

    def test_a_revised_record_can_lose_its_issue_and_the_count_follows(self):
        self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        shard = self.indexer.shard(self.file_id, 1)
        reader = ShardReader(shard)
        rules_layer = reader.layer('rules')
        version = rules_layer['version']
        before = reader.rule_count(version)
        errors_before = sum(1 for row in reader.rule_rows(version, 0, 100)
                            if row.get('issue'))
        self.assertGreaterEqual(errors_before, 1, '夹具里应至少有一条告警级记录')

        # The same ordinal, revised at a higher version, now says INFO.
        record = {key: value for key, value in dict(reader.get(1)).items()
                  if key not in ('raw',)}
        record['version'] = 2
        record['level'] = 'INFO'
        record['message'] = 'dial recovered'
        writer = ShardWriter(shard, file_id=self.file_id, generation=1, signature='default').open()
        try:
            writer.commit_batch([record], {'bytes': len(LINES), 'records': 3}, stage='parsed')
            pool = self.app.index_jobs.pool
            self.indexer._apply_rules(writer, shard, self.file_id, 1, [1], self.plan['rel_path'],
                                      pool, 'revise:1')
        finally:
            writer.close()
        reader = ShardReader(shard)
        after = sum(1 for row in reader.rule_rows(version, 0, 100) if row.get('issue'))
        self.assertEqual(after, errors_before - 1, '修订后的记录不应保留旧告警')
        self.assertEqual(reader.count(), 3, '修订不产生重复记录')

    def test_clear_ordinals_removes_postings_and_timeline_rows(self):
        self.indexer.sync(self.file_id, 1, self.plan['rel_path'])
        shard = self.indexer.shard(self.file_id, 1)
        reader = ShardReader(shard)
        version = reader.layer('rules')['version']
        postings_before = reader.posting_count(reader.layer('search')['version'])
        self.assertGreater(postings_before, 0)
        writer = ShardWriter(shard, file_id=self.file_id, generation=1, signature='default').open()
        try:
            writer.clear_ordinals(version, [1])
            writer.clear_ordinals(reader.layer('search')['version'], [1])
            writer.write_rule_rows(version, [{'ordinal': 1, 'fields': [], 'issue': None}],
                                   reset=False)
        finally:
            writer.close()
        fresh = ShardReader(shard)
        self.assertLess(fresh.posting_count(fresh.layer('search')['version']), postings_before)
        self.assertIsNone(fresh.rule_row(version, 1).get('issue'))


if __name__ == '__main__':
    unittest.main()
