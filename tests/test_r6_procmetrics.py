"""Platform-neutral process metrics: no `resource`, no `sysconf`, no `uname`.

The regression this pins down: `server/runtime/api.py` used `import resource` at
module scope, which is a `ModuleNotFoundError` on Windows and therefore a startup
failure — not a missing number. `os.uname()` and `os.sysconf()` are the same class
of mistake one level down.

The absence of `resource` is simulated by putting `None` in `sys.modules`, which is
exactly how CPython reports a module that an interpreter does not have; the check
runs in a subprocess so the reload cannot disturb the rest of the suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.runtime import procmetrics
from server.storage import fsutil, guard


class ProcMetricsTests(unittest.TestCase):
    def test_peak_rss_is_a_positive_byte_count(self):
        value = procmetrics.peak_rss_bytes()
        self.assertIsNotNone(value)
        self.assertGreater(value, 1024 * 1024)

    def test_rss_source_names_the_mechanism(self):
        self.assertIn(procmetrics.rss_source(), ('ru_maxrss', 'peak_working_set', 'unavailable'))

    def test_physical_memory_is_positive(self):
        value = procmetrics.physical_memory_bytes()
        self.assertIsNotNone(value)
        self.assertGreater(value, 1024 * 1024 * 64)

    def test_affinity_is_none_or_a_real_mask_size(self):
        value = procmetrics.cpu_affinity_count()
        if hasattr(os, 'sched_getaffinity'):
            self.assertEqual(value, max(1, len(os.sched_getaffinity(0))))
        else:  # pragma: no cover - macOS/Windows
            self.assertIsNone(value)

    def test_missing_resource_module_degrades_to_none(self):
        with patch.object(procmetrics, '_resource', None):
            if sys.platform == 'win32':  # pragma: no cover - ctypes path
                self.assertEqual(procmetrics.rss_source(), 'peak_working_set')
            else:
                self.assertIsNone(procmetrics.peak_rss_bytes())
                self.assertEqual(procmetrics.rss_source(), 'unavailable')


class MissingResourceModuleTests(unittest.TestCase):
    def test_runtime_api_imports_and_reports_null(self):
        script = (
            'import sys\n'
            "sys.modules['resource'] = None\n"
            'from server.runtime import api\n'
            'print(api._rss_bytes())\n'
            'print(api._memory_bytes() is not None)\n'
            'print(api._cpu_limit() > 0)\n'
        )
        result = subprocess.run([sys.executable, '-c', script], cwd=str(ROOT),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.split()
        self.assertEqual(lines[0], 'None')
        self.assertEqual(lines[1], 'True')
        self.assertEqual(lines[2], 'True')

    def test_degraded_module_still_answers_the_status_page(self):
        script = (
            'import sys\n'
            "sys.modules['resource'] = None\n"
            "from server.capabilities import probe_cpu\n"
            'info = probe_cpu()\n'
            'print(info["affinity_count"] > 0)\n'
            'print(info["affinity_source"] in ("cgroup", "logical_cores"))\n'
        )
        result = subprocess.run([sys.executable, '-c', script], cwd=str(ROOT),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ['True', 'True'])


class CgroupLimitTests(unittest.TestCase):
    """Container limits on Linux: cgroup v2 and v1, read from injected files.

    `os.cpu_count()` reports the host and `sched_getaffinity` only reflects a
    cpuset, so a quota-only container would otherwise be handed the whole
    machine's job slots. The v1 sentinel for "unlimited" is a number, not a word,
    which is the trap these cases pin down.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _file(self, name, content):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return path

    def test_v2_cpu_quota_is_rounded_up_to_whole_cpus(self):
        with patch.object(procmetrics, 'CGROUP_V2_CPU',
                          self._file('cpu.max', '250000 100000')):
            self.assertEqual(procmetrics.cgroup_cpu_quota(), 3)

    def test_v2_cpu_unlimited_is_none(self):
        with patch.object(procmetrics, 'CGROUP_V2_CPU',
                          self._file('cpu.max', 'max 100000')):
            self.assertIsNone(procmetrics.cgroup_cpu_quota())

    def test_v1_cpu_quota_is_used_when_v2_is_absent(self):
        with patch.object(procmetrics, 'CGROUP_V2_CPU', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_QUOTA',
                             self._file('quota', '50000')), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_PERIOD',
                             self._file('period', '100000')):
            self.assertEqual(procmetrics.cgroup_cpu_quota(), 1)

    def test_v1_unlimited_cpu_quota_is_none(self):
        with patch.object(procmetrics, 'CGROUP_V2_CPU', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_QUOTA',
                             self._file('quota', '-1')), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_PERIOD',
                             self._file('period', '100000')):
            self.assertIsNone(procmetrics.cgroup_cpu_quota())

    def test_v2_memory_limit_is_read_in_bytes(self):
        with patch.object(procmetrics, 'CGROUP_V2_MEMORY',
                          self._file('memory.max', '1073741824')):
            self.assertEqual(procmetrics.cgroup_memory_limit(), 1073741824)

    def test_v2_memory_unlimited_is_none(self):
        with patch.object(procmetrics, 'CGROUP_V2_MEMORY',
                          self._file('memory.max', 'max')):
            self.assertIsNone(procmetrics.cgroup_memory_limit())

    def test_v1_memory_sentinel_counts_as_unlimited(self):
        with patch.object(procmetrics, 'CGROUP_V2_MEMORY', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_MEMORY',
                             self._file('limit', '9223372036854771712')):
            self.assertIsNone(procmetrics.cgroup_memory_limit())

    def test_no_cgroup_files_means_not_measured(self):
        with patch.object(procmetrics, 'CGROUP_V2_MEMORY', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_MEMORY', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V2_CPU', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_QUOTA', self.root / 'absent'), \
                patch.object(procmetrics, 'CGROUP_V1_CPU_PERIOD', self.root / 'absent'):
            self.assertIsNone(procmetrics.cgroup_memory_limit())
            self.assertIsNone(procmetrics.cgroup_cpu_quota())

    def test_cpu_limit_takes_the_smaller_of_affinity_and_quota(self):
        from server.runtime import api
        with patch.object(procmetrics, 'cpu_affinity_count', return_value=16), \
                patch.object(procmetrics, 'cgroup_cpu_quota', return_value=2):
            self.assertEqual(api._cpu_limit(), 2)
        with patch.object(procmetrics, 'cpu_affinity_count', return_value=4), \
                patch.object(procmetrics, 'cgroup_cpu_quota', return_value=None):
            self.assertEqual(api._cpu_limit(), 4)

    def test_probe_cpu_names_the_container_limits(self):
        from server.capabilities import probe_cpu
        info = probe_cpu()
        self.assertIn('cgroup_memory_max', info)
        self.assertIn('cgroup_cpu_quota', info)


class RemotePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_local_temporary_directory_is_not_remote(self):
        self.assertFalse(fsutil.is_remote_path(self.root))

    def test_unc_prefixed_path_is_remote_on_any_platform(self):
        self.assertTrue(fsutil._windows_is_remote(r'\\fileserver\team-logs'))
        self.assertTrue(fsutil._windows_is_remote('//fileserver/team-logs'))

    def test_drive_letter_on_this_platform_is_not_claimed_remote(self):
        # On POSIX the ctypes branch is unreachable, so the answer is "not remote"
        # rather than an exception; on Windows a local drive answers the same way.
        self.assertFalse(fsutil._windows_is_remote(str(self.root)))

    def test_guard_uses_the_shared_name_set(self):
        self.assertIs(guard.NETWORK_FS, fsutil.NETWORK_FS_NAMES)
        self.assertFalse(guard.is_network_fs(self.root))


if __name__ == '__main__':
    unittest.main()
