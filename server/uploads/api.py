"""Private upload API (R6-047).

Routes:

    POST   /api/v2/uploads                  create a session (name, size, chunk size)
    GET    /api/v2/uploads/{id}             status, bytes done, missing parts
    PUT    /api/v2/uploads/{id}/parts/{n}   one part, body is the raw bytes
    POST   /api/v2/uploads/{id}/commit      verify, assemble, seal, enqueue indexing
    DELETE /api/v2/uploads/{id}             cancel (parts are kept)

The part body is binary, so it is the one write path that is not JSON. The CSRF
header is still required: this is a browser-initiated write, and a cross-site page
must not be able to fill the server's disk.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .sessions import MAX_PART_BYTES, UploadError, UploadStore

PART_ROUTE = re.compile(r'^/api/v2/uploads/([A-Za-z0-9-]+)/parts/(\d{1,6})$')
COMMIT_ROUTE = re.compile(r'^/api/v2/uploads/([A-Za-z0-9-]+)/commit$')
SESSION_ROUTE = re.compile(r'^/api/v2/uploads/([A-Za-z0-9-]+)$')


class UploadApi:
    def __init__(self, app):
        self.app = app
        # R6-052: when an administrator registered a private raw root, new private
        # uploads land there instead of inside the shared deployment directory.
        private = app.storage.roots.private_root() if hasattr(app, 'storage') else None
        base = Path(private['path']) if private else app.config.data_dir
        self.private_base = base
        self.store = UploadStore(app.store, base)

    def route(self, method: str, path: str, payload: dict, actor: dict, headers: dict):
        from ..app import fail
        if path == '/api/v2/uploads':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '上传会话只支持创建。')
            return self.create(actor, payload)
        match = PART_ROUTE.match(path)
        if match:
            if method != 'PUT':
                fail(405, 'METHOD_NOT_ALLOWED', '分片只支持 PUT。')
            return self.put_part(actor, match.group(1), int(match.group(2)), headers)
        match = COMMIT_ROUTE.match(path)
        if match:
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '封存只支持 POST。')
            return self.commit(actor, match.group(1))
        match = SESSION_ROUTE.match(path)
        if match:
            session_id = match.group(1)
            if method in ('GET', 'HEAD'):
                return {'item': self.status(actor, session_id)}
            if method == 'DELETE':
                return {'item': self.store.cancel(session_id, actor['id'])}
            fail(405, 'METHOD_NOT_ALLOWED', '上传会话只支持读取或取消。')
        fail(404, 'NOT_FOUND', '上传接口不存在。')

    # -------------------------------------------------------------------- verbs
    def create(self, actor: dict, payload: dict) -> dict:
        from ..app import fail, integer, text
        name = text(payload.get('name'), '文件名', 128)
        size = integer(payload.get('size'), '文件大小', 1, 8 * 1024 * 1024 * 1024)
        chunk = payload.get('chunkSize')
        chunk = integer(chunk, '分片大小', 64 * 1024, MAX_PART_BYTES) if chunk is not None else None
        session = self.store.create(actor['id'], name=name, size=size, chunk_size=chunk)
        return {'item': session, 'accepted': True}

    def status(self, actor: dict, session_id: str) -> dict:
        item = self.store.get(session_id, actor['id'])
        if item.get('file_id'):
            # Once sealed, the session point at a real file: revocation of that file
            # must close the session view too, not just the record reads.
            from ..indexing.cache_versions import CacheError, CacheRegistry
            try:
                CacheRegistry(self.app.store).read(item['file_id'], 'raw', owner=actor['id'])
            except CacheError:
                raise UploadError('unknown_session', '上传会话不存在。')
        if item.get('file_id'):
            try:
                job = self.app.jobs.get(actor, item.get('job_id') or '')
                item['job'] = job
                item['indexed'] = job['state'] == 'indexed'
            except Exception:  # noqa: BLE001 - job detail is supplementary
                pass
        return item

    def put_part(self, actor: dict, session_id: str, part_no: int, headers: dict) -> dict:
        from ..app import fail
        raw = getattr(getattr(self.app, 'request_local', None), 'body', None)
        if raw is None:
            fail(400, 'INVALID_INPUT', '分片请求缺少内容。')
        digest = headers.get('x-logbench-digest', '') or headers.get('X-LogBench-Digest', '')
        item = self.store.put_part(session_id, actor['id'], part_no, raw,
                                   digest=digest or None)
        return {'item': item}

    def commit(self, actor: dict, session_id: str) -> dict:
        from ..app import fail
        sealed = self.store.commit(session_id, actor['id'])
        sealed.pop('relative_path', None)
        if sealed.get('idempotent') and sealed.get('file_id'):
            # A retried seal returns what the first one produced. Registering the
            # path again is harmless (the catalog dedupes by path + owner) but
            # creating a second index job is not: it would re-read the same bytes
            # on a worker while the first job may still be running.
            session = self.store.get(session_id, actor['id'])
            job_id = session.get('job_id') or ''
            state = 'unknown'
            if job_id:
                try:
                    state = self.app.jobs.get(actor, job_id)['state']
                except Exception:  # noqa: BLE001 - job state is supplementary here
                    state = 'unknown'
            return {'item': dict(sealed, job_id=job_id, index_state=state),
                    'accepted': False}
        # The sealed bytes become a library row *owned by this principal*. That is
        # the only place ownership is decided; a request body cannot name an owner.
        identity = self.app.storage.catalog.register_path(
            Path(sealed['sealed_path']), owner=actor['id'], scope='private',
            logical_name=sealed.get('name'))
        job = self.app.index_jobs.create(actor, {'fileId': identity['file_id']})
        file_id = identity['file_id']
        with self.app.store.transaction() as db:
            db.execute('UPDATE upload_sessions SET file_id=?,job_id=? WHERE id=?',
                       (file_id, job['item']['id'], session_id))
        return {'item': dict(sealed, file_id=file_id, job_id=job['item']['id'],
                             index_state=job['item']['state']),
                'accepted': True}


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
