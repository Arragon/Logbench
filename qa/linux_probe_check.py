"""Linux smoke check for the platform probes behind storage and the status page.

The unit suite runs everywhere and simulates the missing-symbol cases. This script
is the complementary half: it asserts the *real* Linux host answers correctly,
because two behaviours cannot be simulated faithfully on macOS —

* whether the mount table is readable and names this host's file systems;
* whether the cgroup limits (v1 or v2) are visible from inside the process.

Run it on the deployment target, or in the published image:

    python3 qa/linux_probe_check.py
    docker run --rm -v "$PWD":/app -w /app python:3.12-slim python qa/linux_probe_check.py

Exit code 0 means every check passed; 1 means at least one did not.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.capabilities import probe_cpu                       # noqa: E402
from server.runtime import api, procmetrics                     # noqa: E402
from server.storage import fsutil                               # noqa: E402
from server.indexing.shards import assert_local_fs, ShardError   # noqa: E402

FAILED = []
LINUX = sys.platform.startswith('linux')


def check(label, condition, detail=''):
    mark = 'ok  ' if condition else 'FAIL'
    print('%s %-46s %s' % (mark, label, detail))
    if not condition:
        FAILED.append(label)


def linux_only(check_fn):
    """Run a Linux-specific assertion, or report it as skipped elsewhere."""
    if LINUX:
        check_fn()
    else:
        print('skip %-46s not Linux' % check_fn.__name__)


def main() -> int:
    if not LINUX:
        print('note: this host is %s — Linux-only checks are skipped\n' % sys.platform)
    check('os.name is posix', os.name == 'posix', os.name)

    def mount_table():
        rows = fsutil._read_mount_table()
        check('mount table readable', bool(rows), '%d entries' % len(rows))
        root_type = fsutil.fs_type('/')
        check('root file system is named', root_type not in ('', 'unknown'), root_type)
        check('root is not remote', not fsutil.is_remote_path('/'), root_type)

    linux_only(mount_table)

    usage = fsutil.disk_usage('/')
    check('root usage is measured', usage is not None and usage.total > 0,
          '' if usage is None else 'total=%d free=%d avail=%d'
          % (usage.total, usage.free, usage.available))
    if usage is not None and getattr(os, 'statvfs', None) is not None:
        raw = os.statvfs('/')
        expected = raw.f_blocks * (raw.f_frsize or raw.f_bsize)
        check('statvfs agrees with the probe', usage.total == expected,
              'statvfs=%d' % expected)
        check('f_bavail is preserved as available',
              usage.available == raw.f_bavail * (raw.f_frsize or raw.f_bsize))

    check('temporary directory is local', not fsutil.is_remote_path('/tmp'))
    try:
        assert_local_fs(ROOT)
        local_ok = True
    except ShardError as error:
        local_ok = False
        print('     shard root refused: %s' % error.message)
    check('application directory passes the shard check', local_ok, str(ROOT))

    rss = procmetrics.peak_rss_bytes()
    check('peak RSS in bytes', bool(rss) and rss > 1024 * 1024,
          '' if rss is None else '%.1f MiB' % (rss / (1024.0 * 1024.0)))
    check('rss source is ru_maxrss', procmetrics.rss_source() == 'ru_maxrss',
          procmetrics.rss_source())

    memory = procmetrics.physical_memory_bytes()
    check('physical memory measured', bool(memory) and memory > 64 * 1024 * 1024,
          '' if memory is None else '%.1f GiB' % (memory / float(1 << 30)))

    affinity = procmetrics.cpu_affinity_count()

    def affinity_mask():
        check('affinity mask is usable', bool(affinity), str(affinity))

    linux_only(affinity_mask)

    quota = procmetrics.cgroup_cpu_quota()
    limit = procmetrics.cgroup_memory_limit()
    print('     cgroup cpu quota: %s | cgroup memory limit: %s'
          % (quota if quota is not None else 'unbounded',
             limit if limit is not None else 'unbounded'))
    effective = api._cpu_limit()
    if affinity:
        check('effective cpu limit respects the affinity mask', effective <= affinity,
              'effective=%d affinity=%d' % (effective, affinity))
    check('cpu limit is at least one', effective >= 1, str(effective))

    info = probe_cpu()
    check('capabilities report the container limits',
          'cgroup_memory_max' in info and 'cgroup_cpu_quota' in info)
    check('affinity count is positive', (info.get('affinity_count') or 0) > 0,
          str(info.get('affinity_count')))

    print('')
    if FAILED:
        print('%d check(s) failed: %s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
