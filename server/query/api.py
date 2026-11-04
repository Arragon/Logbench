"""Asynchronous query API (R6-042) and snapshot export (R6-049).

The request thread never scans. `POST /api/v2/queries` validates, freezes a
snapshot and returns 202; a bounded worker pool does the scan; the client polls,
pages through the snapshot, cancels, or continues a paused scan.

Shape decisions worth stating:

* the response body is a **superset of `QueryState`** — the frozen contract's
  required fields plus `signature`/`plan`/`order_final`, which the page also reads
  from the local engine. Both providers therefore hand the view layer the same
  object; see `qa/evidence/R6-042.md`.
* `count.relation` is derived from coverage, never from "we stopped early but it
  looked done": `done` ⇒ `exact`, `paused` ⇒ `at_least`, `cancelled` ⇒ `unknown`.
* paging is by `seq` in `query_hits`, so page 900 does not rescan pages 1–899 and
  a cursor cannot be used to reach another snapshot.
* export streams records that are re-authorized on the way out, and a capped
  export says so instead of quietly truncating.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from ..indexing.cache_versions import CacheError, CacheRegistry
from ..indexing.node_pool import PoolError
from ..indexing.shards import ShardError, ShardReader
from . import planner as planner_module
from .executor import Executor, state_of
from .snapshots import DEFAULT_TTL, QueryError, SnapshotStore

EXPORT_MAX_RECORDS = 10000
EXPORT_HARD_CAP = 100000
EXPORT_MAX_BYTES = 64 * 1024 * 1024


class SlotGate:
    """Bounded concurrency that can be resized while jobs are running (R6-083).

    A ThreadPoolExecutor cannot change its worker count, so the budget would be a
    number in a form and nothing else. Here the threads are just carriers: how many
    scans may run at once is decided by `slots`, and lowering it takes effect for the
    next admission instead of at the next restart.
    """

    def __init__(self, slots: int = 2):
        self._condition = threading.Condition()
        self._slots = max(1, int(slots))
        self._active = 0
        self._waiting = 0

    def set_slots(self, slots: int) -> None:
        with self._condition:
            self._slots = max(1, int(slots))
            self._condition.notify_all()

    def acquire(self) -> None:
        with self._condition:
            self._waiting += 1
            try:
                while self._active >= self._slots:
                    self._condition.wait(timeout=0.25)
            finally:
                self._waiting -= 1
            self._active += 1

    def release(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify()

    def snapshot(self) -> dict:
        with self._condition:
            return {'slots': self._slots, 'active': self._active, 'waiting': self._waiting}


class QueryService:
    def __init__(self, app, *, background: bool = True, workers: int = 2):
        self.app = app
        self.store = app.store
        self.registry = CacheRegistry(app.store)
        self.snapshots = SnapshotStore(app.store, app.secret)
        self.executor = Executor(app.store, app.index_jobs.pool, self._shard_for, self.snapshots)
        # Threads are carriers, not the limit: `gate` is the limit. Eight carriers let
        # a raised query budget take effect without rebuilding the pool.
        self.executor_pool = (ThreadPoolExecutor(max_workers=8, thread_name_prefix='lb-query')
                              if background else None)
        self.gate = SlotGate(workers)
        self.lock = threading.RLock()
        self.failures: list = []
        #: Measured counters, not estimates. `first_page_ms` is create → first page.
        self.metrics = {'created': 0, 'cancelled': 0, 'resumed': 0, 'failed': 0,
                        'first_page_samples': 0, 'first_page_ms': [],
                        'exact_results': 0, 'partial_results': 0}
        self._opened_at: dict = {}

    def set_slots(self, slots: int) -> dict:
        self.gate.set_slots(slots)
        return self.gate.snapshot()

    def _record(self, name: str, value: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + value

    def _record_first_page(self, query_id: str) -> None:
        opened = self._opened_at.pop(query_id, None)
        if opened is None:
            return
        elapsed = max(0, int((time.time() - opened) * 1000))
        samples = self.metrics['first_page_ms']
        samples.append(elapsed)
        # Bounded sample window: a metrics list that grows per query is a leak.
        if len(samples) > 200:
            del samples[:-200]
        self._record('first_page_samples')


    # ------------------------------------------------------------------ helpers
    def close(self) -> dict:
        if self.executor_pool is not None:
            self.executor_pool.shutdown(wait=False)
        return {'closed': True}

    def _shard_for(self, file_id: str, generation: int) -> Path:
        pointer = self.registry.pointer(file_id, 'structure')
        shard = (pointer or {}).get('stats', {}).get('shard')
        if not shard or not Path(shard).is_file():
            raise QueryError('index_pending', '该文件尚未建立索引，无法查询。')
        return Path(shard)

    def _authorize(self, actor: dict, file_id: str) -> dict:
        """Every query request re-checks access; ACL changes take effect at once."""
        try:
            return self.registry.read(file_id, 'raw', owner=actor['id'])
        except CacheError:
            raise QueryError('not_found', '文件不存在。')

    def _acl_version(self, actor: dict, file_id: str) -> int:
        version = self._authorize(actor, file_id)
        return int(version.get('generation') or 1)

    # ------------------------------------------------------------------- create
    def create(self, actor: dict, body: dict) -> dict:
        from ..app import fail, integer, text
        file_id = text(body.get('file_id'), '文件标识', 64)
        access = self._authorize(actor, file_id)
        generation = body.get('generation')
        if generation is None:
            generation = int(access.get('generation') or 1)
        generation = int(integer(generation, '代次', 0, 1 << 30))
        spec = _normalise_spec(body)
        limit = integer(body.get('limit', 200), '单页数量', 1, 2000)
        budget = integer(body.get('budget_ms', 1500), '预算', 1, 60000)
        spec['query_id'] = 'pending'
        spec['dataset_id'] = file_id

        try:
            opened = self.executor.pool.query_open(
                {'text': body.get('text', ''), 'terms': body.get('terms'),
                 'joiner': body.get('joiner'), 'flags': body.get('flags') or {},
                 'levels': body.get('levels'), 'time_range': body.get('time_range'),
                 'projection_version': body.get('projection_version'),
                 'match_all': bool(body.get('match_all'))},
                key='validate')
        except PoolError as exc:
            # The query language lives in JS; its rejection is the API's rejection.
            raise QueryError(exc.code, exc.message)
        self.executor.pool.query_close(opened['qid'], key='validate')
        signature = opened['signature']
        plan_spec = dict(spec, terms=opened['summary']['terms'])
        plan = planner_module.plan_query(
            self._shard_for(file_id, generation), plan_spec,
            postings_version=self._search_version(file_id),
            postings_complete=self._search_complete(file_id), total_records=0)
        snapshot = self.snapshots.create(
            principal=actor['id'], file_id=file_id, generation=generation, signature=signature,
            spec=dict(spec, limit=limit, budget_ms=budget),
            plan=plan.report(), versions={'index_version': generation},
            acl_version=self._acl_version(actor, file_id))
        # The raw spec is what reopens the query; the JS summary is only for planning.
        self.snapshots.update(snapshot['query_id'],
                              payload=dict(snapshot['payload'], terms=opened['summary']['terms']))
        self.executor.open_token(snapshot['query_id'], dict(spec, limit=limit, budget_ms=budget))
        self._opened_at[snapshot['query_id']] = time.time()
        self._record('created')
        self._submit(snapshot['query_id'], actor['id'])
        row = self.snapshots.get(snapshot['query_id'], actor['id'])
        return {'item': state_of(row), 'accepted': True}

    def _search_version(self, file_id: str) -> Optional[str]:
        pointer = self.registry.pointer(file_id, 'search')
        return pointer['version_id'] if pointer else None

    def _search_complete(self, file_id: str) -> bool:
        # A published search layer went through coverage verification in R6-038, so
        # its presence is the proof that every record contributed postings.
        return self.registry.pointer(file_id, 'search') is not None

    def _submit(self, query_id: str, principal: str, *, budget_ms: Optional[int] = None,
                start_ordinal: int = -1) -> None:
        def work():
            self.gate.acquire()
            try:
                row = self.snapshots.get(query_id, principal)
                if row['state'] in ('cancelled', 'done', 'failed'):
                    # It was cancelled while queued: a slot must not be spent scanning.
                    return
                try:
                    self.executor.run(query_id, principal=principal, budget_ms=budget_ms,
                                      start_ordinal=start_ordinal)
                except QueryError as exc:
                    self._fail(query_id, principal, exc.code, exc.message)
                except (ShardError, OSError) as exc:
                    self._fail(query_id, principal, getattr(exc, 'code', 'io_error'), str(exc))
                except Exception as exc:  # noqa: BLE001 - a query must not stay `running`
                    self._fail(query_id, principal, 'internal', repr(exc))
            finally:
                self.gate.release()

        if self.executor_pool is not None:
            self.executor_pool.submit(work)
        else:
            work()

    def _fail(self, query_id: str, principal: str, code: str, message: str) -> None:
        self._record('failed')
        self.failures.append({'query': query_id, 'code': code, 'message': message})
        try:
            row = self.snapshots.get(query_id, principal)
            payload = row['payload']
            payload['error'] = {'code': code, 'message': message[:300]}
            payload['resume_token'] = None
            coverage = payload.get('coverage') or {}
            coverage['complete'] = False
            payload['coverage'] = coverage
            self.snapshots.update(query_id, state='failed', payload=payload)
        except QueryError:
            pass

    # --------------------------------------------------------------------- read
    def get(self, actor: dict, query_id: str) -> dict:
        row = self.snapshots.get(query_id, actor['id'])
        self._authorize(actor, row['file_id'])
        return {'item': state_of(row)}

    def records(self, actor: dict, query_id: str, q) -> dict:
        from ..app import fail, integer
        row = self.snapshots.get(query_id, actor['id'])
        self._authorize(actor, row['file_id'])
        limit = integer(q('limit', '200'), '单页数量', 1, 2000)
        page = self.snapshots.page(query_id, cursor=q('cursor', '') or None, limit=limit,
                                   principal=actor['id'],
                                   acl_version=int(row['payload'].get('acl_version') or 0))
        if not q('cursor'):
            self._record_first_page(query_id)
            relation = (page.get('count') or {}).get('relation')
            self._record('exact_results' if relation == 'exact' else 'partial_results')
        shard = self._shard_for(row['file_id'], int(row['generation']))
        reader = ShardReader(shard)
        headers = [_header(record) for record in reader.records_for(
            [item['ordinal'] for item in page['items']])]
        return {'items': headers, 'next_cursor': page['next_cursor'], 'count': page['count'],
                'state': page['state'], 'coverage': page['coverage'],
                'query_id': query_id}

    def cancel(self, actor: dict, query_id: str) -> dict:
        row = self.snapshots.get(query_id, actor['id'])
        self._authorize(actor, row['file_id'])
        with self.lock:
            self.executor.cancel(query_id, principal=actor['id'])
        self._record('cancelled')
        return {'item': state_of(self.snapshots.get(query_id, actor['id']))}

    def resume(self, actor: dict, query_id: str, body: dict) -> dict:
        from ..app import integer
        row = self.snapshots.get(query_id, actor['id'])
        self._authorize(actor, row['file_id'])
        if row['state'] in ('done', 'cancelled'):
            raise QueryError('job_finished', '查询已结束，无需继续。')
        payload = row['payload']
        token = body.get('resume_token') or payload.get('resume_token')
        start_ordinal = int(payload.get('last_ordinal', -1))
        if token:
            claims = self.snapshots.read_resume(token, row, actor['id'])
            start_ordinal = int(claims['ordinal'])
        budget = integer(body.get('budget_ms', payload.get('budget_ms') or 1500), '预算', 1, 60000)
        self.snapshots.update(query_id, state='running',
                              payload=dict(payload, resume_token=None))
        self._record('resumed')
        self._submit(query_id, actor['id'], budget_ms=budget, start_ordinal=start_ordinal)
        return {'item': state_of(self.snapshots.get(query_id, actor['id'])), 'accepted': True}

    # ------------------------------------------------------------------- export
    def export(self, actor: dict, query_id: str, q):
        from ..app import Response, fail, integer, text
        row = self.snapshots.get(query_id, actor['id'])
        self._authorize(actor, row['file_id'])
        fmt = (q('format', 'ndjson') or 'ndjson').lower()
        if fmt not in ('ndjson', 'csv'):
            fail(400, 'INVALID_FORMAT', '导出只支持 ndjson 或 csv。')
        limit = integer(q('max', str(EXPORT_MAX_RECORDS)), '导出行数', 1, EXPORT_HARD_CAP)
        shard = self._shard_for(row['file_id'], int(row['generation']))
        reader = ShardReader(shard)

        spool = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        written = 0
        size = 0
        truncated = False
        ordinals = self._hit_ordinals(query_id, limit)
        for chunk_start in range(0, len(ordinals), 500):
            if written >= limit or size >= EXPORT_MAX_BYTES:
                truncated = True
                break
            chunk = ordinals[chunk_start:chunk_start + 500]
            records = reader.records_for(chunk)
            payload = _render(fmt, records)
            spool.write(payload)
            size += len(payload)
            written += len(records)
        total_hits = self.snapshots.page(query_id, cursor=None, limit=1, principal=actor['id'],
                                         acl_version=int(row['payload'].get('acl_version') or 0))['total_hits']
        truncated = truncated or written < total_hits
        spool.seek(0)
        headers = {
            'Content-Type': 'application/x-ndjson; charset=utf-8' if fmt == 'ndjson'
            else 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename="%s.%s"' % (query_id, fmt),
            'X-LogBench-Export-Rows': str(written),
            'X-LogBench-Export-Truncated': 'true' if truncated else 'false',
            'X-LogBench-Export-Total': str(total_hits),
        }
        return Response(headers=headers, stream=spool, length=size or spool.tell())

    def _hit_ordinals(self, query_id: str, limit: int) -> list:
        with self.store.connect() as db:
            rows = db.execute('SELECT ordinal FROM query_hits WHERE snapshot_id=? ORDER BY seq LIMIT ?',
                              (query_id, limit)).fetchall()
        return [row['ordinal'] for row in rows]

    # ------------------------------------------------------------------ metrics
    def metrics_snapshot(self) -> dict:
        """Measured query counters for the runtime page (R6-083).

        `first_page_p50/p95` are over the last ≤200 queries. Nothing here is an
        estimate: an empty sample window reports `null`, not zero.
        """
        samples = sorted(self.metrics['first_page_ms'])
        def percentile(ratio):
            if not samples:
                return None
            index = min(len(samples) - 1, max(0, int(round(ratio * (len(samples) - 1)))))
            return samples[index]
        with self.store.connect() as db:
            rows = db.execute('SELECT state,COUNT(*) AS n FROM query_snapshots GROUP BY state').fetchall()
        active = sum(row['n'] for row in rows if row['state'] in ('queued', 'running', 'paused', 'partial'))
        return {
            'created': self.metrics['created'],
            'cancelled': self.metrics['cancelled'],
            'resumed': self.metrics['resumed'],
            'failed': self.metrics['failed'],
            'first_page_samples': self.metrics['first_page_samples'],
            'first_page_p50_ms': percentile(0.5),
            'first_page_p95_ms': percentile(0.95),
            'exact_results': self.metrics['exact_results'],
            'partial_results': self.metrics['partial_results'],
            'active': active,
            'by_state': {row['state']: row['n'] for row in rows},
            'slots': self.gate.snapshot(),
            'result_cache': {'state': 'not_implemented',
                             'reason': '查询结果缓存（B-029）尚未实现：每次查询都会新建快照重扫。'},
        }

    # -------------------------------------------------------------------- route
    def route(self, method: str, path: str, q, payload: dict, actor: dict):
        from ..app import fail
        if path == '/api/v2/queries':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '查询只支持创建（POST）。')
            return self.create(actor, payload)
        rest = path[len('/api/v2/queries/'):] if path.startswith('/api/v2/queries/') else ''
        if not rest:
            fail(404, 'NOT_FOUND', '查询接口不存在。')
        parts = [part for part in rest.split('/') if part]
        query_id = parts[0]
        tail = parts[1] if len(parts) > 1 else ''
        if not tail:
            if method in ('GET', 'HEAD'):
                return self.get(actor, query_id)
            if method == 'DELETE':
                return self.cancel(actor, query_id)
            fail(405, 'METHOD_NOT_ALLOWED', '查询只支持读取或取消。')
        if tail == 'records':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '结果只支持读取。')
            return self.records(actor, query_id, q)
        if tail == 'continue':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '继续只支持 POST。')
            return self.resume(actor, query_id, payload)
        if tail == 'export':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '导出只支持读取。')
            return self.export(actor, query_id, q)
        fail(404, 'NOT_FOUND', '查询接口不存在。')


def _normalise_spec(body: dict) -> dict:
    from ..app import fail, text
    spec = {
        'text': text(body.get('text', ''), '查询文本', 4096, empty=True),
        'terms': body.get('terms') if isinstance(body.get('terms'), list) else None,
        'joiner': 'or' if body.get('joiner') == 'or' else 'and',
        'flags': body.get('flags') if isinstance(body.get('flags'), dict) else {},
        'levels': body.get('levels') if isinstance(body.get('levels'), list) else None,
        'time_range': body.get('time_range') if isinstance(body.get('time_range'), dict) else None,
        'projection_version': 'v2' if body.get('projection_version') == 'v2' else 'v1',
        # "Everything in this range": a time scope or level filter with no text. It is
        # an explicit flag so an empty search string stays an error.
        'match_all': body.get('match_all') is True,
    }
    if spec['levels'] is not None:
        if len(spec['levels']) > 32 or any(not isinstance(item, str) for item in spec['levels']):
            fail(400, 'INVALID_INPUT', 'levels 必须是不超过 32 个字符串。')
    if spec['time_range'] is not None:
        for key in ('from_ns', 'to_ns'):
            if key not in spec['time_range']:
                fail(400, 'INVALID_INPUT', 'time_range 需要 from_ns 与 to_ns。')
            try:
                int(str(spec['time_range'][key]))
            except (TypeError, ValueError):
                fail(400, 'INVALID_INPUT', 'time_range 的边界必须是十进制字符串。')
        if int(spec['time_range']['from_ns']) > int(spec['time_range']['to_ns']):
            fail(400, 'INVALID_RANGE', '时间范围起点不能晚于终点。')
    return spec


def _header(record: dict) -> dict:
    return {
        'record_id': record['record_id'],
        'dataset_id': record['record_id'].split(':', 1)[0],
        'ordinal': record['ordinal'],
        'version': record['version'],
        'ts': record['ts'],
        'ts_ns': record['ts_ns'],
        'level': record['level'],
        'logger': record['logger'],
        'fn': record['fn'],
        'src': record['src'],
        'src_line': record['src_line'],
        'lines': record['lines'],
        'bytes': record['bytes'],
        'has_trace': record['has_trace'],
        'message': record['message'],
        'provisional': False,
    }


def _render(fmt: str, records: list) -> bytes:
    if fmt == 'csv':
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator='\n')
        writer.writerow(['ordinal', 'ts', 'level', 'logger', 'fn', 'src', 'line', 'message'])
        for record in records:
            writer.writerow([record['ordinal'], record['ts'] or '', record['level'] or '',
                             record['logger'] or '', record['fn'] or '', record['src'] or '',
                             record['lines']['first'], record['message']])
        return buffer.getvalue().encode('utf-8')
    lines = []
    for record in records:
        lines.append(json.dumps({
            'ordinal': record['ordinal'], 'record_id': record['record_id'],
            'ts': record['ts'], 'ts_ns': record['ts_ns'], 'level': record['level'],
            'logger': record['logger'], 'fn': record['fn'], 'src': record['src'],
            'lines': record['lines'], 'bytes': record['bytes'], 'message': record['message'],
            'body': record.get('body') or [],
        }, ensure_ascii=False))
    return ('\n'.join(lines) + ('\n' if lines else '')).encode('utf-8')
