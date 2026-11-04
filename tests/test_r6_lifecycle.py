"""R6-076: disk pressure pauses writes, GC only reclaims what can be rebuilt.

Real SQLite and real files; the only fake is the free-space reading, because filling a
real volume is not something a test may do.
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
from server.storage.lifecycle import GIB, Lifecycle, LifecycleError, retention_report

ROOT = Path(__file__).resolve().parents[1]


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.free = 1000 * GIB

        def sampler(path):
            return {'free': self.free, 'total': 4000 * GIB, 'error': None}

        self.lifecycle = Lifecycle(self.app, sampler=sampler, staging_ttl=0)
        self.root_row = self.lifecycle.roots()[0]

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------------ roots
    def test_threshold_is_the_larger_of_fifty_gib_and_five_percent(self):
        self.assertEqual(self.lifecycle.threshold(100 * GIB), 50 * GIB)
        self.assertEqual(self.lifecycle.threshold(4000 * GIB), 200 * GIB)

    def test_low_watermark_pauses_the_root_but_not_the_others(self):
        self.free = 10 * GIB
        state = self.lifecycle.check_root(self.root_row)
        self.assertEqual(state['state'], 'low')
        with self.assertRaises(LifecycleError) as caught:
            self.lifecycle.guard_write(self.root_row['id'])
        self.assertEqual(caught.exception.code, 'storage_low')
        self.assertEqual(caught.exception.details['threshold'], 200 * GIB)

    def test_offline_root_is_reported_as_offline(self):
        missing = dict(self.root_row, id='root_missing', path=str(self.root / 'gone'))
        self.assertEqual(self.lifecycle.check_root(missing)['state'], 'offline')

    # ---------------------------------------------------------------- enforce
    def _seed_source(self):
        with self.app.store.transaction() as db:
            db.execute("INSERT INTO remote_sources(id,adapter,label,endpoint,credential_ref,host_pin,"
                       "acl,revision,enabled,paused,policy,capabilities,last_probe,created_by,"
                       "created_at,updated_at) VALUES('src_1','sftp','PSG','sftp://h',NULL,NULL,"
                       "'admin',1,1,0,'{}','{}',NULL,'a','2026-11-03T00:00:00Z','2026-11-03T00:00:00Z')")
            db.execute("INSERT INTO source_files(id,source_id,mapping_id,remote_path,file_id,generation,"
                       "root_id,rel_path,size,evidence,archived_bytes,contiguous_bytes,gap_state,"
                       "sealed,state,last_seen_at,last_error,retry_at) VALUES('sf_1','src_1','m_1',"
                       "'/var/log/a.log','f_1',1,?,'collected/a.log',10,'{}',10,10,'none',0,'archived',"
                       "NULL,NULL,NULL)", (self.root_row['id'],))

    def test_enforce_pauses_a_source_on_a_full_root_and_resumes_when_it_recovers(self):
        self._seed_source()
        self.free = 1 * GIB
        report = self.lifecycle.enforce()
        self.assertEqual(len(report['paused']), 1)
        with self.app.store.connect() as db:
            row = db.execute('SELECT paused,policy FROM remote_sources WHERE id=?',
                             ('src_1',)).fetchone()
        self.assertEqual(row['paused'], 1)
        self.assertEqual(json.loads(row['policy'])['waitingStorage'], self.root_row['id'])
        self.free = 1000 * GIB
        report = self.lifecycle.enforce()
        self.assertEqual(len(report['resumed']), 1)
        with self.app.store.connect() as db:
            row = db.execute('SELECT paused,policy FROM remote_sources WHERE id=?',
                             ('src_1',)).fetchone()
        self.assertEqual(row['paused'], 0)
        self.assertNotIn('waitingStorage', json.loads(row['policy']))

    def test_an_operator_pause_is_not_undone_by_recovery(self):
        self._seed_source()
        self.free = 1 * GIB
        self.lifecycle.enforce()
        # The operator pauses while the disk is still full: that intent outranks ours.
        self.app.collectors.service.action({'ip': '127.0.0.1', 'role': 'admin'}, 'src_1',
                                          'pause', {'actionId': 'op-pause-1'})
        self.free = 1000 * GIB
        report = self.lifecycle.enforce()
        self.assertEqual(report['resumed'], [])
        with self.app.store.connect() as db:
            row = db.execute('SELECT paused,policy FROM remote_sources WHERE id=?',
                             ('src_1',)).fetchone()
        self.assertEqual(row['paused'], 1)
        self.assertEqual(json.loads(row['policy']).get('pausedBy'), 'operator')

    # --------------------------------------------------------------------- gc
    def _seed_versions(self):
        stamp = '2026-11-03T00:00:00+00:00'
        with self.app.store.transaction() as db:
            for file_id, name in (('f_1', 'a.log'), ('f_2', 'b.log'), ('f_3', 'c.log')):
                db.execute('INSERT INTO logical_files(id,root_id,rel_path,display_name,size,'
                           'generation,content_sig,created_at,updated_at) '
                           'VALUES(?,?,?,?,1,1,?,?,?)',
                           (file_id, self.root_row['id'], 'collected/' + name, name,
                            'sig:' + file_id, stamp, stamp))
            rows = [
                ('v_raw', 'f_1', 1, 'raw', 'published', 10),
                ('v_stage', 'f_1', 1, 'rules', 'staging', 100),
                ('v_failed', 'f_1', 1, 'search', 'failed', 200),
                ('v_old', 'f_1', 1, 'rules', 'superseded', 300),
                ('v_pinned', 'f_2', 1, 'search', 'superseded', 400),
                ('v_active', 'f_3', 1, 'rules', 'staging', 500),
            ]
            for version_id, file_id, generation, layer, state, size in rows:
                db.execute('INSERT INTO layer_versions(version_id,file_id,generation,layer,scope,'
                           'owner,signature,deps,state,rows,digest,bytes,stats,supersedes,'
                           'created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,0,NULL,?,?,NULL,?,NULL)',
                           (version_id, file_id, generation, layer, 'shared', 'sys',
                            'sig:' + version_id, '{}', state, size, '{}', stamp))
            db.execute("INSERT INTO layer_pins(version_id,token,owner,created_at,expires_at) "
                       "VALUES('v_pinned','t','reader','2026-11-03T00:00:00Z',?)", (time.time() + 600,))
            db.execute("INSERT INTO generations(file_id,generation,raw_bytes,content_sig,state,"
                       "created_at) VALUES('f_3',1,1,'sig','indexing','2026-11-03T00:00:00Z')")
            db.execute("INSERT INTO generations(file_id,generation,raw_bytes,content_sig,state,"
                       "created_at) VALUES('f_1',1,1,'sig','superseded','2026-11-03T00:00:00Z')")
            index_root = Path(self.app.config.state_dir) / 'index' / 'f_1'
            index_root.mkdir(parents=True, exist_ok=True)
            (index_root / 'g1-r-old.sqlite3').write_bytes(b'x' * 2048)

    def test_gc_keeps_pins_active_generations_and_raw_versions(self):
        self._seed_versions()
        report = self.lifecycle.collect_garbage()
        removed = {item['versionId'] for item in report['versions']}
        self.assertNotIn('v_raw', removed, '原文层不是缓存')
        self.assertNotIn('v_pinned', removed, '被引用的版本不能回收')
        self.assertNotIn('v_active', removed, '当前代次还在写')
        self.assertIn('v_stage', removed)
        self.assertIn('v_failed', removed)
        self.assertIn('v_old', removed)
        self.assertEqual(report['kept']['pinned'], 1)
        with self.app.store.connect() as db:
            left = {row['version_id'] for row in
                    db.execute('SELECT version_id FROM layer_versions').fetchall()}
        self.assertEqual(left, {'v_raw', 'v_pinned', 'v_active'})

    def test_gc_removes_only_superseded_shards(self):
        self._seed_versions()
        report = self.lifecycle.collect_garbage()
        paths = [item['path'] for item in report['shards']]
        self.assertTrue(any('g1-' in path for path in paths))
        self.assertFalse((Path(self.app.config.state_dir) / 'index' / 'f_1' /
                          'g1-r-old.sqlite3').exists())

    def test_dry_run_reports_without_deleting(self):
        self._seed_versions()
        report = self.lifecycle.collect_garbage(dry_run=True)
        self.assertTrue(report['versions'])
        with self.app.store.connect() as db:
            count = db.execute('SELECT COUNT(*) AS n FROM layer_versions').fetchone()['n']
        self.assertEqual(count, 6)
        self.assertTrue((Path(self.app.config.state_dir) / 'index' / 'f_1' /
                         'g1-r-old.sqlite3').exists())

    def test_retention_report_shows_archive_and_cache_sizes_separately(self):
        self._seed_source()
        self._seed_versions()
        report = retention_report(self.app)
        self.assertEqual(report['archive']['files'], 1)
        self.assertEqual(report['archive']['bytes'], 10)
        self.assertGreaterEqual(report['caches']['versions'], 5)
        self.assertTrue(report['roots'])


if __name__ == '__main__':
    unittest.main()
