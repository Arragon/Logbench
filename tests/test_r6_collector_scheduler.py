"""R6-072: one collection pass over a fake transport, with the real archive.

The transport is a fake because there is no SFTP/SMB endpoint here (B-007), but every
other participant is real: real SQLite, real files, real generation decisions, real
lease fencing, real byte accounting. The cases are the ones that decide whether a
scheduler is safe to leave running: new bytes only, bounded work, truncation, and an
error that must not look like an empty folder.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.collectors import base as adapter_base
from server.collectors.archive import Archive
from server.collectors.base import (Adapter, Capabilities, Chunk, CollectorError, Entry,
                                    Listing, RemoteStat, SUPPORTED, UNSUPPORTED)
from server.collectors.scheduler import CollectScheduler, Quota, backoff
from server.collectors.sources import SourceService

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport(Adapter):
    name = 'fake-collect'
    label = '采集夹具'
    default_port = 22
    requires_tools = ()

    def __init__(self, source, *, files=None, **kwargs):
        super().__init__(source, **kwargs)
        self.files = files if files is not None else {}
        self.reads = 0
        self.bytes_read = 0
        self.list_error = None
        self.read_error = None

    def capabilities(self):
        return Capabilities(directory_discovery=SUPPORTED, range_read=SUPPORTED,
                            stable_identity='size-mtime', append_contract='declared')

    def probe(self, *, config_revision, fingerprint, guard=None):
        raise NotImplementedError

    def list(self, directory, *, cursor=None, limit=None):
        if self.list_error:
            raise self.list_error
        prefix = directory.rstrip('/') + '/'
        entries = []
        for path, payload in sorted(self.files.items()):
            if not path.startswith(prefix):
                continue
            entries.append(Entry(path=path, name=path.rsplit('/', 1)[-1],
                                 size=len(payload['data']),
                                 mtime_ns=payload.get('mtime', 1000),
                                 identity={'id': payload.get('id', path)}))
        return Listing(directory=directory, entries=entries, scanned=len(entries))

    def stat(self, path):
        payload = self.files[path]
        return RemoteStat(path=path, size=len(payload['data']),
                          mtime_ns=payload.get('mtime', 1000),
                          identity={'id': payload.get('id', path),
                                    'modTime': payload.get('mtime', 1000)})

    def read_range(self, path, *, offset, length, identity=None):
        if self.read_error:
            raise self.read_error
        payload = self.files[path]['data']
        chunk = payload[offset:offset + length]
        self.reads += 1
        self.bytes_read += len(chunk)
        return Chunk(chunk, offset, offset + len(chunk),
                     identity={'size': len(payload)}, verified=True)


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server.collectors import base as registry
        if registry.adapter_class(FakeTransport.name) is None:
            registry.register(FakeTransport)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.service = SourceService(self.app)
        self.created = self.service.create(
            {'role': 'admin', 'ip': '127.0.0.1', 'id': 'adm'},
            {'adapter': 'fake-collect', 'label': 'PSG-A',
             'endpoint': 'sftp://127.0.0.1', 'acl': 'admin',
             'policy': {'allowedHosts': ['127.0.0.1'], 'allowLoopback': True},
             'mappings': [{'remoteDir': '/var/log/', 'rootId': 'legacy',
                           'targetSubdir': 'psg-a'}]})
        self.source_id = self.created['id']
        with self.app.store.transaction() as db:
            db.execute('UPDATE remote_sources SET enabled=1 WHERE id=?', (self.source_id,))
        self.files = {'/var/log/app.log': {'data': b'line-1\nline-2\n', 'mtime': 1000,
                                           'id': 'inode-1'}}
        self.transport = None

        self.inject = {'list': None, 'read': None}

        def factory(source):
            self.transport = FakeTransport(source, files=self.files)
            self.transport.list_error = self.inject['list']
            self.transport.read_error = self.inject['read']
            return self.transport

        self.scheduler = CollectScheduler(self.app, adapters_factory=factory,
                                          settings={'global_quota': 2, 'host_quota': 1,
                                                    'max_bytes_per_run': 1024 * 1024})

    def tearDown(self):
        self.temp.cleanup()

    def row(self):
        with self.app.store.connect() as db:
            return db.execute('SELECT * FROM source_files').fetchone()

    def archived_bytes(self) -> bytes:
        path = Path(self.root) / 'data' / self.row()['rel_path']
        return path.read_bytes() if path.exists() else b''

    # -------------------------------------------------------------------- cases
    def test_first_pass_archives_the_file(self):
        report = self.scheduler.run_source(self.source_id)
        self.assertIsNone(report['skipped'])
        self.assertEqual(report['bytes'], len(self.files['/var/log/app.log']['data']))
        self.assertEqual(self.archived_bytes(), b'line-1\nline-2\n')
        row = self.row()
        self.assertEqual(row['state'], 'archived')
        self.assertEqual(row['contiguous_bytes'], len(b'line-1\nline-2\n'))
        self.assertEqual(row['generation'], 1)

    def test_second_pass_reads_only_new_bytes(self):
        self.scheduler.run_source(self.source_id)
        before = self.row()['contiguous_bytes']
        read_before = self.transport.bytes_read
        self.files['/var/log/app.log']['data'] += b'line-3\n'
        self.files['/var/log/app.log']['mtime'] = 1200
        report = self.scheduler.run_source(self.source_id)
        self.assertEqual(report['bytes'], len(b'line-3\n'), '只归档新增字节')
        self.assertLessEqual(report['readBytes'], len(b'line-1\nline-2\nline-3\n') + 1024,
                             '读取量只包含头部样本与新增范围')
        self.assertEqual(self.row()['contiguous_bytes'], before + len(b'line-3\n'))
        delta = self.transport.bytes_read - read_before
        self.assertLessEqual(delta, len(b'line-1\nline-2\nline-3\n') + 64,
                             '第二次只重新读取头部样本与新增范围：%d 字节' % delta)
        self.assertEqual(self.archived_bytes(), b'line-1\nline-2\nline-3\n')

    def test_unchanged_file_costs_only_the_head_read(self):
        self.scheduler.run_source(self.source_id)
        before = self.row()['contiguous_bytes']
        report = self.scheduler.run_source(self.source_id)
        self.assertEqual(report['bytes'], 0, '没有新增字节可归档')
        self.assertEqual(self.row()['contiguous_bytes'], before)
        self.assertEqual(self.row()['archived_bytes'], before)

    def test_truncation_opens_a_new_generation_and_keeps_the_old_bytes(self):
        self.scheduler.run_source(self.source_id)
        old_file = self.archived_bytes()
        self.files['/var/log/app.log'] = {'data': b'line-1\n', 'mtime': 1300, 'id': 'inode-1'}
        self.scheduler.run_source(self.source_id)
        row = self.row()
        self.assertEqual(row['generation'], 2)
        self.assertEqual(row['gap_state'], 'unknown_length')
        self.assertEqual(row['contiguous_bytes'], len(b'line-1\n'))
        archive = Archive(self.app)
        old = archive.watermark(row['file_id'], 1)
        self.assertEqual(old['archived'], len(old_file), '旧代次区间仍可读')

    def test_same_size_rewrite_is_a_replacement(self):
        self.scheduler.run_source(self.source_id)
        self.files['/var/log/app.log'] = {'data': b'LINE-1\nLINE-2\n', 'mtime': 1400,
                                          'id': 'inode-1'}
        self.scheduler.run_source(self.source_id)
        self.assertEqual(self.row()['generation'], 2)
        self.assertEqual(self.archived_bytes(), b'LINE-1\nLINE-2\n')

    def test_a_listing_error_is_not_an_empty_directory(self):
        self.scheduler.run_source(self.source_id)
        self.inject['list'] = CollectorError('permission_denied', '权限不足')
        report = self.scheduler.run_source(self.source_id)
        self.assertTrue(report['errors'])
        self.assertEqual(report['errors'][0]['code'], 'permission_denied')
        row = self.row()
        self.assertEqual(row['state'], 'stopped', '权限问题需要人工，不能无限重试')
        self.assertTrue(json.loads(row['last_error'])['needs_human'])

    def test_retryable_error_sets_a_backoff_time(self):
        self.scheduler.run_source(self.source_id)
        self.inject['read'] = CollectorError('timeout', '超时')
        # The head read happens before the archive read, so this fails the file.
        report = self.scheduler.run_source(self.source_id)
        row = self.row()
        self.assertEqual(row['state'], 'retry')
        self.assertIsNotNone(row['retry_at'])
        self.assertGreater(row['retry_at'], time.time() - 1)

    def test_budget_limits_bytes_per_run_and_resumes_later(self):
        self.files['/var/log/app.log']['data'] = b''.join(b'line-%05d\n' % i for i in range(4000))
        scheduler = CollectScheduler(self.app,
                                     adapters_factory=lambda source: FakeTransport(
                                         source, files=self.files),
                                     settings={'max_bytes_per_run': 4096})
        first = scheduler.run_source(self.source_id)
        self.assertEqual(first['bytes'], 4096)
        self.assertEqual(self.row()['contiguous_bytes'], 4096)
        second = scheduler.run_source(self.source_id)
        self.assertGreater(second['bytes'], 0)
        self.assertEqual(self.row()['contiguous_bytes'], 8192)

    def test_disabled_and_paused_sources_do_nothing(self):
        with self.app.store.transaction() as db:
            db.execute('UPDATE remote_sources SET paused=1 WHERE id=?', (self.source_id,))
        self.assertEqual(self.scheduler.run_source(self.source_id)['skipped'], 'paused')
        with self.app.store.transaction() as db:
            db.execute('UPDATE remote_sources SET enabled=0, paused=0 WHERE id=?',
                       (self.source_id,))
        self.assertEqual(self.scheduler.run_source(self.source_id)['skipped'], 'disabled')

    def test_file_filters_are_applied_before_reading(self):
        self.files['/var/log/notes.txt'] = {'data': b'hello', 'mtime': 1000}
        self.service.delete_mapping({'role': 'admin', 'ip': '127.0.0.1'}, self.source_id,
                                    self.created['mappings'][0]['id'])
        self.service.add_mapping({'role': 'admin', 'ip': '127.0.0.1'}, self.source_id,
                                 {'remoteDir': '/var/log/', 'rootId': 'legacy',
                                  'targetSubdir': 'psg-a', 'include': ['*.log']})
        self.scheduler.run_source(self.source_id)
        with self.app.store.connect() as db:
            paths = [row['remote_path'] for row in
                     db.execute('SELECT remote_path FROM source_files').fetchall()]
        self.assertEqual(paths, ['/var/log/app.log'])


    # ------------------------------------------------------------------ events
    def test_the_pass_publishes_references_and_never_record_bodies(self):
        class StubIndexer:
            def sync(self, file_id, generation, rel_path, lease=None):
                return {'state': 'indexed', 'mode': 'incremental', 'records': 2,
                        'newRecords': 2, 'parsed': 9, 'archive': 9}

        self.scheduler.indexer = StubIndexer()
        self.scheduler.run_source(self.source_id)
        with self.app.store.connect() as db:
            rows = db.execute('SELECT kind,payload FROM event_log ORDER BY id').fetchall()
        kinds = [row['kind'] for row in rows]
        self.assertIn('archive_commit', kinds)
        self.assertIn('record_revision', kinds)
        self.assertIn('index_commit', kinds)
        for row in rows:
            payload = row['payload']
            self.assertNotIn('line-1', payload, '事件不得携带正文')
            self.assertIn('fileId', payload, '每个事件都带定位引用')

    def test_a_new_generation_is_announced_as_a_rotation(self):
        self.scheduler.run_source(self.source_id)
        self.files['/var/log/app.log'] = {'data': b'line-1\n', 'mtime': 1300, 'id': 'inode-1'}
        self.scheduler.run_source(self.source_id)
        with self.app.store.connect() as db:
            rows = db.execute("SELECT payload FROM event_log WHERE kind='rotation'").fetchall()
        self.assertTrue(rows, '截断/改写必须通知阅读器重新取锚点')
        self.assertIn('truncated', rows[-1]['payload'])

    def test_a_gap_is_published_as_a_gap(self):
        self.scheduler.run_source(self.source_id)
        self.files['/var/log/app.log'] = {'data': b'line-1\n', 'mtime': 1300, 'id': 'inode-1'}
        self.scheduler.run_source(self.source_id)
        with self.app.store.connect() as db:
            rows = db.execute("SELECT payload FROM event_log WHERE kind='gap'").fetchall()
        self.assertTrue(rows)
        self.assertIn('unknown_length', rows[-1]['payload'])


class QuotaTests(unittest.TestCase):
    def test_per_host_limit_is_enforced_before_the_global_one(self):
        quota = Quota(total=4, per_host=1)
        self.assertTrue(quota.acquire('a'))
        self.assertFalse(quota.acquire('a'))
        self.assertTrue(quota.acquire('b'))
        self.assertEqual(quota.snapshot()['active'], 2)
        quota.release('a')
        self.assertTrue(quota.acquire('a'))
        self.assertEqual(quota.snapshot()['peak'], 2, '峰值是同时在飞的数量，不是累计次数')

    def test_global_limit_is_enforced(self):
        quota = Quota(total=1, per_host=2)
        self.assertTrue(quota.acquire('a'))
        self.assertFalse(quota.acquire('b'))
        quota.release('a')
        self.assertTrue(quota.acquire('b'))

    def test_backoff_grows_and_is_jittered(self):
        values = [backoff(n, base=5, cap=300, rng=lambda: 1.0) for n in range(1, 8)]
        self.assertEqual(values[:4], [5, 10, 20, 40])
        self.assertEqual(values[-1], 300)
        low = backoff(5, base=5, cap=300, rng=lambda: 0.0)
        self.assertAlmostEqual(low, 40.0)
        self.assertLess(low, backoff(5, base=5, cap=300, rng=lambda: 1.0))


if __name__ == '__main__':
    unittest.main()
