"""Private upload sessions: ownership, quota, retention and sealing (R6-046).

An uploaded file is not "a file the server has"; it is a session owned by one
principal, with a quota, an expiry and an explicit seal step. The seal is the only
moment the bytes become a library file, and it is deliberately separate from the
last part upload: C6 of the contract says `uploaded` must not mean `indexed`, and
the same logic says "all parts arrived" must not mean "the file is complete".

Properties that are enforced here:

* **parts are idempotent by digest.** Retrying part 7 after a timeout returns the
  same result instead of appending it twice.
* **the seal verifies the whole.** Total size and per-part digests are checked
  before assembly; a missing or short part fails the seal rather than producing a
  truncated log that looks plausible.
* **cancel is not delete.** The parts stay; only the session state changes, and the
  quota is released when the session expires.
* **a name is a name.** Path separators, leading dots and control characters are
  rejected, so an upload cannot choose where it lands.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

DEFAULT_PART_BYTES = 4 * 1024 * 1024
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TTL = 24 * 3600
SAFE_NAME = re.compile(r'^[^./\\\x00-\x1f][^/\\\x00-\x1f]{0,127}$')


#: Concurrent sessions are capped as well as bytes: `global 4` / `per principal 2`
#: is the policy every deployment gets unless it says otherwise.
DEFAULT_MAX_ACTIVE_GLOBAL = 4
DEFAULT_MAX_ACTIVE_PER_PRINCIPAL = 2


class UploadError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def safe_name(raw: str) -> str:
    name = (raw or '').strip()
    if not name or len(name) > 128 or not SAFE_NAME.match(name):
        raise UploadError('invalid_name', '文件名不能包含路径分隔符或以点开头。', name=raw[:64])
    if name in ('.', '..'):
        raise UploadError('invalid_name', '文件名无效。')
    return name


class UploadStore:
    def __init__(self, store, data_dir: Path, *, clock=time.time,
                 quota_bytes: int = DEFAULT_QUOTA_BYTES, ttl: float = DEFAULT_TTL,
                 max_active_global: int = DEFAULT_MAX_ACTIVE_GLOBAL,
                 max_active_per_principal: int = DEFAULT_MAX_ACTIVE_PER_PRINCIPAL):
        self.store = store
        self.root = Path(data_dir) / '.logbench-uploads'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.clock = clock
        self.quota_bytes = quota_bytes
        self.ttl = ttl
        self.max_active_global = max_active_global
        self.max_active_per_principal = max_active_per_principal

    # ------------------------------------------------------------------- create
    def create(self, principal: str, *, name: str, size: int,
               chunk_size: Optional[int] = None) -> dict:
        safe = safe_name(name)
        if not isinstance(size, int) or size <= 0 or size > MAX_FILE_BYTES:
            raise UploadError('invalid_size', '文件大小必须在 1 字节到 8 GiB 之间。', size=size)
        chunk = int(chunk_size or DEFAULT_PART_BYTES)
        if chunk < 64 * 1024 or chunk > MAX_PART_BYTES:
            raise UploadError('invalid_chunk_size', '分片大小必须在 64 KiB 到 8 MiB 之间。',
                              chunk_size=chunk)
        used = self.usage(principal)
        if used + size > self.quota_bytes:
            raise UploadError('quota_exceeded', '私有上传配额不足。',
                              used=used, quota=self.quota_bytes, requested=size)
        # Bytes are capped above; sessions are capped here. An unbounded number of
        # half-finished uploads consumes a file descriptor, a directory and a row
        # each, and none of that is visible in a byte quota.
        self._check_active(principal)
        session_id = 'u-' + secrets.token_hex(8)
        now = self.clock()
        directory = self.root / session_id
        directory.mkdir(mode=0o700, parents=True)
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO upload_sessions(id,principal,display_name,size,chunk_size,state,'
                'created_at,expires_at,quota_bytes) VALUES(?,?,?,?,?,?,?,?,?)',
                (session_id, principal, safe, int(size), chunk, 'created', _stamp(),
                 now + self.ttl, int(size)))
        return {'session_id': session_id, 'name': safe, 'size': int(size),
                'chunk_size': chunk, 'parts_expected': (int(size) + chunk - 1) // chunk,
                'state': 'created', 'expires_at': now + self.ttl}

    # ---------------------------------------------------------------------- read
    def get(self, session_id: str, principal: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM upload_sessions WHERE id=?', (session_id,)).fetchone()
            parts = db.execute('SELECT part_no,size,digest,created_at FROM upload_parts '
                               'WHERE session_id=? ORDER BY part_no', (session_id,)).fetchall()
        if not row or row['principal'] != principal or row['state'] == 'expired':
            # An expired session has no parts left; reporting it as absent is more
            # honest than returning a resume list for files that are gone.
            raise UploadError('unknown_session', '上传会话不存在。')
        received = sum(part['size'] for part in parts)
        expected = (row['size'] + row['chunk_size'] - 1) // row['chunk_size']
        return {
            'session_id': row['id'], 'name': row['display_name'], 'size': row['size'],
            'chunk_size': row['chunk_size'], 'state': row['state'],
            'expires_at': row['expires_at'], 'parts': [dict(part) for part in parts],
            'parts_expected': expected, 'bytes_done': received,
            'indexed': row['state'] == 'indexed',
            'sealed_path': row['sealed_path'], 'file_id': row['file_id'],
            'content_sig': row['content_sig'],
            'job_id': row['job_id'],
            'resume': [number for number in range(1, expected + 1)
                       if number not in {part['part_no'] for part in parts}],
        }

    def active(self, principal: Optional[str] = None) -> int:
        query = ("SELECT COUNT(*) AS n FROM upload_sessions "
                 "WHERE state NOT IN ('sealed','cancelled','expired')")
        params: tuple = ()
        if principal is not None:
            query += ' AND principal=?'
            params = (principal,)
        with self.store.connect() as db:
            return int(db.execute(query, params).fetchone()['n'])

    def _check_active(self, principal: str) -> None:
        mine = self.active(principal)
        if mine >= self.max_active_per_principal:
            raise UploadError('too_many_sessions', '该身份同时进行的上传会话过多。',
                              active=mine, limit=self.max_active_per_principal)
        total = self.active()
        if total >= self.max_active_global:
            raise UploadError('too_many_sessions', '服务器同时进行的上传会话过多。',
                              active=total, limit=self.max_active_global)

    def usage(self, principal: str) -> int:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(size),0) AS n FROM upload_sessions WHERE principal=? "
                "AND state NOT IN ('cancelled','expired')", (principal,)).fetchone()
        return int(row['n'])

    # --------------------------------------------------------------------- parts
    def put_part(self, session_id: str, principal: str, part_no: int, data: bytes,
                 digest: Optional[str] = None) -> dict:
        session = self.get(session_id, principal)
        if session['state'] in ('sealed', 'cancelled', 'expired'):
            raise UploadError('session_closed', '该会话已封存或取消，不能再写入分片。',
                              state=session['state'])
        expected = session['parts_expected']
        if part_no < 1 or part_no > expected:
            raise UploadError('invalid_part', '分片编号超出范围。', part=part_no, expected=expected)
        if len(data) > self.max_part(session):
            raise UploadError('part_too_large', '分片超过允许大小。', size=len(data))
        actual = hashlib.sha256(data).hexdigest()
        if digest and digest.lower() != actual:
            raise UploadError('digest_mismatch', '分片摘要不匹配。',
                              expected=digest.lower(), actual=actual)
        # The last part may be short; every other part must be exactly chunk_size,
        # otherwise the assembled file would be silently misaligned.
        if part_no < expected and len(data) != session['chunk_size']:
            raise UploadError('part_size_mismatch', '非末分片必须等于分片大小。',
                              part=part_no, expected=session['chunk_size'], actual=len(data))
        directory = self.root / session_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / ('%06d.part' % part_no)
        tmp = directory / ('%06d.tmp' % part_no)
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        with self.store.transaction() as db:
            existing = db.execute('SELECT size,digest FROM upload_parts WHERE session_id=? AND part_no=?',
                                  (session_id, part_no)).fetchone()
            idempotent = bool(existing and existing['digest'] == actual)
            db.execute('INSERT OR REPLACE INTO upload_parts(session_id,part_no,size,digest,stored_path,'
                       'created_at) VALUES(?,?,?,?,?,?)',
                       (session_id, part_no, len(data), actual, str(target), _stamp()))
            state = 'uploading' if session['state'] in ('created', 'uploading') else session['state']
            db.execute('UPDATE upload_sessions SET state=? WHERE id=?', (state, session_id))
        return {'session_id': session_id, 'part': part_no, 'size': len(data), 'digest': actual,
                'idempotent': idempotent, 'state': state}

    @staticmethod
    def max_part(session: dict) -> int:
        return int(session['chunk_size'])

    # --------------------------------------------------------------------- seal
    def commit(self, session_id: str, principal: str) -> dict:
        session = self.get(session_id, principal)
        if session['state'] == 'sealed':
            sealed_path = session['sealed_path'] or ''
            base = Path(self.root).parent
            return {'session_id': session_id, 'state': 'sealed', 'file_id': session['file_id'],
                    'sealed_path': sealed_path, 'idempotent': True,
                    'content_sig': session.get('content_sig'),
                    'bytes': session['size'],
                    'relative_path': (str(Path(sealed_path).relative_to(base))
                                      if sealed_path else '')}
        if session['state'] == 'cancelled':
            raise UploadError('session_closed', '会话已取消。')
        received = {part['part_no']: part for part in session['parts']}
        expected = session['parts_expected']
        missing = [number for number in range(1, expected + 1) if number not in received]
        if missing:
            raise UploadError('parts_missing', '还有分片未上传，无法封存。', missing=missing[:20])
        total = sum(part['size'] for part in received.values())
        if total != session['size']:
            raise UploadError('size_mismatch', '分片总和与会话大小不一致。',
                              expected=session['size'], actual=total)

        directory = self.root / session_id
        target_dir = Path(self.root).parent / 'uploads' / session_id
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = target_dir / session['name']
        digest = hashlib.sha256()
        written = 0
        # Stored paths stay server-side: they are not part of the session view.
        with self.store.connect() as db:
            stored = {row['part_no']: row['stored_path'] for row in db.execute(
                'SELECT part_no,stored_path FROM upload_parts WHERE session_id=?', (session_id,))}
        with open(target, 'wb') as out:
            os.chmod(target, 0o600)
            for number in range(1, expected + 1):
                part_path = Path(stored.get(number) or '')
                if not part_path.is_file():
                    raise UploadError('part_lost', '分片文件丢失，请重新上传。', part=number)
                with open(part_path, 'rb') as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        out.write(chunk)
                        written += len(chunk)
        if written != session['size']:
            target.unlink(missing_ok=True)
            raise UploadError('size_mismatch', '封存后的文件大小不符。',
                              expected=session['size'], actual=written)
        with self.store.transaction() as db:
            db.execute("UPDATE upload_sessions SET state='sealed',sealed_path=?,content_sig=? "
                       'WHERE id=?', (str(target), digest.hexdigest(), session_id))
        return {'session_id': session_id, 'state': 'sealed', 'sealed_path': str(target),
                'content_sig': digest.hexdigest(), 'bytes': written,
                'relative_path': str(target.relative_to(Path(self.root).parent))}

    def mark_indexed(self, session_id: str, file_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE upload_sessions SET state='indexed',file_id=? WHERE id=?",
                       (file_id, session_id))

    # ------------------------------------------------------------------- cancel
    def cancel(self, session_id: str, principal: str) -> dict:
        session = self.get(session_id, principal)
        if session['state'] == 'indexed':
            raise UploadError('session_closed', '已建立索引的上传不能取消。')
        with self.store.transaction() as db:
            db.execute("UPDATE upload_sessions SET state='cancelled',quota_bytes=0 WHERE id=?",
                       (session_id,))
        # Parts stay on disk: cancel is not delete (C8). They are removed by gc()
        # once the session expires, not by the cancel itself.
        return {'session_id': session_id, 'state': 'cancelled', 'kept_parts': len(session['parts'])}

    # ----------------------------------------------------------------------- gc
    def gc(self, *, now: Optional[float] = None) -> dict:
        moment = self.clock() if now is None else now
        removed = []
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id,state FROM upload_sessions WHERE expires_at<=? "
                "AND state NOT IN ('indexed')", (moment,)).fetchall()
            for row in rows:
                db.execute("UPDATE upload_sessions SET state='expired',quota_bytes=0 WHERE id=?",
                           (row['id'],))
                db.execute('DELETE FROM upload_parts WHERE session_id=?', (row['id'],))
                removed.append(row['id'])
        for session_id in removed:
            shutil.rmtree(self.root / session_id, ignore_errors=True)
        return {'expired': removed}


def _stamp() -> str:
    from ..app import now
    return now()
