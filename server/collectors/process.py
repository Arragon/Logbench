"""Bounded subprocess transport (R6-060).

Everything remote goes through this file, for one reason: a spawn is not a detail.
Two R5-era habits are refused here on purpose.

* **A shell string is never built.** `argv` is a list, `shell=False`, and
  `security.guard_argv` has already rejected options that can run a program.
* **Reads are windowed.** Asking `rclone cat` for 4 MiB at a time means one TCP
  connection and one authentication per 4 MiB, which is both slow and a poor
  excuse for "the network is bad". `WindowedReader` fetches a whole window and
  serves consecutive ranges from it, and `spawns` is a number the tests assert on.

Output is capped while it is being produced, not after `communicate()` returns,
because a runaway listing can otherwise exhaust memory before the cap is checked.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .base import CollectorError

DEFAULT_TIMEOUT = 120
MAX_STDOUT = 8 * 1024 * 1024
MAX_STDERR = 64 * 1024


@dataclass
class Result:
    argv: list
    returncode: int
    stdout: bytes = b''
    stderr: str = ''
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    overflow: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled and not self.overflow

    def to_dict(self) -> dict:
        return {'argv': list(self.argv), 'returncode': self.returncode,
                'bytes': len(self.stdout), 'duration': round(self.duration, 3),
                'timed_out': self.timed_out, 'cancelled': self.cancelled,
                'overflow': self.overflow}


class ToolLocator:
    """Where a binary came from, so "it works on my machine" is checkable."""

    def __init__(self, paths: Optional[dict] = None):
        self.paths = dict(paths or {})
        self._versions: dict = {}

    def find(self, name: str) -> Optional[str]:
        if name in self.paths:
            candidate = self.paths[name]
            return candidate if candidate and os.path.isfile(candidate) else None
        explicit = os.environ.get('LOGBENCH_%s' % name.upper().replace('-', '_'))
        if explicit:
            return explicit if os.path.isfile(explicit) else None
        return shutil.which(name)

    def version(self, name: str, runner: Optional['Runner'] = None) -> Optional[str]:
        if name in self._versions:
            return self._versions[name]
        path = self.find(name)
        if not path:
            return None
        runner = runner or Runner()
        flag = '--version' if name != 'scp' else '-V'
        try:
            result = runner.run([path, flag], timeout=10, max_stdout=8192)
        except CollectorError:
            return None
        text = (result.stdout or b'').decode('utf-8', 'replace') or result.stderr
        line = (text.strip().splitlines() or [''])[0][:160]
        self._versions[name] = line or None
        return self._versions[name]

    def report(self) -> dict:
        return {name: {'path': self.find(name), 'version': self.version(name)}
                for name in ('rclone', 'scp', 'ssh')}


class Runner:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, max_stdout: int = MAX_STDOUT,
                 max_stderr: int = MAX_STDERR, env: Optional[dict] = None,
                 secrets: Iterable[str] = ()):
        self.timeout = timeout
        self.max_stdout = max_stdout
        self.max_stderr = max_stderr
        self.env = dict(env or {})
        self.secrets = tuple(secret for secret in secrets if secret)
        self.spawns = 0
        self._active: list = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ running
    def run(self, argv: Iterable[str], *, timeout: Optional[float] = None,
            max_stdout: Optional[int] = None, max_stderr: Optional[int] = None,
            env: Optional[dict] = None, input: Optional[bytes] = None) -> Result:
        values = [str(value) for value in argv]
        if not values:
            raise CollectorError('invalid_argv', '命令参数为空。')
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith('RCLONE_') and not k.startswith('SSH_')}
        environment.update(self.env)
        environment.update(env or {})
        started = time.monotonic()
        with self._lock:
            self.spawns += 1
        try:
            process = subprocess.Popen(
                values, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                shell=False, start_new_session=True)
        except FileNotFoundError:
            raise CollectorError('tool_missing', '找不到传输组件，请先完成环境预检。')
        except OSError as exc:
            raise CollectorError('spawn_failed', '无法启动传输组件。', detail=str(exc))
        with self._lock:
            self._active.append(process)
        buffers = {'out': [], 'err': []}
        sizes = {'out': 0, 'err': 0}
        overflow = threading.Event()
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, 'out', buffers, sizes,
                                                  max_stdout or self.max_stdout, overflow),
                             daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, 'err', buffers, sizes,
                                                  max_stderr or self.max_stderr, overflow),
                             daemon=True),
        ]
        for reader in readers:
            reader.start()
        if input is not None:
            try:
                process.stdin.write(input)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        timed_out = cancelled = False
        try:
            process.wait(timeout=timeout if timeout is not None else self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(process)
        if overflow.is_set() and process.poll() is None:
            _terminate(process)
        for reader in readers:
            reader.join(timeout=2)
        if process.poll() is None:
            _terminate(process)
        with self._lock:
            if process in self._active:
                self._active.remove(process)
        cancelled = getattr(process, '_lb_cancelled', False)
        stdout = b''.join(buffers['out'])
        stderr = b''.join(buffers['err']).decode('utf-8', 'replace')
        return Result(argv=values, returncode=process.returncode if process.returncode is not None else -9,
                      stdout=stdout, stderr=stderr, duration=time.monotonic() - started,
                      timed_out=timed_out, cancelled=cancelled, overflow=overflow.is_set())

    def cancel(self):
        with self._lock:
            active = list(self._active)
        for process in active:
            process._lb_cancelled = True
            _terminate(process)

    def close(self):
        self.cancel()


def _drain(stream, key: str, buffers: dict, sizes: dict, cap: int, overflow: threading.Event):
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if sizes[key] + len(chunk) > cap:
                allowed = max(0, cap - sizes[key])
                if allowed:
                    buffers[key].append(chunk[:allowed])
                    sizes[key] += allowed
                overflow.set()
                break
            buffers[key].append(chunk)
            sizes[key] += len(chunk)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _terminate(process: subprocess.Popen):
    try:
        if os.name == 'posix':
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:  # pragma: no cover - Windows fallback
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == 'posix':
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


# rclone exit codes we care about; documented in `rclone help flags`.
RCLONE_CODES = {
    3: ('directory_not_found', '远端目录不存在。'),
    4: ('not_found', '远端文件不存在。'),
    5: ('transport_error', '传输暂时失败，可重试。'),
    6: ('transport_error', '传输出现错误。'),
    7: ('transport_error', '传输遇到致命错误。'),
    8: ('partial_read', '传输未完成全部字节。'),
    9: ('not_found', '没有匹配的文件。'),
    10: ('timeout', '传输超过时间限制。'),
}


def raise_for_result(result: Result, *, tool: str = 'rclone', secrets: Iterable[str] = ()):
    """Turn a finished process into the right error, with a redacted message."""
    from .security import redact
    if result.cancelled:
        raise CollectorError('cancelled', '操作已取消。')
    if result.timed_out:
        raise CollectorError('timeout', '传输超时，已终止子进程。', tool=tool)
    if result.overflow:
        raise CollectorError('output_limit', '子进程输出超过上限，已终止。', tool=tool)
    if result.ok:
        return
    code, message = RCLONE_CODES.get(result.returncode, ('transport_error', '传输组件返回错误。'))
    detail = redact(result.stderr.strip()[:400], secrets)
    raise CollectorError(code, message, tool=tool, returncode=result.returncode, detail=detail)


class WindowedReader:
    """Serve sequential byte ranges from one fetch per window.

    `fetch(offset, length) -> bytes` is whatever the protocol uses (one `rclone
    cat`, one HTTP Range request). `spawns` counts those calls, so a test can prove
    that reading 64 MiB in 4 MiB steps did not open sixteen connections.
    """

    def __init__(self, fetch: Callable[[int, int], bytes], *, window: int = 8 * 1024 * 1024,
                 size: Optional[int] = None):
        self.fetch = fetch
        self.window = max(64 * 1024, int(window))
        self.size = size
        self._start = 0
        self._buffer = b''
        self.spawns = 0
        self.bytes_fetched = 0

    def read(self, offset: int, length: int) -> bytes:
        if length < 0 or offset < 0:
            raise CollectorError('invalid_range', '读取范围无效。')
        if length > self.window:
            raise CollectorError('range_too_large', '单次窗口读取超过上限。')
        if self._start <= offset and offset + length <= self._start + len(self._buffer):
            begin = offset - self._start
            return self._buffer[begin:begin + length]
        want = self.window
        if self.size is not None:
            if offset >= self.size:
                return b''
            want = min(want, self.size - offset)
        data = self.fetch(offset, want)
        self.spawns += 1
        self.bytes_fetched += len(data)
        self._start = offset
        self._buffer = data
        return data[:length]

    def discard(self):
        self._buffer = b''
        self._start = 0
