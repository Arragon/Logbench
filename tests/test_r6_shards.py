"""R6-037: shard writes are atomic, replay-safe and index-backed."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.indexing.shards import (ShardError, ShardReader, ShardWriter,
                                    assert_local_fs, shard_path)


def record(ordinal: int, **overrides) -> dict:
    base = {
        'ordinal': ordinal,
        'record_id': 'f_test:%d' % ordinal,
        'version': 1,
        'level': 'ERROR' if ordinal % 7 == 0 else 'INFO',
        'ts': '2026-01-01 00:00:%02d.000' % (ordinal % 60),
        'ts_ns': '1767225600%012d' % ordinal,
        'ts_key': 1767225600000 + ordinal,
        'lines': {'first': ordinal * 2 + 1, 'last': ordinal * 2 + 2},
        'bytes': {'start': str(ordinal * 100), 'end': str(ordinal * 100 + 90)},
        'has_trace': ordinal % 11 == 0,
        'logger': 'worker' if ordinal % 2 else 'gateway',
        'fn': 'run' if ordinal % 3 else 'tick',
        'message': 'record %d energy' % ordinal,
        'body': ['  detail %d' % ordinal],
        'fields': [['k', str(ordinal)]],
        'event': {'type': 'error', 'label': '原始告警等级'} if ordinal % 7 == 0 else None,
    }
    base.update(overrides)
    return base


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = shard_path(self.root, 'f_test', 1, 'parser-a')
        self.writer = ShardWriter(self.path, file_id='f_test', generation=1,
                                  signature='parser-a').open()

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def test_round_trip_of_a_batch_with_its_checkpoint(self):
        result = self.writer.commit_batch([record(i) for i in range(50)],
                                          {'bytes': 5000, 'records': 50})
        self.assertEqual(result['written'], 50)
        reader = ShardReader(self.path)
        self.assertEqual(reader.count(), 50)
        self.assertEqual(reader.checkpoint('parsed'), {'bytes': 5000, 'records': 50})
        first = reader.get(0)
        self.assertEqual(first['message'], 'record 0 energy')
        self.assertEqual(first['body'], ['  detail 0'])
        self.assertEqual(first['fields'], [['k', '0']])

    def test_replaying_a_batch_is_idempotent(self):
        batch = [record(i) for i in range(20)]
        self.writer.commit_batch(batch, {'bytes': 1, 'records': 20})
        self.writer.commit_batch(batch, {'bytes': 1, 'records': 20})
        self.assertEqual(ShardReader(self.path).count(), 20)

    def test_a_failed_batch_advances_neither_records_nor_checkpoint(self):
        self.writer.commit_batch([record(i) for i in range(5)], {'bytes': 100, 'records': 5})
        broken = [record(10), {'ordinal': -1, 'record_id': 'bad'}]
        with self.assertRaises(ShardError):
            self.writer.commit_batch(broken, {'bytes': 999, 'records': 7})
        reader = ShardReader(self.path)
        self.assertEqual(reader.count(), 5, 'the failed batch must not add records')
        self.assertEqual(reader.checkpoint('parsed'), {'bytes': 100, 'records': 5})

    def test_seek_by_line_and_byte_lands_on_the_same_record(self):
        self.writer.commit_batch([record(i) for i in range(100)], {'bytes': 10000, 'records': 100})
        reader = ShardReader(self.path)
        by_line = reader.seek_line(41)
        by_byte = reader.seek_byte(2050)
        self.assertIsNotNone(by_line)
        self.assertIsNotNone(by_byte)
        self.assertEqual(by_line['ordinal'], 20)
        self.assertEqual(by_byte['ordinal'], 20)

    def test_nineteen_digit_timestamps_survive_as_text(self):
        self.writer.commit_batch([record(0, ts_ns='1767225600123456789')], {'bytes': 1, 'records': 1})
        row = ShardReader(self.path).get(0)
        self.assertEqual(row['ts_ns'], '1767225600123456789')
        self.assertEqual(len(row['ts_ns']), 19)

    def test_records_without_time_are_kept_and_sorted_last(self):
        self.writer.commit_batch([
            record(0, ts=None, ts_ns=None, ts_key=None),
            record(1),
        ], {'bytes': 2, 'records': 2})
        reader = ShardReader(self.path)
        rows = reader.time_range(None, None)
        self.assertEqual([row['ordinal'] for row in rows], [1, 0],
                         'a null ts_key sorts before numeric keys only if the query asks; '
                         'here the ordered scan puts the timed record first')

    def test_duplicate_and_backward_timestamps_keep_ordinal_order(self):
        rows = [
            record(0, ts_key=500),
            record(1, ts_key=500),
            record(2, ts_key=100),
        ]
        self.writer.commit_batch(rows, {'bytes': 3, 'records': 3})
        page = ShardReader(self.path).page(0, 10)
        self.assertEqual([row['ordinal'] for row in page], [0, 1, 2])
        timed = ShardReader(self.path).time_range(None, None)
        self.assertEqual([row['ordinal'] for row in timed], [2, 0, 1],
                         'time order is explicit and stable by ordinal')

    def test_structure_queries_use_indexes(self):
        self.writer.commit_batch([record(i) for i in range(200)], {'bytes': 1, 'records': 200})
        reader = ShardReader(self.path)
        plan = ' '.join(reader.query_plan(
            'SELECT * FROM records WHERE ts_key>=? AND ts_key<=? ORDER BY ts_key, ordinal LIMIT 10',
            (1767225600000, 1767225600200)))
        self.assertIn('records_ts', plan)
        level_plan = ' '.join(reader.query_plan(
            'SELECT * FROM records WHERE level=? ORDER BY ordinal LIMIT 10', ('ERROR',)))
        self.assertIn('records_level', level_plan)

    def test_timeline_pages_are_bounded_and_flag_the_last_page(self):
        self.writer.commit_batch([record(i) for i in range(70)], {'bytes': 1, 'records': 70})
        reader = ShardReader(self.path)
        first = reader.timeline(0, 50)
        self.assertEqual(len(first['nodes']), 10, 'only error-level records become nodes')
        self.assertFalse(first['has_next'])
        with self.assertRaises(ShardError):
            reader.timeline(0, 42)

    def test_the_default_journal_is_conservative(self):
        cursor = self.writer.conn.execute('PRAGMA journal_mode')
        mode = cursor.fetchone()[0]
        cursor.close()
        self.assertNotEqual(mode.lower(), 'wal',
                            'SQLite 3.38 has no safe WAL; the default must not claim it')

    def test_network_paths_are_refused(self):
        with self.assertRaises(ShardError) as caught:
            assert_local_fs(Path('/Volumes/team-share/logbench'))
        self.assertEqual(caught.exception.code, 'network_filesystem')

    def test_a_missing_shard_is_an_error_not_an_empty_result(self):
        with self.assertRaises(ShardError) as caught:
            ShardReader(self.root / 'missing.sqlite3')
        self.assertEqual(caught.exception.code, 'no_shard')

    def test_page_size_over_two_thousand_is_refused(self):
        reader = ShardReader(self.path)
        with self.assertRaises(ShardError):
            reader.page(0, 2001)


if __name__ == '__main__':
    unittest.main()
