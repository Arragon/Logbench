"""Discovering remote files over HTTP without guessing (R6-064).

Three modes, in decreasing order of trust:

1. **manifest** (`logbench.manifest/1`) — an explicit generation and an explicit
   file list, optionally with per-file validators. This is the only mode that can
   support a sealed/append contract, because the publisher stated it.
2. **explicit URL** — one file, no discovery at all. The wizard offers this and
   nothing else when there is no listing.
3. **directory index** — a plain HTML page of `<a href>` links, parsed with hard
   limits. It gives names and (at best) sizes; it never implies that the prefix is
   immutable.

Refusals worth naming: a link that resolves to another host, a path that escapes
the configured directory, a manifest with an unknown schema version, an entry count
over the limit, and a page that links to itself (cycle). Each returns a specific
error instead of an empty listing, because "no files" would hide a misconfiguration
for weeks.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Optional

from .base import CollectorError, Entry, Listing
from .security import UNSAFE_CHARS

MANIFEST_SCHEMA = 'logbench.manifest/1'
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024


def _safe_entry_path(value: str, *, base: str) -> str:
    text = str(value or '').strip()
    if not text or len(text) > 2048 or any(ch in text for ch in UNSAFE_CHARS):
        raise CollectorError('invalid_path', '清单中的路径非法。', value=text[:120])
    if text.startswith('/'):
        raise CollectorError('invalid_path', '清单路径必须是相对路径。', value=text[:120])
    parts = [part for part in text.split('/') if part]
    if any(part in ('.', '..') for part in parts):
        raise CollectorError('invalid_path', '清单路径不能包含上级目录。', value=text[:120])
    joined = '/'.join(parts)
    prefix = base.strip('/')
    if prefix and not (joined == prefix or joined.startswith(prefix + '/')):
        raise CollectorError('path_not_allowed', '清单条目超出已批准的目录。', value=joined[:160])
    return joined


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'a':
            return
        for name, value in attrs:
            if name.lower() == 'href' and value:
                self.links.append(value)

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


class HttpListing:
    def __init__(self, transport, *, policy: Optional[dict] = None, limits: Optional[dict] = None):
        self.transport = transport
        self.policy = dict(policy or transport.policy or {})
        self.limits = dict(limits or {})
        self.max_entries = int(self.limits.get('maxEntries') or 20000)

    # ------------------------------------------------------------------ helpers
    def _base_dir(self, directory: str) -> str:
        return (directory or self.policy.get('directory') or '').strip('/')

    def _listing_url(self, directory: str) -> str:
        explicit = self.policy.get('listingUrl')
        if explicit:
            return str(explicit)
        target = directory or self.transport.endpoint.path or '/'
        if not target.endswith('/'):
            target += '/'
        return target

    def _manifest_url(self, directory: str) -> str:
        explicit = self.policy.get('manifestUrl')
        if explicit:
            return str(explicit)
        target = self._listing_url(directory)
        return urllib.parse.urljoin(target if target.endswith('/') else target + '/',
                                    str(self.policy.get('manifestName') or 'logbench.manifest.json'))

    # -------------------------------------------------------------------- modes
    def mode(self) -> str:
        if self.policy.get('manifestUrl') or self.policy.get('manifestName'):
            return 'manifest'
        if self.policy.get('listingUrl'):
            return 'index'
        if self.policy.get('explicitUrl') or self.policy.get('directoryMode') == 'explicit':
            return 'explicit'
        return 'none'

    def list(self, directory: str, *, cursor: Optional[str] = None,
             limit: Optional[int] = None) -> Listing:
        mode = self.mode()
        if mode == 'manifest':
            return self.manifest(directory)
        if mode == 'index':
            return self.index(directory, cursor=cursor, limit=limit)
        if mode == 'explicit':
            path = self.policy.get('explicitUrl') or self.transport.endpoint.path
            name = str(path).rstrip('/').rsplit('/', 1)[-1]
            stat = self.transport_head(path)
            return Listing(directory=directory, entries=[Entry(path=str(path), name=name,
                                                               size=stat, is_dir=False)], scanned=1)
        raise CollectorError('unsupported', 'HTTP 源未配置清单、目录索引或显式文件，无法发现文件。')

    def transport_head(self, path: str) -> int:
        status, headers, _, _ = self.transport.request('HEAD', path, from_root=True)
        lowered = {key.lower(): value for key, value in headers.items()}
        if status in (401, 403):
            raise CollectorError('auth_failed', 'HTTP 源拒绝访问。', status=status)
        if status == 404:
            raise CollectorError('not_found', '远端文件不存在。')
        if status >= 400:
            raise CollectorError('transport_error', '无法读取远端文件元数据。', status=status)
        if lowered.get('content-length', '').isdigit():
            return int(lowered['content-length'])
        return 0

    def manifest(self, directory: str) -> Listing:
        url = self._manifest_url(directory)
        status, headers, body, final = self.transport.request('GET', url, limit=MAX_MANIFEST_BYTES,
                                                              from_root=True)
        if status == 404:
            raise CollectorError('unsupported', '该 HTTP 源没有提供代次清单（manifest）。',
                                 url=url)
        if status in (401, 403):
            raise CollectorError('auth_failed', '清单需要授权。', status=status)
        if status >= 400:
            raise CollectorError('transport_error', '读取清单失败。', status=status)
        try:
            document = json.loads(body.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise CollectorError('invalid_manifest', '清单不是有效的 UTF-8 JSON。')
        if not isinstance(document, dict):
            raise CollectorError('invalid_manifest', '清单必须是对象。')
        schema = document.get('schema')
        if schema != MANIFEST_SCHEMA:
            raise CollectorError('invalid_manifest', '清单版本不受支持。',
                                 expected=MANIFEST_SCHEMA, actual=str(schema)[:40])
        allowed = {'schema', 'generation', 'sealed', 'generatedAt', 'files', 'notes'}
        unknown = sorted(set(document) - allowed)
        if unknown:
            raise CollectorError('invalid_manifest', '清单包含未知字段。', fields=unknown[:8])
        generation = document.get('generation')
        if not isinstance(generation, str) or not generation.strip():
            raise CollectorError('invalid_manifest', '清单缺少 generation。')
        files = document.get('files')
        if not isinstance(files, list):
            raise CollectorError('invalid_manifest', '清单缺少 files 数组。')
        if len(files) > self.max_entries:
            raise CollectorError('manifest_too_large', '清单条目数超过上限。',
                                 entries=len(files), limit=self.max_entries)
        base = self._base_dir(directory)
        seen, entries = set(), []
        for row in files:
            if not isinstance(row, dict):
                raise CollectorError('invalid_manifest', '清单条目必须是对象。')
            extra = sorted(set(row) - {'path', 'size', 'sealed', 'etag', 'sha256', 'mtime'})
            if extra:
                raise CollectorError('invalid_manifest', '清单条目包含未知字段。', fields=extra[:8])
            path = _safe_entry_path(row.get('path'), base=base)
            if path in seen:
                raise CollectorError('invalid_manifest', '清单中路径重复。', path=path)
            seen.add(path)
            size = row.get('size')
            if not isinstance(size, int) or size < 0:
                raise CollectorError('invalid_manifest', '清单条目缺少合法 size。', path=path)
            entries.append(Entry(
                path=path, name=path.rsplit('/', 1)[-1], size=size,
                mtime_ns=_mtime_ns(row.get('mtime')),
                identity={'etag': row.get('etag'), 'sha256': row.get('sha256'),
                          'sealed': bool(row.get('sealed', document.get('sealed', False))),
                          'generation': generation}))
        listing = Listing(directory=directory, entries=entries, scanned=len(entries))
        listing.manifest = {'schema': schema, 'generation': generation,
                            'sealed': bool(document.get('sealed')),
                            'url': final, 'notes': document.get('notes')}
        return listing

    def index(self, directory: str, *, cursor: Optional[str] = None,
              limit: Optional[int] = None) -> Listing:
        target = self._listing_url(directory)
        if cursor:
            if not str(cursor).isdigit():
                raise CollectorError('invalid_cursor', '目录游标无效。')
            target = target + ('&' if '?' in target else '?') + 'page=' + str(cursor)
        status, headers, body, final = self.transport.request('GET', target, limit=MAX_INDEX_BYTES,
                                                              from_root=True)
        if status in (401, 403):
            raise CollectorError('auth_failed', '目录索引需要授权。', status=status)
        if status == 404:
            raise CollectorError('unsupported', '该 HTTP 源没有目录索引。', url=target)
        if status >= 400:
            raise CollectorError('transport_error', '读取目录索引失败。', status=status)
        try:
            text = body.decode('utf-8')
        except UnicodeError:
            raise CollectorError('invalid_listing', '目录索引不是 UTF-8 文本。')
        parser = _Anchors()
        try:
            parser.feed(text)
        except Exception:  # html.parser is lenient; treat a hard failure as malformed
            raise CollectorError('invalid_listing', '目录索引无法解析。')
        base_url = final if final.endswith('/') else final.rsplit('/', 1)[0] + '/'
        origin = urllib.parse.urlsplit(base_url)
        base_prefix = '/' + self._base_dir(directory).strip('/')
        entries, seen = [], set()
        page_limit = int(limit or self.max_entries)
        for href in parser.links:
            if len(entries) >= page_limit:
                break
            if href.startswith(('#', 'mailto:', 'javascript:')):
                continue
            resolved = urllib.parse.urljoin(base_url, href)
            hop = urllib.parse.urlsplit(resolved)
            if hop.scheme not in ('http', 'https'):
                continue
            if (hop.hostname, hop.port) != (origin.hostname, origin.port):
                # External links are refused rather than followed.
                continue
            policy_paths = self.policy.get('allowedPaths') or []
            if policy_paths and not any(hop.path.startswith(prefix) for prefix in policy_paths):
                continue
            relative = hop.path
            if base_prefix and base_prefix != '/' and not relative.startswith(base_prefix):
                continue
            relative = relative[len(base_prefix):].lstrip('/') if base_prefix != '/' else relative.lstrip('/')
            if not relative or relative in seen:
                continue
            seen.add(relative)
            is_dir = hop.path.endswith('/')
            name = urllib.parse.unquote(relative.rstrip('/').rsplit('/', 1)[-1])
            if not name or name in ('.', '..'):
                continue
            try:
                _safe_entry_path(relative.rstrip('/'), base='')
            except CollectorError:
                continue
            entries.append(Entry(path=relative.rstrip('/'), name=name, size=0,
                                 is_dir=is_dir, identity={'url': resolved}))
        if not entries:
            # A page with no usable links is a capability gap, not an empty folder.
            raise CollectorError('unsupported', '目录索引没有可解析的文件链接。', url=target)
        return Listing(directory=directory, entries=entries, scanned=len(parser.links))


def _mtime_ns(value) -> Optional[int]:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value * 1e9)
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z?', value):
        import datetime
        try:
            parsed = datetime.datetime.strptime(value.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S%z'
                                               if 'T' in value and value.count(':') == 2 else '%Y-%m-%dT%H:%M:%S.%f%z')
        except ValueError:
            return None
        return int(parsed.timestamp() * 1e9)
    return None
