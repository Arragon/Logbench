"""Layered cache management for the R6 解析缓存 page (R6-082).

The page needs three things a reader view does not: which layer of which file is
current, why a layer is not current, and how to rebuild or drop it without touching
the source bytes.

Rules enforced here:

* **`raw` is not a cache.** It is the only layer that cannot be rebuilt, so it is
  neither listed as "purgeable" nor accepted by `purge`. Purging derived layers
  removes pointers, not bytes; the reader keeps serving a stale-but-readable version
  through its pin until that pin expires.
* **a rebuild is an index job, not a synchronous request.** `rebuild` creates the
  same job the index API creates, so progress lands in the same checkpoint the task
  panel already reads. Rebuilding `rules` reuses `structure` because the structure
  signature does not contain rule semantics — that is what makes "只重算异常层"
  true rather than aspirational.
* **state is never invented.** A file with no pointer and no attempt is `pending`,
  not `complete`. A failed attempt keeps its error code so the page can show
  `failed: coverage_mismatch` instead of a green row with zero rows.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .cache_versions import LAYERS, REBUILDABLE, CacheError, CacheRegistry
from ..settings import CACHE_QUOTA, SettingsService

SCHEMA = 'logbench.cache/1'
USAGE_SCHEMA = 'logbench.cache.usage/1'
DERIVED = ('structure', 'rules', 'search')
PAGE_MAX = 100
ATTEMPT_STATES = ('building', 'failed', 'cancelled', 'stale', 'superseded', 'revoked')
# The quota is a warning threshold, not a hard stop (see `usage()`), so the
# default is generous: a 1 GiB derived cache covers roughly a million framed
# lines before an operator has to think about it.
DEFAULT_QUOTA_BYTES = 1024 ** 3
MIN_QUOTA_BYTES = 16 * 1024 ** 2
MAX_QUOTA_BYTES = 1024 ** 4

STATUS = {'invalid_layer': 400, 'raw_not_purgeable': 400, 'confirmation_required': 400,
          'not_found': 404, 'root_offline': 409, 'nothing_to_rebuild': 409,
          'pinned': 409, 'storage_unavailable': 503, 'invalid_quota': 400,
          'version_pinned': 409}


class CacheApiError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class CacheApi:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.registry = CacheRegistry(app.store)
        self.settings = SettingsService(app)

    # ------------------------------------------------------------------ routing
    def route(self, method: str, path: str, q, payload: dict, actor: dict):
        from ..app import fail
        from ..storage.guard import StorageError
        try:
            return self._route(method, path, q, payload, actor)
        except CacheApiError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)
        except CacheError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)
        except StorageError as exc:
            # An unauthorized or unknown file is the same answer: not found.
            fail(404, 'NOT_FOUND', '文件不存在。')

    def _route(self, method: str, path: str, q, payload: dict, actor: dict):
        from ..app import fail, integer
        if path == '/api/v2/cache':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '缓存列表只支持读取。')
            self.app.require_admin(actor)
            return self.list(actor, {
                'scope': q('scope', 'shared'), 'q': q('q'), 'cursor': q('cursor', ''),
                'limit': integer(q('limit', '50'), '数量', 1, PAGE_MAX),
                'layer': q('layer'), 'state': q('state')})
        if path == '/api/v2/cache/usage' and method in ('GET', 'HEAD'):
            # Readable by every signed-in principal: the header meter is visible to
            # all of them, and the payload carries totals only — no file names, no
            # paths, nothing that would leak which logs exist.
            return self.usage(actor)
        if path == '/api/v2/cache/quota':
            if method in ('GET', 'HEAD'):
                return self.quota()
            if method in ('PUT', 'POST'):
                self.app.require_admin(actor)
                return self.set_quota(actor, payload)
            fail(405, 'METHOD_NOT_ALLOWED', '缓存限额只支持读取或修改。')
        if path == '/api/v2/cache/rebuild':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '重建只支持 POST。')
            self.app.require_admin(actor)
            return self.rebuild(actor, payload)
        if path == '/api/v2/cache/purge':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '清理只支持 POST。')
            self.app.require_admin(actor)
            return self.purge(actor, payload)
        fail(404, 'NOT_FOUND', '缓存接口不存在。')

    # --------------------------------------------------------------------- list
    def list(self, actor: dict, params: dict) -> dict:
        scope = params.get('scope') or 'shared'
        if scope not in ('shared', 'private'):
            raise CacheApiError('invalid_scope', '范围只支持 shared 或 private。')
        limit = max(1, min(PAGE_MAX, int(params.get('limit') or 50)))
        cursor = _decode_cursor(params.get('cursor') or '')
        where = ["f.state<>'trashed'", 'f.scope=?']
        args: list = [scope]
        if scope == 'private':
            where.append('f.owner=?')
            args.append(actor['id'])
        if params.get('q'):
            needle = '%' + _escape_like(params['q']) + '%'
            where.append("(f.rel_path LIKE ? ESCAPE '\\' OR f.display_name LIKE ? ESCAPE '\\')")
            args.extend([needle, needle])
        if cursor:
            where.append('(f.root_id>? OR (f.root_id=? AND f.rel_path>?))')
            args.extend([cursor['root'], cursor['root'], cursor['path']])
        sql = ('SELECT f.id, f.root_id, f.rel_path, f.display_name, f.scope, f.owner, f.size, '
               'f.generation, f.observed_at, r.label AS root_label, r.enabled AS root_enabled '
               'FROM logical_files f JOIN storage_roots r ON r.id=f.root_id '
               'WHERE ' + ' AND '.join(where) +
               ' ORDER BY f.root_id, f.rel_path LIMIT ?')
        args.append(limit + 1)
        with self.store.connect() as db:
            rows = db.execute(sql, args).fetchall()
        page, more = rows[:limit], len(rows) > limit
        if not page:
            return {'schema': SCHEMA, 'items': [], 'nextCursor': None, 'scope': scope,
                    'total': 0, 'summary': {'files': 0, 'byCoverage': {}}}
        items = [self._item(actor, row, scope) for row in page]
        items = self._apply_filters(items, params)
        next_cursor = _encode_cursor(page[-1]['root_id'], page[-1]['rel_path']) if more else None
        return {'schema': SCHEMA, 'items': items, 'nextCursor': next_cursor, 'scope': scope,
                'total': len(items), 'summary': self._summary(items, scope),
                'filters': {'layer': params.get('layer') or None,
                            'state': params.get('state') or None}}

    @staticmethod
    def _apply_filters(items: list, params: dict) -> list:
        layer = params.get('layer')
        state = params.get('state')
        if not layer and not state:
            return items
        out = []
        for item in items:
            layers = item['layers']
            if layer:
                target = layers.get(layer)
                if not target:
                    continue
                if state and target['state'] != state:
                    continue
            elif state:
                if not any(entry['state'] == state for entry in layers.values()):
                    continue
            out.append(item)
        return out

    @staticmethod
    def _summary(items: list, scope: str) -> dict:
        by_coverage: dict = {}
        totals = {'complete': 0, 'failed': 0, 'pending': 0, 'stale': 0}
        for item in items:
            by_coverage[item['coverage']] = by_coverage.get(item['coverage'], 0) + 1
            for entry in item['layers'].values():
                if entry['state'] in totals:
                    totals[entry['state']] += 1
        return {'files': len(items), 'byCoverage': by_coverage, 'byState': totals, 'scope': scope}

    # ------------------------------------------------------------------- detail
    def _item(self, actor: dict, row, scope: str) -> dict:
        file_id = row['id']
        pointers = self._pointers(file_id, scope)
        attempts = self._attempts(file_id, scope)
        pins = self._pins(file_id, scope)
        layers = {}
        for layer in DERIVED:
            pointer = pointers.get(layer)
            attempt = attempts.get(layer)
            entry = {
                'state': 'pending', 'version': None, 'rows': 0, 'bytes': None,
                'publishedAt': None, 'signature': None, 'reason': None,
                'attempt': None,
            }
            if pointer:
                entry.update({
                    'state': 'complete', 'version': pointer['version_id'],
                    'rows': pointer['rows'], 'bytes': pointer['bytes'] or None,
                    'publishedAt': pointer['published_at'],
                    'signature': pointer['signature'],
                })
            if attempt and attempt['version_id'] != (pointer or {}).get('version_id'):
                # An attempt that is not the published version is the explanation for
                # "why this layer is not current"; it never replaces the pointer.
                entry['attempt'] = {
                    'state': attempt['state'], 'version': attempt['version_id'],
                    'createdAt': attempt['created_at'],
                    'signature': attempt['signature'],
                    'error': (attempt['stats'] or {}).get('error'),
                }
                failed = attempt['state'] == 'failed'
                if entry['state'] == 'complete':
                    # The old version is still readable; the new attempt did not land.
                    entry['state'] = 'stale' if failed else entry['state']
                elif attempt['state'] == 'building':
                    entry['state'] = 'building'
                elif failed:
                    entry['state'] = 'failed'
                else:
                    entry['state'] = 'pending'
            if pins.get(layer):
                entry['pins'] = pins[layer]
            if entry['state'] == 'pending':
                entry['reason'] = '该层尚未建立。'
            elif entry['state'] == 'building':
                entry['reason'] = '正在重建；当前仍可读取上一版本（如有）。'
            elif entry['state'] == 'failed':
                error = (entry.get('attempt') or {}).get('error')
                entry['reason'] = '上次重建失败（%s）。' % (error or '未记录原因')
            elif entry['state'] == 'stale':
                error = (entry.get('attempt') or {}).get('error')
                entry['reason'] = ('规则或原文已变更，该层需要重建' +
                                   ('（上次失败：%s）' % error if error else '') + '。')
            layers[layer] = entry
        coverage = 'none'
        for layer in DERIVED:
            if layers[layer]['state'] == 'complete':
                coverage = layer
        shard_bytes = self._shard_bytes(pointers.get('structure'))
        return {
            'fileId': file_id,
            'rootId': row['root_id'],
            'rootLabel': row['root_label'],
            'scope': row['scope'],
            'displayName': row['display_name'],
            'relPath': row['rel_path'] if actor['role'] == 'admin' or row['scope'] == 'shared' else None,
            'size': row['size'],
            'generation': row['generation'],
            'observedAt': row['observed_at'],
            'state': 'active',
            'coverage': coverage,
            'layers': layers,
            'shardBytes': shard_bytes,
            'purgeable': [layer for layer in ('rules', 'search')
                          if layers[layer]['version']],
            'rootEnabled': bool(row['root_enabled']),
            'missing': (self._raw_present(file_id, scope) is False),
        }

    def _pointers(self, file_id: str, scope: str) -> dict:
        import json as _json
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT p.layer, p.version_id, v.rows, v.bytes, v.published_at, v.signature, '
                'v.stats, v.created_at FROM layer_pointers p '
                'LEFT JOIN layer_versions v ON v.version_id=p.version_id '
                'WHERE p.file_id=? AND p.scope=?', (file_id, scope)).fetchall()
        return {row['layer']: {'version_id': row['version_id'], 'rows': row['rows'] or 0,
                               'bytes': row['bytes'] or 0,
                               'published_at': row['published_at'],
                               'signature': row['signature'], 'layer': row['layer'],
                               'stats': _json.loads(row['stats'] or '{}')}
                for row in rows}

    def _attempts(self, file_id: str, scope: str) -> dict:
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT * FROM layer_versions WHERE file_id=? AND scope=? '
                'ORDER BY created_at DESC', (file_id, scope)).fetchall()
        out: dict = {}
        import json as _json
        for row in rows:
            if row['layer'] in out:
                continue
            out[row['layer']] = {
                'version_id': row['version_id'], 'state': row['state'],
                'created_at': row['created_at'], 'signature': row['signature'],
                'stats': _json.loads(row['stats'] or '{}'),
            }
        return out

    def _pins(self, file_id: str, scope: str, layer: Optional[str] = None) -> dict:
        sql = ('SELECT v.layer, p.token, p.owner, p.expires_at FROM layer_pins p '
               'JOIN layer_versions v ON v.version_id=p.version_id '
               'WHERE v.file_id=? AND v.scope=? AND p.expires_at>?')
        args = [file_id, scope, time.time()]
        if layer:
            sql += ' AND v.layer=?'
            args.append(layer)
        with self.store.connect() as db:
            rows = db.execute(sql, tuple(args)).fetchall()
        out: dict = {}
        for row in rows:
            out.setdefault(row['layer'], []).append(
                {'token': row['token'], 'owner': row['owner'], 'expiresAt': row['expires_at']})
        return out

    def _scope_of(self, file_id: str, layer: str) -> str:
        with self.store.connect() as db:
            row = db.execute('SELECT scope FROM layer_pointers WHERE file_id=? AND layer=?',
                             (file_id, layer)).fetchone()
        return row['scope'] if row else 'shared'

    def _raw_present(self, file_id: str, scope: str) -> Optional[bool]:
        pointer = self.registry.pointer(file_id, 'raw', scope=scope)
        return pointer is not None

    @staticmethod
    def _shard_bytes(pointer: Optional[dict]) -> Optional[int]:
        if not pointer:
            return None
        path = (pointer or {}).get('stats', {}).get('shard')
        if not path:
            return None
        try:
            return os.stat(str(Path(path))).st_size
        except OSError:
            return None

    # -------------------------------------------------------------------- usage
    def usage(self, actor: dict) -> dict:
        """Derived-cache bytes as the operator sees them, not as one file sees them.

        `raw` is excluded on purpose: it cannot be rebuilt, so counting it would
        mean the meter only drops when a log is deleted, and would invite an
        operator to "clean" bytes that are really source data.

        Exceeding the quota is a warning, never a refusal. Refusing to index
        because a cache is full would turn a background optimisation into an
        outage for the operator who set the number.
        """
        marks = ','.join('?' * len(DERIVED))
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT layer,COUNT(*) AS n,COALESCE(SUM(bytes),0) AS bytes '
                'FROM layer_versions WHERE layer IN (%s) GROUP BY layer' % marks,
                DERIVED).fetchall()
            published = db.execute(
                'SELECT v.layer AS layer,COALESCE(SUM(v.bytes),0) AS bytes '
                'FROM layer_pointers p JOIN layer_versions v ON v.version_id=p.version_id '
                'WHERE v.layer IN (%s) GROUP BY v.layer' % marks, DERIVED).fetchall()
            files = db.execute(
                'SELECT COUNT(DISTINCT file_id) AS n FROM layer_versions '
                'WHERE layer IN (%s)' % marks, DERIVED).fetchone()
        layers = {name: {'bytes': 0, 'versions': 0, 'publishedBytes': 0} for name in DERIVED}
        total = 0
        for row in rows:
            entry = layers.setdefault(row['layer'], {'bytes': 0, 'versions': 0,
                                                     'publishedBytes': 0})
            entry['bytes'] = int(row['bytes'] or 0)
            entry['versions'] = int(row['n'] or 0)
            total += entry['bytes']
        published_total = 0
        for row in published:
            entry = layers.setdefault(row['layer'], {'bytes': 0, 'versions': 0,
                                                     'publishedBytes': 0})
            entry['publishedBytes'] = int(row['bytes'] or 0)
            published_total += entry['publishedBytes']
        quota = self.settings.get_int(CACHE_QUOTA, DEFAULT_QUOTA_BYTES)
        unlimited = quota is None or quota <= 0
        return {
            'schema': USAGE_SCHEMA,
            'usedBytes': total,
            'publishedBytes': published_total,
            'quotaBytes': None if unlimited else quota,
            'unlimited': unlimited,
            'ratio': None if unlimited else round(total / float(quota), 4),
            'overQuota': (not unlimited) and total > quota,
            # Stated in the payload so the UI does not have to hardcode the policy.
            'enforcement': 'warn',
            'files': int(files['n'] or 0),
            'layers': layers,
            'observedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }

    def quota(self) -> dict:
        value = self.settings.get_int(CACHE_QUOTA, DEFAULT_QUOTA_BYTES)
        return {'schema': USAGE_SCHEMA, 'quotaBytes': value,
                'unlimited': value is None or value <= 0,
                'defaultBytes': DEFAULT_QUOTA_BYTES,
                'minBytes': MIN_QUOTA_BYTES, 'maxBytes': MAX_QUOTA_BYTES}

    def set_quota(self, actor: dict, body: dict) -> dict:
        raw = body.get('quotaBytes')
        if raw is None and 'bytes' in body:
            raw = body.get('bytes')
        if raw is None or raw == '' or raw == 'unlimited':
            value = None
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise CacheApiError('invalid_quota', '限额必须是字节数，或 null 表示不限。')
            if value < MIN_QUOTA_BYTES:
                raise CacheApiError('invalid_quota',
                                    '限额不能小于 16 MiB；如需不限请传 null。',
                                    minBytes=MIN_QUOTA_BYTES)
            if value > MAX_QUOTA_BYTES:
                raise CacheApiError('invalid_quota', '限额超出可配置范围。',
                                    maxBytes=MAX_QUOTA_BYTES)
        saved = self.settings.set(actor, CACHE_QUOTA, value)
        return {'schema': USAGE_SCHEMA, 'quotaBytes': saved['value'],
                'unlimited': value is None, 'revision': saved.get('revision'),
                'enforcement': 'warn'}

    # ------------------------------------------------------------------ rebuild
    def rebuild(self, actor: dict, body: dict) -> dict:
        from ..app import fail, text
        layers = _layers_of(body.get('layers') or ['rules'])
        file_id = text(body.get('fileId'), '文件标识', 64)
        meta = self.app.storage.catalog.metadata(actor, file_id)
        if meta['rootState'] != 'online':
            raise CacheApiError('root_offline', '存储位置离线，无法重建索引。',
                                rootId=meta['rootId'])
        payload = {'fileId': file_id}
        action = body.get('actionId')
        if isinstance(action, str) and action.strip():
            payload['actionId'] = action.strip()[:96]
        created = self.app.index_jobs.create(actor, payload)
        with self.store.transaction() as db:
            self.store.audit(db, actor['id'], 'cache.rebuild', file_id,
                             {'layers': layers, 'job': created['item']['id']})
        return {
            'schema': SCHEMA, 'fileId': file_id, 'layers': layers,
            'item': created['item'], 'accepted': True,
            'note': ('重建复用未受影响的层：改规则不会重分帧，改原文才重建结构层。'
                     if 'structure' not in layers else '结构层重建需要重新分帧原文。'),
        }

    # -------------------------------------------------------------------- purge
    def purge(self, actor: dict, body: dict) -> dict:
        from ..app import text
        file_id = text(body.get('fileId'), '文件标识', 64)
        layers = _layers_of(body.get('layers') or [])
        if body.get('confirm') not in ('purge-index', 'purge-derived'):
            raise CacheApiError('confirmation_required',
                                '清理缓存需要显式确认（confirm=purge-derived），且不会删除原文。')
        if 'raw' in layers:
            raise CacheApiError('raw_not_purgeable', '原文不是缓存，不能通过清理缓存删除。')
        self.app.storage.catalog.metadata(actor, file_id)
        removed, pinned = [], []
        for layer in layers:
            if layer not in REBUILDABLE:
                raise CacheApiError('invalid_layer', '该层不可清理：%s' % layer, layer=layer)
            scope = self._scope_of(file_id, layer)
            pins = self._pins(file_id, scope, layer)
            result = self.registry.invalidate(file_id, layer, scope=scope,
                                              reason='purge_by_%s' % actor['id'])
            if result.get('invalidated'):
                removed.append({'layer': layer, 'version': result.get('version')})
            if pins:
                pinned.append({'layer': layer, 'refs': pins})
        with self.store.transaction() as db:
            self.store.audit(db, actor['id'], 'cache.purge', file_id,
                             {'layers': layers, 'removed': len(removed)})
        return {
            'schema': SCHEMA, 'fileId': file_id, 'layers': layers,
            'removed': removed,
            'pinned': pinned,
            'rawKept': True,
            'note': ('已移除派生层指针，原文与分片未删除；'
                     '存在活动引用时旧版本保留到引用过期，阅读不会中断。'),
        }


def _layers_of(values) -> list:
    if not isinstance(values, list) or not values:
        raise CacheApiError('invalid_layer', '请至少指定一个缓存层。')
    out = []
    for value in values:
        if value not in LAYERS:
            raise CacheApiError('invalid_layer', '未知缓存层：%s' % value, layer=value)
        if value not in out:
            out.append(value)
    return out


def _escape_like(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _encode_cursor(root_id: str, rel_path: str) -> str:
    import base64
    raw = ('%s\0%s' % (root_id, rel_path)).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii')


def _decode_cursor(value: str) -> Optional[dict]:
    if not value:
        return None
    import base64
    try:
        raw = base64.urlsafe_b64decode(value.encode('ascii')).decode('utf-8')
    except (ValueError, UnicodeError):
        raise CacheApiError('invalid_cursor', '分页游标无效。')
    root, _, path = raw.partition('\0')
    if not root:
        raise CacheApiError('invalid_cursor', '分页游标无效。')
    return {'root': root, 'path': path}
