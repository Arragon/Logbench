"""HTTP(S) range reads with honest validators (R6-063).

The single hardest thing about HTTP as a log source is that **206 is not a promise**.
A proxy may serve 200 for a Range request, a server may ignore If-Range, an ETag may
be weak, and a CDN may answer 416 for a file that grew. Each of those has a correct
interpretation, and none of them is "append the body to the previous chunk":

| response | meaning here |
|---|---|
| 206 with matching `Content-Range` | the requested bytes; verified |
| 206 with a different start | `generation_changed` (served another range) |
| 200 | a *new full representation*; never appended to an old tail |
| 416 | complete (offset at end) or changed (validator/size differ) |
| weak `ETag` only | `unverified` identity: read, then confirm the validator did not move |

Redirects are re-admitted hop by hop and the `Authorization` header is dropped when
the hop changes host, so a redirect cannot carry a private token to another origin.
"""
from __future__ import annotations

import email.utils
import hashlib
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .base import (Adapter, Capabilities, Check, Chunk, CollectorError, ProbeResult,
                   RemoteStat, SUPPORTED, UNSUPPORTED, UNVERIFIED, register)
from .security import Endpoint, Guard, SecurityError, redact
from .process import WindowedReader

MAX_REDIRECTS = 3
SAMPLE_BYTES = 1024
DEFAULT_TIMEOUT = 30


def _iso_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _parse_content_range(value: str) -> Optional[dict]:
    """`bytes 0-1023/4096` → {'start','end','total'}; total may be '*'."""
    if not value:
        return None
    text = value.strip()
    if not text.lower().startswith('bytes '):
        return None
    spec = text[6:].strip()
    if '/' not in spec:
        return None
    span, total = spec.split('/', 1)
    if '-' not in span:
        return None
    start, end = span.split('-', 1)
    if not start.isdigit() or not end.isdigit():
        return None
    total_value = None
    if total.strip().isdigit():
        total_value = int(total.strip())
    return {'start': int(start), 'end': int(end), 'total': total_value}


def _strong_etag(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if text.upper().startswith('W/'):
        return None
    return text


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Admission and credential scoping on every hop, not only the first."""

    def __init__(self, transport):
        self.transport = transport

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = self.transport.admit(newurl)
        origin = urllib.parse.urlsplit(req.full_url)
        hop = urllib.parse.urlsplit(target)
        if (hop.scheme, hop.hostname, hop.port) != (origin.scheme, origin.hostname, origin.port):
            # A redirect to another origin must not inherit the credential.
            req.remove_header('Authorization')
        self.transport.redirects += 1
        if self.transport.redirects > MAX_REDIRECTS:
            raise CollectorError('redirect_denied', '重定向次数超过上限。')
        return super().redirect_request(req, fp, code, msg, headers, target)


class HttpTransport:
    """Shared HTTP plumbing: TLS verification, admission, bounded bodies."""

    def __init__(self, source: dict, *, credentials=None, guard: Optional[Guard] = None,
                 policy: Optional[dict] = None):
        self.source = dict(source or {})
        self.policy = dict(policy or self.source.get('policy') or {})
        self.endpoint = Endpoint.parse(self.source.get('endpoint') or 'https://localhost',
                                       default_port=443)
        self.guard = guard or Guard(_admission(self.policy))
        self.credentials = credentials
        self.redirects = 0
        self.secret = None
        self.max_body = int(self.policy.get('maxResponseBytes') or 16 * 1024 * 1024)
        self.timeout = int(self.policy.get('readTimeout') or DEFAULT_TIMEOUT)

    # --------------------------------------------------------------- admission
    def base_url(self) -> str:
        return self.source.get('endpoint') or ('https://' + self.endpoint.host)

    def admit(self, url: str) -> str:
        hop = urllib.parse.urlsplit(url)
        if hop.scheme not in ('http', 'https'):
            raise CollectorError('redirect_denied', '只允许 http/https 重定向。')
        if not hop.hostname:
            raise CollectorError('invalid_endpoint', 'URL 缺少主机名。')
        port = hop.port or (443 if hop.scheme == 'https' else 80)
        try:
            self.guard.admit(Endpoint(host=hop.hostname, port=port, path=hop.path or '/'),
                             expected_addresses=self.policy.get('expectedAddresses'))
            self.guard.check_path(hop.path or '/') if self.policy.get('allowedPaths') else None
        except SecurityError as exc:
            raise CollectorError(exc.code, exc.message, **exc.details)
        return url

    def url_for(self, path: str, *, from_root: bool = False) -> str:
        """Resolve a target.

        Two kinds of value are meaningful and they are not the same string space:
        a *file path* is relative to the source's directory (what a listing
        returned), while a *policy URL* such as `listingUrl: /index/` is relative to
        the host root. Conflating them is how `/index/` silently becomes
        `/ok/a.log/index/`.
        """
        if path.startswith('http://') or path.startswith('https://'):
            return self.admit(path)
        base = self.base_url()
        if from_root:
            joined = urllib.parse.urljoin(base, path)
        else:
            directory = base if base.endswith('/') else base.rsplit('/', 1)[0] + '/'
            joined = urllib.parse.urljoin(directory, path.lstrip('/'))
        return self.admit(joined)

    # ------------------------------------------------------------------ opener
    def opener(self):
        context = ssl.create_default_context(cafile=self.policy.get('caFile') or None)
        # Deliberately no `_create_unverified_context`: a private CA is configured,
        # verification is never skipped.
        handlers = [urllib.request.HTTPSHandler(context=context),
                    _RedirectGuard(self)]
        return urllib.request.build_opener(*handlers)

    def auth_header(self) -> dict:
        ref = self.source.get('credential_ref')
        if not ref or self.credentials is None:
            return {}
        try:
            self.secret = self.credentials.read(ref)
        except Exception as exc:  # CredentialError, but keep this module import-light
            raise CollectorError('credential_missing', '凭据不可用，请管理员重新录入。',
                                 detail=getattr(exc, 'code', 'credential_error'))
        kind = self.source.get('credential_kind') or self.policy.get('credentialKind') or 'token'
        if kind == 'password':
            user = self.policy.get('user') or 'logbench'
            import base64
            token = base64.b64encode(('%s:%s' % (user, self.secret)).encode('utf-8')).decode('ascii')
            return {'Authorization': 'Basic ' + token}
        return {'Authorization': 'Bearer ' + self.secret}

    def request(self, method: str, path: str, *, headers: Optional[dict] = None,
                limit: Optional[int] = None, from_root: bool = False,
                truncate: bool = False) -> tuple:
        """Return (status, headers, body, url). Body is capped at `limit`."""
        url = self.url_for(path, from_root=from_root)
        self.redirects = 0
        merged = {'User-Agent': 'LogBench-R6', 'Accept-Encoding': 'identity'}
        merged.update(self.auth_header())
        merged.update(headers or {})
        request = urllib.request.Request(url, method=method, headers=merged)
        cap = int(limit or self.max_body)
        try:
            with self.opener().open(request, timeout=self.timeout) as response:
                body = _read_capped(response, cap, strict=not truncate)
                return response.status, dict(response.headers.items()), body, response.geturl()
        except urllib.error.HTTPError as exc:
            body = _read_capped(exc, min(cap, 64 * 1024)) if exc.fp else b''
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, body, url
        except socket.timeout:
            raise CollectorError('timeout', 'HTTP 请求超时。')
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, ssl.SSLError):
                raise CollectorError('certificate_invalid', 'TLS 证书校验失败，不会跳过证书验证。',
                                     detail=redact(str(reason)))
            if isinstance(reason, socket.gaierror):
                raise CollectorError('dns_failed', '无法解析主机名。')
            raise CollectorError('transport_error', 'HTTP 连接失败。', detail=redact(str(reason)))
        except http.client.IncompleteRead as exc:
            raise CollectorError('partial_read', '远端响应被提前截断。',
                                 received=len(getattr(exc, 'partial', b'') or b''))
        except (http.client.HTTPException, ConnectionError, OSError) as exc:
            raise CollectorError('transport_error', 'HTTP 传输中断。', detail=redact(str(exc)))


def _read_capped(response, cap: int, *, strict: bool = True) -> bytes:
    """Read at most `cap` bytes; `strict` decides whether more is an error.

    A probe only needs the status and the first bytes, so a `200` that carries the
    whole file must not be read to the end to find out that it ignored `Range`.
    """
    chunks, total = [], 0
    while total <= cap:
        block = response.read(min(65536, cap + 1 - total))
        if not block:
            break
        chunks.append(block)
        total += len(block)
    data = b''.join(chunks)
    if len(data) > cap:
        if not strict:
            return data[:cap]
        raise CollectorError('output_limit', 'HTTP 响应超过上限，已停止读取。', limit=cap)
    return data


def _admission(policy: dict):
    from .security import Admission
    merged = dict(policy or {})
    merged.setdefault('allowedHosts', [])
    merged.setdefault('allowedPorts', [])
    return Admission.from_policy(merged)


@register
class HttpRangeAdapter(Adapter):
    name = 'http'
    label = 'HTTP(S)'
    default_port = 443
    requires_tools = ()
    notes = ('普通 HTTP 目录不提供文件发现；持续跟随需要源端提供代次/不可变前缀约定。',)

    def __init__(self, source, **kwargs):
        super().__init__(source, **kwargs)
        self.transport = HttpTransport(source, credentials=self.credentials,
                                       guard=self.guard, policy=self.source.get('policy'))
        self._cancelled = False
        self._cached: dict = {}

    # ------------------------------------------------------------------ helpers
    def _path(self, path: Optional[str] = None) -> str:
        return path or self.source.get('path') or self.transport.endpoint.path or '/'

    def _stat(self, path: Optional[str] = None) -> RemoteStat:
        target = self._path(path)
        status, headers, body, url = self.transport.request('HEAD', target)
        if status in (405, 501):
            status, headers, body, url = self.transport.request(
                'GET', target, headers={'Range': 'bytes=0-0'}, limit=1024)
        if status in (401, 403):
            raise CollectorError('auth_failed', 'HTTP 源拒绝访问，请检查凭据与权限。', status=status)
        if status == 404:
            raise CollectorError('not_found', '远端文件不存在。')
        if status >= 500:
            raise CollectorError('server_error', '远端服务暂时不可用。', status=status)
        if status not in (200, 206):
            raise CollectorError('transport_error', '无法读取远端文件元数据。', status=status)
        lowered = {key.lower(): value for key, value in headers.items()}
        size = None
        if status == 206:
            span = _parse_content_range(lowered.get('content-range', ''))
            if span and span['total'] is not None:
                size = span['total']
        if size is None and lowered.get('content-length', '').isdigit():
            size = int(lowered['content-length'])
        if size is None:
            raise CollectorError('unverified', '远端未提供长度，无法做范围读取。')
        etag = lowered.get('etag')
        last_modified = lowered.get('last-modified')
        identity = {'etag': etag, 'lastModified': last_modified, 'size': size,
                    'strong': bool(_strong_etag(etag)), 'url': url}
        mtime_ns = None
        if last_modified:
            try:
                mtime_ns = int(email.utils.parsedate_to_datetime(last_modified).timestamp() * 1e9)
            except (TypeError, ValueError):
                mtime_ns = None
        return RemoteStat(path=target, size=size, mtime_ns=mtime_ns, is_dir=False,
                          identity={k: v for k, v in identity.items() if k != 'url'},
                          validators={'acceptRanges': lowered.get('accept-ranges', ''),
                                      'encoding': lowered.get('content-encoding', 'identity')})

    # --------------------------------------------------------------- contract
    def capabilities(self) -> Capabilities:
        return Capabilities(
            directory_discovery=UNSUPPORTED,
            range_read=SUPPORTED,
            stable_identity=UNVERIFIED,
            append_contract=UNSUPPORTED,
            transport_version='http/1.1',
            security_status={'tls': 'verified-by-default'},
            limits=dict(self.limits),
            notes=list(self.notes))

    def probe(self, *, config_revision: int, fingerprint: str, guard=None) -> ProbeResult:
        checks, warnings, samples = [], [], []
        capabilities = self.capabilities()
        try:
            stat = self._stat()
            checks.append(Check('stat', 'pass', '读取到远端长度与验证器。',
                                {'size': stat.size, 'etag': stat.identity.get('etag'),
                                 'lastModified': stat.identity.get('lastModified')}))
        except CollectorError as exc:
            checks.append(Check('stat', 'fail', exc.message, exc.to_dict()))
            return ProbeResult(self.source.get('id', ''), config_revision, fingerprint, False,
                               capabilities, checks, warnings, samples, _iso_now(),
                               time.time() + 300)

        strong = bool(stat.identity.get('strong'))
        capabilities.stable_identity = 'validators' if strong else (
            'size-mtime' if stat.identity.get('lastModified') else UNVERIFIED)
        if not strong:
            warnings.append('远端未提供强 ETag：续传只能按长度+修改时间降级验证。')

        # Actual Range behaviour, not the Accept-Ranges header's opinion.
        status, headers, body, _ = self.transport.request(
            'GET', stat.path, headers={'Range': 'bytes=0-%d' % (SAMPLE_BYTES - 1)},
            limit=SAMPLE_BYTES, truncate=True)
        lowered = {key.lower(): value for key, value in headers.items()}
        if status == 206:
            span = _parse_content_range(lowered.get('content-range', ''))
            expected = min(SAMPLE_BYTES, stat.size)
            ok = bool(span) and span['start'] == 0 and span['end'] == expected - 1
            capabilities.range_read = SUPPORTED if ok else UNVERIFIED
            checks.append(Check('range', 'pass' if ok else 'fail',
                                'Range 返回 206 且 Content-Range 与请求一致。' if ok
                                else 'Range 返回 206 但 Content-Range 不符。',
                                {'contentRange': lowered.get('content-range'), 'bytes': len(body)}))
            samples.append({'kind': 'range', 'bytes': len(body),
                            'digest': hashlib.sha256(body).hexdigest()[:16]})
        elif status == 200:
            capabilities.range_read = UNSUPPORTED
            checks.append(Check('range', 'fail', '服务端忽略 Range 并返回 200，不能做范围续采。',
                                {'status': status}))
        elif status == 416:
            capabilities.range_read = UNVERIFIED
            checks.append(Check('range', 'fail', '服务端对合法范围返回 416。', {'status': 416}))
        else:
            capabilities.range_read = UNVERIFIED
            checks.append(Check('range', 'fail', 'Range 请求返回意外状态。', {'status': status}))

        declared = str(self.transport.policy.get('appendContract') or '').lower()
        generation = self.transport.policy.get('generationId')
        if declared == 'declared' and generation:
            capabilities.append_contract = 'declared'
            checks.append(Check('append', 'pass', '源端声明append契约并提供代次标识。',
                                {'generationId': str(generation)[:80]}))
        else:
            capabilities.append_contract = UNSUPPORTED
            checks.append(Check('append', 'skip',
                                '未声明不可变前缀/代次约定，仅开放稳定快照读取。',
                                {'declared': declared}))

        # Discovery is a separate capability with its own failure mode: a source
        # without a manifest is still a valid single-file source.
        listing = self.listing()
        mode = listing.mode()
        if mode == 'none':
            capabilities.directory_discovery = UNSUPPORTED
            checks.append(Check('discovery', 'skip', '未配置清单/目录索引，仅支持显式文件。',
                                {'mode': mode}))
        else:
            try:
                found = listing.list(self.source.get('path') or '/')
                capabilities.directory_discovery = SUPPORTED if mode == 'manifest' else UNVERIFIED
                checks.append(Check('discovery', 'pass' if mode == 'manifest' else 'warn',
                                    '发现 %d 个文件（%s）。' % (len(found.entries), mode),
                                    {'mode': mode, 'entries': len(found.entries),
                                     'generation': (getattr(found, 'manifest', None) or {}).get('generation'),
                                     'truncated': found.truncated}))
                samples.append({'kind': 'discovery', 'mode': mode, 'entries': len(found.entries)})
            except CollectorError as exc:
                capabilities.directory_discovery = UNSUPPORTED
                checks.append(Check('discovery', 'fail', exc.message, exc.to_dict()))

        ok = capabilities.range_read == SUPPORTED
        result = ProbeResult(self.source.get('id', ''), config_revision, fingerprint, ok,
                             capabilities, checks, warnings, samples, _iso_now(),
                             time.time() + int(self.transport.policy.get('probeTtl') or 3600))
        return result

    def stat(self, path: str) -> RemoteStat:
        return self._stat(path)

    # -------------------------------------------------------------- discovery
    def listing(self):
        from .http_listing import HttpListing
        return HttpListing(self.transport, policy=self.transport.policy, limits=self.limits)

    def list(self, directory: str, *, cursor: Optional[str] = None,
             limit: Optional[int] = None):
        try:
            return self.listing().list(directory, cursor=cursor, limit=limit)
        except CollectorError as exc:
            if exc.code == 'unsupported':
                # Keep the specific reason but present it as a capability gap, so a
                # caller cannot mistake it for an empty directory.
                raise CollectorError.unsupported('directoryDiscovery')
            raise

    def read_range(self, path: str, *, offset: int, length: int,
                   identity: Optional[dict] = None) -> Chunk:
        if length <= 0:
            raise CollectorError('invalid_range', '读取长度必须为正。')
        if length > int(self.limits['maxReadBytes']):
            raise CollectorError('range_too_large', '单次读取超过上限。', requested=length)
        target = self._path(path)
        identity = dict(identity or {})
        size = identity.get('size')
        want = length
        if size is not None:
            want = max(0, min(int(length), int(size) - offset))
            if want == 0:
                # Reading exactly at the end is a valid no-op, not an error.
                return Chunk(b'', offset, offset, identity=dict(identity), verified=True)
        window = min(int(self.limits['readWindow']), int(self.limits['maxReadBytes']))
        key = (target, identity.get('etag'), identity.get('size'))
        reader = self._cached.get(key)
        if reader is None:
            # One HTTP request per window, not per archive block: a 4 MiB block read
            # out of an 8 MiB window reuses the response instead of re-asking.
            reader = WindowedReader(
                lambda start, count: self._fetch(target, start, count, identity),
                window=window, size=int(size) if size is not None else None)
            self._cached = {key: reader}
        data = reader.read(offset, want)
        if len(data) != want:
            raise CollectorError('partial_read', '远端返回的字节数与请求不一致。',
                                 expected=want, received=len(data))
        return Chunk(data, offset, offset + len(data),
                     identity={'etag': identity.get('etag'), 'size': identity.get('size')},
                     validators={'requests': reader.spawns},
                     verified=bool(_strong_etag(identity.get('etag'))))

    def _fetch(self, target: str, offset: int, length: int, identity: dict) -> bytes:
        strong = _strong_etag(identity.get('etag'))
        headers = {'Range': 'bytes=%d-%d' % (offset, offset + length - 1)}
        if strong:
            headers['If-Range'] = strong
        status, response_headers, body, _ = self.transport.request('GET', target, headers=headers)
        lowered = {key.lower(): value for key, value in response_headers.items()}
        if status in (401, 403):
            raise CollectorError('auth_failed', 'HTTP 源拒绝访问。', status=status)
        if status == 404:
            raise CollectorError('not_found', '远端文件不存在。')
        if status >= 500:
            raise CollectorError('server_error', '远端服务暂时不可用。', status=status)

        current_etag = lowered.get('etag')
        if status == 206:
            span = _parse_content_range(lowered.get('content-range', ''))
            if not span or span['start'] != offset:
                raise CollectorError('generation_changed', '远端返回了不同的字节范围。',
                                     requested=offset, contentRange=lowered.get('content-range'))
            if span['total'] is not None and identity.get('size') is not None \
                    and int(identity['size']) != int(span['total']):
                raise CollectorError('generation_changed', '远端长度与归档记录不一致。',
                                     expected=identity['size'], actual=span['total'])
            expected = min(length, (span['total'] or (offset + length)) - offset)
            if len(body) != expected:
                raise CollectorError('partial_read', '远端返回的字节数与请求不一致。',
                                     expected=expected, received=len(body))
            if not strong and identity.get('etag') and current_etag \
                    and current_etag != identity.get('etag'):
                raise CollectorError('generation_changed', '弱验证器在读取期间发生变化。')
            return body

        if status == 200:
            # A full representation. It is only usable as bytes [0, len) and must
            # never be spliced onto an existing archived prefix.
            total = len(body)
            if offset != 0 or total != length:
                raise CollectorError('generation_changed',
                                     '远端忽略 Range 并返回完整表示，已拒绝拼接旧尾部。',
                                     offset=offset, received=total)
            return body

        if status == 416:
            span = _parse_content_range(lowered.get('content-range', '')) or {}
            total = span.get('total')
            if identity.get('size') is not None and total is not None \
                    and int(identity['size']) != int(total):
                raise CollectorError('generation_changed', '远端长度变化（416）。',
                                     expected=identity['size'], actual=total)
            if total is not None and offset >= int(total):
                return b''
            raise CollectorError('generation_changed', '远端拒绝该范围（416），已停止追加。',
                                 offset=offset, total=total)

        raise CollectorError('transport_error', '无法读取远端范围。', status=status)

    def cancel(self):
        self._cancelled = True

    def close(self):
        self.cancel()
