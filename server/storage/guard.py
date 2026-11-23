"""Root-scoped path safety, mount identity and offline write protection (R6-053).

The v3.0.3 cage assumed exactly one root (`data_dir`). With several roots, three
failures become possible that the old code never had to answer:

1. **A path escapes its root.** `..`, an absolute sub-path, a double-encoded
   separator or a symlink placed after the check. Normalisation alone is not
   enough; the resolved real path must still be under the root.
2. **The mount is gone but the mountpoint is not.** A power loss, an unmounted
   SMB share or a re-plugged disk leaves an empty directory (or, worse, the
   system disk) where the log root used to be. Writing there would scatter
   archives across the wrong device, so identity is recorded as a `volume_id`
   plus a sentinel file and re-checked before every write.
3. **A name the filesystem accepts but the product cannot represent.** Control
   characters, names over the limit and Windows-reserved shapes are mapped to a
   reversible physical name, and the original is kept — truncation alone would
   collide two different logs into one file.

Everything here is deliberately conservative: when the evidence is insufficient
(no sentinel, unknown file system, a descriptor check the platform does not
support) the dangerous operation is refused rather than attempted.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import BinaryIO, Optional

from .fsutil import NETWORK_FS_NAMES, disk_usage, fs_type, is_remote_path  # `os.statvfs` is POSIX-only; see fsutil.

ROOT_ID = r'^[a-z0-9][a-z0-9_-]{0,63}$'
KINDS = ('shared_raw', 'private_raw', 'index')
SENTINEL_NAME = '.logbench-root.json'
TRASH_NAME = '.logbench-trash'
PROBE_PREFIX = '.logbench-probe-'
MAX_COMPONENT_BYTES = 200
#: File systems where a SQLite index must not live (design §9: WAL is single-writer
#: and does not survive a shared network file system). Names are the POSIX
#: spellings; Windows answers through the drive type instead — see `is_network_fs`.
NETWORK_FS = NETWORK_FS_NAMES
_UNSAFE = set('\x7f<>:"\\|?*')


class StorageError(RuntimeError):
    """A storage failure with a stable code. `code` is part of the API contract."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------- names
def encode_component(name: str) -> str:
    """A reversible physical file name for an arbitrary logical name.

    Percent-style: every byte outside the portable set becomes `%XX`. Names that
    would still exceed the per-component limit are shortened and given a digest
    suffix; the caller must persist the logical name so the mapping stays
    reversible (see `catalog.name_map`).
    """
    out = []
    for character in name:
        if character in _UNSAFE or ord(character) < 0x20 or character == '%':
            out.append('%%%02X' % ord(character))
        else:
            out.append(character)
    encoded = ''.join(out)
    if len(encoded.encode('utf-8')) <= MAX_COMPONENT_BYTES:
        return encoded
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]
    keep = encoded.encode('utf-8')[:MAX_COMPONENT_BYTES - 12]
    # Cut on a character boundary so the physical name is still valid UTF-8.
    while keep:
        try:
            head = keep.decode('utf-8')
            break
        except UnicodeDecodeError:
            keep = keep[:-1]
    else:
        head = ''
    return head + '~' + digest


def decode_component(physical: str) -> str:
    """Inverse of `encode_component` for the escape part of the mapping."""
    out = bytearray()
    index = 0
    raw = physical.encode('utf-8')
    while index < len(raw):
        byte = raw[index]
        if byte == 0x25 and index + 2 < len(raw):
            try:
                out.append(int(raw[index + 1:index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass
        out.append(byte)
        index += 1
    return out.decode('utf-8', errors='replace')


# --------------------------------------------------------------------- probing
def is_network_fs(path: Path) -> bool:
    """Network file system by name (POSIX) or by drive type (Windows)."""
    return is_remote_path(path)


class StorageGuard:
    """Turn `(root, relative path)` into a verified real path — or refuse."""

    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------------ lookup
    def root_row(self, root_id: str):
        with self.app.store.connect() as db:
            row = db.execute('SELECT * FROM storage_roots WHERE id=?', (root_id,)).fetchone()
        if row is None:
            raise StorageError('unknown_root', '存储位置不存在。', root_id=root_id)
        return row

    def root(self, root_id: str) -> dict:
        row = self.root_row(root_id)
        return {
            'id': row['id'], 'kind': row['kind'], 'label': row['label'],
            'path': Path(row['path']), 'enabled': bool(row['enabled']),
            'revision': row['revision'],
            'volume_id': row['volume_id'] or '',
            'sentinel_id': row['sentinel_id'] or '',
            'quota_bytes': row['quota_bytes'] or 0,
            'probe_state': row['probe_state'] or 'unknown',
        }

    # -------------------------------------------------------------- path shape
    @staticmethod
    def normal_rel(raw, *, allow_root: bool = True) -> str:
        """A strict, root-relative path. Mirrors `Application.normal_path` on purpose.

        Kept separate because roots differ in what they may contain: an index root
        has no user-visible path at all, while a raw root must still refuse hidden
        segments — those are reserved for the sentinel and the recycling bin.
        """
        if not isinstance(raw, str):
            raise StorageError('invalid_path', '路径必须是文本。')
        if len(raw) > 2048:
            raise StorageError('invalid_path', '路径过长。')
        if raw.startswith('/') or '\\' in raw or ':' in raw or '\x00' in raw:
            raise StorageError('invalid_path', '请输入存储位置内的相对路径。')
        parts = raw.split('/') if raw else []
        for part in parts:
            if not part or part in ('.', '..'):
                raise StorageError('invalid_path', '路径不能包含空段或上级目录。')
            if part.startswith('.'):
                raise StorageError('invalid_path', '路径不能包含隐藏目录。')
            if any(ord(character) < 32 for character in part):
                raise StorageError('invalid_path', '路径包含控制字符。')
        if not parts and not allow_root:
            raise StorageError('invalid_path', '不能对存储位置根目录执行此操作。')
        return '/'.join(parts)

    def path_for(self, root: dict, rel: str) -> Path:
        """Join without touching the disk, then prove the result stays inside."""
        base = Path(os.path.realpath(str(root['path'])))
        target = base.joinpath(*[part for part in rel.split('/') if part])
        try:
            target.relative_to(base)
        except ValueError:
            raise StorageError('path_escape', '路径超出存储位置范围。', path=rel)
        return target

    # ------------------------------------------------------------- mount state
    def state(self, root: dict) -> dict:
        """`online` / `offline` with a stable reason. Never raises."""
        base = Path(root['path'])
        if not root['enabled']:
            return {'state': 'disabled', 'reason': 'disabled'}
        try:
            info = os.lstat(str(base))
        except FileNotFoundError:
            return {'state': 'offline', 'reason': 'missing'}
        except OSError:
            return {'state': 'offline', 'reason': 'unreadable'}
        if stat.S_ISLNK(info.st_mode):
            # A registered root that becomes a link is either a misconfiguration
            # or a swap attack; both mean "do not follow".
            return {'state': 'offline', 'reason': 'became_symlink'}
        if not stat.S_ISDIR(info.st_mode):
            return {'state': 'offline', 'reason': 'not_a_directory'}
        if root['volume_id'] and str(info.st_dev) != str(root['volume_id']):
            return {'state': 'offline', 'reason': 'volume_changed',
                    'expected': root['volume_id'], 'actual': str(info.st_dev)}
        if root['sentinel_id']:
            sentinel = base / SENTINEL_NAME
            try:
                data = json.loads(sentinel.read_text('utf-8'))
            except (OSError, ValueError):
                return {'state': 'offline', 'reason': 'sentinel_missing'}
            if data.get('sentinel_id') != root['sentinel_id'] \
                    or data.get('root_id') not in (None, root['id']):
                return {'state': 'offline', 'reason': 'sentinel_mismatch'}
        return {'state': 'online', 'reason': None}

    def require_online(self, root: dict, *, writing: bool = False) -> dict:
        state = self.state(root)
        if state['state'] == 'disabled':
            raise StorageError('storage_root_disabled', '该存储位置已停用。', root_id=root['id'])
        if state['state'] != 'online':
            # The caller gets a specific code, not a generic 500: the UI must be
            # able to say "offline, registration kept" instead of "empty library".
            raise StorageError('storage_root_offline', '存储位置当前不可用，已保留登记信息。',
                               root_id=root['id'], reason=state['reason'],
                               writing=writing)
        return state

    def free_bytes(self, root: dict) -> Optional[int]:
        usage = disk_usage(root['path'])
        return None if usage is None else usage.available

    # ---------------------------------------------------------------- resolve
    def resolve(self, root: dict, rel: str, *, must_exist: bool = True,
                want: str = 'any') -> Path:
        """Resolve a relative path inside `root`, refusing links and escapes.

        Symlinks are checked component by component with `lstat`, so a link in the
        middle of the path cannot be used to reach outside the root. This is an
        advisory check for metadata reads; `open_regular` re-verifies with a
        descriptor, which is what actually closes the TOCTOU window.
        """
        self.require_online(root)
        rel = self.normal_rel(rel, allow_root=True)
        base = Path(os.path.realpath(str(root['path'])))
        current = base
        parts = [part for part in rel.split('/') if part]
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = os.lstat(str(current))
            except FileNotFoundError:
                if must_exist:
                    raise StorageError('not_found', '文件或目录不存在。', path=rel)
                if index == len(parts) - 1:
                    break
                raise StorageError('not_found', '上级目录不存在。', path=rel)
            except OSError:
                raise StorageError('storage_root_offline', '路径不可读取。',
                                   root_id=root['id'], path=rel)
            if stat.S_ISLNK(info.st_mode):
                raise StorageError('symlink_denied', '存储位置不读取符号链接。', path=rel)
        if want == 'file' and not current.is_file():
            raise StorageError('not_a_file', '请选择一个日志文件。', path=rel)
        if want == 'dir' and not current.is_dir():
            raise StorageError('not_a_directory', '请选择一个目录。', path=rel)
        try:
            current.relative_to(base)
        except ValueError:
            raise StorageError('path_escape', '路径超出存储位置范围。', path=rel)
        return current

    def open_regular(self, root: dict, rel: str) -> BinaryIO:
        """Open a file for reading with descriptor-relative, no-follow access."""
        self.require_online(root)
        rel = self.normal_rel(rel, allow_root=False)
        base = Path(os.path.realpath(str(root['path'])))
        parts = [part for part in rel.split('/') if part]
        if os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            directory = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    os.close(directory)
                    directory = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise StorageError('not_a_file', '请选择常规文件。', path=rel)
                return os.fdopen(fd, 'rb')
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise StorageError('symlink_denied', '存储位置不读取符号链接。', path=rel)
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    raise StorageError('not_found', '文件不存在。', path=rel)
                raise
            finally:
                os.close(directory)
        # Platforms without O_NOFOLLOW still re-check right before opening.
        target = self.resolve(root, rel, want='file')
        return target.open('rb')

    # ------------------------------------------------------------------ probe
    def probe(self, root: dict) -> dict:
        """Verify identity and write access with the service account.

        The probe writes one byte into a uniquely named file and removes it. It
        does not `sudo`, `chmod` or mount anything: if the service account cannot
        write, that is the answer, not a problem to work around.
        """
        base = Path(root['path'])
        if not root['enabled']:
            raise StorageError('storage_root_disabled', '该存储位置已停用。', root_id=root['id'])
        try:
            info = os.lstat(str(base))
        except OSError:
            raise StorageError('storage_root_offline', '存储位置目录不存在或不可访问。',
                               root_id=root['id'], reason='missing')
        if stat.S_ISLNK(info.st_mode):
            raise StorageError('storage_root_offline', '存储位置不能是符号链接。',
                               root_id=root['id'], reason='symlink')
        if not stat.S_ISDIR(info.st_mode):
            raise StorageError('storage_root_offline', '存储位置必须是目录。',
                               root_id=root['id'], reason='not_a_directory')
        volume_id = str(info.st_dev)
        if root['volume_id'] and volume_id != str(root['volume_id']):
            raise StorageError('storage_root_offline', '挂载身份已改变，拒绝探测与写入。',
                               root_id=root['id'], reason='volume_changed',
                               expected=root['volume_id'], actual=volume_id)
        sentinel_id = root['sentinel_id']
        sentinel = base / SENTINEL_NAME
        if sentinel.exists() or sentinel.is_symlink():
            if sentinel.is_symlink():
                raise StorageError('storage_root_offline', '哨兵文件是符号链接。',
                                   root_id=root['id'], reason='sentinel_symlink')
            try:
                data = json.loads(sentinel.read_text('utf-8'))
            except (OSError, ValueError):
                raise StorageError('storage_root_offline', '哨兵文件不可读。',
                                   root_id=root['id'], reason='sentinel_invalid')
            if sentinel_id and data.get('sentinel_id') != sentinel_id:
                raise StorageError('storage_root_offline', '哨兵文件与登记身份不一致。',
                                   root_id=root['id'], reason='sentinel_mismatch')
            if (data.get('root_id') or root['id']) != root['id']:
                raise StorageError('sentinel_conflict', '该目录已登记为其他存储位置。',
                                   root_id=root['id'], existing=data.get('root_id'))
            sentinel_id = data.get('sentinel_id') or sentinel_id
        writable = True
        reason = None
        temporary = base / (PROBE_PREFIX + secrets.token_hex(6) + '.tmp')
        try:
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b'logbench-probe\n')
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except PermissionError:
            writable, reason = False, 'permission_denied'
        except OSError as exc:
            writable, reason = False, ('no_space' if exc.errno == errno.ENOSPC else 'write_failed')
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        if writable and not sentinel_id:
            sentinel_id = secrets.token_hex(12)
            payload = json.dumps({'root_id': root['id'], 'sentinel_id': sentinel_id,
                                  'kind': root['kind']}, ensure_ascii=False).encode('utf-8')
            try:
                temporary = base / (PROBE_PREFIX + secrets.token_hex(6) + '.tmp')
                descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(str(temporary), str(sentinel))
            except OSError:
                writable, reason = False, 'sentinel_write_failed'
                try:
                    temporary.unlink()
                except OSError:
                    pass
        usage = disk_usage(base)
        kind_fs = fs_type(base)
        return {
            'state': 'online' if writable else 'unauthorized',
            'reason': reason, 'writable': writable, 'volume_id': volume_id,
            'sentinel_id': sentinel_id, 'fs_type': kind_fs,
            'network_fs': is_network_fs(base),
            'free_bytes': usage.available if usage else None,
            'total_bytes': usage.total if usage else None,
        }
