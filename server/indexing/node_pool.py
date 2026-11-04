"""Resident Node parse pool (R6-035).

Python owns the file descriptors and the authorization; Node owns framing and
rules. The pool exists so that "parse a 1 GiB file" does not mean "start Node
per chunk" — the whole point is a fixed set of long-lived workers with bounded
messages between them.

Safety rules implemented here, not in the caller:

* the executable is fixed (`node`), `shell=False`, and arguments are a list;
* a handshake must report the expected protocol, otherwise the worker is killed;
* payloads are capped at 4 MiB before they are sent, and replies are capped when
  they are read, so one oversized line cannot exhaust the parent;
* a worker that stops answering is killed and replaced. The request fails with
  `worker_timeout` and a token the caller can use to resume from a checkpoint —
  the pool does not pretend the work succeeded.
* stderr is drained and truncated; it never joins stdout.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROTOCOL = 'logbench.node/1'
MAX_PAYLOAD = 4 * 1024 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024


class PoolError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class Worker:
    index: int
    process: subprocess.Popen
    ready: dict = field(default_factory=dict)
    inflight: Optional[str] = None
    replies: 'queue.Queue' = field(default_factory=queue.Queue)
    stderr_tail: list = field(default_factory=list)
    spawned_at: float = 0.0
    requests: int = 0
    jobs: set = field(default_factory=set)
    #: One request at a time per worker. The protocol correlates replies by order
    #: on a single stream, so two concurrent callers on one worker would each get
    #: the other's reply — which is how a parse result ends up attached to the
    #: wrong job. Concurrency comes from more workers, not from more callers here.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def alive(self) -> bool:
        return self.process.poll() is None


class NodePool:
    def __init__(self, entry: Path, *, workers: int = 2, heap_mb: int = 768,
                 node_bin: Optional[str] = None, handshake_timeout: float = 10.0,
                 request_timeout: float = 60.0):
        self.entry = Path(entry)
        self.workers_wanted = max(1, workers)
        self.heap_mb = heap_mb
        self.node_bin = node_bin or shutil.which('node') or ''
        self.handshake_timeout = handshake_timeout
        self.request_timeout = request_timeout
        self.workers: list[Worker] = []
        self.stats = {'spawned': 0, 'killed': 0, 'restarted': 0, 'requests': 0, 'payload_bytes': 0}
        self._lock = threading.RLock()

    # --------------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return bool(self.node_bin) and Path(self.node_bin).exists()

    def start(self) -> dict:
        if not self.available:
            raise PoolError('node_missing', '未找到 Node 可执行文件，索引任务无法启动。')
        if not self.entry.is_file():
            raise PoolError('entry_missing', '解析入口不存在：' + str(self.entry))
        with self._lock:
            while len(self.workers) < self.workers_wanted:
                self.workers.append(self._spawn(len(self.workers)))
            return {'workers': len(self.workers), 'protocol': PROTOCOL,
                    'runtime': self.workers[0].ready.get('runtime') if self.workers else None}

    def _spawn(self, index: int) -> Worker:
        if not self.available:
            raise PoolError('node_missing', '未找到 Node 可执行文件。')
        argv = [self.node_bin, '--max-old-space-size=%d' % self.heap_mb, str(self.entry)]
        process = subprocess.Popen(  # noqa: S603 - fixed executable, list argv, no shell
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(self.entry.parent), bufsize=1, text=True, encoding='utf-8', errors='replace')
        worker = Worker(index=index, process=process, spawned_at=time.time())
        threading.Thread(target=self._read_stdout, args=(worker,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(worker,), daemon=True).start()
        self.stats['spawned'] += 1
        try:
            ready = self._request_sync(worker, {'type': 'hello'}, timeout=self.handshake_timeout)
        except PoolError:
            self._kill(worker)
            raise
        if ready.get('type') != 'ready' or ready.get('protocol') != PROTOCOL:
            self._kill(worker)
            raise PoolError('protocol_mismatch', '解析进程协议不匹配。',
                            got=ready.get('protocol'), expected=PROTOCOL)
        worker.ready = ready
        return worker

    def _read_stdout(self, worker: Worker):
        stream = worker.process.stdout
        if stream is None:
            return
        for line in stream:
            if len(line) > MAX_REPLY_BYTES:
                worker.replies.put(PoolError('reply_too_large', '解析进程回包超过上限。'))
                continue
            line = line.strip()
            if not line:
                continue
            try:
                worker.replies.put(json.loads(line))
            except ValueError:
                worker.replies.put(PoolError('bad_reply', '解析进程回包不是 JSON。'))

    def _read_stderr(self, worker: Worker):
        stream = worker.process.stderr
        if stream is None:
            return
        for line in stream:
            # Keep a short tail for diagnostics; it never becomes protocol data and
            # never contains file content by construction.
            worker.stderr_tail.append(line.strip()[:200])
            del worker.stderr_tail[:-20]

    def _kill(self, worker: Worker):
        try:
            if worker.alive():
                worker.process.kill()
                worker.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        self.stats['killed'] += 1

    def _ensure_alive(self):
        """Replace dead workers before choosing one, so a killed child is not
        selected again just because it was idle."""
        for index, worker in enumerate(list(self.workers)):
            if not worker.alive():
                self._replace(worker, counted=True)

    def _request_sync(self, worker: Worker, message: dict, timeout: float) -> dict:
        if not worker.alive():
            raise PoolError('worker_dead', '解析进程已退出。', stderr=worker.stderr_tail[-5:])
        line = json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n'
        with worker.lock:
            try:
                worker.process.stdin.write(line)
                worker.process.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise PoolError('worker_dead', '无法写入解析进程：%s' % exc)
            try:
                reply = worker.replies.get(timeout=timeout)
            except queue.Empty:
                self._kill(worker)
                raise PoolError('worker_timeout', '解析进程超时未响应，已终止。',
                                job=message.get('job'), token=message.get('token'))
            if isinstance(reply, PoolError):
                raise reply
            # The reply now belongs to *this* request, because only one request
            # per worker can be in flight.
            if message.get('id') and reply.get('id') not in (None, message.get('id')):
                raise PoolError('reply_mismatch', '解析进程回包与请求不匹配。',
                                expected=message.get('id'), actual=reply.get('id'))
            return reply

    # ------------------------------------------------------------------------ calls
    def parse(self, job: str, token: int, data: bytes, *, eof: bool = False,
              profile: Optional[dict] = None, offset: int = 0,
              timeout: Optional[float] = None) -> dict:
        if len(data) > MAX_PAYLOAD:
            raise PoolError('payload_too_large', '单批数据超过 4 MiB。', size=len(data))
        message = {
            'type': 'parse', 'id': '%s:%d' % (job, offset), 'job': job, 'token': token,
            'data': _b64(data), 'eof': eof,
        }
        if profile is not None:
            message['profile'] = profile
        return self._dispatch(message, timeout or self.request_timeout)

    def sleep(self, ms: int, timeout: Optional[float] = None) -> dict:
        """Test hook: make a worker stop answering so the watchdog can be proven."""
        return self._dispatch({'type': 'sleep', 'id': 'sleep-%d' % ms, 'ms': ms},
                              timeout if timeout is not None else (ms / 1000.0) + 30)

    def query_open(self, spec: dict, *, key: str, timeout: Optional[float] = None) -> dict:
        """Normalise a query spec in the shared JS implementation.

        The server never re-implements `query.js`: the AST (with compiled regexes)
        stays in the worker and the parent only gets a token plus a serialisable
        summary. `key` pins every later call for this query to the same worker —
        the token is meaningless anywhere else.
        """
        return self._dispatch({'type': 'query-open', 'id': 'qo-%s' % key, 'job': key,
                               'spec': spec}, timeout or self.request_timeout)

    def query_eval(self, qid: str, records: list, *, key: str,
                   timeout: Optional[float] = None) -> dict:
        if len(records) > 10000:
            raise PoolError('batch_too_large', '查询批不超过 10000 条。', size=len(records))
        return self._dispatch({'type': 'query-eval', 'id': 'qe-%s' % qid, 'job': key,
                               'qid': qid, 'records': records},
                              timeout or self.request_timeout)

    def query_close(self, qid: str, *, key: str, timeout: Optional[float] = None) -> dict:
        return self._dispatch({'type': 'query-close', 'id': 'qc-%s' % qid, 'job': key,
                               'qid': qid}, timeout or 10.0)

    def apply(self, job: str, records: list, profile: Optional[dict] = None, *,
              timeout: Optional[float] = None) -> dict:
        """Re-run the rule layer over already-framed records.

        No framing and no source bytes: this is what makes a rule edit cost a
        projection rebuild instead of a re-parse, and the reply is capped so a
        caller cannot smuggle a huge batch through it.
        """
        if len(records) > 2000:
            raise PoolError('batch_too_large', '规则重放单批不超过 2000 条。', size=len(records))
        message = {'type': 'apply', 'id': '%s:apply' % job, 'job': job, 'records': records}
        if profile is not None:
            message['profile'] = profile
        return self._dispatch(message, timeout or self.request_timeout)

    def signatures(self, job: str, profile: Optional[dict] = None, *,
                   timeout: Optional[float] = None) -> dict:
        """Semantic rule signatures from the one implementation both sides share."""
        message = {'type': 'signature', 'id': '%s:signature' % job, 'job': job}
        if profile is not None:
            message['profile'] = profile
        return self._dispatch(message, timeout or self.request_timeout)

    def restore(self, job: str, checkpoint: Optional[dict] = None, *, start_ordinal: Optional[int] = None,
                start_position: int = 0, start_line: int = 0,
                profile: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        """Reset a job's framing state to a persisted checkpoint.

        A worker is a cache of streaming state, not the owner of it: after a
        restart (or a worker replacement) the state comes from the shard, so the
        run continues at the exact ordinal and byte offset it stopped at.
        """
        message = {
            'type': 'restore', 'id': '%s:restore' % job, 'job': job,
            'start_ordinal': int(start_ordinal if start_ordinal is not None
                                  else (checkpoint or {}).get('ordinal') or 0),
            'start_position': int(start_position or 0),
            'start_line': int(start_line or 0),
        }
        if checkpoint is not None:
            message['checkpoint'] = checkpoint
        if profile is not None:
            message['profile'] = profile
        return self._dispatch(message, timeout or self.request_timeout)

    def _dispatch(self, message: dict, timeout: float) -> dict:
        with self._lock:
            if not self.workers:
                self.start()
            self._ensure_alive()
            job = message.get('job')
            # A job holds streaming framer state, so it must stay on one worker.
            # Round-robin per request would restart the framer and reuse ordinals.
            pinned = [item for item in self.workers if job and job in item.jobs]
            candidates = pinned or self.workers
            worker = min(candidates, key=lambda item: (item.inflight is not None, item.requests))
            if not worker.alive():
                self._replace(worker)
                worker = self.workers[worker.index]
            worker.inflight = message.get('id')
            worker.requests += 1
            if job:
                worker.jobs.add(job)
        try:
            reply = self._request_sync(worker, message, timeout)
        except PoolError as exc:
            # A dead or hung worker is replaced immediately; the caller decides
            # whether to resume from a checkpoint.
            with self._lock:
                if not worker.alive():
                    self._replace(worker, counted=True)
            raise exc
        finally:
            worker.inflight = None
        self.stats['requests'] += 1
        self.stats['payload_bytes'] += len(message.get('data') or '')
        if reply.get('type') == 'error':
            raise PoolError(reply.get('code') or 'worker_error', reply.get('message') or '解析失败',
                            job=reply.get('job'), token=reply.get('token'))
        return reply

    def _replace(self, worker: Worker, counted: bool = False):
        if counted:
            self.stats['restarted'] += 1
        self._kill(worker)
        index = worker.index
        fresh = self._spawn(index)
        self.workers[index] = fresh

    def reset(self, job: str) -> None:
        for worker in self.workers:
            worker.jobs.discard(job)
            if worker.alive():
                try:
                    self._request_sync(worker, {'type': 'reset', 'job': job}, timeout=5)
                except PoolError:
                    continue

    def resize(self, workers: int) -> dict:
        """Change the parse pool size now, not on the next restart (R6-083).

        Growing spawns real workers. Shrinking stops only workers that are not
        answering a request: killing one mid-parse would lose the framer state for
        that job, and a budget edit is not a reason to drop a chunk of a file. When
        every worker is busy the pool reports `busy` and stays larger until the next
        resize; the caller shows that instead of claiming the limit took effect.
        """
        wanted = max(1, int(workers))
        spawned, stopped, busy = 0, 0, []
        with self._lock:
            self.workers_wanted = wanted
            while len(self.workers) < wanted:
                self.workers.append(self._spawn(len(self.workers)))
                spawned += 1
            while len(self.workers) > wanted:
                victim = None
                for worker in reversed(self.workers):
                    if worker.inflight is None:
                        victim = worker
                        break
                if victim is None:
                    busy = [worker.index for worker in self.workers]
                    break
                self._kill(victim)
                self.workers.remove(victim)
                stopped += 1
            for index, worker in enumerate(self.workers):
                worker.index = index
            return {'workers': len(self.workers), 'wanted': wanted,
                    'spawned': spawned, 'stopped': stopped, 'busy': busy}

    def close(self, drain: bool = True) -> dict:
        closed = []
        with self._lock:
            workers, self.workers = self.workers, []
        for worker in workers:
            try:
                if worker.alive() and drain:
                    worker.process.stdin.write(json.dumps({'type': 'shutdown'}) + '\n')
                    worker.process.stdin.flush()
                    ack = worker.replies.get(timeout=3)
                    closed.append({'worker': worker.index, 'ack': isinstance(ack, dict) and ack.get('type') == 'bye'})
            except (OSError, ValueError, queue.Empty):
                closed.append({'worker': worker.index, 'ack': False})
            finally:
                self._kill(worker)
        return {'closed': closed, 'workers': len(workers)}

    def snapshot(self) -> dict:
        return {
            'available': self.available,
            'protocol': PROTOCOL,
            'wanted': self.workers_wanted,
            'heap_mb': self.heap_mb,
            'workers': [{'index': worker.index, 'alive': worker.alive(),
                         'requests': worker.requests,
                         'inflight': worker.inflight is not None,
                         'runtime': worker.ready.get('runtime')} for worker in self.workers],
            'stats': dict(self.stats),
        }


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode('ascii')
