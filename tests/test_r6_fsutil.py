"""Cross-platform disk probe: `os.statvfs` exists on POSIX only.

The regression this pins down: a direct `os.statvfs(...)` call on an interpreter
without that symbol (Windows, and any 3.9/3.12 build that reports it as
unimplemented) raises `module 'os' has no attribute 'statvfs'` before any
fallback can run, so the storage root probe dies instead of reporting a state.

Every case below runs on both 3.9 and 3.12: nothing here uses syntax or APIs
newer than 3.9, and the "no statvfs" shape is simulated rather than assumed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.storage import fsutil
from server.storage import lifecycle
from server.storage.lifecycle import default_sampler


class DiskUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_directory_reports_ordered_numbers(self):
        usage = fsutil.disk_usage(self.root)
        self.assertIsNotNone(usage)
        self.assertGreater(usage.total, 0)
        self.assertGreaterEqual(usage.used, 0)
        self.assertGreaterEqual(usage.free, 0)
        self.assertGreaterEqual(usage.available, 0)
        self.assertLessEqual(usage.free, usage.total)
        self.assertLessEqual(usage.available, usage.total)
        self.assertEqual(usage.used, usage.total - usage.free)

    def test_posix_probe_agrees_with_shutil(self):
        if getattr(os, 'statvfs', None) is None:
            self.skipTest('this interpreter has no os.statvfs')
        usage = fsutil.disk_usage(self.root)
        reference = shutil.disk_usage(str(self.root))
        self.assertEqual(usage.total, reference.total)
        self.assertEqual(usage.free, reference.free)

    def test_missing_path_reports_none_instead_of_raising(self):
        self.assertIsNone(fsutil.disk_usage(self.root / 'absent-directory'))

    def test_absent_statvfs_symbol_falls_back_to_shutil(self):
        """Windows-shaped interpreter: the attribute does not exist at all."""
        with patch.object(fsutil, '_statvfs_callable', return_value=None):
            usage = fsutil.disk_usage(self.root)
        self.assertIsNotNone(usage)
        self.assertGreater(usage.total, 0)

    def test_unimplemented_statvfs_is_not_an_exception(self):
        with patch.object(os, 'statvfs', side_effect=NotImplementedError('unavailable')):
            self.assertIsNone(fsutil.disk_usage(self.root))

    def test_free_bytes_is_none_only_when_usage_is_unknown(self):
        self.assertIsNone(fsutil.free_bytes(self.root / 'absent-directory'))
        self.assertGreater(fsutil.free_bytes(self.root), 0)


class FsTypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_directory_names_a_file_system_or_unknown(self):
        value = fsutil.fs_type(self.root)
        self.assertIsInstance(value, str)
        self.assertTrue(value)
        self.assertEqual(value, value.lower())

    def test_absent_statvfs_symbol_does_not_raise(self):
        with patch.object(fsutil, '_statvfs_callable', return_value=None):
            value = fsutil.fs_type(self.root)
        self.assertIsInstance(value, str)
        self.assertTrue(value)

    def test_failed_statvfs_reports_unknown_not_a_guess(self):
        if getattr(os, 'statvfs', None) is None:
            self.skipTest('this interpreter has no os.statvfs')
        with patch.object(os, 'statvfs', side_effect=OSError('denied')):
            if os.name == 'nt':  # pragma: no cover - Windows takes the ctypes branch
                self.skipTest('Windows reads the volume name through ctypes')
            self.assertEqual(fsutil.fs_type(self.root), 'unknown')


class LifecycleSamplerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sampler_reports_real_numbers(self):
        sample = default_sampler(self.root)
        self.assertIsNone(sample['error'])
        self.assertGreater(sample['total'], 0)
        self.assertGreaterEqual(sample['free'], 0)

    def test_sampler_survives_an_interpreter_without_statvfs(self):
        with patch.object(fsutil, '_statvfs_callable', return_value=None):
            sample = default_sampler(self.root)
        self.assertIsNone(sample['error'])
        self.assertGreater(sample['total'], 0)

    def test_sampler_names_the_failure_when_usage_is_unavailable(self):
        """`disk_usage` is bound in the lifecycle module, so patch it there."""
        with patch.object(lifecycle, 'disk_usage', return_value=None):
            sample = default_sampler(self.root)
        self.assertEqual(sample, {'free': 0, 'total': 0, 'error': 'disk_usage_unavailable'})


class LinuxMountTableTests(unittest.TestCase):
    """Linux has no `f_fstypename`; the mount table is the only source of a name.

    The table is a real file written by the test and injected through
    `fsutil.MOUNT_TABLES`, so the parser, the escape decoder and the longest-match
    rule are exercised as written — not re-implemented in the test.
    """

    TABLE = (
        '/dev/sda1 / ext4 rw,relatime 0 0\n'
        'overlay /srv/containers/abc/merged overlay rw 0 0\n'
        '/dev/sdb1 /mnt/My\\040Disk ext4 rw 0 0\n'
        '10.0.0.5:/export /mnt/share nfs4 rw 0 0\n'
        '//fileserver/team /mnt/team cifs rw 0 0\n'
        'rclone /mnt/remote fuse.rclone rw 0 0\n'
        'tmpfs /run tmpfs rw 0 0\n'
    )

    def setUp(self):
        if os.name == 'nt':  # pragma: no cover - Windows reads the drive type
            self.skipTest('the mount table is a POSIX concept')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.table = self.root / 'mounts'
        self.table.write_text(self.TABLE, encoding='utf-8')
        self.patchers = [
            patch.object(fsutil, 'MOUNT_TABLES', (str(self.table),)),
            patch.object(fsutil, '_statvfs_callable', return_value=self._linux_statvfs),
            # Linux has no `statfs` name lookup through libc here; the mount table
            # is the only source, exactly as on the real host.
            patch.object(fsutil, '_darwin_fs_type', return_value='unknown'),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def _linux_statvfs(path):
        """Linux-shaped result: no `f_fstypename` field at all."""
        return SimpleNamespace(f_bsize=4096, f_frsize=4096, f_blocks=1000,
                               f_bfree=500, f_bavail=400)

    def test_nfs_and_cifs_mounts_are_remote(self):
        self.assertEqual(fsutil.fs_type('/mnt/share/logs'), 'nfs4')
        self.assertTrue(fsutil.is_remote_path('/mnt/share/logs'))
        self.assertEqual(fsutil.fs_type('/mnt/team/logs'), 'cifs')
        self.assertTrue(fsutil.is_remote_path('/mnt/team/logs'))

    def test_fuse_transports_are_remote(self):
        self.assertEqual(fsutil.fs_type('/mnt/remote/bucket'), 'fuse.rclone')
        self.assertTrue(fsutil.is_remote_path('/mnt/remote/bucket'))

    def test_local_linux_mounts_are_not_remote(self):
        self.assertEqual(fsutil.fs_type('/srv/containers/abc/merged/volume'), 'overlay')
        self.assertFalse(fsutil.is_remote_path('/srv/containers/abc/merged/volume'))
        self.assertFalse(fsutil.is_remote_path('/run/lock'))

    def test_longest_mount_point_wins_over_the_root(self):
        """`/` is also a prefix; the deeper mount must be the answer."""
        self.assertEqual(fsutil.fs_type('/mnt/share/deeply/nested/path'), 'nfs4')

    def test_octal_escapes_are_decoded_before_matching(self):
        self.assertEqual(fsutil._unescape_mount('/mnt/My\\040Disk'), '/mnt/My Disk')
        self.assertEqual(fsutil._unescape_mount('/a\\011b'), '/a\tb')
        self.assertEqual(fsutil._unescape_mount('/a\\134b'), '/a\\b')
        self.assertEqual(fsutil._unescape_mount('/a\\q'), '/a\\q')
        self.assertEqual(fsutil.fs_type('/mnt/My Disk/logs'), 'ext4')

    def test_a_symlinked_root_is_resolved_before_matching(self):
        mount = self.root / 'mounted'
        mount.mkdir()
        link = self.root / 'link'
        try:
            link.symlink_to(mount, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - restricted host
            self.skipTest('symlinks are not permitted here')
        table = self.root / 'mounts2'
        table.write_text('/dev/sda1 / ext4 rw 0 0\n/dev/sdx1 %s nfs4 rw 0 0\n' % os.path.realpath(str(mount)),
                         encoding='utf-8')
        with patch.object(fsutil, 'MOUNT_TABLES', (str(table),)):
            self.assertEqual(fsutil.fs_type(link), 'nfs4')
            self.assertTrue(fsutil.is_remote_path(link))

    def test_local_volume_under_volumes_is_not_refused_on_linux(self):
        """`/Volumes` is a macOS convention; on Linux it is an ordinary directory."""
        from server.indexing.shards import assert_local_fs
        table = self.root / 'mounts3'
        table.write_text('/dev/sda1 / ext4 rw 0 0\n/dev/sdc1 /Volumes/data ext4 rw 0 0\n',
                         encoding='utf-8')
        with patch.object(fsutil, 'MOUNT_TABLES', (str(table),)):
            self.assertEqual(fsutil.fs_type('/Volumes/data/logs'), 'ext4')
            with patch('server.indexing.shards.sys.platform', 'linux'):
                assert_local_fs(Path('/Volumes/data/logs'))

    def test_unreadable_table_reports_unknown_and_never_raises(self):
        with patch.object(fsutil, 'MOUNT_TABLES', (str(self.root / 'absent'),)):
            self.assertEqual(fsutil.fs_type(self.root), 'unknown')
            self.assertFalse(fsutil.is_remote_path(self.root))
            self.assertEqual(fsutil._read_mount_table(), [])


class GuardProbeTests(unittest.TestCase):
    """The probe is where the original crash surfaced: it called statvfs twice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_reports_usage_when_statvfs_is_absent(self):
        from tests.test_r6_storage_guard import make_app
        app = make_app(self.root / 'deploy')
        try:
            guard = app.storage.guard
            root = guard.root('legacy')
            with patch.object(fsutil, '_statvfs_callable', return_value=None):
                state = guard.probe(root)
        finally:
            app.index_jobs.close()
            app.queries.close()
        self.assertEqual(state['state'], 'online')
        self.assertGreater(state['total_bytes'], 0)
        self.assertGreaterEqual(state['free_bytes'], 0)
        self.assertIsInstance(state['fs_type'], str)
        self.assertTrue(state['fs_type'])


if __name__ == '__main__':
    unittest.main()
