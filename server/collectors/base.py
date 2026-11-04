"""The adapter contract every protocol must satisfy (R6-060).

The point of this file is to make *capability honesty* structural. In R5 the
remote page showed the same "持续采集" switch for every transport, which is how a
user comes to expect tail-following over a plain HTTP URL. Here a capability is an
explicit state:

* `supported`   — measured, with the check that measured it recorded in the probe;
* `unverified`  — the protocol allows it, but this endpoint/configuration has not
  proven it (for example an HTTP server without a strong validator);
* `unsupported` — the protocol cannot do it.

`Adapter.list()` raises `unsupported` instead of returning an empty directory, so
"no files" and "cannot list" never look the same. `probe()` is bound to the
configuration revision it measured: `sources.py` refuses to enable a revision whose
probe fingerprint does not match.

`archive.py` (R6-070) owns the actual commit. A transport that can read bytes is
not thereby a reliable incremental source — that judgement belongs to
`generations.py`, which compares identity, size and boundary evidence.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

SUPPORTED = 'supported'
UNVERIFIED = 'unverified'
UNSUPPORTED = 'unsupported'

#: Errors the scheduler may retry with backoff. Anything else stops until a human
#: acts, because retrying a bad credential or a changed host key is pointless noise.
RETRYABLE = frozenset({'timeout', 'transport_error', 'busy', 'locked', 'dns_failed',
                       'partial_read', 'too_many_requests', 'server_error'})
#: Errors that must stop collection and surface a recovery action.
MANUAL = frozenset({'auth_failed', 'credential_missing', 'credential_permissions',
                    'hostkey_changed', 'certificate_invalid', 'host_not_allowed',
                    'port_not_allowed', 'address_not_allowed', 'metadata_address_denied',
                    'loopback_denied', 'path_not_allowed', 'permission_denied',
                    'forbidden_flag', 'unsafe_argument', 'tool_missing'})
#: Errors that are a property of one file, not of the source.
FILE_LEVEL = frozenset({'not_found', 'generation_changed', 'snapshot_unstable',
                        'unsafe_name', 'directory_not_found', 'unsupported'})


class CollectorError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': self.message, 'details': self.details,
                'retryable': self.code in RETRYABLE, 'needs_human': self.code in MANUAL}

    @classmethod
    def unsupported(cls, capability: str):
        return cls('unsupported', '该协议不支持所需能力：' + capability, capability=capability)


def error_class(code: str) -> str:
    if code in RETRYABLE:
        return 'retry'
    if code in MANUAL:
        return 'manual'
    if code in FILE_LEVEL:
        return 'file'
    return 'fatal'


@dataclass
class Capabilities:
    directory_discovery: str = UNSUPPORTED
    range_read: str = UNSUPPORTED
    stable_identity: str = 'none'          # inode | validators | size-mtime | none
    append_contract: str = UNSUPPORTED     # declared | unverified | unsupported
    transport_version: str = 'unknown'
    security_status: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def state(self, name: str) -> str:
        value = getattr(self, name, UNSUPPORTED)
        return value if value in (SUPPORTED, UNVERIFIED, UNSUPPORTED) else str(value)

    def require(self, name: str):
        state = self.state(name)
        if state == UNSUPPORTED:
            raise CollectorError.unsupported(name)
        return state

    def allows_follow(self) -> bool:
        """Whether tail-following may be offered at all."""
        return (self.state('range_read') != UNSUPPORTED
                and self.state('append_contract') == 'declared')

    def to_dict(self) -> dict:
        return {
            'directoryDiscovery': self.directory_discovery,
            'rangeRead': self.range_read,
            'stableIdentity': self.stable_identity,
            'appendContract': self.append_contract,
            'transportVersion': self.transport_version,
            'securityStatus': self.security_status,
            'limits': self.limits,
            'notes': list(self.notes),
            'followCapable': self.allows_follow(),
        }


@dataclass
class Check:
    id: str
    state: str          # pass | fail | blocked | skip
    detail: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'id': self.id, 'state': self.state, 'detail': self.detail, 'evidence': self.evidence}


@dataclass
class ProbeResult:
    source_id: str
    config_revision: int
    fingerprint: str
    ok: bool
    capabilities: Capabilities
    checks: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    samples: list = field(default_factory=list)
    checked_at: str = ''
    expires_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            'sourceId': self.source_id,
            'configRevision': self.config_revision,
            'fingerprint': self.fingerprint,
            'ok': self.ok,
            'capabilities': self.capabilities.to_dict(),
            'checks': [check.to_dict() if isinstance(check, Check) else check for check in self.checks],
            'warnings': list(self.warnings),
            'samples': list(self.samples),
            'checkedAt': self.checked_at,
            'expiresAt': self.expires_at,
            'expired': self.expires_at < time.time(),
        }


@dataclass
class RemoteStat:
    path: str
    size: int
    mtime_ns: Optional[int] = None
    is_dir: bool = False
    identity: dict = field(default_factory=dict)
    validators: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'path': self.path, 'size': self.size, 'mtimeNs': self.mtime_ns,
                'isDir': self.is_dir, 'identity': dict(self.identity),
                'validators': dict(self.validators)}


@dataclass
class Entry:
    path: str
    name: str
    size: int
    mtime_ns: Optional[int] = None
    is_dir: bool = False
    identity: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'path': self.path, 'name': self.name, 'size': self.size,
                'mtimeNs': self.mtime_ns, 'isDir': self.is_dir,
                'identity': dict(self.identity)}


@dataclass
class Listing:
    directory: str
    entries: list = field(default_factory=list)
    next_cursor: Optional[str] = None
    truncated: bool = False
    scanned: int = 0

    def to_dict(self) -> dict:
        return {'directory': self.directory,
                'entries': [entry.to_dict() if isinstance(entry, Entry) else entry
                            for entry in self.entries],
                'nextCursor': self.next_cursor, 'truncated': self.truncated,
                'scanned': self.scanned}


@dataclass
class Chunk:
    data: bytes
    byte_start: int
    byte_end: int
    identity: dict = field(default_factory=dict)
    validators: dict = field(default_factory=dict)
    verified: bool = False

    def to_dict(self) -> dict:
        return {'byteStart': self.byte_start, 'byteEnd': self.byte_end,
                'length': len(self.data), 'identity': dict(self.identity),
                'validators': dict(self.validators), 'verified': self.verified}


class Adapter:
    """Read-only transport. No adapter may write, delete or execute remotely."""

    name = 'base'
    default_port = 22
    requires_tools: tuple = ()

    def __init__(self, source: dict, *, credentials=None, runner=None, guard=None,
                 limits: Optional[dict] = None):
        self.source = dict(source or {})
        self.credentials = credentials
        self.runner = runner
        self.guard = guard
        self.limits = self.default_limits()
        self.limits.update({key: value for key, value in (limits or {}).items() if value})

    # ----------------------------------------------------------------- contract
    def default_limits(self) -> dict:
        return {
            'maxEntries': 20000,        # one listing, before pagination
            'maxDepth': 8,
            'maxDirectoryBytes': 4 * 1024 * 1024,
            'maxReadBytes': 8 * 1024 * 1024,
            'readWindow': 8 * 1024 * 1024,
            'readTimeout': 60,
            'listTimeout': 120,
            'maxConcurrentReads': 1,
        }

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def probe(self, *, config_revision: int, fingerprint: str, guard=None) -> ProbeResult:
        raise NotImplementedError

    def list(self, directory: str, *, cursor: Optional[str] = None, limit: Optional[int] = None) -> Listing:
        raise CollectorError.unsupported('directoryDiscovery')

    def stat(self, path: str) -> RemoteStat:
        raise CollectorError.unsupported('stat')

    def read_range(self, path: str, *, offset: int, length: int,
                   identity: Optional[dict] = None) -> Chunk:
        raise CollectorError.unsupported('rangeRead')

    def open_snapshot(self, path: str, target) -> dict:
        raise CollectorError.unsupported('snapshot')

    def cancel(self):
        """Cancel in-flight work. Must be safe to call twice."""

    def close(self):
        self.cancel()

    # ------------------------------------------------------------------ helpers
    def read_bounded(self, start: int, end: int, *, window_chunks: int = 1) -> bytes:
        """Read [start, end) through `read_range`, refusing unbounded requests."""
        if end < start:
            raise CollectorError('invalid_range', '读取范围无效。')
        length = end - start
        if length > int(self.limits['maxReadBytes']) * window_chunks:
            raise CollectorError('range_too_large', '单次读取超过上限，请分段读取。',
                                 requested=length)
        pieces, position = [], start
        while position < end:
            want = min(int(self.limits['readWindow']), end - position)
            chunk = self.read_range(self.source.get('path', ''), offset=position, length=want)
            if not chunk.data:
                raise CollectorError('partial_read', '远端未返回数据。', at=position)
            pieces.append(chunk.data)
            position += len(chunk.data)
            if len(chunk.data) < want:
                raise CollectorError('partial_read', '远端提前结束响应。', at=position)
        return b''.join(pieces)


_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def adapter_class(name: str):
    return _REGISTRY.get(str(name))


def adapter_names() -> list:
    return sorted(_REGISTRY)


def availability(name: str) -> dict:
    """What the UI may offer. A missing tool is `unavailable`, not an empty list."""
    cls = adapter_class(name)
    if cls is None:
        return {'adapter': name, 'available': False, 'reason': 'unknown_adapter',
                'requirements': [], 'label': name}
    from .process import ToolLocator
    locator = ToolLocator()
    missing = [tool for tool in getattr(cls, 'requires_tools', ()) if not locator.find(tool)]
    return {
        'adapter': name,
        'label': getattr(cls, 'label', name),
        'available': not missing,
        'reason': None if not missing else 'tool_missing',
        'missingTools': missing,
        'requirements': list(getattr(cls, 'requires_tools', ())),
        'notes': list(getattr(cls, 'notes', ())),
    }


def build(source: dict, *, credentials=None, runner=None, guard=None, limits=None) -> Adapter:
    cls = adapter_class(source.get('adapter'))
    if cls is None:
        raise CollectorError('unsupported', '未知的传输协议：' + str(source.get('adapter')))
    state = availability(cls.name)
    if not state['available']:
        raise CollectorError('tool_missing', '运行环境缺少传输组件：' + ', '.join(state['missingTools']),
                             adapter=cls.name, missing=state['missingTools'])
    return cls(source, credentials=credentials, runner=runner, guard=guard, limits=limits)
