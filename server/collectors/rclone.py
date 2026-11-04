"""rclone as a transport primitive, not as an archiver (R6-060/061/062).

rclone is used through an **on-the-fly backend string** (`:sftp,host=…,user=…:`)
rather than a config file. That removes three problems at once: no `rclone.conf`
with secrets in it, no `--config` flag an administrator could point elsewhere, and
no per-source configuration that a second process could edit underneath us.

Passwords never appear in argv. They are passed through the backend's environment
variable (`RCLONE_SFTP_PASS`, `RCLONE_SMB_PASS`), which the runner merges only for
that call.

rclone does not decide what a "generation" is, and it does not commit anything to
our archive. It lists, stats and reads bytes; `archive.py` decides.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from .base import CollectorError, Entry, RemoteStat
from .process import Result, ToolLocator, raise_for_result

SAFE_SEGMENT = re.compile(r'^[A-Za-z0-9._@-]{1,255}$')
MAX_LISTING = 32 * 1024 * 1024


def _segment(value: str, label: str) -> str:
    text = str(value or '')
    if not SAFE_SEGMENT.match(text):
        raise CollectorError('invalid_endpoint', '%s包含不支持的字符。' % label, value=text[:80])
    return text


def connection(backend: str, *, host: str, user: str, port: int, extra: str = '') -> str:
    """`:sftp,host=h,user=u,port=22:` — validated so a comma cannot inject a field."""
    parts = ['host=' + _segment(host, '主机名'), 'user=' + _segment(user, '用户名'),
             'port=%d' % int(port)]
    if extra:
        parts.append(extra)
    return ':%s,%s:' % (backend, ','.join(parts))


def remote(conn: str, path: str) -> str:
    """Join a validated absolute remote path onto a backend string."""
    if not path.startswith('/'):
        raise CollectorError('invalid_path', '远端路径必须是绝对路径。', path=str(path)[:80])
    if ':' in path:
        # rclone treats the first colon as the remote separator.
        raise CollectorError('invalid_path', '远端路径不能包含冒号。', path=str(path)[:80])
    return conn + path


def parse_listing(payload: bytes, *, directory: str, limit: int) -> list:
    try:
        rows = json.loads(payload.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise CollectorError('invalid_listing', '远端列表不是有效 JSON。')
    if not isinstance(rows, list):
        raise CollectorError('invalid_listing', '远端列表格式不符。')
    if len(rows) > limit:
        raise CollectorError('listing_too_large', '远端条目数超过上限。', entries=len(rows), limit=limit)
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise CollectorError('invalid_listing', '远端条目格式不符。')
        name = str(row.get('Path') or row.get('Name') or '')
        if not name:
            continue
        full = (directory.rstrip('/') + '/' + name.lstrip('/')) if directory else '/' + name
        size = row.get('Size')
        mtime = row.get('ModTime')
        entries.append(Entry(
            path=full, name=name.rsplit('/', 1)[-1],
            size=int(size) if isinstance(size, (int, float)) else 0,
            mtime_ns=_mtime_ns(mtime),
            is_dir=bool(row.get('IsDir')),
            identity={'size': size, 'modTime': mtime,
                      'id': row.get('ID') or row.get('Hashes', {})}))
    return entries


def stat_from(payload: bytes, *, path: str) -> RemoteStat:
    try:
        row = json.loads(payload.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise CollectorError('invalid_listing', '远端 stat 不是有效 JSON。')
    if not isinstance(row, dict):
        raise CollectorError('invalid_listing', '远端 stat 格式不符。')
    size = row.get('Size')
    mtime = row.get('ModTime')
    return RemoteStat(path=path, size=int(size) if isinstance(size, (int, float)) else 0,
                      mtime_ns=_mtime_ns(mtime), is_dir=bool(row.get('IsDir')),
                      identity={'size': size, 'modTime': mtime, 'id': row.get('ID')},
                      validators={key: value for key, value in (row.get('Hashes') or {}).items()})


def _mtime_ns(value) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace('Z', '+00:00')
    try:
        import datetime
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        import datetime as _dt
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp() * 1e9)


class Rclone:
    """Thin, counted wrapper. Every call is one bounded subprocess."""

    def __init__(self, runner, *, secrets: Iterable[str] = (), device: Optional[ToolLocator] = None):
        self.runner = runner
        self.secrets = tuple(secret for secret in secrets if secret)
        self.device = device or ToolLocator()
        self.path = self.device.find('rclone')

    def require(self):
        if not self.path:
            raise CollectorError('tool_missing', '未安装 rclone，该协议不可用。',
                                 requirement='rclone')

    def _run(self, args: list, *, timeout: int, max_stdout: int, env: Optional[dict] = None) -> Result:
        self.require()
        result = self.runner.run([self.path] + args, timeout=timeout,
                                 max_stdout=max_stdout, env=env or {})
        raise_for_result(result, tool='rclone', secrets=self.secrets)
        return result

    def lsjson(self, target: str, *, timeout: int, max_depth: Optional[int] = None,
               max_stdout: int = MAX_LISTING) -> bytes:
        args = ['lsjson', '--files-only', target]
        if max_depth is not None:
            args += ['--max-depth', str(int(max_depth))]
        return self._run(args, timeout=timeout, max_stdout=max_stdout).stdout

    def stat(self, target: str, *, timeout: int) -> bytes:
        return self._run(['lsjson', '--stat', target], timeout=timeout, max_stdout=1024 * 1024).stdout

    def cat(self, target: str, *, offset: int, count: int, timeout: int,
            max_stdout: int) -> bytes:
        args = ['cat', target]
        if offset:
            args += ['--offset', str(int(offset))]
        if count:
            args += ['--count', str(int(count))]
        return self._run(args, timeout=timeout, max_stdout=max_stdout).stdout

    def copyto(self, target: str, local: str, *, timeout: int) -> None:
        self._run(['copyto', target, local], timeout=timeout, max_stdout=64 * 1024)

    def version(self) -> Optional[str]:
        return self.device.version('rclone', self.runner)
