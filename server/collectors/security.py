"""Host admission, argv shape and redaction (R6-059).

The threat model is not "the remote host is malicious". It is that a *configuration*
can be made to point somewhere it should not (metadata endpoint, loopback admin
port, another tenant's share), or that a value from a form can become a command
argument. Three checks address that:

1. **Admission** — every hostname is resolved at connect time and *all* answers
   must be in the approved set. A host that resolved to `10.0.0.5` when the probe
   ran and resolves to `169.254.169.254` now is refused, not retried.
2. **argv shape** — arguments are a fixed list, `shell=False`, and a small set of
   rclone/OpenSSH options that can execute a program (`-oProxyCommand`,
   `--sftp-ssh`, `--config`) cannot be supplied by configuration.
3. **Redaction** — every string that leaves this process (error message, log line,
   audit detail, probe result) passes through `redact()` with the actual secret
   values of that attempt.

None of this makes IP identity a strong login. The README keeps saying that, and
so does `docs/R6/collectors.md`.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Iterable, Optional

METADATA_ADDRESSES = (
    ipaddress.ip_address('169.254.169.254'),
    ipaddress.ip_address('fd00:ec2::254'),
)
#: Characters that let a value escape its position in a shell or a remote command.
UNSAFE_CHARS = set(';|&$`><\n\r\0')
#: rclone/OpenSSH options that turn a transfer tool into a command runner.
FORBIDDEN_FLAGS = (
    'proxycommand', '--sftp-ssh', '--config', '--rc', '--rc-addr', '--daemon',
    '--ssh', '-o', '-e', '--ssh-command', '--header-command',
)


class SecurityError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _address(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _networks(values: Iterable[str]) -> list:
    nets = []
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            raise SecurityError('invalid_network', '准入网段格式无效：' + text)
    return nets


@dataclass
class Endpoint:
    host: str
    port: int = 22
    path: str = '/'

    @classmethod
    def parse(cls, raw: str, *, default_port: int = 22) -> 'Endpoint':
        text = str(raw or '').strip()
        if not text or len(text) > 512:
            raise SecurityError('invalid_endpoint', '连接地址不能为空且不超过 512 字符。')
        host, port, path = text, default_port, '/'
        if '://' in text:
            scheme, rest = text.split('://', 1)
            if scheme.lower() not in ('sftp', 'smb', 'http', 'https', 'scp', 'ssh'):
                raise SecurityError('invalid_endpoint', '不支持的连接协议：' + scheme)
            if scheme.lower() in ('http', 'https'):
                rest = rest.split('/', 1)
                path = '/' + (rest[1] if len(rest) > 1 else '')
                rest = rest[0]
            else:
                rest = rest.split('/', 1)[0]
            text = rest
            if '@' in text:
                raise SecurityError('credentials_in_url', '连接地址不能包含用户名或口令，请使用凭据引用。')
            host = text
            if ':' in host:
                host, _, tail = host.rpartition(':')
                if tail.isdigit():
                    port = int(tail)
                else:
                    host, port = text, default_port
        elif '/' in text and not text.startswith('//'):
            host, path = text.split('/', 1)
            path = '/' + path
        if '@' in host:
            raise SecurityError('credentials_in_url', '连接地址不能包含用户名或口令，请使用凭据引用。')
        return cls(host=host.strip('[]'), port=port, path=path or '/')


@dataclass
class Admission:
    """What an administrator approved for one source."""

    allowed_networks: list = field(default_factory=list)
    allowed_hosts: list = field(default_factory=list)
    allowed_ports: list = field(default_factory=list)
    allowed_paths: list = field(default_factory=list)
    allow_loopback: bool = False

    @classmethod
    def from_policy(cls, policy: Optional[dict]) -> 'Admission':
        policy = policy or {}
        allowed_hosts = [str(v).strip().lower() for v in policy.get('allowedHosts') or () if str(v).strip()]
        ports = []
        for value in policy.get('allowedPorts') or ():
            try:
                ports.append(int(value))
            except (TypeError, ValueError):
                raise SecurityError('invalid_port', '准入端口必须是整数。')
        paths = [str(v) for v in policy.get('allowedPaths') or () if str(v).strip()]
        return cls(allowed_networks=_networks(policy.get('allowedNetworks') or ()),
                   allowed_hosts=allowed_hosts, allowed_ports=ports,
                   allowed_paths=paths, allow_loopback=bool(policy.get('allowLoopback')))


class Guard:
    """Admission decisions for one attempt. Never reuses a previous resolution."""

    def __init__(self, admission: Admission, *, resolver=None):
        self.admission = admission
        self._resolver = resolver or self._default_resolve

    @staticmethod
    def _default_resolve(host: str, port: int) -> list:
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SecurityError('dns_failed', '无法解析主机名，请检查 DNS 与准入配置。', detail=str(exc))
        found = []
        for info in infos:
            address = _address(info[4][0])
            if address is not None and str(address) not in found:
                found.append(str(address))
        if not found:
            raise SecurityError('dns_failed', '主机名没有可用的地址记录。')
        return found

    def resolve(self, host: str, port: int) -> list:
        return [str(value) for value in self._resolver(host, port)]

    def admit(self, endpoint: Endpoint, *, expected_addresses: Optional[Iterable[str]] = None) -> list:
        """Return the resolved addresses, or raise. Called per connection *and*
        per redirect hop."""
        admission = self.admission
        host = endpoint.host.strip().lower()
        if not host or len(host) > 255:
            raise SecurityError('invalid_host', '主机名无效。')
        literal = _address(host)
        addresses = [str(literal)] if literal is not None else self.resolve(host, endpoint.port)
        if admission.allowed_hosts and host not in admission.allowed_hosts and literal is None:
            # A name that was not approved cannot be admitted merely because it
            # currently resolves into an allowed range (that is the rebinding case).
            raise SecurityError('host_not_allowed', '主机未在准入名单中。', host=host)
        if admission.allowed_ports and endpoint.port not in admission.allowed_ports:
            raise SecurityError('port_not_allowed', '端口未在准入范围内。', port=endpoint.port)
        for value in addresses:
            address = _address(value)
            if address is None:
                raise SecurityError('invalid_address', '解析结果不是 IP 地址。')
            if any(address == meta for meta in METADATA_ADDRESSES):
                raise SecurityError('metadata_address_denied', '拒绝连接云元数据地址。')
            if address.is_loopback:
                # `::1` also reports is_reserved, so this branch must come first.
                if not admission.allow_loopback:
                    raise SecurityError('loopback_denied', '默认拒绝环回地址，需管理员显式批准。')
                continue
            if address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
                raise SecurityError('address_denied', '目标地址属于保留/链路本地范围。', address=value)
            if admission.allowed_networks and not any(address in net for net in admission.allowed_networks):
                raise SecurityError('address_not_allowed', '目标地址不在准入网段内。', address=value)
        if expected_addresses:
            expected = {str(_address(value)) for value in expected_addresses if _address(value)}
            if expected and set(addresses) != expected:
                raise SecurityError('dns_changed', '主机解析结果与预检时不同，已停止采集。',
                                    expected=sorted(expected), actual=sorted(addresses))
        return addresses

    def check_path(self, path: str) -> str:
        text = str(path or '').strip()
        if not text.startswith('/'):
            raise SecurityError('invalid_path', '远端路径必须是绝对路径。')
        if len(text) > 4096 or any(ch in text for ch in UNSAFE_CHARS):
            raise SecurityError('invalid_path', '远端路径包含非法字符。')
        if any(ord(ch) < 32 for ch in text):
            raise SecurityError('invalid_path', '远端路径包含控制字符。')
        parts = [part for part in text.split('/') if part]
        if any(part in ('.', '..') for part in parts):
            raise SecurityError('invalid_path', '远端路径不能包含上级目录。')
        prefixes = self.admission.allowed_paths
        if prefixes and not any(text == prefix.rstrip('/') or text.startswith(prefix.rstrip('/') + '/')
                                for prefix in prefixes):
            raise SecurityError('path_not_allowed', '远端路径不在批准范围内。')
        return text


def guard_args(values: Iterable[str], *, positional_safe: bool = True) -> list:
    """Validate user-derived arguments. The caller assembles its own fixed argv on
    top of these; a value from a form never becomes an option."""
    items = list(values)
    for value in items:
        if not isinstance(value, str) or '\0' in value:
            raise SecurityError('invalid_argv', '命令参数必须是字符串。')
        if any(ch in value for ch in '\n\r'):
            raise SecurityError('invalid_argv', '命令参数不能包含换行。')
        lowered = value.lower()
        for flag in FORBIDDEN_FLAGS:
            if lowered == flag or lowered.startswith(flag + '='):
                raise SecurityError('forbidden_flag', '不允许在配置中使用该参数：' + flag)
        if lowered.startswith('-o') and len(lowered) > 2:
            # `-oProxyCommand=...` is the same instruction as `-o ProxyCommand=...`.
            tail = lowered[2:].lstrip('=')
            for flag in FORBIDDEN_FLAGS:
                if tail.startswith(flag.lstrip('-') + '=') or tail == flag.lstrip('-'):
                    raise SecurityError('forbidden_flag', '不允许在配置中使用该参数：' + flag)
        if positional_safe and any(ch in value for ch in ';|&$`><'):
            # Paths with spaces are fine because argv is not a shell string; shell
            # metacharacters are refused because they signal a value that was meant
            # for a shell.
            raise SecurityError('unsafe_argument', '命令参数包含 shell 元字符。')
    return items


def guard_argv(argv: Iterable[str], *, positional_safe: bool = True) -> list:
    values = list(argv)
    if not values or not isinstance(values[0], str):
        raise SecurityError('invalid_argv', '命令参数为空。')
    return guard_args(values, positional_safe=positional_safe)


def safe_remote_name(path: str, *, limit: int = 180) -> str:
    """A local file name that cannot escape a directory, while the original path
    stays in the evidence row."""
    base = str(path or '').rstrip('/').rsplit('/', 1)[-1]
    cleaned = ''.join(('_' if (ord(ch) < 32 or ch in '/\\:*?"<>|') else ch) for ch in base).strip()
    cleaned = cleaned.lstrip('.') or 'log'
    if len(cleaned) > limit:
        stem, dot, suffix = cleaned.rpartition('.')
        keep = limit - len(suffix) - 1 if dot and len(suffix) <= 8 else limit
        cleaned = (stem[:max(1, keep)] + (dot + suffix if dot and len(suffix) <= 8 else ''))
    return cleaned


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    value = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            value = value.replace(secret, '***')
    # URL userinfo and common key/value shapes, for secrets we were not handed.
    value = re.sub(r'([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]*@', r'\1***:***@', value, flags=re.I)
    value = re.sub(r'(?i)(pass(?:word)?|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+', r'\1=***', value)
    return value
