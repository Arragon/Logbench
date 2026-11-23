"""Disk statistics and file-system naming for every supported runtime.

`os.statvfs` is a POSIX-only symbol. On Windows the attribute does not exist at
all, so a direct `os.statvfs(...)` call fails with `module 'os' has no attribute
'statvfs'` before any fallback can run — the probe never gets a chance to
degrade. The storage package needs the same three numbers on every supported
interpreter (3.9 and 3.12 alike), so the probe lives here exactly once:

* POSIX — `os.statvfs` when it exists: block counts give the real `available`
  figure (`f_bavail`), which is what "can this root still take a write" means;
* Windows, or any interpreter without `statvfs` — `shutil.disk_usage`, which is
  `GetDiskFreeSpaceExW` underneath and present since 3.3.

Naming the file system is a separate problem with three answers: `statvfs` on BSD
builds that carry `f_fstypename`, `statfs` on macOS (its `statvfs` result holds
only the ten POSIX fields), and the mount table on Linux. See `fs_type`.

Nothing in this module raises. A missing path, a refused permission or an
unsupported call yields `None` (usage) or `'unknown'` (file-system name); callers
decide what an absent measurement means. `guard.py` treats it as "not enough
evidence to write", which is the conservative answer.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import NamedTuple, Optional


class DiskUsage(NamedTuple):
    """`shutil.disk_usage` plus the POSIX `f_bavail` figure.

    `free` is total free space, `available` is what the calling user may
    actually consume — the two differ when a POSIX volume holds reserved blocks
    for root. On Windows both are the same number, by definition.
    """

    total: int
    used: int
    free: int
    available: int


#: File systems where a SQLite index must not live (design §9: WAL is
#: single-writer and does not survive a shared network file system). The names are
#: the POSIX spellings — Linux mount tables call SMB `cifs`/`smb3`, macOS calls it
#: `smbfs`, and FUSE transports carry a `fuse.` prefix. The Windows equivalent is a
#: drive type, not a name, which is why `is_remote_path` exists instead of a
#: second list.
NETWORK_FS_NAMES = frozenset({
    'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'smb', 'afpfs', 'afs',
    'webdav', 'davfs', 'sshfs', 'fuse.sshfs', 'fuse.rclone', 'fuse.smbnetfs',
    'fuse.afpfs', 'fuse.davfs2', 'rclone',
    '9p', 'ceph', 'cephfs', 'glusterfs', 'gpfs', 'lustre', 'beegfs',
    'moosefs', 'orangefs', 'coda', 'ncpfs',
})


def _statvfs_callable():
    """`os.statvfs`, or `None` where the platform has no such symbol."""
    return getattr(os, 'statvfs', None)


def disk_usage(path) -> Optional[DiskUsage]:
    """Total / used / free / available bytes for `path`, or `None`.

    The `statvfs` branch is attempted first so POSIX keeps its exact reserved
    block accounting; the `shutil` branch is the Windows path and the safety net
    for an interpreter that reports `statvfs` as unimplemented.
    """
    target = str(path)
    statvfs = _statvfs_callable()
    if statvfs is not None:
        try:
            raw = statvfs(target)
        except (AttributeError, NotImplementedError):
            raw = None
        except (OSError, ValueError, TypeError):
            return None
        if raw is not None:
            block = getattr(raw, 'f_frsize', 0) or getattr(raw, 'f_bsize', 0)
            blocks = getattr(raw, 'f_blocks', None)
            if block and blocks is not None:
                total = int(blocks) * int(block)
                free = int(getattr(raw, 'f_bfree', 0)) * int(block)
                available = int(getattr(raw, 'f_bavail', 0)) * int(block)
                return DiskUsage(total, total - free, free, available)
    try:
        raw = shutil.disk_usage(target)
    except (OSError, ValueError, TypeError, NotImplementedError):
        return None
    total = int(raw.total)
    free = int(raw.free)
    return DiskUsage(total, max(total - free, 0), free, free)


def free_bytes(path) -> Optional[int]:
    """Bytes the current user can still write to the volume holding `path`."""
    usage = disk_usage(path)
    return None if usage is None else usage.available


def fs_type(path) -> str:
    """Best-effort file-system name. `unknown` is reported, never guessed."""
    if os.name == 'nt':
        return _windows_fs_type(path)
    return _posix_fs_type(path)


def is_remote_path(path) -> bool:
    """True when `path` is on a network file system, on the platform's own terms.

    A single name list cannot answer this on Windows: `GetVolumeInformationW`
    on an SMB share returns the *remote* file system name (`NTFS`, `SMB`), so the
    path would be classified as local and a SQLite index could be placed on a
    share — the failure design §9 forbids. Windows therefore asks the drive type
    (and treats any `\\\\server\\share` path as remote). POSIX cannot use one
    source either: Linux reads `/proc/mounts`, while macOS has to ask `statfs`
    because its `statvfs` result omits the file-system name.
    """
    if os.name == 'nt':
        return _windows_is_remote(path)
    return fs_type(path) in NETWORK_FS_NAMES


#: Mount tables in preference order. `/proc/mounts` and `/proc/self/mounts` are the
#: same file on Linux; `/etc/mtab` covers a container whose `/proc` is restricted
#: (and older systems where mtab was a real file). None of them exists on macOS.
MOUNT_TABLES = ('/proc/mounts', '/proc/self/mounts', '/etc/mtab')


def _unescape_mount(field: str) -> str:
    """Decode the octal escapes `mount(8)` writes into the table.

    Space, tab, newline and backslash cannot appear literally in a fstab-style
    field, so a mount point such as `/mnt/My Disk` is written `/mnt/My\\040Disk`.
    Only `\\040` was handled before, which meant a mount point containing a space
    or a backslash never matched and the file system was reported as `unknown`.
    """
    if '\\' not in field:
        return field
    raw = field.encode('utf-8')
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == 0x5C and index + 4 <= len(raw):
            digits = raw[index + 1:index + 4]
            if all(0x30 <= digit <= 0x37 for digit in digits):
                out.append((digits[0] - 0x30) * 64 + (digits[1] - 0x30) * 8
                           + (digits[2] - 0x30))
                index += 4
                continue
        out.append(byte)
        index += 1
    return out.decode('utf-8', errors='replace')


def _read_mount_table():
    """`(mount point, fs type)` pairs from the first readable table; `[]` if none.

    Never raises: the caller treats an empty table as "no evidence", which keeps
    the old conservative behaviour on a platform that has no table at all.
    """
    for name in MOUNT_TABLES:
        try:
            with open(name, 'r', encoding='utf-8', errors='replace') as handle:
                rows = []
                for line in handle:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    rows.append((_unescape_mount(parts[1]), parts[2].lower()))
                return rows
        except OSError:
            continue
    return []


def _posix_fs_type(path) -> str:
    target = str(path)
    statvfs = _statvfs_callable()
    if statvfs is not None:
        try:
            info = statvfs(target)
        except (AttributeError, NotImplementedError):
            info = None
        except (OSError, ValueError, TypeError):
            return 'unknown'
        if info is not None:
            # BSD/macOS expose the name directly — when the interpreter carries
            # that extension; most macOS builds do not.
            name = getattr(info, 'f_fstypename', None)
            if name:
                return str(name).lower()
    if sys.platform == 'darwin':
        name = _darwin_fs_type(target)
        if name != 'unknown':
            return name
    # Linux has no `f_fstypename`: longest matching mount point wins. The path is
    # resolved first because a storage root is often a symlink onto another
    # volume, and the mount table only knows the real location.
    resolved = os.path.realpath(target)
    best, best_len = 'unknown', -1
    for mount, kind in _read_mount_table():
        if resolved == mount or resolved.startswith(mount.rstrip('/') + '/'):
            if len(mount) > best_len:
                best, best_len = kind, len(mount)
    return best


#: `struct statfs` from macOS `<sys/mount.h>`, complete. It must be complete:
#: `statfs(2)` writes all 2168 bytes, so a truncated declaration corrupts the
#: heap instead of failing loudly. `_STATFS_SIZE` is the ABI size on both x86_64
#: and arm64 and is checked before the layout is trusted.
_STATFS = None
_STATFS_SIZE = 2168


def _statfs_class(ctypes):
    global _STATFS
    if _STATFS is None:
        class Statfs(ctypes.Structure):
            _fields_ = [('f_bsize', ctypes.c_uint32), ('f_iosize', ctypes.c_int32),
                        ('f_blocks', ctypes.c_uint64), ('f_bfree', ctypes.c_uint64),
                        ('f_bavail', ctypes.c_uint64), ('f_files', ctypes.c_uint64),
                        ('f_ffree', ctypes.c_uint64), ('f_fsid', ctypes.c_int32 * 2),
                        ('f_owner', ctypes.c_uint32), ('f_type', ctypes.c_uint32),
                        ('f_flags', ctypes.c_uint32), ('f_fssubtype', ctypes.c_uint32),
                        ('f_fstypename', ctypes.c_char * 16),
                        ('f_mntonname', ctypes.c_char * 1024),
                        ('f_mntfromname', ctypes.c_char * 1024),
                        ('f_flags_ext', ctypes.c_uint32),
                        ('f_reserved', ctypes.c_uint32 * 7)]
        _STATFS = Statfs
    return _STATFS


def _darwin_fs_type(path) -> str:
    """`statfs(2)` for the volume name; `unknown` on any refusal.

    CPython's `statvfs_result` carries only the ten POSIX-standard fields, so the
    BSD `f_fstypename` is not exposed on macOS at all — `getattr` returns `None`
    and, without this call, *every* macOS path would be reported as `unknown`.
    That made the network check unable to refuse an SMB volume, which is the one
    thing it exists for. `statfs` is in libc and its layout is stable, which is
    why the size is verified rather than assumed.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover - ctypes is always present on CPython
        return 'unknown'
    try:
        statfs_type = _statfs_class(ctypes)
        if ctypes.sizeof(statfs_type) != _STATFS_SIZE:
            return 'unknown'
        buffer = statfs_type()
        if ctypes.CDLL(None).statfs(ctypes.c_char_p(os.fsencode(str(path))),
                                    ctypes.byref(buffer)) != 0:
            return 'unknown'
        name = buffer.f_fstypename
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        return str(name).split('\x00')[0].strip().lower() or 'unknown'
    except (AttributeError, OSError, ValueError):
        return 'unknown'


def _windows_fs_type(path) -> str:
    """`GetVolumeInformationW` for the volume root; `unknown` on any refusal."""
    try:
        import ctypes
    except ImportError:  # pragma: no cover - ctypes is always present on CPython
        return 'unknown'
    try:
        drive, _rest = os.path.splitdrive(os.path.abspath(str(path)))
        # A drive root ("C:\\") or a UNC share root ("\\\\server\\share\\") is
        # required; the volume API rejects deeper paths.
        root = drive + '\\' if drive else os.path.abspath(str(path)) + '\\'
        name = ctypes.create_unicode_buffer(261)
        kernel32 = ctypes.windll.kernel32
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, name, len(name))
        if ok and name.value:
            return name.value.strip().lower()
    except (AttributeError, OSError, ValueError):
        return 'unknown'
    return 'unknown'


DRIVE_REMOTE = 4


def _windows_is_remote(path) -> bool:
    """UNC prefix, or a drive that `GetDriveTypeW` reports as `DRIVE_REMOTE`."""
    text = str(path)
    if text.startswith('\\\\') or text.startswith('//'):
        return True
    try:
        import ctypes
    except ImportError:  # pragma: no cover
        return False
    try:
        drive, _rest = os.path.splitdrive(os.path.abspath(text))
        if not drive:
            return False
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive + '\\'))
        return int(kind) == DRIVE_REMOTE
    except (AttributeError, OSError, ValueError):
        return False
