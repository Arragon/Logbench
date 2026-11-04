"""Deterministic tool fixtures for the rclone/scp adapters.

These fixtures stand in for a *binary*, not for a server: they produce the same
shapes rclone emits (`lsjson`, `cat --offset --count`) and count spawns, so the
adapter's parsing, windowing, identity checks and cleanup are exercised for real.
They are explicitly **not** protocol integration evidence — that is recorded as
BLOCKED in `qa/evidence/M6-gate.md`, because this machine has no SFTP/SMB endpoint
and no installed rclone.
"""
from __future__ import annotations

import json
import posixpath
from typing import Optional

from server.collectors.process import Result


def remote_path(target: str) -> str:
    """`:sftp,host=h,user=u,port=22:/var/log` → `/var/log`."""
    if target.startswith(':') and ':' in target[1:]:
        return '/' + target.split(':', 1)[1].lstrip('/') if target.endswith(':') is False else '/'
    return '/'


class FakeTool:
    def __init__(self, files: Optional[dict] = None, *, version: str = 'rclone v1.66.0',
                 public_path: str = '/bin/echo', directories=None):
        self.files = dict(files or {})
        self.directories = set(directories or ())
        self.version_line = version
        self.public_path = public_path
        self.spawns = 0
        self.calls = []
        self.fail_with: Optional[Result] = None
        self.secrets = ()

    # ------------------------------------------------------------------ runner
    def run(self, argv, *, timeout=None, max_stdout=None, max_stderr=None, env=None,
            input=None) -> Result:
        values = [str(value) for value in argv]
        self.spawns += 1
        self.calls.append(values)
        if self.fail_with is not None:
            return self.fail_with
        if '--version' in values or '-V' in values:
            return Result(values, 0, (self.version_line + '\n').encode(), '', 0.0)
        command = values[1] if len(values) > 1 else ''
        target = _find_target(values) if command != 'copyto' else values[2]
        path = _path(target)
        if command == 'lsjson':
            if '--stat' in values:
                if path not in self.files:
                    return _missing(values, path)
                info = self.files[path]
                return Result(values, 0, json.dumps({
                    'Path': posixpath.basename(path), 'Name': posixpath.basename(path),
                    'Size': len(info['data']), 'ModTime': info['mtime'],
                    'IsDir': False, 'Hashes': {}}).encode(), '', 0.0)
            if self.directories and path not in self.directories:
                return Result(values, 3, b'', 'directory not found: ' + path, 0.0)
            rows = []
            prefix = path.rstrip('/') + '/'
            for name, info in sorted(self.files.items()):
                if not name.startswith(prefix):
                    continue
                relative = name[len(prefix):]
                if '/' in relative:
                    continue
                rows.append({'Path': relative, 'Name': relative,
                             'Size': len(info['data']), 'ModTime': info['mtime'],
                             'IsDir': False, 'Hashes': {}})
            return Result(values, 0, json.dumps(rows).encode(), '', 0.0)
        if command == 'lsd':
            rows = [{'Path': share, 'Name': share, 'IsDir': True}
                    for share in (self.shares if hasattr(self, 'shares') else ['logs'])]
            return Result(values, 0, json.dumps(rows).encode(), '', 0.0)
        if command == 'cat':
            if path not in self.files:
                return _missing(values, path)
            data = self.files[path]['data']
            offset = 0
            count = len(data)
            for index, value in enumerate(values):
                if value == '--offset':
                    offset = int(values[index + 1])
                if value == '--count':
                    count = int(values[index + 1])
            return Result(values, 0, data[offset:offset + count], '', 0.0)
        if command == 'copyto':
            source, local = values[2], values[3]
            data = self.files.get(_path(source), {'data': b''})['data']
            with open(local, 'wb') as handle:
                handle.write(data)
            return Result(values, 0, b'', '', 0.0)
        return Result(values, 2, b'', 'unknown command', 0.0)

    def cancel(self):
        return None


def _find_target(values: list) -> str:
    for value in values[1:]:
        if value.startswith(':'):
            return value
    return values[-1] if values else ''


def _path(target: str) -> str:
    if target.startswith(':') and target.count(':') >= 2:
        tail = target.split(':', 2)[2]
        return tail if tail.startswith('/') else '/' + tail
    if ':' in target:
        return '/' + target.split(':', 1)[1].lstrip('/')
    return target


def _missing(argv, path):
    return Result(argv, 4, b'', 'directory not found: ' + path, 0.0)
