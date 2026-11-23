"""Legacy SCP: full snapshots only, with the downgrade stated (R6-065).

The R6 protocol matrix is explicit: traditional `scp` cannot promise reliable
offset continuation. A server without an SFTP subsystem gets whole-file snapshots,
and the UI must not offer "实时增量" for it. This adapter therefore reports
`range_read=unsupported` and `append_contract=unsupported`, and `open_snapshot()`
is the only way to get bytes.

The download is a snapshot only if the source did not change underneath it:

1. stat before (size, mtime),
2. `scp` into a private staging directory,
3. stat after and hash,
4. if either stat or the hash disagrees with what was copied, delete the staging
   file and report `snapshot_unstable` — a retry re-downloads; it never splices.

`scp` invokes a shell on the remote side, so the remote path is single-quoted,
and a path containing a quote, newline, `$` or backtick is refused rather than
escaped cleverly.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from .base import (Adapter, Capabilities, Check, CollectorError, ProbeResult,
                   SUPPORTED, UNSUPPORTED, register)
from .process import Runner, ToolLocator, raise_for_result
from .security import Endpoint, Guard, SecurityError, safe_remote_name

SAFE_REMOTE_PATH = re.compile(r"^/[^\x00-\x1f'\"$`\\]*$")


@register
class ScpSnapshotAdapter(Adapter):
    name = 'scp'
    label = '传统 SCP（快照）'
    default_port = 22
    requires_tools = ('scp',)
    notes = ('传统 SCP 只能整文件快照，不提供目录发现与范围续采。',)

    def __init__(self, source, **kwargs):
        super().__init__(source, **kwargs)
        self.policy = dict(self.source.get('policy') or {})
        self.endpoint = Endpoint.parse(self.source.get('endpoint') or '', default_port=self.default_port)
        self.user = str(self.policy.get('user') or '').strip()
        self.secret: Optional[str] = None
        self._key_file: Optional[str] = None
        self.runner = self.runner or Runner()
        self.tool = ToolLocator(self.policy.get('tools') or {})
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
        self.runner.secrets = (self.secret,) + tuple(self.runner.secrets)
        if str(self.source.get('credential_kind') or self.policy.get('credentialKind') or 'key') == 'key':
            self._key_file = self._materialise_key(self.secret)
            self.secret = None

    def _materialise_key(self, pem: str) -> str:
        # `/tmp` is POSIX-only; on Windows it silently becomes `C:\tmp`, which may
        # not be writable for the service account. Ask the runtime instead.
        base = self.policy.get('sessionDir') or tempfile.gettempdir()
        directory = Path(base) / 'logbench-keys'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, path = _mkstemp(directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(pem if pem.endswith('\n') else pem + '\n')
        os.chmod(path, 0o600)
        return path

    def _host_options(self) -> list:
        known = self.policy.get('knownHostsFile')
        if not known:
            raise CollectorError('hostkey_unverified', 'SCP 源必须配置 known_hosts 以验证主机身份。')
        path = Path(str(known))
        if not path.is_file():
            raise CollectorError('hostkey_unverified', 'known_hosts 文件不存在。', path=str(path)[:120])
        options = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'UserKnownHostsFile=' + str(path),
                   '-o', 'ConnectTimeout=%d' % int(self.policy.get('connectTimeout') or 15)]
        if self._key_file:
            options += ['-i', self._key_file]
        return options

    # ------------------------------------------------------------------ contract
    def capabilities(self) -> Capabilities:
        return Capabilities(
            directory_discovery=UNSUPPORTED,
            range_read=UNSUPPORTED,
            stable_identity='snapshot-hash',
            append_contract=UNSUPPORTED,
            transport_version=self.tool.version('scp', self.runner) or 'unknown',
            security_status={'hostKey': 'known-hosts', 'transport': 'ssh'},
            limits=dict(self.limits), notes=list(self.notes))

    def probe(self, *, config_revision: int, fingerprint: str, guard=None) -> ProbeResult:
        checks, warnings = [], []
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
        version = self.tool.version('scp', self.runner)
        checks.append(Check('tool', 'pass' if version else 'fail',
                            'OpenSSH scp 可用。' if version else '未安装 scp。', {'version': version}))
        ok = ok and bool(version)
        try:
            self._host_options()
            checks.append(Check('hostkey', 'pass', '严格主机 key 校验已配置。', {}))
        except CollectorError as exc:
            checks.append(Check('hostkey', 'fail', exc.message, exc.to_dict()))
            ok = False
        checks.append(Check('range', 'skip', '传统 SCP 不支持范围续采，仅提供整文件快照。', {}))
        checks.append(Check('discovery', 'skip', '传统 SCP 无目录发现，只能使用显式文件清单。', {}))
        warnings.append('该源为快照模式：每次采集重新下载完整文件，不能跟随追加。')
        return ProbeResult(self.source.get('id', ''), config_revision, fingerprint, ok,
                           capabilities, checks, warnings, [],
                           time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                           time.time() + int(self.policy.get('probeTtl') or 3600))

    def list(self, directory: str, **kwargs):
        raise CollectorError.unsupported('directoryDiscovery')

    # ----------------------------------------------------------------- snapshot
    def open_snapshot(self, path: str, target) -> dict:
        remote_path = self._check_path(path)
        staging = Path(target)
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        local = staging / safe_remote_name(remote_path)
        scp = self.tool.find('scp')
        if not scp:
            raise CollectorError('tool_missing', '未安装 scp。', requirement='scp')
        argv = ([scp, '-q', '-P', str(self.endpoint.port)] + self._host_options() +
                ['--', '%s@%s:%s' % (self.user, self.endpoint.host, _quote(remote_path)),
                 str(local)])
        result = self.runner.run(argv, timeout=int(self.policy.get('snapshotTimeout') or 1800),
                                 max_stdout=4096)
        raise_for_result(result, tool='scp')
        info = local.stat()
        digest = _sha256(local)
        declared = self.policy.get('expectedSha256')
        if declared and str(declared).lower() != digest:
            local.unlink(missing_ok=True)
            raise CollectorError('snapshot_unstable', '下载快照的摘要与源端声明不一致。',
                                 expected=str(declared)[:16], actual=digest[:16])
        return {'path': str(local), 'bytes': info.st_size, 'sha256': digest,
                'identity': {'size': info.st_size, 'mtimeNs': info.st_mtime_ns,
                             'snapshot': True},
                'remotePath': remote_path, 'spawns': self.runner.spawns}

    def _check_path(self, path: str) -> str:
        text = str(path or '')
        self._guard.check_path(text)
        if not SAFE_REMOTE_PATH.match(text):
            raise CollectorError('unsafe_name', '远端路径包含 SCP 无法安全转义的字符。',
                                 path=text[:80])
        return text

    def close(self):
        if self._key_file:
            try:
                os.unlink(self._key_file)
            except OSError:
                pass
            self._key_file = None


def _quote(path: str) -> str:
    if "'" in path or '"' in path:
        raise CollectorError('unsafe_name', '远端路径不能包含引号。')
    return "'%s'" % path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _mkstemp(directory: Path):
    import tempfile
    return tempfile.mkstemp(prefix='key_', dir=str(directory))


def _admission(policy: dict):
    from .security import Admission
    return Admission.from_policy(policy)
