"""Original-byte archive: one writer, one generation, committed intervals (R6-070).

The order is the whole point:

    read a bounded range → write to the target → flush + fsync
    → commit the interval row (start, end, digest) in one transaction
    → only then may the indexer read that range

A crash before the commit leaves bytes on disk that no watermark mentions; recovery
re-reads the last committed interval, compares its digest, and drops the interval if
it does not match. It never truncates confirmed bytes, and it never advances a
watermark to `file_size` just because writes were issued.

Three refusals:

* **same offset, different bytes** is a conflict (409), not an overwrite;
* **an expired or superseded lease** cannot commit, so a rescheduled worker cannot
  keep writing into a generation it no longer owns;
* **a non-contiguous append** is refused instead of silently creating a hole the
  indexer would later read as continuous data.

`source_files` keeps the per-file watermarks; `archive_intervals` is the evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from ..models import file_id_for, new_id

CHUNK_MAX = 8 * 1024 * 1024
SLUG = re.compile(r'[^a-z0-9._-]+')


class ArchiveError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': self.message, 'details': self.details}


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _get(row, key: str, default=None):
    """Accept a dict or a sqlite3.Row: both appear in this codebase."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def slug(value: str, *, fallback: str = 'source') -> str:
    text = SLUG.sub('-', str(value or '').strip().lower()).strip('-')
    return (text[:48] or fallback)


class Archive:
    def __init__(self, app, *, clock=time.time):
        self.app = app
        self.store = app.store
        self.clock = clock

    # ------------------------------------------------------------------ paths
    def root_path(self, root_id: str) -> Path:
        storage = getattr(self.app, 'storage', None)
        if storage is not None:
            try:
                root = storage.roots.get(root_id)
            except Exception:  # noqa: BLE001 - unknown root raises a typed error below
                root = None
            if root:
                path = root['path'] if isinstance(root, dict) else root.path
                return Path(path)
        rows = None
        with self.store.connect() as db:
            rows = db.execute('SELECT path,kind,enabled FROM storage_roots WHERE id=?',
                              (root_id,)).fetchone()
        if rows:
            if rows['kind'] == 'private':
                raise ArchiveError('invalid_root', '私有区不能作为共享归档目标。', rootId=root_id)
            if not rows['enabled']:
                raise ArchiveError('root_offline', '目标存储根已停用。', rootId=root_id)
            return Path(rows['path'])
        if root_id in ('legacy', 'default'):
            return Path(self.app.config.data_dir)
        raise ArchiveError('unknown_root', '目标存储根不存在。', rootId=root_id)

    def plan(self, *, source: dict, mapping: dict, remote_path: str, name: str,
             generation: int = 1) -> dict:
        root_id = _get(mapping, 'root_id') or _get(mapping, 'rootId') or 'legacy'
        target = str(_get(mapping, 'target_subdir') or _get(mapping, 'targetSubdir') or '').strip('/')
        safe = slug(name.rsplit('/', 1)[-1] or 'log')[:120]
        source_slug = slug(_get(source, 'label') or _get(source, 'id') or 'source')
        # A generation owns its own bytes. Writing generation 2 at offset 0 of
        # generation 1's path would destroy confirmed original bytes — the one thing
        # this module promises never to do — so every generation gets a directory.
        parts = ['collected', source_slug, target]
        logical_rel = '/'.join(part for part in parts + [safe] if part)
        rel = logical_rel
        if int(generation) > 1:
            rel = '/'.join(part for part in parts + ['g%d' % int(generation), safe] if part)
        # The logical id stays stable across generations (readers, pins and index
        # shards are keyed by it); only the physical path is per generation.
        file_id = file_id_for(root_id, logical_rel)
        return {'root_id': root_id, 'rel_path': rel, 'file_id': file_id,
                'generation': int(generation),
                'path': self.root_path(root_id) / rel, 'name': safe,
                'remote_path': remote_path}

    # --------------------------------------------------------------- register
    def register(self, plan: dict, *, source_id: str, mapping_id: str, size: int = 0,
                 evidence: Optional[dict] = None) -> dict:
        path = Path(plan['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ArchiveError('unsafe_path', '归档路径是符号链接，已拒绝写入。')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM source_files WHERE mapping_id=? AND remote_path=?',
                             (mapping_id, plan['remote_path'])).fetchone()
            stamp = _now()
            if row is None:
                db.execute(
                    'INSERT INTO source_files(id,source_id,mapping_id,remote_path,file_id,'
                    'generation,root_id,rel_path,size,evidence,archived_bytes,contiguous_bytes,'
                    'gap_state,sealed,state,last_seen_at,last_error,retry_at) '
                    "VALUES(?,?,?,?,?,?,?,?,?,? ,0,0,'none',0,'idle',?,NULL,NULL)",
                    (new_id('sf'), source_id, mapping_id, plan['remote_path'], plan['file_id'],
                     plan['generation'], plan['root_id'], plan['rel_path'], int(size),
                     json.dumps(evidence or {}, ensure_ascii=False), stamp))
                state = 'new'
            else:
                if row['generation'] != plan['generation']:
                    # A new generation starts from zero, on its own path: the previous
                    # generation's confirmed bytes stay where they are.
                    db.execute('UPDATE source_files SET generation=?, rel_path=?, archived_bytes=0, '
                               'contiguous_bytes=0, gap_state=\'none\', sealed=0, state=\'new\', '
                               'size=?, evidence=?, last_seen_at=?, last_error=NULL, retry_at=NULL '
                               'WHERE id=?',
                               (plan['generation'], plan['rel_path'], int(size),
                                json.dumps(evidence or {}, ensure_ascii=False), stamp, row['id']))
                else:
                    db.execute('UPDATE source_files SET rel_path=?, size=?, evidence=?, '
                               'last_seen_at=?, last_error=NULL, retry_at=NULL WHERE id=?',
                               (plan['rel_path'], int(size),
                                json.dumps(evidence or {}, ensure_ascii=False), stamp, row['id']))
                state = row['state']
            row = db.execute('SELECT * FROM source_files WHERE mapping_id=? AND remote_path=?',
                             (mapping_id, plan['remote_path'])).fetchone()
            db.execute('INSERT OR IGNORE INTO logical_files(id,root_id,rel_path,display_name,size,'
                       "generation,content_sig,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (plan['file_id'], plan['root_id'], plan['rel_path'], plan['name'],
                        int(size), plan['generation'], '', _now(), _now()))
            # The logical file keeps one id but points at the newest generation's bytes,
            # so the library and the reader open what is actually current.
            db.execute('UPDATE logical_files SET rel_path=?, generation=?, size=?, updated_at=? '
                       'WHERE id=?',
                       (plan['rel_path'], plan['generation'], int(size), _now(),
                        plan['file_id']))
        return {'id': row['id'], 'fileId': plan['file_id'], 'state': state,
                'generation': plan['generation'], 'relPath': plan['rel_path'],
                'rootId': plan['root_id']}

    # ----------------------------------------------------------------- append
    def append(self, file_id: str, generation: int, *, offset: int, data: bytes,
               lease=None, writer: str = 'collector') -> dict:
        if not data:
            return {'committed': False, 'offset': offset, 'bytes': 0}
        if len(data) > CHUNK_MAX:
            raise ArchiveError('chunk_too_large', '单次归档区间超过上限。', size=len(data))
        row = self._source_file(file_id, generation)
        if row is None:
            raise ArchiveError('unknown_file', '归档文件未登记。', fileId=file_id)
        contiguous = int(row['contiguous_bytes'] or 0)
        path = Path(self.root_path(row['root_id'])) / row['rel_path']

        if offset < contiguous:
            existing = self._interval_digest(file_id, generation, offset, offset + len(data))
            if existing == _digest(data):
                return {'committed': False, 'idempotent': True, 'offset': offset,
                        'bytes': len(data)}
            raise ArchiveError('offset_conflict', '该偏移已归档且内容不同。',
                               offset=offset, fileId=file_id)
        if offset > contiguous:
            raise ArchiveError('non_contiguous', '归档必须从连续水位继续写。',
                               expected=contiguous, got=offset)

        self._require_lease(lease) if lease is not None else None
        digest = _digest(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, 'r+b' if path.exists() else 'w+b')
        try:
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
            raise ArchiveError('write_failed', '原文写入失败，未提交该区间。', detail=str(exc))
        finally:
            handle.close()

        try:
            return self._commit(file_id, generation, offset, offset + len(data), digest, writer)
        except Exception:
            # The bytes are on disk but unconfirmed: cut them off so a later append at
            # the same offset cannot be mistaken for them.
            try:
                with open(path, 'r+b') as handle:
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            raise

    def _commit(self, file_id: str, generation: int, start: int, end: int, digest: str,
                writer: str) -> dict:
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO archive_intervals(file_id,generation,byte_start,'
                       'byte_end,digest,writer,committed_at) VALUES(?,?,?,?,?,?,?)',
                       (file_id, generation, start, end, digest, writer, _now()))
            rows = db.execute('SELECT byte_start,byte_end,digest FROM archive_intervals '
                              'WHERE file_id=? AND generation=? ORDER BY byte_start',
                              (file_id, generation)).fetchall()
            contiguous, archived, cursor = 0, 0, 0
            for row in rows:
                archived += int(row['byte_end']) - int(row['byte_start'])
                if int(row['byte_start']) == cursor:
                    cursor = int(row['byte_end'])
                    contiguous = cursor
            db.execute('UPDATE source_files SET archived_bytes=?, contiguous_bytes=?, '
                       'state=?, last_seen_at=?, last_error=NULL WHERE file_id=? AND generation=?',
                       (archived, contiguous, 'archived', _now(), file_id, generation))
            db.execute('INSERT OR REPLACE INTO archive_manifests(file_id,generation,manifest,'
                       'updated_at) VALUES(?,?,?,?)',
                       (file_id, generation,
                        json.dumps({'intervals': len(rows), 'archived': archived,
                                    'contiguous': contiguous}, ensure_ascii=False), _now()))
        return {'committed': True, 'offset': start, 'bytes': end - start, 'digest': digest,
                'contiguous': contiguous, 'archived': archived}

    def _require_lease(self, lease) -> None:
        with self.store.connect() as db:
            row = db.execute('SELECT fencing,epoch,owner,expires_at FROM job_leases WHERE job_id=?',
                             (lease.job_id,)).fetchone()
        if row is None:
            raise ArchiveError('stale_writer', '归档租约不存在。', jobId=lease.job_id)
        if (int(row['fencing']) != int(lease.fencing)
                or int(row['epoch']) != int(lease.epoch)
                or str(row['owner']) != str(lease.owner)
                or float(row['expires_at']) < self.clock()):
            raise ArchiveError('stale_writer', '归档租约已过期或被更高代次取代。',
                               jobId=lease.job_id, fencing=lease.fencing)

    # ------------------------------------------------------------------ queries
    def _source_file(self, file_id: str, generation: int):
        with self.store.connect() as db:
            return db.execute('SELECT * FROM source_files WHERE file_id=? AND generation=?',
                              (file_id, generation)).fetchone()

    def _interval_digest(self, file_id: str, generation: int, start: int, end: int) -> Optional[str]:
        with self.store.connect() as db:
            row = db.execute('SELECT digest FROM archive_intervals WHERE file_id=? AND '
                             'generation=? AND byte_start=? AND byte_end=?',
                             (file_id, generation, start, end)).fetchone()
        return row['digest'] if row else None

    def watermark(self, file_id: str, generation: int) -> dict:
        with self.store.connect() as db:
            rows = db.execute('SELECT byte_start,byte_end,digest,committed_at FROM archive_intervals '
                              'WHERE file_id=? AND generation=? ORDER BY byte_start',
                              (file_id, generation)).fetchall()
            file_row = db.execute('SELECT * FROM source_files WHERE file_id=? AND generation=?',
                                  (file_id, generation)).fetchone()
        intervals = [{'start': int(row['byte_start']), 'end': int(row['byte_end']),
                      'digest': row['digest'], 'committedAt': row['committed_at']} for row in rows]
        contiguous, cursor = 0, 0
        for interval in intervals:
            if interval['start'] == cursor:
                cursor = interval['end']
                contiguous = cursor
        base = intervals[0]['start'] if intervals else 0
        return {'intervals': intervals, 'contiguous': contiguous, 'base': base,
                'gaps': [{'from': 0, 'to': base}] if base > 0 else [],
                'archived': sum(item['end'] - item['start'] for item in intervals),
                'sealed': bool(file_row['sealed']) if file_row else False,
                'size': int(file_row['size']) if file_row else 0}

    def verify(self, file_id: str, generation: int) -> dict:
        """Re-hash every committed interval against the file on disk."""
        row = self._source_file(file_id, generation)
        if row is None:
            raise ArchiveError('unknown_file', '归档文件未登记。', fileId=file_id)
        path = Path(self.root_path(row['root_id'])) / row['rel_path']
        if not path.exists():
            return {'ok': False, 'reason': 'missing', 'path': str(path)}
        mismatched = []
        with open(path, 'rb') as handle:
            for interval in self.watermark(file_id, generation)['intervals']:
                handle.seek(interval['start'])
                data = handle.read(interval['end'] - interval['start'])
                if _digest(data) != interval['digest']:
                    mismatched.append(interval)
        return {'ok': not mismatched, 'mismatched': mismatched, 'path': str(path),
                'size': path.stat().st_size}

    def seal(self, file_id: str, generation: int, *, expected_size: Optional[int] = None) -> dict:
        watermark = self.watermark(file_id, generation)
        if expected_size is not None and watermark['contiguous'] != int(expected_size):
            raise ArchiveError('incomplete', '已确认区间不等于源文件长度，不能封存。',
                               contiguous=watermark['contiguous'], expected=expected_size)
        with self.store.transaction() as db:
            db.execute('UPDATE source_files SET sealed=1, state=? WHERE file_id=? AND generation=?',
                       ('sealed', file_id, generation))
        return {'sealed': True, 'contiguous': watermark['contiguous']}

    # ----------------------------------------------------------------- recovery
    def recover(self, file_id: str, generation: int, *, lease=None) -> dict:
        """Drop unconfirmed bytes; never touch the confirmed prefix.

        Two different things can be wrong after a crash:

        * an interval whose bytes no longer match its digest — the row is deleted and
          everything at or after it is discarded;
        * bytes past `contiguous_bytes` that no interval mentions — a crash between
          `write+fsync` and the commit. They are unconfirmed by definition, so they
          are trimmed instead of being left for a reader to find as if archived.
        """
        row = self._source_file(file_id, generation)
        if row is None:
            raise ArchiveError('unknown_file', '归档文件未登记。', fileId=file_id)
        if lease is not None:
            self._require_lease(lease)
        path = Path(self.root_path(row['root_id'])) / row['rel_path']
        if not path.exists():
            return {'dropped': [], 'reason': 'missing', 'contiguous': 0, 'trimmed': 0}
        dropped = []
        with open(path, 'rb') as handle:
            for interval in self.watermark(file_id, generation)['intervals']:
                handle.seek(interval['start'])
                data = handle.read(interval['end'] - interval['start'])
                if _digest(data) != interval['digest']:
                    dropped.append(interval)
                    break
        if dropped:
            with self.store.transaction() as db:
                db.execute('DELETE FROM archive_intervals WHERE file_id=? AND generation=? '
                           'AND byte_start>=?',
                           (file_id, generation, dropped[0]['start']))
                db.execute('UPDATE source_files SET state=?, last_error=? WHERE file_id=? AND generation=?',
                           ('recovered', json.dumps({'code': 'interval_digest_mismatch',
                                                     'at': dropped[0]['start']}, ensure_ascii=False),
                            file_id, generation))
        watermark = self.watermark(file_id, generation)
        contiguous = int(watermark['contiguous'])
        trimmed = 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > contiguous:
            # Any bytes after the surviving prefix are unconfirmed; trimming them keeps a
            # later append from being read as if it had been committed.
            with open(path, 'r+b') as handle:
                handle.truncate(contiguous)
                handle.flush()
                os.fsync(handle.fileno())
            trimmed = size - contiguous
        return {'dropped': dropped, 'contiguous': contiguous, 'trimmed': trimmed}
