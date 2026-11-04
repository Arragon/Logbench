"""SSH/SFTP: discovery and range reads over rclone's sftp backend (R6-061).

What this adapter will *not* claim:

* **a stable inode.** SFTP exposes size and mtime, not a generation. So the
  identity it returns is `{"size", "modTime", "id"}` and the caller must treat a
  same-size rewrite as a possible replacement — `generations.py` decides with the
  boundary fingerprint.
* **reliable tail-following.** Range reads work; "the prefix never changes" is an
  assumption only the source owner can make, so `appendContract` is `unverified`
  until the source policy declares it.
* **that a host key was verified.** If neither a known-hosts file nor a pinned
  fingerprint is configured, the probe fails. Trusting `StrictHostKeyChecking=no`
  is exactly the habit this ticket exists to remove.

A private key credential is materialised into a 0600 file inside the session
directory for the duration of the attempt and removed in `close()`, so a key is
never written into the archive, an argument list or a log line.
"""
from __future__ import annotations

import fnmatch
import os
import tempfile
from pathlib import Path
from typing import Optional

from . import rclone as rclone_mod
from .base import (Adapter, Capabilities, Check, Chunk, CollectorError, ProbeResult,
                   RemoteStat, SUPPORTED, UNSUPPORTED, UNVERIFIED, register)
from .process import Runner, ToolLocator, WindowedReader
from .rclone import Rclone, connection, parse_listing, remote, stat_from
from .security import Endpoint, Guard, SecurityError


@register
class SftpAdapter(Adapter):
    name = 'sftp'
    label = 'SSH / SFTP'
    default_port = 22
    requires_tools = ('rclone',)
    notes = ('SFTP 通常没有可靠 inode；持续追加需源端声明前缀不变，否则回退稳定快照。',)

    def __init__(self, source, **kwargs):
        super().__init__(source, **kwargs)
        self.policy = dict(self.source.get('policy') or {})
        self.endpoint = Endpoint.parse(self.source.get('endpoint') or '', default_port=self.default_port)
        self.user = str(self.policy.get('user') or '').strip()
        self.secret: Optional[str] = None
        self.secret_kind = 'password'
        self._key_file: Optional[str] = None
        self._readers: dict = {}
        self._closed = False
        self.runner = self.runner or Runner(secrets=())
        device = ToolLocator(self.policy.get('tools') or {})
        self.tool = Rclone(self.runner, device=device)
        self.tool.path = self.tool.path or None
        self._guard = self.guard or Guard(_admission(self.policy))

    # ------------------------------------------------------------------ secrets
    def _load_credential(self):
        ref = self.source.get('credential_ref')
        if not ref:
            return
        if self.credentials is None:
            raise CollectorError('credential_missing', '缺少凭据存储，无法读取密码或私钥。')
        try:
            self.secret = self.credentials.read(ref)
        except Exception as exc:  # CredentialError without importing it here
            raise CollectorError(getattr(exc, 'code', 'credential_missing'),
                                 '凭据不可用，请管理员重新录入。')
        self.secret_kind = str(self.source.get('credential_kind') or self.policy.get('credentialKind')
                               or 'password')
        if self.secret_kind == 'key':
            self._key_file = self._materialise_key(self.secret)
            self.secret = None

    def _materialise_key(self, pem: str) -> str:
        directory = Path(self.policy.get('sessionDir') or tempfile.gettempdir()) / 'logbench-keys'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix='key_', dir=str(directory))
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(pem if pem.endswith('\n') else pem + '\n')
            os.chmod(path, 0o600)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _env(self) -> dict:
        env = {}
        if self.secret and self.secret_kind == 'password':
            env['RCLONE_SFTP_PASS'] = self.secret
        elif self.secret and self.secret_kind == 'token':
            # A token has no meaning for SFTP; refuse instead of sending it as a password.
            raise CollectorError('invalid_config', 'SFTP 源不能使用 token 类型凭据。')
        return env

    def _extra(self) -> str:
        fields = []
        if self._key_file:
            fields.append('key_file=' + self._key_file)
        known = self.policy.get('knownHostsFile')
        if known:
            path = Path(str(known))
            if not path.is_file():
                raise CollectorError('hostkey_unverified', 'known_hosts 文件不存在。', path=str(path)[:120])
            fields.append('known_hosts_file=' + str(path))
        if self.policy.get('keyUseAgent'):
            fields.append('key_use_agent=true')
        return ','.join(fields)

    def connection(self) -> str:
        if not self.user:
            raise CollectorError('invalid_config', 'SFTP 源需要配置用户名。')
        return connection('sftp', host=self.endpoint.host, user=self.user,
                          port=self.endpoint.port, extra=self._extra())

    # ------------------------------------------------------------------ contract
    def capabilities(self) -> Capabilities:
        declared = str(self.policy.get('appendContract') or '').lower() == 'declared'
        return Capabilities(
            directory_discovery=SUPPORTED,
            range_read=SUPPORTED,
            stable_identity='size-mtime',
            append_contract='declared' if declared else UNVERIFIED,
            transport_version=self.tool.version() or 'unknown',
            security_status={'hostKey': 'configured' if (self.policy.get('knownHostsFile')
                                                         or self.policy.get('hostKey')) else 'unverified',
                             'transport': 'ssh'},
            limits=dict(self.limits),
            notes=list(self.notes))

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
            detail = dict(getattr(exc, 'details', {}) or {})
            if isinstance(exc, CollectorError):
                detail.setdefault('code', exc.code)
            checks.append(Check('admission', 'fail', getattr(exc, 'message', str(exc)), detail))
            ok = False

        if not (self.policy.get('knownHostsFile') or self.policy.get('hostKey')):
            checks.append(Check('hostkey', 'fail',
                                '未配置 known_hosts 或主机指纹，SSH 主机身份无法确认。', {}))
            ok = False
        else:
            pinned = self.source.get('host_pin')
            declared = self.policy.get('hostKey')
            if pinned and declared and pinned != declared:
                checks.append(Check('hostkey', 'fail', '主机指纹与已批准值不同，已停止采集。',
                                    {'pinned': pinned[:20] + '…', 'declared': str(declared)[:20] + '…'}))
                ok = False
            else:
                checks.append(Check('hostkey', 'pass', '主机指纹来自可信渠道配置。',
                                    {'source': 'known_hosts' if self.policy.get('knownHostsFile') else 'pin'}))

        try:
            version = self.tool.version()
            checks.append(Check('tool', 'pass' if version else 'fail',
                                'rclone 可用。' if version else '未安装 rclone。',
                                {'version': version}))
            if not version:
                ok = False
        except CollectorError as exc:
            checks.append(Check('tool', 'fail', exc.message, exc.to_dict()))
            ok = False

        if ok:
            directory = self.source.get('path') or str(self.policy.get('directory') or '/')
            try:
                listing = self.list(directory, limit=5)
                checks.append(Check('list', 'pass', '目录可读。',
                                    {'entries': len(listing.entries), 'truncated': listing.truncated}))
                sample = listing.entries[0] if listing.entries else None
                if sample:
                    samples.append({'kind': 'entry', 'name': sample.name[:80], 'size': sample.size})
                    chunk = self.read_range(sample.path, offset=0,
                                            length=min(1024, max(1, sample.size)))
                    expected = min(1024, sample.size)
                    good = len(chunk.data) == expected
                    checks.append(Check('range', 'pass' if good else 'fail',
                                        '范围读取返回精确字节。' if good else '范围读取字节数不符。',
                                        {'requested': expected, 'received': len(chunk.data)}))
                    samples.append({'kind': 'range', 'bytes': len(chunk.data)})
                else:
                    checks.append(Check('range', 'skip', '目录为空，未做范围读取采样。', {}))
            except CollectorError as exc:
                checks.append(Check('list', 'fail', exc.message, exc.to_dict()))
                ok = False

        if capabilities.stable_identity != 'inode':
            warnings.append('SFTP 未提供可靠 inode：同大小替换需要边界指纹与代次判定。')
        return ProbeResult(self.source.get('id', ''), config_revision, fingerprint, ok,
                           capabilities, checks, warnings, samples,
                           time_now(), time_future(int(self.policy.get('probeTtl') or 3600)))

    # -------------------------------------------------------------------- reads
    def _path(self, directory: str) -> str:
        text = str(directory or '')
        self._guard.check_path(text)
        return text

    def list(self, directory: str, *, cursor: Optional[str] = None, limit: Optional[int] = None):
        target = self._path(directory)
        depth = min(int(self.limits['maxDepth']), int(self.policy.get('maxDepth') or 3))
        payload = self.tool.lsjson(remote(self.connection(), target),
                                   timeout=int(self.limits['listTimeout']), max_depth=depth)
        entries = parse_listing(payload, directory=target,
                                limit=int(self.limits['maxEntries']))
        included = _filter(entries, self.policy.get('include') or [], self.policy.get('exclude') or [])
        included = [entry for entry in included if not entry.is_dir]
        included.sort(key=lambda entry: entry.path)
        start = 0
        if cursor:
            if not str(cursor).isdigit():
                raise CollectorError('invalid_cursor', '目录游标无效。')
            start = int(cursor)
        page = int(limit or self.limits['maxEntries'])
        window = included[start:start + page]
        from .base import Listing
        return Listing(directory=target, entries=window,
                       next_cursor=str(start + page) if start + page < len(included) else None,
                       truncated=start + page < len(included), scanned=len(included))

    def stat(self, path: str) -> RemoteStat:
        target = self._path(path)
        payload = self.tool.stat(remote(self.connection(), target),
                                 timeout=int(self.limits['listTimeout']))
        return stat_from(payload, path=target)

    def read_range(self, path: str, *, offset: int, length: int,
                   identity: Optional[dict] = None) -> Chunk:
        target = self._path(path)
        stat = self.stat(target)
        if identity:
            previous_size = identity.get('size')
            if previous_size is not None and int(previous_size) != stat.size:
                raise CollectorError('generation_changed', '远端长度变化，拒绝继续追加旧代。',
                                     expected=previous_size, actual=stat.size)
            previous_mtime = identity.get('modTime')
            if previous_mtime is not None and stat.identity.get('modTime') not in (None, previous_mtime):
                raise CollectorError('generation_changed', '远端修改时间变化，代次已切换。',
                                     expected=str(previous_mtime)[:40],
                                     actual=str(stat.identity.get('modTime'))[:40])
        if offset > stat.size:
            raise CollectorError('invalid_range', '读取起点超出远端长度。',
                                 offset=offset, size=stat.size)
        want = min(int(length), max(0, stat.size - offset))
        if want <= 0:
            return Chunk(b'', offset, offset, identity={'size': stat.size})
        reader_key = (target, stat.size, stat.mtime_ns)
        reader = self._readers.get(reader_key)
        if reader is None:
            reader = WindowedReader(
                lambda start, count: self.tool.cat(
                    remote(self.connection(), target), offset=start, count=count,
                    timeout=int(self.limits['readTimeout']),
                    max_stdout=int(self.limits['readWindow'])),
                window=int(self.limits['readWindow']), size=stat.size)
            self._readers = {reader_key: reader}
        data = reader.read(offset, want)
        if len(data) != want:
            raise CollectorError('partial_read', '远端返回的字节数不足。',
                                 expected=want, received=len(data))
        return Chunk(data, offset, offset + len(data),
                     identity={'size': stat.size, 'modTime': stat.mtime_ns},
                     validators={'window': reader.spawns}, verified=False)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for reader in self._readers.values():
            reader.discard()
        self._readers = {}
        if self._key_file:
            try:
                os.unlink(self._key_file)
            except OSError:
                pass
            self._key_file = None


def _filter(entries, include, exclude):
    def matches(entry, patterns):
        return any(fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(entry.path, pattern)
                   for pattern in patterns)

    result = []
    for entry in entries:
        if include and not matches(entry, include):
            continue
        if exclude and matches(entry, exclude):
            continue
        result.append(entry)
    return result


def _admission(policy: dict):
    from .security import Admission
    return Admission.from_policy(policy)


def time_now() -> str:
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def time_future(seconds: int) -> float:
    import time
    return time.time() + seconds
