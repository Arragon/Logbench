"""Process and machine metrics without assuming a POSIX interpreter.

`resource`, `os.sysconf`, `os.sched_getaffinity` and `os.uname` are Unix-only
symbols. On Windows the first is a missing *module* (the import fails, taking any
caller with it) and the rest are missing attributes, so a probe written as
`import resource` at the top of a module is a startup failure, not a degraded
metric.

Every function here is total: it returns a number or `None`, never raises. That
matters because these numbers feed a status page whose rule is "when there is no
measurement, report `null`, never `0`".

Measurement per platform:

* peak RSS — `resource.getrusage(RUSAGE_SELF).ru_maxrss` on POSIX (bytes on
  macOS, KiB elsewhere); `GetProcessMemoryInfo` peak working set on Windows,
  which is the same quantity;
* physical memory — `os.sysconf` on POSIX, `/proc/meminfo` as the Linux fallback,
  `GlobalMemoryStatusEx` on Windows;
* CPU affinity — `os.sched_getaffinity` on Linux; `None` elsewhere, because a
  macOS or Windows container has no cgroup CPU quota to report;
* container limits — cgroup v2 (`cpu.max`, `memory.max`) and cgroup v1
  (`cpu.cfs_quota_us`, `memory.limit_in_bytes`), read only where those files
  exist. A CPU-quota-only container still reports the host's core count through
  `os.cpu_count()` and an unrestricted affinity mask, so the quota has to be
  read separately to keep the job budget honest.

`cgroup_memory_limit()` and `cgroup_cpu_quota()` are Linux-specific by nature:
on macOS and Windows the paths do not exist and both return `None`, which the
budget code reads as "no limit was measured" rather than "unlimited".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

try:  # Unix: the only way to read peak RSS without a subprocess.
    import resource as _resource
except ImportError:  # Windows has no `resource` module at all.
    _resource = None

_MEMINFO = Path('/proc/meminfo')

#: cgroup v2 is the unified hierarchy; v1 is still what RHEL/CentOS 7 and older
#: Ubuntu images ship. Both are probed — a host runs one of them, and an absent
#: file must read as "not measured", never as "no limit".
CGROUP_V2_MEMORY = Path('/sys/fs/cgroup/memory.max')
CGROUP_V1_MEMORY = Path('/sys/fs/cgroup/memory/memory.limit_in_bytes')
CGROUP_V2_CPU = Path('/sys/fs/cgroup/cpu.max')
CGROUP_V1_CPU_QUOTA = Path('/sys/fs/cgroup/cpu/cpu.cfs_quota_us')
CGROUP_V1_CPU_PERIOD = Path('/sys/fs/cgroup/cpu/cpu.cfs_period_us')

#: cgroup v1 reports "unlimited" as a near-2^63 sentinel rather than a word, so
#: anything at or above 2^60 bytes is treated as unlimited.
_UNLIMITED_BYTES = 1 << 60


def _macos_bytes() -> bool:
    """`ru_maxrss` is bytes on macOS and KiB on Linux/BSD. Never guess by size."""
    return sys.platform == 'darwin'


def peak_rss_bytes() -> Optional[int]:
    """Peak resident set size of this process in bytes."""
    if _resource is not None:
        try:
            value = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        except (AttributeError, ValueError, OSError):
            return None
        if value is None:
            return None
        return int(value) if _macos_bytes() else int(value) * 1024
    return _windows_peak_rss()


def rss_source() -> str:
    """Where the RSS number came from, for the diagnostics page."""
    if _resource is not None:
        return 'ru_maxrss'
    return 'peak_working_set' if sys.platform == 'win32' else 'unavailable'


def cpu_affinity_count() -> Optional[int]:
    """CPUs this process may run on, or `None` where the platform cannot say.

    Linux reports the cgroup quota through the affinity mask; macOS and Windows
    do not, and returning `os.cpu_count()` here would silently claim a container
    limit that was never measured.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return None


def physical_memory_bytes() -> Optional[int]:
    """Total physical memory in bytes."""
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        size = os.sysconf('SC_PAGE_SIZE')
        if pages > 0 and size > 0:
            return int(pages) * int(size)
    except (AttributeError, ValueError, OSError):
        pass
    if _MEMINFO.is_file():
        try:
            for line in _MEMINFO.read_text('utf-8', 'replace').splitlines():
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
        except (OSError, IndexError, ValueError):
            return None
    return _windows_total_memory()


# --------------------------------------------------------------------- cgroups
def _read_text(path) -> Optional[str]:
    """Whole file, stripped, or `None` when it cannot be read."""
    try:
        return Path(path).read_text('utf-8', 'replace').strip()
    except (OSError, ValueError):
        return None


def cgroup_memory_limit() -> Optional[int]:
    """Container memory limit in bytes; `None` when unlimited or not cgroup-managed."""
    for path in (CGROUP_V2_MEMORY, CGROUP_V1_MEMORY):
        raw = _read_text(path)
        if not raw or raw == 'max':
            continue
        try:
            limit = int(raw)
        except ValueError:
            continue
        if 0 < limit < _UNLIMITED_BYTES:
            return limit
    return None


def cgroup_cpu_quota() -> Optional[int]:
    """CPUs this container may actually burn, rounded up; `None` when unbounded.

    `os.cpu_count()` on Linux reports the host's cores and `sched_getaffinity`
    only shrinks that when a cpuset is pinned — a container limited by CPU quota
    alone would otherwise be handed the whole machine's worth of job slots.
    """
    raw = _read_text(CGROUP_V2_CPU)
    if raw:
        parts = raw.split()
        if len(parts) >= 2 and parts[0] != 'max':
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                quota = period = 0
            if quota > 0 and period > 0:
                return max(1, -(-quota // period))
    quota_raw = _read_text(CGROUP_V1_CPU_QUOTA)
    period_raw = _read_text(CGROUP_V1_CPU_PERIOD)
    if quota_raw and period_raw:
        try:
            quota, period = int(quota_raw), int(period_raw)
        except ValueError:
            return None
        if quota > 0 and period > 0:
            return max(1, -(-quota // period))
    return None


# --------------------------------------------------------------------- Windows
def _windows_peak_rss() -> Optional[int]:
    try:
        import ctypes
    except ImportError:  # pragma: no cover - CPython always ships ctypes
        return None
    try:
        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL('kernel32')
        psapi = ctypes.WinDLL('psapi')
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters),
                                        ctypes.sizeof(counters))
        if ok:
            return int(counters.PeakWorkingSetSize)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def _windows_total_memory() -> Optional[int]:
    try:
        import ctypes
    except ImportError:  # pragma: no cover
        return None
    try:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL('kernel32')
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except (AttributeError, OSError, ValueError):
        return None
    return None
