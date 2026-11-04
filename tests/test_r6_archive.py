"""R6-070: archive transactions, confirmed intervals and crash recovery.

Real files, real SQLite, real fsync, real lease fencing. The crash cases are produced
by making the commit step fail *after* the bytes are on disk, which is exactly the
window the ticket is about: data written, watermark not advanced.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.collectors.archive import Archive, ArchiveError
from server.jobs.repository import JobRepository

ROOT = Path(__file__).resolve().parents[1]


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


SOURCE = {'id': 'src_1', 'label': 'PSG-A 调试'}
MAPPING = {'id': 'map_1', 'root_id': 'legacy', 'target_subdir': 'psg-a/debug'}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.archive = Archive(self.app)
        self.plan = self.archive.plan(source=SOURCE, mapping=MAPPING,
                                      remote_path='/var/log/app.log', name='app.log')
        self.archive.register(self.plan, source_id='src_1', mapping_id='map_1', size=3000,
                              evidence={'identity': {'id': 'inode-1'}, 'mtimeNs': 1})
        self.file_id = self.plan['file_id']

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------------ writes
    def test_contiguous_appends_commit_intervals_and_advance_the_watermark(self):
        payload = b'a' * 1000 + b'b' * 1000 + b'c' * 1000
        for index in range(3):
            chunk = payload[index * 1000:(index + 1) * 1000]
            result = self.archive.append(self.file_id, 1, offset=index * 1000, data=chunk)
            self.assertTrue(result['committed'])
        watermark = self.archive.watermark(self.file_id, 1)
        self.assertEqual(watermark['contiguous'], 3000)
        self.assertEqual(watermark['archived'], 3000)
        self.assertEqual(len(watermark['intervals']), 3)
        self.assertEqual(self.plan['path'].read_bytes(), payload)
        self.assertTrue(self.archive.verify(self.file_id, 1)['ok'])

    def test_same_offset_same_bytes_is_idempotent_and_different_bytes_conflict(self):
        self.archive.append(self.file_id, 1, offset=0, data=b'x' * 100)
        again = self.archive.append(self.file_id, 1, offset=0, data=b'x' * 100)
        self.assertFalse(again['committed'])
        self.assertTrue(again['idempotent'])
        self.assertEqual(self.archive.watermark(self.file_id, 1)['archived'], 100)
        with self.assertRaises(ArchiveError) as caught:
            self.archive.append(self.file_id, 1, offset=0, data=b'y' * 100)
        self.assertEqual(caught.exception.code, 'offset_conflict')
        self.assertEqual(self.plan['path'].read_bytes()[:100], b'x' * 100)

    def test_non_contiguous_append_is_refused_instead_of_creating_a_hole(self):
        self.archive.append(self.file_id, 1, offset=0, data=b'a' * 100)
        with self.assertRaises(ArchiveError) as caught:
            self.archive.append(self.file_id, 1, offset=200, data=b'b' * 100)
        self.assertEqual(caught.exception.code, 'non_contiguous')
        self.assertEqual(self.archive.watermark(self.file_id, 1)['contiguous'], 100)

    def test_commit_failure_leaves_no_confirmed_bytes(self):
        original = self.archive._commit

        def explode(*args, **kwargs):
            raise ArchiveError('disk_full', '模拟提交失败')

        self.archive._commit = explode
        with self.assertRaises(ArchiveError):
            self.archive.append(self.file_id, 1, offset=0, data=b'z' * 500)
        self.archive._commit = original
        # The unconfirmed tail must be gone: a retry at offset 0 has to start clean.
        self.assertEqual(self.archive.watermark(self.file_id, 1)['contiguous'], 0)
        self.assertEqual(self.plan['path'].stat().st_size, 0)
        result = self.archive.append(self.file_id, 1, offset=0, data=b'w' * 500)
        self.assertTrue(result['committed'])
        self.assertEqual(self.plan['path'].read_bytes(), b'w' * 500)

    # ----------------------------------------------------------------- leases
    def test_a_superseded_lease_cannot_commit(self):
        repo = JobRepository(self.app.store)
        job = repo.create('collect', 'system:collector', {'source_id': 'src_1'})
        first = repo.claim(job.id, 'worker-a', ttl=0.05)
        time.sleep(0.08)
        repo.reclaim_expired()
        second = repo.claim(job.id, 'worker-b', ttl=60)
        self.assertGreater(second.fencing, first.fencing)
        with self.assertRaises(ArchiveError) as caught:
            self.archive.append(self.file_id, 1, offset=0, data=b'x' * 10, lease=first)
        self.assertEqual(caught.exception.code, 'stale_writer')
        self.assertEqual(self.archive.watermark(self.file_id, 1)['contiguous'], 0)
        self.assertTrue(self.archive.append(self.file_id, 1, offset=0, data=b'x' * 10,
                                            lease=second)['committed'])

    def test_an_expired_lease_cannot_commit(self):
        repo = JobRepository(self.app.store)
        job = repo.create('collect', 'system:collector', {'source_id': 'src_1'})
        lease = repo.claim(job.id, 'worker-a', ttl=0.01)
        archive = Archive(self.app, clock=lambda: lease.expires_at + 1)
        with self.assertRaises(ArchiveError) as caught:
            archive.append(self.file_id, 1, offset=0, data=b'x' * 10, lease=lease)
        self.assertEqual(caught.exception.code, 'stale_writer')

    # --------------------------------------------------------------- recovery
    def test_recovery_drops_intervals_whose_bytes_changed(self):
        self.archive.append(self.file_id, 1, offset=0, data=b'a' * 100)
        self.archive.append(self.file_id, 1, offset=100, data=b'b' * 100)
        with open(self.plan['path'], 'r+b') as handle:
            handle.seek(120)
            handle.write(b'Z' * 10)
        report = self.archive.recover(self.file_id, 1)
        self.assertEqual(len(report['dropped']), 1)
        self.assertEqual(report['contiguous'], 100)
        watermark = self.archive.watermark(self.file_id, 1)
        self.assertEqual(watermark['archived'], 100)
        self.assertEqual(self.plan['path'].stat().st_size, 100, '未确认的尾部应被裁掉')

    def test_recovery_does_not_touch_a_valid_prefix(self):
        self.archive.append(self.file_id, 1, offset=0, data=b'a' * 128)
        report = self.archive.recover(self.file_id, 1)
        self.assertEqual(report['dropped'], [])
        self.assertEqual(report['contiguous'], 128)
        self.assertEqual(self.plan['path'].stat().st_size, 128)

    def test_seal_requires_the_declared_length(self):
        self.archive.append(self.file_id, 1, offset=0, data=b'a' * 100)
        with self.assertRaises(ArchiveError) as caught:
            self.archive.seal(self.file_id, 1, expected_size=200)
        self.assertEqual(caught.exception.code, 'incomplete')
        self.assertTrue(self.archive.seal(self.file_id, 1, expected_size=100)['sealed'])

    def test_register_is_idempotent_and_creates_the_logical_file(self):
        plan = self.archive.plan(source=SOURCE, mapping=MAPPING,
                                 remote_path='/var/log/app.log', name='app.log')
        again = self.archive.register(plan, source_id='src_1', mapping_id='map_1', size=3000)
        self.assertEqual(again['fileId'], self.file_id)
        with self.app.store.connect() as db:
            rows = db.execute('SELECT COUNT(*) AS n FROM source_files').fetchone()['n']
            logical = db.execute('SELECT * FROM logical_files WHERE id=?',
                                 (self.file_id,)).fetchone()
        self.assertEqual(rows, 1)
        self.assertEqual(logical['rel_path'], plan['rel_path'])

    def test_plan_keeps_remote_path_out_of_the_local_path(self):
        plan = self.archive.plan(source=SOURCE, mapping=MAPPING,
                                 remote_path='/var/log/../../etc/passwd', name='passwd')
        self.assertNotIn('..', plan['rel_path'])
        self.assertTrue(plan['rel_path'].startswith('collected/'))


if __name__ == '__main__':
    unittest.main()
