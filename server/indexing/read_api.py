"""Authorized record, raw-range, timeline and index-status API (R6-040).

Design constraints that show up in the code:

* **no paths on the wire.** A request names an opaque file id or record id; the
  file's location is resolved from the `raw` layer the principal registered when
  the index job was created. `..`, absolute paths and symlinks never reach here.
* **authorization first, and indistinguishable when it fails.** Every route
  resolves the `raw` layer for the calling principal before doing anything else.
  A missing file, another principal's file and a revoked grant all return the same
  `not_found`, because "forbidden" already tells an attacker the file exists.
* **a missing index is `pending`, not an empty result.** An empty timeline that
  means "not indexed yet" is the single most misleading thing a log viewer can
  show, so it says `pending` and the UI can wait.
* **a clipped payload is not a complete payload.** Raw reads and record bodies
  carry `truncated` and a cursor, and the byte/line boundaries are reported
  exactly, including when a range starts or ends mid-codepoint.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .cache_versions import CacheError, CacheRegistry
from .shards import ShardError, ShardReader

MAX_RAW_BYTES = 256 * 1024
MAX_BODY_BYTES = 64 * 1024
TIMELINE_PAGES = (20, 50, 100, 150)
RECORD_ID = r'^[A-Za-z0-9_-]{4,64}:[0-9]{1,12}$'


class ReadApi:
    def __init__(self, app, *, registry: Optional[CacheRegistry] = None):
        self.app = app
        self.registry = registry or CacheRegistry(app.store)

    # ------------------------------------------------------------------ routing
    def route(self, method: str, path: str, q, actor: dict) -> dict:
        from ..app import fail
        # The only write on this surface is "index this file", which is a control
        # action on an already-authorized file, not a data upload.
        if method not in ('GET', 'HEAD') and not (method == 'POST' and path.endswith('/index')):
            fail(405, 'METHOD_NOT_ALLOWED', '读取接口只支持 GET。')
        if not path.startswith('/api/v2/'):
            fail(404, 'NOT_FOUND', '接口不存在。')
        rest = path[len('/api/v2/'):]
        if rest.startswith('records/'):
            return self.record(rest[len('records/'):], q, actor)
        if rest.startswith('files/'):
            return self.file_route(rest[len('files/'):], q, actor, method)
        fail(404, 'NOT_FOUND', '接口不存在。')

    # -------------------------------------------------------------------- files
    def file_route(self, rest: str, q, actor: dict, method: str = 'GET') -> dict:
        from ..app import fail, integer
        parts = [part for part in rest.split('/') if part]
        if not parts:
            fail(404, 'NOT_FOUND', '文件不存在。')
        file_id = parts[0]
        if len(parts) == 1:
            return self.status(file_id, actor)
        name = parts[1]
        if name == 'index' and method == 'POST':
            # Indexing is initiated by file id, never by a path from the client: the
            # authorized relative path is already stored with the `raw` layer.
            version, _, rel_path, _ = self._access(file_id, actor, require_shard=False)
            return self.app.index_jobs.create(actor, {'path': rel_path})
        if name == 'records':
            return self.records(file_id, actor, q)
        if name == 'timeline':
            page_size = integer(q('pageSize', '50'), '每页数量', 1, 150)
            if page_size not in TIMELINE_PAGES:
                fail(400, 'INVALID_PAGE_SIZE', '时间线每页只支持 20/50/100/150。')
            page = integer(q('page', '0'), '页码', 0, 100000)
            return self.timeline(file_id, actor, page=page, page_size=page_size)
        if name == 'generations':
            return self.generations(file_id, actor)
        if name == 'seek':
            return self.seek(file_id, actor, q)
        fail(404, 'NOT_FOUND', '接口不存在。')

    def status(self, file_id: str, actor: dict) -> dict:
        version, shard, rel_path, structure_version = self._access(file_id, actor,
                                                                   require_shard=False)
        if shard is None:
            # "Not indexed yet" is a state, not an error: the reader can show
            # `pending` and wait, instead of an empty list that looks like a result.
            return {'file_id': file_id, 'generation': version['generation'],
                    'layers': {name: {'state': 'pending', 'version': None, 'rows': None,
                                      'signature': None}
                               for name in ('structure', 'rules', 'search')},
                    'coverage': {'state': 'pending', 'record_count': 0, 'relation': 'unknown'},
                    'display_name': Path(rel_path).name, 'bytes': None}
        layers = {}
        coverage = 'pending'
        reader = None
        try:
            reader = ShardReader(shard)
            stored = reader.layers()
        except ShardError:
            stored = {}
        for name in ('structure', 'rules', 'search'):
            published = self.registry.pointer(file_id, name)
            layer = stored.get(name) or {}
            layers[name] = {
                'state': published['state'] if published else ('indexing' if layer else 'pending'),
                'version': published['version_id'] if published else None,
                'rows': layer.get('rows'),
                'signature': published['signature'] if published else layer.get('signature'),
            }
        if layers['structure']['state'] == 'complete' and layers['rules']['state'] == 'complete':
            coverage = 'complete'
        elif layers['structure']['state'] == 'complete':
            coverage = 'partial'
        record_count = reader.count() if reader else 0
        with self.app.store.connect() as db:
            row = db.execute('SELECT size FROM logical_files WHERE id=?', (file_id,)).fetchone()
        return {
            'file_id': file_id,
            'generation': version['generation'],
            'layers': layers,
            'coverage': {'state': coverage, 'record_count': record_count,
                         'relation': 'exact' if coverage != 'pending' else 'unknown'},
            'display_name': Path(rel_path).name,
            'bytes': row['size'] if row else None,
        }

    def records(self, file_id: str, actor: dict, q) -> dict:
        """Paged headers by ordinal, or a specific set by `ordinals=`.

        The cursor is the last ordinal served, not a byte offset: the table can be
        reopened at any point without the server remembering where a reader was.
        """
        from ..app import fail, integer
        version, shard, _, _ = self._access(file_id, actor)
        limit = integer(q('limit', '200'), '单页数量', 1, 2000)
        wanted = q('ordinals', '')
        if wanted:
            try:
                ordinals = [int(item) for item in wanted.split(',') if item]
            except ValueError:
                fail(400, 'INVALID_INPUT', 'ordinals 必须是逗号分隔的整数。')
            if len(ordinals) > 2000:
                fail(400, 'PAGE_TOO_LARGE', '单次最多取 2000 条。')
            items = ShardReader(shard).records_for(ordinals)
            return {'items': [_header(row) for row in items], 'next_cursor': None,
                    'count': {'matched': len(items), 'returned': len(items), 'relation': 'exact'},
                    'coverage': {'complete': True}, 'generation': version['generation']}
        cursor = integer(q('cursor', '0'), '游标', 0, 1 << 50)
        rows = ShardReader(shard).page(cursor, limit)
        next_cursor = (rows[-1]['ordinal'] + 1) if len(rows) == limit else None
        return {'items': [_header(row) for row in rows], 'next_cursor': next_cursor,
                'count': {'matched': None, 'returned': len(rows), 'relation': 'at_least'},
                'coverage': {'complete': True}, 'generation': version['generation']}

    def timeline(self, file_id: str, actor: dict, *, page: int, page_size: int) -> dict:
        version, shard, _, _ = self._access(file_id, actor)
        rules = self.registry.pointer(file_id, 'rules')
        if not rules:
            # Explicitly pending: an empty node list here would read as "no errors".
            return {'file_id': file_id, 'generation': version['generation'], 'nodes': [],
                    'page': page, 'page_size': page_size, 'total': 0,
                    'has_prev': False, 'has_next': False,
                    'count': {'relation': 'unknown', 'matched': None},
                    'coverage': {'state': 'pending', 'complete': False}}
        try:
            reader = ShardReader(shard)
            result = reader.rule_timeline(rules['version_id'], page, page_size)
        except ShardError as exc:
            from ..app import fail
            fail(404, 'NOT_FOUND', '索引尚未就绪。', reason=exc.code)
        result.update({'file_id': file_id, 'generation': version['generation'],
                       'version': rules['version_id'],
                       'coverage': {'state': 'complete', 'complete': True}})
        return result

    def generations(self, file_id: str, actor: dict) -> dict:
        version, _, _, _ = self._access(file_id, actor)
        with self.app.store.connect() as db:
            rows = db.execute('SELECT generation,raw_bytes,state,created_at FROM generations '
                              'WHERE file_id=? ORDER BY generation DESC LIMIT 50', (file_id,)).fetchall()
        return {'file_id': file_id, 'current': version['generation'],
                'items': [{'generation': row['generation'], 'bytes': row['raw_bytes'],
                           'state': row['state'], 'created_at': row['created_at']} for row in rows]}

    def seek(self, file_id: str, actor: dict, q) -> dict:
        from ..app import fail, integer
        version, shard, _, _ = self._access(file_id, actor)
        line = q('line', '')
        byte = q('byte', '')
        if bool(line) == bool(byte):
            fail(400, 'INVALID_INPUT', '请提供 line 或 byte 之一。')
        try:
            reader = ShardReader(shard)
            if line:
                record = reader.seek_line(integer(line, '行号', 1, 1 << 40))
            else:
                record = reader.seek_byte(integer(byte, '字节偏移', 0, 1 << 50))
        except ShardError as exc:
            fail(409, 'INDEX_PENDING', '索引尚未完成，无法定位。', reason=exc.code)
        if not record:
            return {'file_id': file_id, 'generation': version['generation'], 'found': False,
                    'requested': {'line': line or None, 'byte': byte or None},
                    'coverage': {'state': 'partial'}}
        return {'file_id': file_id, 'generation': version['generation'], 'found': True,
                'record_id': record['record_id'], 'ordinal': record['ordinal'],
                'lines': record['lines'], 'bytes': record['bytes'],
                'requested': {'line': line or None, 'byte': byte or None},
                'coverage': {'state': 'complete'}}

    # ------------------------------------------------------------------ records
    def record(self, rest: str, q, actor: dict) -> dict:
        from ..app import fail, integer
        import re
        parts = [part for part in rest.split('/') if part]
        if not parts:
            fail(404, 'NOT_FOUND', '记录不存在。')
        record_id = parts[0]
        if not re.fullmatch(RECORD_ID, record_id):
            fail(404, 'NOT_FOUND', '记录不存在。')
        file_id, _, ordinal = record_id.rpartition(':')
        version, shard, rel_path, structure_version = self._access(file_id, actor)
        try:
            reader = ShardReader(shard)
        except ShardError:
            fail(409, 'INDEX_PENDING', '索引尚未完成。')
        structure = reader.get(int(ordinal))
        if not structure or structure['record_id'] != record_id:
            fail(404, 'NOT_FOUND', '记录不存在。')
        if len(parts) > 1 and parts[1] == 'raw':
            start = integer(q('start', '-1'), '起始字节', -1, 1 << 50)
            end = integer(q('end', '-1'), '结束字节', -1, 1 << 50)
            limit = integer(q('max', str(MAX_RAW_BYTES)), '响应上限', 1, MAX_RAW_BYTES)
            return self.raw(structure, rel_path, start=start, end=end, limit=limit,
                            version=version, file_id=file_id)
        if len(parts) > 1:
            fail(404, 'NOT_FOUND', '接口不存在。')
        rules_version = q('version', '') or None
        rules = None
        if rules_version:
            try:
                rules = self.registry.read(file_id, 'rules', owner=actor['id'],
                                           version_id=rules_version)
            except CacheError:
                # Never answer with a different version than the one asked for.
                fail(404, 'NOT_FOUND', '记录不存在。')
        else:
            rules = self.registry.pointer(file_id, 'rules')
        row = reader.rule_row(rules['version_id'], int(ordinal)) if rules else None
        return self._detail(structure, row, version, file_id, rules, structure_version)

    def _detail(self, structure: dict, rule_row: Optional[dict], version: dict,
                file_id: str, rules: Optional[dict],
                structure_version: Optional[dict] = None) -> dict:
        body = structure.get('body') or []
        body_bytes = 0
        cut = 0
        for index, line in enumerate(body):
            body_bytes += len(line.encode('utf-8'))
            if body_bytes > MAX_BODY_BYTES:
                cut = index
                break
            cut = index + 1
        truncated = cut < len(body)
        fields = (rule_row or {}).get('fields') or structure.get('fields') or []
        return {
            'file_id': file_id,
            'generation': version['generation'],
            'version': {'structure': (structure_version or {}).get('version_id'),
                        'rules': rules['version_id'] if rules else None},
            'record': {
                'record_id': structure['record_id'],
                'ordinal': structure['ordinal'],
                'version': structure['version'],
                'provisional': False,
                'level': structure['level'],
                'ts': structure['ts'],
                'ts_ns': structure['ts_ns'],
                'logger': structure['logger'],
                'fn': structure['fn'],
                'src': structure['src'],
                'src_line': structure['src_line'],
                'lines': structure['lines'],
                'bytes': structure['bytes'],
                'message': structure['message'],
                'has_trace': structure['has_trace'],
                'fields': fields,
                'issue': (rule_row or {}).get('issue'),
                'issue_severity': (rule_row or {}).get('issueSeverity'),
                'node': (rule_row or {}).get('node'),
            },
            'body': {'items': body[:cut], 'truncated': truncated,
                     'bytes': body_bytes if not truncated else sum(
                         len(line.encode('utf-8')) for line in body[:cut]),
                     'next': cut if truncated else None, 'total': len(body)},
            'coverage': {'state': 'complete'},
        }

    # ---------------------------------------------------------------------- raw
    def raw(self, structure: dict, rel_path: str, *, start: int, end: int, limit: int,
            version: dict, file_id: str) -> dict:
        from ..app import fail
        record_start = int(structure['bytes']['start'] or 0)
        record_end = int(structure['bytes']['end'] or 0)
        begin = record_start if start < 0 else max(record_start, min(start, record_end))
        finish = record_end if end < 0 else max(begin, min(end, record_end))
        try:
            handle = self.app.open_file(rel_path)
        except Exception:
            fail(404, 'NOT_FOUND', '原文不可读。')
        try:
            handle.seek(begin)
            chunk = handle.read(max(0, min(finish - begin, limit + 4)))
        finally:
            handle.close()
        text, consumed, adjusted = _decode_range(chunk, begin, finish)
        clipped = consumed < (finish - begin)
        line_first = int(structure['lines']['first'] or 1) + _count_newlines(
            self._prefix(rel_path, record_start, begin))
        # A range that ends exactly on a newline has finished that line; counting
        # the terminator as another line would report one line too many.
        inner = _count_newlines(text.encode('utf-8'))
        if text.endswith('\n'):
            inner = max(0, inner - 1)
        line_last = line_first + inner
        return {
            'file_id': file_id,
            'generation': version['generation'],
            'record_id': structure['record_id'],
            'range': {'start': begin, 'end': begin + consumed,
                      'requested': {'start': start, 'end': end},
                      'adjusted_start': adjusted},
            'lines': {'first': line_first, 'last': line_last},
            'text': text,
            'truncated': clipped or (finish - begin) > limit,
            'next': (begin + consumed) if (clipped or (finish - begin) > limit) else None,
            'complete': not clipped and (finish - begin) <= limit,
        }

    def _prefix(self, rel_path: str, start: int, end: int) -> bytes:
        """Newlines between a record's start and the requested range start."""
        if end <= start:
            return b''
        try:
            handle = self.app.open_file(rel_path)
        except Exception:
            return b''
        try:
            handle.seek(start)
            return handle.read(end - start)
        finally:
            handle.close()

    # ------------------------------------------------------------------- access
    def _access(self, file_id: str, actor: dict, *, require_shard: bool = True):
        """Resolve the caller's access record, or fail as if the file is absent.

        The `raw` layer is the access record: it is created when a principal
        registers a file for indexing, it is never collected, and it carries the
        authorized relative path — so no route ever accepts a path from the wire.
        """
        from ..app import fail, text
        text(file_id, '文件标识', 64)
        try:
            version = self.registry.read(file_id, 'raw', owner=actor['id'])
        except CacheError:
            fail(404, 'NOT_FOUND', '文件不存在。')
        rel_path = (version.get('deps') or {}).get('rel_path')
        if not rel_path:
            fail(404, 'NOT_FOUND', '文件不存在。')
        structure_version = self.registry.pointer(file_id, 'structure')
        shard = (version.get('stats') or {}).get('shard') \
            or (structure_version or {}).get('stats', {}).get('shard')
        if not shard:
            if require_shard:
                fail(409, 'INDEX_PENDING', '该文件尚未建立索引。')
            return version, None, rel_path, structure_version
        return version, Path(shard), rel_path, structure_version


def _decode_range(chunk: bytes, begin: int, finish: int) -> tuple:
    """Decode a byte range that may start or end mid-codepoint.

    A caller that asks for byte 10 in the middle of a three-byte character gets a
    start moved forward to the next character boundary and told so, rather than a
    replacement character that looks like corrupted data. The same holds at the
    tail: an incomplete trailing sequence is dropped and reported through the
    returned consumed length, so the caller can continue from an exact offset.
    """
    offset = 0
    while offset < len(chunk) and 0x80 <= chunk[offset] <= 0xBF:
        offset += 1
    adjusted = begin + offset if offset else None
    window = bytes(chunk[offset:])
    end = len(window)
    while end > 0:
        try:
            text = window[:end].decode('utf-8')
        except UnicodeDecodeError as exc:
            if exc.reason == 'unexpected end of data':
                end = exc.start
                continue
            # Broken bytes in the middle of the range: replace and keep going,
            # because refusing to show anything is worse than showing it marked.
            text = window[:end].decode('utf-8', errors='replace')
            break
        else:
            break
    else:
        text = ''
    return text, offset + end, adjusted


def _count_newlines(data: bytes) -> int:
    return data.count(b'\n')


def _header(record: dict) -> dict:
    """The RecordHeader projection, shared with the query API so both agree."""
    return {
        'record_id': record['record_id'],
        'dataset_id': record['record_id'].split(':', 1)[0],
        'ordinal': record['ordinal'],
        'version': record['version'],
        'ts': record['ts'],
        'ts_ns': record['ts_ns'],
        'level': record['level'],
        'logger': record['logger'],
        'fn': record['fn'],
        'src': record['src'],
        'src_line': record['src_line'],
        'lines': record['lines'],
        'bytes': record['bytes'],
        'has_trace': record['has_trace'],
        'message': record['message'],
        'provisional': False,
    }
