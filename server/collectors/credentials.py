"""Credential references, never credential values (R6-059).

The database stores a *reference*: an opaque id, a kind, a label and the
fingerprint of what was written. The bytes live in one file under
`state/credentials/`, created with 0600 inside a 0700 directory.

Why a file and not a column: SQLite copies are taken by `tools/backup.py`, by the
migration helper and by every ad-hoc `cp` an operator does. A password column
would therefore travel into every backup, every support bundle and every
`pre-r6-N` snapshot. A separate 0600 file is a much smaller blast radius, and the
backup tool can refuse to include it explicitly rather than silently leak it.

Two refusals that matter:

* `describe()` never returns the secret, and `metadata()` is what HTTP routes use;
* a reference that points outside the credential directory is rejected when it is
  resolved, so a tampered row cannot turn into an arbitrary file read.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

KINDS = ('password', 'key', 'token')
MAX_SECRET = 64 * 1024


class CredentialError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def fingerprint(value: str) -> str:
    """Non-reversible marker so an operator can tell "same secret" from "rotated"."""
    return hashlib.sha256(('lb-cred\0' + value).encode('utf-8')).hexdigest()[:16]


class CredentialStore:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / 'credentials'

    # ------------------------------------------------------------------ layout
    def ensure_root(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise CredentialError('unsafe_root', '凭据目录不能是符号链接。')
        try:
            mode = stat.S_IMODE(self.root.stat().st_mode)
            if mode & 0o077:
                os.chmod(self.root, 0o700)
        except OSError as exc:
            raise CredentialError('unsafe_root', '无法确认凭据目录权限。', detail=str(exc))
        return self.root

    def _path(self, ref: str) -> Path:
        if not isinstance(ref, str) or not ref.startswith('cred_') or len(ref) > 48:
            raise CredentialError('invalid_ref', '凭据引用无效。')
        suffix = ref[5:]
        if not suffix or any(ch not in '0123456789abcdef' for ch in suffix):
            raise CredentialError('invalid_ref', '凭据引用无效。')
        root = self.ensure_root().resolve()
        target = (root / suffix).resolve()
        # resolve() plus a prefix check is what stops `../` and symlink swaps.
        if target.parent != root or target.name != suffix:
            raise CredentialError('invalid_ref', '凭据引用指向非法位置。')
        return target

    # ------------------------------------------------------------------- write
    def create(self, *, kind: str, label: str, value: str, actor: str) -> dict:
        if kind not in KINDS:
            raise CredentialError('invalid_kind', '不支持的凭据类型：' + str(kind))
        if not isinstance(value, str) or not value:
            raise CredentialError('empty_secret', '凭据内容不能为空。')
        if len(value.encode('utf-8')) > MAX_SECRET:
            raise CredentialError('secret_too_large', '凭据内容超过 64 KiB。')
        label = (label or '').strip()[:120] or ('%s 凭据' % kind)
        root = self.ensure_root()
        ref = 'cred_' + secrets.token_hex(16)
        target = root / ref[5:]
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(value.encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        if stat.S_IMODE(target.stat().st_mode) != 0o600:
            os.chmod(target, 0o600)
        return {'id': ref, 'kind': kind, 'label': label,
                'fingerprint': fingerprint(value), 'actor': actor}

    # -------------------------------------------------------------------- read
    def read(self, ref: str) -> str:
        path = self._path(ref)
        try:
            info = path.stat()
        except OSError:
            raise CredentialError('credential_missing', '凭据缺失，请管理员重新录入。')
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET:
            raise CredentialError('credential_invalid', '凭据文件不可用。')
        if stat.S_IMODE(info.st_mode) & 0o077:
            # Refuse rather than silently reading a world-readable secret.
            raise CredentialError('credential_permissions', '凭据文件权限过宽，请重新录入。')
        try:
            return path.read_text('utf-8')
        except (OSError, UnicodeError):
            raise CredentialError('credential_invalid', '凭据文件不可读。')

    def metadata(self, ref: str) -> dict:
        """"Is this reference usable?" without decrypting anything for the caller."""
        try:
            self.read(ref)
        except CredentialError as exc:
            return {'ref': ref, 'state': 'unusable', 'code': exc.code}
        return {'ref': ref, 'state': 'ready'}

    def delete(self, ref: str) -> bool:
        path = self._path(ref)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CredentialError('delete_failed', '无法删除凭据文件。', detail=str(exc))


def mask(value: Optional[str]) -> Optional[str]:
    """What an error message or a log line may say about a credential."""
    if not value:
        return value
    return '***'
