"""Storage root registration (R6-052).

A root is a physical directory with an identity that does not depend on the
release directory or the mount name. The `legacy` root is the v3.0.3 `data_dir`:
it is registered automatically, keeps the old file ids and bindings working, and
is never moved.

Rules enforced here, and why:

* **paths are unique and never nested.** Two roots that overlap would let the
  same bytes appear twice in the logical library under two ids, and a delete in
  one would silently hide a file in the other.
* **the legacy root is created, not migrated.** Re-running registration is a
  no-op; no file is copied, so an old deployment keeps its exact layout.
* **a path change is refused while the root holds files.** Redirecting a root
  would repoint every file id at new bytes; the operator has to run an explicit
  migration instead of a silent rewrite.
* **the index root must be local.** SQLite WAL on SMB/NFS is a single-writer
  database on a file system that does not honour those semantics.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .guard import (KINDS, ROOT_ID, StorageError, StorageGuard, fs_type, is_network_fs)

LEGACY_ID = 'legacy'
DEFAULT_QUOTA = 0


def _stamp() -> str:
    from ..app import now
    return now()


@dataclass
class Root:
    id: str
    kind: str
    label: str
    path: Path
    enabled: bool = True
    revision: int = 1
    volume_id: str = ''
    sentinel_id: str = ''
    quota_bytes: int = 0
    probe_state: str = 'unknown'

    @property
    def scope(self) -> str:
        return 'private' if self.kind == 'private_raw' else ('index' if self.kind == 'index' else 'shared')

    def as_dict(self) -> dict:
        return {'id': self.id, 'kind': self.kind, 'label': self.label, 'path': self.path,
                'enabled': self.enabled, 'revision': self.revision,
                'volume_id': self.volume_id, 'sentinel_id': self.sentinel_id,
                'quota_bytes': self.quota_bytes, 'probe_state': self.probe_state}

    # The guard works with plain mappings (rows and dicts both arrive there).
    def __getitem__(self, key: str):
        return self.as_dict()[key]

    def get(self, key: str, default=None):
        return self.as_dict().get(key, default)


class RootService:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.guard = StorageGuard(app)

    # ----------------------------------------------------------------- legacy
    def ensure_legacy(self) -> dict:
        """Register `data_dir` once. Safe to call on every start."""
        from ..app import compact
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM storage_roots WHERE id=?', (LEGACY_ID,)).fetchone()
            if row is None:
                db.execute(
                    'INSERT INTO storage_roots(id,kind,label,path,enabled,created_at,revision,'
                    'volume_id,sentinel_id,quota_bytes,probe_state,updated_at,updated_by) '
                    "VALUES(?,?,?,?,1,?,1,'','',0,'unknown',?,'system')",
                    (LEGACY_ID, 'shared_raw', '部署数据目录', str(self.app.config.data_dir),
                     _stamp(), _stamp()))
                self.store.audit(db, 'system', 'storage.root.legacy', LEGACY_ID,
                                 {'path': str(self.app.config.data_dir)})
                row = db.execute('SELECT * FROM storage_roots WHERE id=?', (LEGACY_ID,)).fetchone()
        root = self._row(row)
        if not root.volume_id:
            # Identity is best effort at startup: a read-only data dir must not stop
            # the service from starting, it only means writes will be refused.
            try:
                self._store_probe(root, self.guard.probe(root), actor='system')
            except StorageError:
                pass
        return self.get(LEGACY_ID)

    # ------------------------------------------------------------------- read
    @staticmethod
    def _row(row) -> Root:
        return Root(id=row['id'], kind=row['kind'], label=row['label'], path=Path(row['path']),
                    enabled=bool(row['enabled']), revision=row['revision'],
                    volume_id=row['volume_id'] or '', sentinel_id=row['sentinel_id'] or '',
                    quota_bytes=row['quota_bytes'] or 0,
                    probe_state=row['probe_state'] or 'unknown')

    def get(self, root_id: str, *, with_state: bool = True) -> dict:
        root = self._row(self.guard.root_row(root_id))
        return self.describe(root, with_state=with_state)

    def all(self, *, kinds: Optional[tuple] = None, with_state: bool = True) -> list:
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM storage_roots ORDER BY kind,label,id').fetchall()
        result = []
        for row in rows:
            root = self._row(row)
            if kinds and root.kind not in kinds:
                continue
            result.append(self.describe(root, with_state=with_state))
        return result

    def describe(self, root: Root, *, with_state: bool = True) -> dict:
        counts = self._counts(root.id)
        state = self.guard.state(root.as_dict()) if with_state else {'state': 'unknown', 'reason': None}
        item = {
            'root_id': root.id, 'id': root.id, 'label': root.label, 'kind': root.kind,
            'scope': root.scope, 'path': str(root.path), 'enabled': root.enabled,
            'revision': root.revision, 'volume_id': root.volume_id or None,
            'sentinel_id': root.sentinel_id or None, 'quota_bytes': root.quota_bytes,
            'state': state['state'], 'reason': state.get('reason'),
            'free_bytes': self.guard.free_bytes(root.as_dict()) if with_state else None,
            'fs_type': fs_type(root.path) if with_state else 'unknown',
            'network_fs': is_network_fs(root.path) if with_state else False,
            'files': counts['files'], 'indexed': counts['indexed'], 'bytes': counts['bytes'],
            'last_probe': self._last_probe(root.id),
        }
        return item

    def _counts(self, root_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT count(*) AS n, COALESCE(SUM(size),0) AS bytes FROM logical_files "
                "WHERE root_id=? AND state='active'", (root_id,)).fetchone()
            indexed = db.execute(
                "SELECT count(DISTINCT p.file_id) AS n FROM layer_pointers p "
                "JOIN logical_files f ON f.id=p.file_id "
                "WHERE f.root_id=? AND f.state='active' AND p.layer='structure' "
                "AND p.scope='shared'", (root_id,)).fetchone()
        return {'files': int(row['n']), 'bytes': int(row['bytes']),
                'indexed': int(indexed['n'] if indexed else 0)}

    def _last_probe(self, root_id: str) -> Optional[dict]:
        with self.store.connect() as db:
            row = db.execute(
                'SELECT * FROM storage_root_probes WHERE root_id=? ORDER BY checked_at DESC LIMIT 1',
                (root_id,)).fetchone()
        if row is None:
            return None
        return {'state': row['state'], 'reason': row['reason'], 'writable': bool(row['writable']),
                'checked_at': row['checked_at'], 'fs_type': row['fs_type'],
                'free_bytes': row['free_bytes']}

    # ------------------------------------------------------------------ write
    def _validate(self, *, label, path, kind, quota_bytes, root_id: Optional[str]) -> dict:
        from ..app import integer, text
        label = text(label, '位置名称', 80)
        kind = kind or 'shared_raw'
        if kind not in KINDS:
            raise StorageError('invalid_kind', '用途必须是 shared_raw、private_raw 或 index。',
                               kind=kind)
        raw = text(path, '服务器绝对路径', 2048)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise StorageError('invalid_root_path', '请填写服务器上的绝对路径。', path=raw)
        resolved = Path(os.path.realpath(str(candidate)))
        if not resolved.exists():
            raise StorageError('invalid_root_path', '目录不存在，请先创建并由服务账号授权。',
                               path=str(resolved))
        if not resolved.is_dir():
            raise StorageError('invalid_root_path', '存储位置必须是目录。', path=str(resolved))
        if candidate.is_symlink():
            raise StorageError('symlink_denied', '存储位置不能是符号链接。', path=str(candidate))
        if kind == 'index' and is_network_fs(resolved):
            raise StorageError('root_not_local',
                               '索引位置必须在本地文件系统；共享盘上的 SQLite 索引不受支持。',
                               path=str(resolved), fs_type=fs_type(resolved))
        self._check_overlap(str(resolved), root_id)
        quota = integer(quota_bytes or 0, '配额', 0, 1 << 60)
        return {'label': label, 'kind': kind, 'path': str(resolved), 'quota_bytes': quota}

    def _check_overlap(self, path: str, root_id: Optional[str]):
        with self.store.connect() as db:
            rows = db.execute('SELECT id,path FROM storage_roots').fetchall()
        for row in rows:
            if root_id and row['id'] == root_id:
                continue
            other = str(row['path']).rstrip('/')
            here = path.rstrip('/')
            if other == here:
                raise StorageError('root_path_duplicate', '该物理目录已登记为存储位置。',
                                   path=path, existing=row['id'])
            if here.startswith(other + '/') or other.startswith(here + '/'):
                raise StorageError('root_overlap', '存储位置不能互相包含。',
                                   path=path, existing=row['id'], existingPath=other)

    def create(self, actor: dict, payload: dict) -> dict:
        from ..app import compact
        self.app.require_admin(actor)
        values = self._validate(label=payload.get('label') or payload.get('name'),
                                path=payload.get('path'),
                                kind=payload.get('kind'),
                                quota_bytes=payload.get('quotaBytes'),
                                root_id=None)
        root_id = self._free_id(payload.get('id') or self._unique_id(values['label']))
        # The probe writes a sentinel naming its root, so it has to know the real
        # id. Probing under a placeholder produced a sentinel for a root that never
        # existed, and every later read then called the new root offline.
        probe = self.guard.probe(Root(id=root_id, kind=values['kind'], label=values['label'],
                                      path=Path(values['path'])))
        if not probe['writable']:
            raise StorageError('root_not_writable',
                               '服务账号无法写入该目录，请先修正权限；产品不会自动 chmod。',
                               path=values['path'], reason=probe['reason'])
        if values['kind'] == 'index' and probe['network_fs']:
            raise StorageError('root_not_local', '索引位置必须是本地文件系统。',
                               path=values['path'], fs_type=probe['fs_type'])
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO storage_roots(id,kind,label,path,enabled,created_at,revision,'
                'volume_id,sentinel_id,quota_bytes,probe_state,updated_at,updated_by) '
                'VALUES(?,?,?,?,1,?,1,?,?,?,\'online\',?,?)',
                (root_id, values['kind'], values['label'], values['path'], _stamp(),
                 probe['volume_id'], probe['sentinel_id'], values['quota_bytes'], _stamp(),
                 actor['ip']))
            self.store.audit(db, actor['ip'], 'storage.root.create', root_id,
                             {'path': values['path'], 'kind': values['kind']})
        self._store_probe(self.get(root_id, with_state=False), probe, actor=actor['ip'])
        return {'item': self.get(root_id)}

    def update(self, actor: dict, root_id: str, payload: dict) -> dict:
        from ..app import integer
        self.app.require_admin(actor)
        root = self._row(self.guard.root_row(root_id))
        if 'path' in payload and payload.get('path') not in (None, ''):
            proposed = Path(str(payload.get('path'))).expanduser()
            if str(proposed) != str(root.path):
                with self.store.connect() as db:
                    held = db.execute('SELECT count(*) FROM logical_files WHERE root_id=?',
                                      (root_id,)).fetchone()[0]
                raise StorageError(
                    'root_path_migration_required',
                    '该存储位置已有文件登记，不能直接重定向物理目录；请建立迁移任务。',
                    root_id=root_id, files=held)
        expected = integer(payload.get('expectedRevision'), '版本号', 1)
        label = root.label if 'label' not in payload else None
        kind = payload.get('kind') or root.kind
        quota = payload.get('quotaBytes', root.quota_bytes)
        enabled = payload.get('enabled', root.enabled)
        if not isinstance(enabled, bool):
            raise StorageError('invalid_input', '启用状态必须是布尔值。')
        if label is None:
            from ..app import text
            label = text(payload.get('label') or root.label, '位置名称', 80)
        if kind not in KINDS:
            raise StorageError('invalid_kind', '用途不正确。', kind=kind)
        if kind == 'private_raw' and root.kind == 'shared_raw' and self._counts(root_id)['files']:
            raise StorageError('root_kind_conflict',
                               '该位置已有共享文件，不能直接改为私有；请先迁移文件。',
                               root_id=root_id)
        if kind == 'index' and is_network_fs(root.path):
            raise StorageError('root_not_local', '索引位置必须在本地文件系统。',
                               path=str(root.path))
        quota = integer(quota or 0, '配额', 0, 1 << 60)
        with self.store.transaction() as db:
            row = db.execute('SELECT revision FROM storage_roots WHERE id=?', (root_id,)).fetchone()
            self.app.check_revision(expected, row['revision'])
            db.execute('UPDATE storage_roots SET label=?,kind=?,enabled=?,quota_bytes=?,'
                       'revision=revision+1,updated_at=?,updated_by=? WHERE id=?',
                       (label, kind, int(enabled), quota, _stamp(), actor['ip'], root_id))
            self.store.audit(db, actor['ip'], 'storage.root.update', root_id,
                             {'revision': expected, 'enabled': enabled, 'kind': kind})
        return {'item': self.get(root_id)}

    def probe(self, actor: dict, root_id: str) -> dict:
        self.app.require_admin(actor)
        root = self.get(root_id, with_state=False)
        result = self.guard.probe(root)
        self._store_probe(root, result, actor=actor['ip'])
        return {'item': {'root_id': root_id, **result}, 'root': self.get(root_id)}

    def _store_probe(self, root: dict, result: dict, *, actor: str):
        from ..app import compact
        with self.store.transaction() as db:
            row = db.execute('SELECT revision,volume_id,sentinel_id FROM storage_roots WHERE id=?',
                             (root['id'],)).fetchone()
            if row is None:
                return
            db.execute(
                'INSERT INTO storage_root_probes(root_id,revision,state,reason,writable,volume_id,'
                'sentinel_id,free_bytes,total_bytes,fs_type,checked_at,detail) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (root['id'], row['revision'], result['state'], result.get('reason') or '',
                 int(bool(result['writable'])), result.get('volume_id') or '',
                 result.get('sentinel_id') or '', result.get('free_bytes'),
                 result.get('total_bytes'), result.get('fs_type') or 'unknown', _stamp(),
                 compact({k: v for k, v in result.items() if k != 'detail'})))
            # Identity is written once and only when absent: a probe must never
            # silently re-point a root at whatever is mounted now.
            volume = row['volume_id'] or (result.get('volume_id') or '')
            sentinel = row['sentinel_id'] or (result.get('sentinel_id') or '')
            db.execute('UPDATE storage_roots SET volume_id=?,sentinel_id=?,probe_state=? '
                       'WHERE id=?',
                       (volume, sentinel, result['state'], root['id']))
            self.store.audit(db, actor, 'storage.root.probe', root['id'],
                             {'state': result['state'], 'reason': result.get('reason')})

    @staticmethod
    def _unique_id(seed: str) -> str:
        """A readable id from the label; a CJK-only label gets a short digest."""
        raw = str(seed).strip()
        base = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')
        if not base:
            # `归档盘` slugs to nothing; a digest keeps ids stable and distinct
            # instead of a meaningless root / root-2 sequence.
            base = 'root-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:6]
        base = base[:40]
        if not re.match(ROOT_ID, base):
            base = 'root-' + base
        return base

    def _free_id(self, seed: str) -> str:
        """A non-CJK label slugs to `root`, so make the id unique explicitly."""
        candidate = re.sub(r'[^a-z0-9_-]+', '-', str(seed).strip().lower()).strip('-') or 'root'
        if not re.match(ROOT_ID, candidate):
            candidate = 'root-' + candidate
        candidate = candidate[:60]
        with self.store.connect() as db:
            existing = {row['id'] for row in db.execute('SELECT id FROM storage_roots')}
        if candidate not in existing:
            return candidate
        for index in range(2, 100):
            probe = '%s-%d' % (candidate, index)
            if probe not in existing:
                return probe
        raise StorageError('root_id_exhausted', '无法为该存储位置生成唯一标识。')

    # ------------------------------------------------------------------ misc
    def private_root(self) -> Optional[dict]:
        """The private root new uploads should use, if one is registered."""
        for item in self.all(kinds=('private_raw',), with_state=False):
            root = self._row(self.guard.root_row(item['id']))
            if self.guard.state(root.as_dict())['state'] == 'online':
                return item
        return None

    def summary(self) -> dict:
        items = self.all()
        return {
            'roots': len(items),
            'online': sum(1 for item in items if item['state'] == 'online'),
            'offline': sum(1 for item in items if item['state'] == 'offline'),
            'files': sum(item['files'] for item in items),
            'indexed': sum(item['indexed'] for item in items),
            'bytes': sum(item['bytes'] for item in items),
        }
