"""`/api/v2` routes for storage roots, the logical library and rule bindings (R6-052/054/055/057).

Routing rules that matter:

* a caller never sends a filesystem path for a file — it sends a `file_id` or a
  `(rootId, relPath)` pair that the guard resolves;
* every read re-checks the actor's access; a signed cursor is a position, not a
  permission;
* writes require the capability that matches their scope (admin for shared,
  owner for private) and inherit CSRF/Origin checks from `Application.handle`.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .guard import StorageError

ROOT_ROUTE = re.compile(r'^/api/v2/storage-roots(?:/([A-Za-z0-9_-]{1,64})(/probe)?)?$')
FILE_ROUTE = re.compile(r'^/api/v2/library/files/([A-Za-z0-9_-]{4,64})(?:/(generations|index|content))?$')
RESTORE_ROUTE = re.compile(r'^/api/v2/library/trash/([A-Za-z0-9_-]{1,64})/restore$')
STATUS = {'unknown_root': 404, 'not_found': 404, 'invalid_cursor': 400,
          'invalid_path': 400, 'path_escape': 403, 'symlink_denied': 403,
          'storage_root_offline': 409, 'storage_root_disabled': 409,
          'root_path_duplicate': 409, 'root_overlap': 409, 'root_path_migration_required': 409,
          'root_not_local': 400, 'root_not_writable': 400, 'root_kind_conflict': 409,
          'invalid_scope': 400, 'invalid_kind': 400, 'invalid_root_path': 400,
          'rule_not_found': 404, 'active_references': 409, 'binding_active': 409,
          'already_exists': 409, 'cross_device_trash': 409, 'trash_missing': 409,
          'trash_not_found': 404, 'trash_not_restorable': 409, 'digest_mismatch': 409,
          'invalid_root': 400, 'not_a_file': 400, 'not_a_directory': 400,
          'sentinel_conflict': 409, 'storage_root_unauthorized': 409}


class StorageApi:
    def __init__(self, app):
        self.app = app
        self.roots = app.storage.roots
        self.catalog = app.storage.catalog
        self.bindings = app.storage.bindings
        self.trash = app.storage.trash
        self.guard = app.storage.guard

    # ------------------------------------------------------------------ entry
    def route(self, method: str, path: str, q, payload: dict, actor: dict, headers: dict):
        from ..app import fail
        try:
            return self._route(method, path, q, payload, actor, headers)
        except StorageError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)

    def _route(self, method, path, q, payload, actor, headers):
        from ..app import fail, integer
        match = ROOT_ROUTE.match(path)
        if match:
            return self.roots_route(method, match.group(1), bool(match.group(2)), q, payload, actor)
        if path == '/api/v2/library' and method in ('GET', 'HEAD'):
            return self.catalog.list_files(actor, {
                'scope': q('scope', 'shared'), 'root': q('root'), 'directory': q('directory'),
                'q': q('q'), 'status': q('status'), 'cursor': q('cursor'),
                'limit': integer(q('limit', '100'), '数量', 1, 200)})
        if path == '/api/v2/library/sources' and method in ('GET', 'HEAD'):
            return self.sources(actor)
        if path == '/api/v2/library/tree' and method in ('GET', 'HEAD'):
            return self.catalog.tree(actor, {
                'scope': q('scope', 'shared'), 'root': q('root'), 'path': q('path'),
                'cursor': q('cursor'), 'sync': q('sync', '1'),
                'limit': integer(q('limit', '200'), '数量', 1, 500)})
        if path == '/api/v2/library/scan' and method == 'POST':
            self.app.require_admin(actor)
            root_id = payload.get('rootId') or payload.get('root') or 'legacy'
            root = self.roots.get(root_id)
            return self.catalog.sync_directory(
                root, payload.get('path') or '',
                limit=integer(payload.get('limit', 500), '数量', 1, 2000),
                offset=integer(payload.get('offset', 0), '偏移', 0, 1000000))
        if path == '/api/v2/library/trash' and method in ('GET', 'HEAD'):
            return self.trash.list(actor, limit=integer(q('limit', '200'), '数量', 1, 500))
        match = RESTORE_ROUTE.match(path)
        if match:
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '恢复只支持 POST。')
            return self.trash.restore(actor, match.group(1))
        match = FILE_ROUTE.match(path)
        if match:
            return self.file_route(method, match.group(1), match.group(2), q, payload,
                                   actor, headers)
        if path.startswith('/api/v2/bindings'):
            return self.bindings_route(method, path, q, payload, actor)
        fail(404, 'NOT_FOUND', '存储接口不存在。')

    # ------------------------------------------------------------------ roots
    def roots_route(self, method, root_id, probe, q, payload, actor):
        from ..app import fail
        if root_id is None:
            if method in ('GET', 'HEAD'):
                self.app.require_admin(actor)
                return {'items': self.roots.all(), 'summary': self.roots.summary()}
            if method == 'POST':
                return self.roots.create(actor, payload)
            fail(405, 'METHOD_NOT_ALLOWED', '存储位置只支持读取或创建。')
        if probe:
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '预检只支持 POST。')
            return self.roots.probe(actor, root_id)
        if method in ('GET', 'HEAD'):
            self.app.require_admin(actor)
            return {'item': self.roots.get(root_id)}
        if method in ('PATCH', 'PUT'):
            return self.roots.update(actor, root_id, payload)
        fail(405, 'METHOD_NOT_ALLOWED', '存储位置只支持读取或修改。')

    def sources(self, actor: dict) -> dict:
        """Authorized logical sources: labels and counters, never physical paths."""
        shared = self.catalog.visible_roots(actor, scope='shared')
        private = self.catalog.visible_roots(actor, scope='private')
        admin = actor['role'] == 'admin'
        items = []
        for item in shared + private:
            items.append({'rootId': item['id'], 'label': item['label'], 'scope': item['scope'],
                          'state': item['state'], 'reason': item['reason'],
                          'files': item['files'], 'bytes': item['bytes'],
                          'indexed': item['indexed']})
        return {'items': items, 'admin': admin,
                'summary': {'roots': len(items),
                            'offline': sum(1 for item in items if item['state'] != 'online'),
                            'files': sum(item['files'] for item in items),
                            'bytes': sum(item['bytes'] for item in items)}}

    # ------------------------------------------------------------- file routes
    def file_route(self, method, file_id, action, q, payload, actor, headers):
        from ..app import fail, text
        if action == 'generations' and method in ('GET', 'HEAD'):
            meta = self.catalog.metadata(actor, file_id)
            return {'fileId': file_id, 'generation': meta['generation'],
                    'items': meta['generations']}
        if action == 'index' and method == 'POST':
            # Indexing by id: the path never comes from the request.
            meta = self.catalog.metadata(actor, file_id)
            if meta['scope'] == 'private' and meta['owner'] != actor['id']:
                fail(404, 'NOT_FOUND', '文件不存在。')
            if meta['rootState'] != 'online':
                fail(409, 'STORAGE_ROOT_OFFLINE', '存储位置离线，无法建立索引。')
            return self.app.index_jobs.create(actor, {'fileId': file_id})
        if action == 'content' and method in ('GET', 'HEAD'):
            return self.content(file_id, actor, headers)
        if action is None:
            if method in ('GET', 'HEAD'):
                return {'item': self.catalog.metadata(actor, file_id)}
            if method == 'DELETE':
                detach = q('detachBindings') == '1'
                return self.trash.delete(actor, file_id=file_id, detach_bindings=detach)
        fail(405, 'METHOD_NOT_ALLOWED', '该文件操作不受支持。')

    def content(self, file_id: str, actor: dict, headers: dict):
        """Authorized download with Range support, served from the right root."""
        from ..app import Response, fail
        meta = self.catalog.metadata(actor, file_id)
        if meta['rootState'] != 'online':
            fail(409, 'STORAGE_ROOT_OFFLINE', '存储位置离线，暂时无法下载。')
        root = self.roots.get(meta['rootId'])
        stream = self.guard.open_regular(root, meta['relPath'])
        info = os.fstat(stream.fileno())
        size = info.st_size
        etag = '"' + hashlib.sha256(
            ('%s:%s:%s' % (file_id, info.st_mtime_ns, size)).encode()).hexdigest()[:24] + '"'
        common = {'Content-Type': 'text/plain; charset=utf-8',
                  'Content-Disposition': "attachment; filename*=UTF-8''" + _quote(meta['displayName']),
                  'ETag': etag, 'Accept-Ranges': 'bytes'}
        if headers.get('if-none-match') == etag:
            stream.close()
            return Response(status=304, headers=common)
        start, end, status = 0, size - 1, 200
        requested = headers.get('range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            try:
                if not match or not any(match.groups()) or not size:
                    raise ValueError()
                left, right = match.groups()
                if not left:
                    count = int(right)
                    if count <= 0:
                        raise ValueError()
                    start = max(0, size - count)
                else:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                if start > end or start >= size:
                    raise ValueError()
            except ValueError:
                stream.close()
                return Response(status=416, headers={'Content-Range': 'bytes */%d' % size},
                                value={'error': {'code': 'INVALID_RANGE',
                                                 'message': '文件范围无效。', 'details': {}}})
            status = 206
            common['Content-Range'] = 'bytes %d-%d/%d' % (start, end, size)
        stream.seek(start)
        return Response(status=status, headers=common, stream=stream,
                        length=max(0, end - start + 1))

    # -------------------------------------------------------------- bindings
    def bindings_route(self, method, path, q, payload, actor):
        from ..app import fail, text
        if path == '/api/v2/bindings' and method in ('GET', 'HEAD'):
            rows = self.bindings._shared_rows()
            return {'items': rows, 'layerOrder': list(_layer_order())}
        if path == '/api/v2/bindings' and method in ('PUT', 'DELETE'):
            body = dict(payload)
            if method == 'DELETE':
                body['_deleted'] = True
            return self.bindings.write_shared(actor, body)
        if path == '/api/v2/bindings/personal':
            if method in ('GET', 'HEAD'):
                return {'items': self.bindings.personal_rows(actor)}
            if method in ('PUT', 'DELETE'):
                body = dict(payload)
                if method == 'DELETE':
                    body['_deleted'] = True
                return self.bindings.write_personal(actor, body)
        if path == '/api/v2/bindings/effective' and method in ('GET', 'HEAD'):
            root_id = text(q('rootId', 'legacy'), '存储位置', 64)
            rel = q('path', '')
            self.roots.get(root_id, with_state=False)
            return self.bindings.resolve(actor, root_id=root_id, rel_path=rel)
        if path == '/api/v2/bindings/personal/resolve' and method == 'POST':
            # Explain a hypothetical personal layer without persisting it: the
            # settings UI uses this to preview "only me" before saving.
            root_id = text(payload.get('rootId') or 'legacy', '存储位置', 64)
            rel = payload.get('path') or ''
            personal = payload.get('personal') or []
            if not isinstance(personal, list):
                fail(400, 'INVALID_INPUT', 'personal 必须是数组。')
            normalized = []
            for row in personal:
                if not isinstance(row, dict) or not isinstance(row.get('ruleId'), str):
                    fail(400, 'INVALID_INPUT', '个人绑定缺少 ruleId。')
                normalized.append({'scope': row.get('scope') or 'file', 'root_id': root_id,
                                   'path': row.get('path') or '', 'rule_id': row['ruleId'],
                                   'revision': row.get('revision') or 0})
            return self.bindings.resolve(actor, root_id=root_id, rel_path=rel,
                                         personal=normalized)
        fail(404, 'NOT_FOUND', '绑定接口不存在。')


def _layer_order():
    from .bindings import LAYERS
    return LAYERS


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe='')
