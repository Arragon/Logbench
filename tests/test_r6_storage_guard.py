"""R6-053: root-scoped path safety, mount identity and offline write protection.

These are the failure modes that lose data rather than annoy a user: a `..` that
escapes the root, a symlink planted after the check, a mount that is gone while
the mountpoint still exists, and a name that two different logs would collide on.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.storage.guard import (StorageError, StorageGuard, decode_component,
                                  encode_component)


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root / 'deploy')
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir()
        (self.data / 'logs' / 'a.log').write_text('hello\n', 'utf-8')
        self.guard: StorageGuard = self.app.storage.guard
        self.root_info = self.guard.root('legacy')

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------- path shape
    def test_traversal_and_absolute_paths_are_refused(self):
        for bad in ('../secret', 'logs/../../etc/passwd', '/etc/passwd', 'C:\\windows',
                    'logs\\a.log', 'logs//a.log', 'logs/./a.log', 'logs/\x01bad'):
            with self.subTest(bad=bad):
                with self.assertRaises(StorageError):
                    self.guard.resolve(self.root_info, bad)

    def test_hidden_segments_are_refused(self):
        for bad in ('.logbench-trash', 'logs/.hidden', '.logbench-root.json'):
            with self.subTest(bad=bad):
                with self.assertRaises(StorageError):
                    self.guard.resolve(self.root_info, bad)

    def test_resolved_path_stays_inside_the_root(self):
        target = self.guard.resolve(self.root_info, 'logs/a.log', want='file')
        self.assertTrue(str(target).startswith(str(Path(self.data).resolve())))

    @unittest.skipUnless(os.name == 'posix', 'symlink semantics are POSIX-specific')
    def test_symlink_inside_the_root_is_denied(self):
        outside = self.root / 'outside.txt'
        outside.write_text('secret', 'utf-8')
        (self.data / 'logs' / 'link.log').symlink_to(outside)
        with self.assertRaises(StorageError) as caught:
            self.guard.resolve(self.root_info, 'logs/link.log')
        self.assertEqual(caught.exception.code, 'symlink_denied')
        with self.assertRaises(StorageError):
            self.guard.open_regular(self.root_info, 'logs/link.log')

    @unittest.skipUnless(os.name == 'posix', 'symlink semantics are POSIX-specific')
    def test_directory_symlink_cannot_be_walked_through(self):
        outside = self.root / 'outside-dir'
        outside.mkdir()
        (outside / 'secret.log').write_text('secret', 'utf-8')
        (self.data / 'logs' / 'leak').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.guard.resolve(self.root_info, 'logs/leak/secret.log')

    def test_open_regular_returns_a_stream_and_refuses_a_directory(self):
        stream = self.guard.open_regular(self.root_info, 'logs/a.log')
        try:
            self.assertEqual(stream.read(), b'hello\n')
        finally:
            stream.close()
        with self.assertRaises(StorageError):
            self.guard.open_regular(self.root_info, 'logs')

    # -------------------------------------------------------- mount identity
    def test_missing_sentinel_marks_the_root_offline(self):
        (self.data / '.logbench-root.json').unlink()
        state = self.guard.state(self.guard.root('legacy'))
        self.assertEqual(state['state'], 'offline')
        self.assertEqual(state['reason'], 'sentinel_missing')
        with self.assertRaises(StorageError) as caught:
            self.guard.require_online(self.guard.root('legacy'), writing=True)
        self.assertEqual(caught.exception.code, 'storage_root_offline')

    def test_changed_volume_marks_the_root_offline(self):
        with self.app.store.transaction() as db:
            db.execute("UPDATE storage_roots SET volume_id='999999' WHERE id='legacy'")
        state = self.guard.state(self.guard.root('legacy'))
        self.assertEqual(state['reason'], 'volume_changed')

    def test_root_that_becomes_a_symlink_is_offline(self):
        moved = self.root / 'moved-data'
        shutil.move(str(self.data), str(moved))
        self.data.symlink_to(moved, target_is_directory=True)
        state = self.guard.state(self.guard.root('legacy'))
        self.assertEqual(state['state'], 'offline')
        self.assertEqual(state['reason'], 'became_symlink')

    def test_missing_root_directory_is_offline_not_empty(self):
        empty = self.root / 'empty-root'
        empty.mkdir()
        created = self.app.storage.roots.create(
            self.app.actor('127.0.0.1'), {'label': 'Empty', 'path': str(empty)})
        root_id = created['item']['root_id']
        shutil.rmtree(empty)
        state = self.guard.state(self.guard.root(root_id))
        self.assertEqual(state['state'], 'offline')
        self.assertEqual(state['reason'], 'missing')

    # ----------------------------------------------------------------- probe
    def test_probe_writes_a_sentinel_and_is_repeatable(self):
        target = self.root / 'probe-root'
        target.mkdir()
        root = {'id': 'probe-root', 'kind': 'shared_raw', 'label': 'P', 'path': target,
                'enabled': True, 'volume_id': '', 'sentinel_id': '', 'quota_bytes': 0,
                'probe_state': 'unknown'}
        first = self.guard.probe(root)
        self.assertTrue(first['writable'])
        self.assertTrue(first['sentinel_id'])
        sentinel = target / '.logbench-root.json'
        self.assertTrue(sentinel.is_file())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode) & 0o777, 0o600)
        root['sentinel_id'] = first['sentinel_id']
        second = self.guard.probe(root)
        self.assertEqual(second['sentinel_id'], first['sentinel_id'])
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['.logbench-root.json'])

    def test_probe_reports_a_directory_it_cannot_write(self):
        if os.name != 'posix' or os.geteuid() == 0:
            self.skipTest('write permission is not enforceable for this account')
        target = self.root / 'readonly-root'
        target.mkdir()
        os.chmod(target, 0o500)
        try:
            root = {'id': 'readonly-root', 'kind': 'shared_raw', 'label': 'R', 'path': target,
                    'enabled': True, 'volume_id': '', 'sentinel_id': '', 'quota_bytes': 0,
                    'probe_state': 'unknown'}
            result = self.guard.probe(root)
        finally:
            os.chmod(target, 0o700)
        self.assertFalse(result['writable'])
        self.assertEqual(result['state'], 'unauthorized')
        self.assertEqual(result['reason'], 'permission_denied')

    # ------------------------------------------------------------ name mapping
    def test_name_mapping_is_reversible(self):
        for name in ('error:10/20?.log', 'tab\tname.log', '普通日志.log',
                     'x' * 300 + '.log'):
            with self.subTest(length=len(name)):
                physical = encode_component(name)
                self.assertLessEqual(len(physical.encode('utf-8')), 200)
                if len(name) <= 200:
                    self.assertEqual(decode_component(physical), name)
        # Over-long names keep a digest suffix so two different names cannot collide.
        long_a = 'a' * 400 + '-one.log'
        long_b = 'a' * 400 + '-two.log'
        self.assertNotEqual(encode_component(long_a), encode_component(long_b))


if __name__ == '__main__':
    unittest.main()
