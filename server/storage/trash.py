"""Per-root recycling with active-reference protection (R6-057).

The v3.0.3 recycling bin was one directory inside `data/`. With several roots
that becomes wrong in two directions: a file on another mount cannot be moved
into it atomically (and pretending a copy is a rename is how half-deleted logs
happen), and a shared bin would leak the existence of private files to whoever
can list it.

So every root gets its own `.logbench-trash/`, and the entry record is written
*before* the move and updated *after* it. A crash between the two leaves a
`pending` row that `repair()` can resolve, which is the only honest behaviour:
the file is either where the row says or where the original path says, never
nowhere.

Protected references are checked before anything moves. A running index job, a
pinned query, an in-flight upload or a binding that names the file all mean
"deleting this now would break a live reader", so the delete is refused with the
list of reasons instead of being carried out and apologised for.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from .guard import TRASH_NAME, StorageError

ACTIVE_JOB_STATES = ('indexed', 'failed', 'cancelled')


def _stamp() -> str:
    from ..app import now
    return now()


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    """Create a directory only when it is missing.

    `mkdir(exist_ok=True)` is not used: some sandboxes broker `mkdir` and turn the
    EEXIST case into an exception, and this path runs during a delete where a
    spurious failure would leave the file in neither place.
    """
    if path.is_dir():
        return
    path.mkdir(mode=mode, parents=True)


class TrashService:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ------------------------------------------------------------- resolution
    def _file_row(self, *, file_id: Optional[str], root_id: Optional[str],
                  rel_path: Optional[str]):
        with self.store.connect() as db:
            if file_id:
                row = db.execute('SELECT * FROM logical_files WHERE id=?', (file_id,)).fetchone()
            elif root_id and rel_path is not None:
                row = db.execute('SELECT * FROM logical_files WHERE root_id=? AND rel_path=?',
                                 (root_id, rel_path)).fetchone()
            else:
                row = None
        if row is None:
            raise StorageError('not_found', '文件不存在。')
        return row

    def _authorize(self, actor: dict, row, *, action: str):
        if row['scope'] == 'private':
            if row['owner'] != actor['id']:
                # Being an administrator does not grant access to private content;
                # the record is the owner's, and admin rights are about configuration.
                raise StorageError('not_found', '文件不存在。')
            return
        self.app.require_admin(actor)

    # ---------------------------------------------------------- references
    def active_refs(self, row) -> dict:
        reasons = []
        file_id = row['id']
        with self.store.connect() as db:
            jobs = db.execute(
                "SELECT id,kind,state FROM jobs WHERE state NOT IN (%s) AND payload LIKE ?"
                % ','.join('?' * len(ACTIVE_JOB_STATES)),
                list(ACTIVE_JOB_STATES) + ['%' + file_id + '%']).fetchall()
            uploads = db.execute(
                "SELECT id,state FROM upload_sessions WHERE file_id=? AND state IN "
                "('created','uploading','sealed','indexed')", (file_id,)).fetchall()
            snapshots = db.execute(
                "SELECT id,state FROM query_snapshots WHERE file_id=? AND state NOT IN "
                "('done','cancelled','failed')", (file_id,)).fetchall()
            pins = db.execute(
                'SELECT pin.token FROM layer_pins pin JOIN layer_versions v '
                'ON v.version_id=pin.version_id WHERE v.file_id=? AND pin.expires_at>?',
                (file_id, time.time())).fetchall()
            parent = row['rel_path'].rsplit('/', 1)[0] if '/' in row['rel_path'] else ''
            # Only bindings that *name* this file or one of its directories count.
            # The service default (scope='default', path='') matches every file by
            # definition and must never block a delete.
            bindings = db.execute(
                "SELECT scope,path FROM root_bindings WHERE root_id=? AND path<>'' "
                "AND path IN (?,?)", (row['root_id'], row['rel_path'], parent)).fetchall()
            if row['root_id'] == 'legacy':
                # The legacy root keeps its bindings in the v3.0.3 table, so a delete
                # that only checked `root_bindings` would miss every shared binding
                # written through the v1 UI.
                bindings = list(bindings) + list(db.execute(
                    "SELECT scope,path FROM bindings WHERE path<>'' AND path IN (?,?)",
                    (row['rel_path'], parent)).fetchall())
            personal = db.execute(
                "SELECT scope,path FROM personal_bindings WHERE root_id=? AND path<>'' "
                "AND path IN (?,?)", (row['root_id'], row['rel_path'], parent)).fetchall()
        for job in jobs:
            reasons.append({'kind': 'job', 'id': job['id'], 'state': job['state'],
                            'detail': '该文件仍有未完成的索引任务。'})
        for item in uploads:
            reasons.append({'kind': 'upload', 'id': item['id'], 'state': item['state'],
                            'detail': '该文件属于一个上传会话。'})
        for item in snapshots:
            reasons.append({'kind': 'query', 'id': item['id'], 'state': item['state'],
                            'detail': '固定查询快照仍在引用该文件。'})
        for item in pins:
            reasons.append({'kind': 'pin', 'id': item['token'],
                            'detail': '索引版本被固定引用。'})
        for item in list(bindings) + list(personal):
            reasons.append({'kind': 'binding', 'scope': item['scope'], 'path': item['path'],
                            'detail': '目录或文件规则绑定指向它。'})
        return {'blocked': bool(reasons), 'reasons': reasons}

    # ----------------------------------------------------------------- delete
    def delete(self, actor: dict, *, file_id: Optional[str] = None,
               root_id: Optional[str] = None, rel_path: Optional[str] = None,
               detach_bindings: bool = False, expect_digest: Optional[str] = None) -> dict:
        row = self._file_row(file_id=file_id, root_id=root_id, rel_path=rel_path)
        self._authorize(actor, row, action='delete')
        refs = self.active_refs(row)
        blocking = [reason for reason in refs['reasons']
                    if reason['kind'] != 'binding']
        if blocking:
            raise StorageError('active_references',
                               '该文件仍被任务、快照或上传引用，请先结束这些操作。',
                               references=blocking)
        bindings = [reason for reason in refs['reasons'] if reason['kind'] == 'binding']
        if bindings and not detach_bindings:
            raise StorageError('binding_active',
                               '规则绑定仍指向该文件，确认后可解除绑定并删除。',
                               bindings=bindings)
        root = self.app.storage.guard.root(row['root_id'])
        self.app.storage.guard.require_online(root, writing=True)
        source = self.app.storage.guard.resolve(root, row['rel_path'], want='file')
        info = source.stat()
        digest = self._digest(source) if info.st_size <= 2 * 1024 * 1024 * 1024 else None
        if expect_digest and digest and digest != expect_digest:
            raise StorageError('digest_mismatch', '文件内容已改变，未执行删除。',
                               expected=expect_digest, actual=digest)
        ident = 't-' + secrets.token_hex(8)
        bin_dir = Path(root['path']) / TRASH_NAME
        target = bin_dir / ident
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO trash_entries(id,file_id,root_id,rel_path,generation,kind,size,owner,'
                'scope,digest,state,deleted_at,deleted_by,restored_at,restore_path,detail) '
                "VALUES(?,?,?,?,?,'file',?,?,?,?,'pending',?,?,NULL,NULL,?)",
                (ident, row['id'], row['root_id'], row['rel_path'], row['generation'],
                 info.st_size, row['owner'] or '', row['scope'], digest or '', _stamp(),
                 actor['ip'], self._json({'trashPath': str(target)})))
        try:
            _ensure_dir(bin_dir)
            os.replace(str(source), str(target))
        except OSError as exc:
            with self.store.transaction() as db:
                db.execute("UPDATE trash_entries SET state='failed',detail=? WHERE id=?",
                           (self._json({'error': exc.errno}), ident))
            if exc.errno == errno.EXDEV:
                raise StorageError('cross_device_trash',
                                   '文件所在目录跨越独立挂载点，无法安全移入回收站。',
                                   rootId=row['root_id'], path=row['rel_path'])
            raise
        with self.store.transaction() as db:
            db.execute("UPDATE trash_entries SET state='trashed' WHERE id=?", (ident,))
            db.execute("UPDATE logical_files SET state='trashed',updated_at=? WHERE id=?",
                       (_stamp(), row['id']))
            self.store.audit(db, actor['ip'], 'file.delete', row['rel_path'],
                             {'trashId': ident, 'rootId': row['root_id'],
                              'generation': row['generation'], 'digest': digest})
        if bindings and detach_bindings:
            self.app.storage.bindings.detach_path(actor, root_id=row['root_id'],
                                                  rel_path=row['rel_path'])
        return {'item': {'id': ident, 'fileId': row['id'], 'rootId': row['root_id'],
                         'path': row['rel_path'], 'type': 'file', 'state': 'trashed',
                         'digest': digest, 'detachedBindings': len(bindings)
                         if detach_bindings else 0}}

    # ---------------------------------------------------------------- restore
    def restore(self, actor: dict, trash_id: str) -> dict:
        with self.store.connect() as db:
            entry = db.execute("SELECT * FROM trash_entries WHERE id=? AND restored_at IS NULL",
                               (trash_id,)).fetchone()
        if entry is None:
            # A v3.0.3 row lives in the old `trash` table. Restoring it is still
            # supported so an upgrade does not strand items in a legacy bin.
            return self._restore_legacy(actor, trash_id)
        if entry['scope'] == 'private' and entry['owner'] != actor['id']:
            raise StorageError('not_found', '回收站条目不存在。')
        if entry['scope'] != 'private':
            self.app.require_admin(actor)
        if entry['state'] not in ('trashed', 'pending'):
            raise StorageError('trash_not_restorable', '该条目当前不可恢复。',
                               state=entry['state'])
        root = self.app.storage.guard.root(entry['root_id'])
        self.app.storage.guard.require_online(root, writing=True)
        detail = json.loads(entry['detail'] or '{}')
        recorded = detail.get('trashPath') or ''
        # An empty string must not become `Path('.')`, which exists and would make
        # the restore read the working directory as if it were the recycling bin.
        source = Path(recorded) if recorded else (Path(root['path']) / TRASH_NAME / entry['id'])
        target = self.app.storage.guard.resolve(root, entry['rel_path'], must_exist=False)
        if target.exists():
            raise StorageError('already_exists', '原路径已有同名文件，恢复不会覆盖它。',
                               path=entry['rel_path'])
        if not source.is_file():
            raise StorageError('trash_missing', '回收站文件缺失，无法恢复。',
                               trashId=entry['id'])
        mismatch = False
        if entry['digest']:
            mismatch = self._digest(source) != entry['digest']
        with self.store.transaction() as db:
            db.execute("UPDATE trash_entries SET state='restoring' WHERE id=?", (entry['id'],))
        try:
            _ensure_dir(target.parent)
            os.replace(str(source), str(target))
        except OSError as exc:
            with self.store.transaction() as db:
                db.execute("UPDATE trash_entries SET state='trashed' WHERE id=?", (entry['id'],))
            if exc.errno == errno.EXDEV:
                raise StorageError('cross_device_trash', '回收站与目标不在同一文件系统。')
            raise
        with self.store.transaction() as db:
            db.execute('UPDATE trash_entries SET state=?,restored_at=?,restore_path=?,detail=? '
                       'WHERE id=?',
                       ('restored', _stamp(), entry['rel_path'],
                        self._json({**detail, 'digestMismatch': mismatch}), entry['id']))
            db.execute("UPDATE logical_files SET state='active',updated_at=? WHERE id=?",
                       (_stamp(), entry['file_id']))
            self.store.audit(db, actor['ip'], 'file.restore', entry['rel_path'],
                             {'trashId': entry['id'], 'rootId': entry['root_id'],
                              'digestMismatch': mismatch})
        return {'path': entry['rel_path'], 'fileId': entry['file_id'],
                'rootId': entry['root_id'], 'digestMismatch': mismatch}

    def _restore_legacy(self, actor: dict, trash_id: str) -> dict:
        """v3.0.3 rows: `data/.logbench-trash/<id>` with the path in `trash`."""
        self.app.require_admin(actor)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM trash WHERE id=? AND restored_at IS NULL',
                             (trash_id,)).fetchone()
        if row is None:
            raise StorageError('trash_not_found', '回收站条目不存在。')
        root = self.app.storage.guard.root('legacy')
        self.app.storage.guard.require_online(root)
        source = Path(self.app.config.data_dir) / TRASH_NAME / trash_id
        if not source.exists() or source.is_symlink():
            raise StorageError('trash_missing', '回收站文件缺失，无法恢复。')
        target = self.app.storage.guard.resolve(root, row['path'], must_exist=False)
        if target.exists():
            raise StorageError('already_exists', '原路径已有同名文件，恢复不会覆盖它。',
                               path=row['path'])
        os.replace(str(source), str(target))
        with self.store.transaction() as db:
            db.execute('UPDATE trash SET restored_at=? WHERE id=?', (_stamp(), trash_id))
            db.execute("UPDATE logical_files SET state='active',updated_at=? WHERE root_id='legacy' "
                       'AND rel_path=?', (_stamp(), row['path']))
            self.store.audit(db, actor['ip'], 'file.restore', row['path'],
                             {'trashId': trash_id, 'legacy': True})
        return {'path': row['path'], 'legacy': True}

    # ------------------------------------------------------------------- list
    def list(self, actor: dict, *, limit: int = 200) -> dict:
        items = []
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT * FROM trash_entries WHERE restored_at IS NULL ORDER BY deleted_at DESC '
                'LIMIT ?', (limit,)).fetchall()
            legacy = db.execute(
                'SELECT * FROM trash WHERE restored_at IS NULL ORDER BY deleted_at DESC LIMIT ?',
                (limit,)).fetchall()
        admin = actor['role'] == 'admin'
        for row in rows:
            if row['scope'] == 'private' and row['owner'] != actor['id']:
                continue
            if row['scope'] != 'private' and not admin:
                continue
            state = self.app.storage.roots.get(row['root_id']) if row['root_id'] else None
            items.append({
                'id': row['id'], 'fileId': row['file_id'], 'rootId': row['root_id'],
                'path': row['rel_path'], 'type': row['kind'], 'size': row['size'],
                'generation': row['generation'], 'state': row['state'],
                'deletedAt': row['deleted_at'], 'deletedBy': row['deleted_by'],
                'owner': row['owner'] or None, 'scope': row['scope'],
                'rootState': (state or {}).get('state'),
                'restorable': (state or {}).get('state') == 'online',
            })
        if admin:
            for row in legacy:
                items.append({'id': row['id'], 'fileId': None, 'rootId': 'legacy',
                              'path': row['path'], 'type': row['type'], 'size': None,
                              'generation': None, 'state': 'trashed',
                              'deletedAt': row['deleted_at'], 'deletedBy': row['deleted_by'],
                              'owner': None, 'scope': 'shared', 'legacy': True,
                              'rootState': self.app.storage.roots.get('legacy')['state'],
                              'restorable': True})
        items.sort(key=lambda item: item['deletedAt'] or '', reverse=True)
        return {'items': items[:limit],
                'counts': {'total': len(items),
                           'blocked': sum(1 for item in items if not item['restorable'])}}

    # ----------------------------------------------------------------- repair
    def repair(self) -> dict:
        """Resolve rows left `pending` by a crash between record and move."""
        fixed = []
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM trash_entries WHERE state IN ('pending','restoring')"
                              ).fetchall()
        for row in rows:
            detail = json.loads(row['detail'] or '{}')
            recorded = detail.get('trashPath') or ''
            target = Path(recorded) if recorded else None
            try:
                root = self.app.storage.guard.root(row['root_id'])
                original = Path(root['path']) / row['rel_path']
            except StorageError:
                continue
            if target is not None and target.exists() and not original.exists():
                state = 'trashed'
            elif original.exists() and (target is None or not target.exists()):
                state = 'failed'
            else:
                continue
            with self.store.transaction() as db:
                db.execute('UPDATE trash_entries SET state=? WHERE id=?', (state, row['id']))
            fixed.append({'id': row['id'], 'state': state})
        return {'repaired': fixed}

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _json(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
