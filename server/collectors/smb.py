"""SMB 2/3 shares: discovery, ranged reads and reconnect checks (R6-062).

A Windows/Samba share is the least cooperative of the four transports:

* an open handle does not survive a network drop, so an offset is only meaningful
  together with a fresh `stat` of the same path;
* a share can return `STATUS_SHARING_VIOLATION` (another process holds the file),
  which is retryable, or `STATUS_ACCESS_DENIED`, which is not;
* signing/encryption negotiation can silently fail. We record what the client
  negotiated and never disable signing to "make it work".

So `read_range` re-stats before every window and refuses to continue when size,
mtime or the identity changed. `_classify` turns rclone's message into `locked`
(retry later) versus `permission_denied` (needs a human).
"""
from __future__ import annotations

import fnmatch
from typing import Optional

from . import rclone as rclone_mod
from .base import (Adapter, Capabilities, Check, Chunk, CollectorError, Listing,
                   ProbeResult, RemoteStat, SUPPORTED, UNVERIFIED, register)
from .process import Runner, ToolLocator, WindowedReader
from .rclone import Rclone, connection, parse_listing, remote, stat_from
from .security import Endpoint, Guard, SecurityError


@register
class SmbAdapter(Adapter):
    name = 'smb'
    label = 'SMB 2/3'
    default_port = 445
    requires_tools = ('rclone',)
    notes = ('SMB 读取需在每次重连后复核 identity/长度/边界；共享锁可能是暂时性的。',)

    def __init__(self, source, **kwargs):
        super().__init__(source, **kwargs)
        self.policy = dict(self.source.get('policy') or {})
        self.endpoint = Endpoint.parse(self.source.get('endpoint') or '', default_port=self.default_port)
        self.user = str(self.policy.get('user') or '').strip() or 'guest'
        self.share = str(self.policy.get('share') or '').strip().strip('/')
        self.secret: Optional[str] = None
        self.runner = self.runner or Runner()
        self.tool = Rclone(self.runner, device=ToolLocator(self.policy.get('tools') or {}))
        self._readers: dict = {}
        self._guard = self.guard or Guard(_admission(self.policy))

    # ------------------------------------------------------------------ secrets
    def _load_credential(self):
        ref = self.source.get('credential_ref')
        if not ref:
            return
        if self.credentials is None:
            raise CollectorError('credential_missing', '缺少凭据存储。')
        try:
            self.secret = self.credentials.read(ref)
        except Exception as exc:
            raise CollectorError(getattr(exc, 'code', 'credential_missing'),
                                 '凭据不可用，请管理员重新录入。')
        self.tool.secrets = (self.secret,)

    def connection(self) -> str:
        extra = ''
        if self.policy.get('domain'):
            extra = 'domain=' + str(self.policy['domain']).strip()
        if self.policy.get('disableSigning'):
            # Not silently: refusing is the point of this check.
            raise CollectorError('invalid_config', '不允许通过关闭 SMB 签名来绕过协商失败。')
        return connection('smb', host=self.endpoint.host, user=self.user,
                          port=self.endpoint.port, extra=extra)

    def _env(self) -> dict:
        return {'RCLONE_SMB_PASS': self.secret} if self.secret else {}

    def _run(self, args, *, timeout, max_stdout):
        try:
            return self.tool._run(args, timeout=timeout, max_stdout=max_stdout, env=self._env())
        except CollectorError as exc:
            raise _classify(exc)

    # ------------------------------------------------------------------ contract
    def capabilities(self) -> Capabilities:
        declared = str(self.policy.get('appendContract') or '').lower() == 'declared'
        return Capabilities(
            directory_discovery=SUPPORTED,
            range_read=SUPPORTED,
            stable_identity='size-mtime',
            append_contract='declared' if declared else UNVERIFIED,
            transport_version=self.tool.version() or 'unknown',
            security_status={'signing': str(self.policy.get('signing') or 'negotiated'),
                             'encryption': str(self.policy.get('encryption') or 'negotiated')},
            limits=dict(self.limits), notes=list(self.notes))

    def probe(self, *, config_revision: int, fingerprint: str, guard=None) -> ProbeResult:
        checks, warnings, samples = [], [], []
        capabilities = self.capabilities()
        ok = True
        try:
            self._load_credential()
            addresses = (guard or self._guard).admit(
                self.endpoint, expected_addresses=self.policy.get('expectedAddresses'))
            checks.append(Check('admission', 'pass', '主机准入通过。',
                                {'addresses': addresses, 'port': self.endpoint.port}))
        except (CollectorError, SecurityError) as exc:
            checks.append(Check('admission', 'fail', getattr(exc, 'message', str(exc)),
                                getattr(exc, 'details', {})))
            ok = False
            addresses = []
        version = self.tool.version()
        checks.append(Check('tool', 'pass' if version else 'fail',
                            'rclone 可用。' if version else '未安装 rclone。', {'version': version}))
        if not version:
            ok = False
        if ok:
            try:
                shares = self.shares()
                checks.append(Check('shares', 'pass', '共享可见（实际协商由服务端决定）。',
                                    {'shares': shares[:8], 'count': len(shares)}))
                if self.share and self.share not in shares:
                    checks.append(Check('share', 'fail', '配置的共享不在可见列表中。',
                                        {'share': self.share, 'visible': shares[:8]}))
                    ok = False
                else:
                    listing = self.list(self.directory())
                    checks.append(Check('list', 'pass', '目录可读。',
                                        {'entries': len(listing.entries)}))
                    if listing.entries:
                        sample = listing.entries[0]
                        chunk = self.read_range(sample.path, offset=0,
                                                length=min(1024, max(1, sample.size)))
                        expected = min(1024, sample.size)
                        checks.append(Check('range', 'pass' if len(chunk.data) == expected else 'fail',
                                            '范围读取返回精确字节。',
                                            {'requested': expected, 'received': len(chunk.data)}))
                        samples.append({'kind': 'range', 'bytes': len(chunk.data)})
            except CollectorError as exc:
                checks.append(Check('list', 'fail', exc.message, exc.to_dict()))
                ok = False
        if capabilities.append_contract != 'declared':
            warnings.append('SMB 源未声明前缀不可变，持续采集按弱一致性处理。')
        return ProbeResult(self.source.get('id', ''), config_revision, fingerprint, ok,
                           capabilities, checks, warnings, samples,
                           _now(), _future(int(self.policy.get('probeTtl') or 3600)))

    # ------------------------------------------------------------------ reading
    def directory(self) -> str:
        return '/' + '/'.join(part for part in (self.share, str(self.policy.get('directory') or '').strip('/')) if part)

    def shares(self) -> list:
        result = self._run(['lsd', self.connection()], timeout=int(self.limits['listTimeout']),
                           max_stdout=2 * 1024 * 1024)
        entries = parse_listing(result.stdout, directory='/', limit=256)
        return [entry.name for entry in entries if entry.is_dir] or [entry.name for entry in entries]

    def _target(self, path: str) -> str:
        text = str(path or '')
        if self.share and not text.startswith('/' + self.share):
            text = '/' + self.share + '/' + text.lstrip('/')
        self._guard.check_path(text)
        return remote(self.connection(), text)

    def list(self, directory: str, *, cursor: Optional[str] = None,
             limit: Optional[int] = None) -> Listing:
        target = str(directory or self.directory())
        result = self._run(['lsjson', '--files-only', self._target(target),
                            '--max-depth', str(int(self.policy.get('maxDepth') or 3))],
                           timeout=int(self.limits['listTimeout']),
                           max_stdout=int(self.limits['maxDirectoryBytes']))
        entries = parse_listing(result.stdout, directory=target,
                                limit=int(self.limits['maxEntries']))
        entries = [entry for entry in entries if not entry.is_dir]
        include, exclude = self.policy.get('include') or [], self.policy.get('exclude') or []
        if include:
            entries = [e for e in entries if any(fnmatch.fnmatch(e.name, p) for p in include)]
        if exclude:
            entries = [e for e in entries if not any(fnmatch.fnmatch(e.name, p) for p in exclude)]
        entries.sort(key=lambda entry: entry.path)
        start = int(cursor) if cursor and str(cursor).isdigit() else 0
        page = int(limit or self.limits['maxEntries'])
        window = entries[start:start + page]
        return Listing(directory=target, entries=window,
                       next_cursor=str(start + page) if start + page < len(entries) else None,
                       truncated=start + page < len(entries), scanned=len(entries))

    def stat(self, path: str) -> RemoteStat:
        result = self._run(['lsjson', '--stat', self._target(path)],
                           timeout=int(self.limits['listTimeout']), max_stdout=1024 * 1024)
        return stat_from(result.stdout, path=str(path))

    def read_range(self, path: str, *, offset: int, length: int,
                   identity: Optional[dict] = None) -> Chunk:
        stat = self.stat(path)
        if identity:
            previous_size = identity.get('size')
            previous_mtime = identity.get('modTime') or identity.get('mtimeNs')
            if previous_size is not None and int(previous_size) != stat.size:
                raise CollectorError('generation_changed', '重连后长度变化，拒绝继续追加。',
                                     expected=previous_size, actual=stat.size)
            if previous_mtime is not None and stat.identity.get('modTime') not in (None, previous_mtime):
                raise CollectorError('generation_changed', '重连后修改时间变化。',
                                     expected=str(previous_mtime)[:40],
                                     actual=str(stat.identity.get('modTime'))[:40])
        want = min(int(length), max(0, stat.size - offset))
        if want <= 0:
            return Chunk(b'', offset, offset, identity={'size': stat.size})
        key = (str(path), stat.size, stat.identity.get('modTime'))
        reader = self._readers.get(key)
        if reader is None:
            reader = WindowedReader(
                lambda start, count: self._read_window(path, start, count),
                window=int(self.limits['readWindow']), size=stat.size)
            self._readers = {key: reader}
        data = reader.read(offset, want)
        if len(data) != want:
            raise CollectorError('partial_read', '共享读取未返回完整字节。',
                                 expected=want, received=len(data))
        return Chunk(data, offset, offset + len(data),
                     identity={'size': stat.size, 'modTime': stat.identity.get('modTime')},
                     validators={'window': reader.spawns}, verified=False)

    def _read_window(self, path: str, offset: int, count: int) -> bytes:
        args = ['cat', self._target(path), '--offset', str(int(offset)), '--count', str(int(count))]
        return self._run(args, timeout=int(self.limits['readTimeout']),
                         max_stdout=int(self.limits['readWindow'])).stdout

    def close(self):
        self._readers = {}


def _classify(exc: CollectorError) -> CollectorError:
    if exc.code != 'transport_error':
        return exc
    detail = str(exc.details.get('detail') or '').lower()
    if 'sharing violation' in detail or 'being used by another process' in detail or 'lock' in detail:
        return CollectorError('locked', '共享文件被其他程序占用，稍后重试。', detail=exc.details.get('detail'))
    if 'access is denied' in detail or 'permission denied' in detail:
        return CollectorError('permission_denied', '共享权限不足，请检查账号授权。',
                              detail=exc.details.get('detail'))
    if 'signing' in detail or 'encrypt' in detail:
        return CollectorError('permission_denied', '共享要求签名/加密协商，未通过。',
                              detail=exc.details.get('detail'))
    return exc


def _admission(policy: dict):
    from .security import Admission
    return Admission.from_policy(policy)


def _now() -> str:
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _future(seconds: int) -> float:
    import time
    return time.time() + seconds
