"""Runtime status, resource budget and diagnostics (R6-083).

This is the one page whose job is to stay honest about load, so the rules are:

* **no metric is computed from the form.** Pool sizes come from the Node pool's own
  worker list; queue lengths come from the jobs table; `first_page_ms` comes from
  measured create→page timings. When a value has no measurement, it is `null` and
  the page prints "未采样", not `0`.
* **a budget edit says what it changed.** Each field is labelled `live` (applied to
  the running process), `next_job` (read when the next job is admitted) or
  `next_restart` (a value the current process cannot change). Shrinking the parse
  pool stops only idle workers, so a busy pool reports `busy` instead of silently
  keeping the old size.
* **the audit trail records before and after.** A rejected edit changes nothing; an
  accepted edit writes one row with both values and the actor.
* **diagnostics are redacted by construction.** The export is a whitelist of
  counters and stage names: no paths, no log text, no query strings, no credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..jobs.budget import Budget, Pressure, derive, slot_for
from . import procmetrics

SCHEMA = 'logbench.runtime/1'
SETTING_KEY = 'runtime_budget'
QUEUE_STATES = ('queued', 'reading', 'framing', 'uploading', 'uploaded', 'indexing')
TERMINAL_FAILED = ('failed', 'cancelled')

#: What a changed field actually affects. This table is the reason the page can label
#: a field instead of implying that every value is live.
APPLIES = {
    'parse_slots': 'live', 'query_slots': 'live', 'total_cpu': 'live',
    'history_slots': 'next_job', 'interactive_reserve': 'next_job',
    'per_principal_queries': 'next_job', 'per_principal_uploads': 'live',
    'upload_slots': 'live', 'writer_slots': 'next_job',
    'transfer_connections': 'next_job', 'per_host_connections': 'next_job',
    'mem_soft_bytes': 'next_restart', 'hot_cache_bytes': 'next_restart',
}

STATUS = {'invalid_budget': 400, 'revision_conflict': 409, 'value_too_large': 413}


class RuntimeError_(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class RuntimeApi:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.started = time.time()
        self.base, self.revision = self._load()
        # The stored query budget takes effect for the first query, not for the first
        # budget edit: a restart must not silently ignore the saved value.
        queries = getattr(app, 'queries', None)
        if queries is not None and hasattr(queries, 'set_slots'):
            queries.set_slots(self.base.query_slots)

    # ------------------------------------------------------------------ storage
    def _load(self) -> tuple:
        stored, _ = self.store.get_setting(SETTING_KEY, None)
        if not isinstance(stored, dict) or not isinstance(stored.get('value'), dict):
            return Budget(), 0
        try:
            budget = _budget_from(stored['value'])
        except ValueError:
            # A corrupt stored budget must not stop the server from starting.
            return Budget(), int(stored.get('revision') or 0)
        return budget, int(stored.get('revision') or 0)

    def _save(self, budget: Budget, actor: dict, previous: dict) -> int:
        from ..app import compact, now
        revision = self.revision + 1
        payload = {'revision': revision, 'value': budget.to_dict(),
                   'updatedAt': now(), 'updatedBy': actor['id']}
        with self.store.transaction() as db:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                       'SET value=excluded.value', (SETTING_KEY, compact(payload)))
            self.store.audit(db, actor['id'], 'runtime.budget', SETTING_KEY,
                             {'before': previous, 'after': budget.to_dict(),
                              'revision': revision})
        self.revision = revision
        return revision

    # ------------------------------------------------------------------ routing
    def route(self, method: str, path: str, q, payload: dict, actor: dict, headers: dict):
        from ..app import fail
        try:
            return self._route(method, path, q, payload, actor, headers)
        except RuntimeError_ as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)

    def _route(self, method: str, path: str, q, payload: dict, actor: dict, headers: dict):
        from ..app import fail
        self.app.require_admin(actor)
        if path == '/api/v2/runtime':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '运行状态只支持读取。')
            return self.status(actor, probe=q('probe') or '')
        if path == '/api/v2/runtime/budget':
            if method in ('GET', 'HEAD'):
                return self.budget_view()
            if method in ('PUT', 'POST'):
                return self.update_budget(actor, payload)
            fail(405, 'METHOD_NOT_ALLOWED', '预算只支持读取或修改。')
        if path == '/api/v2/runtime/diagnostics':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '诊断导出只支持读取。')
            return self.diagnostics(actor, q)
        fail(404, 'NOT_FOUND', '运行接口不存在。')

    # ------------------------------------------------------------------- status
    def effective(self) -> tuple:
        pressure = Pressure(cpu_limit=_cpu_limit(), rss_bytes=_rss_bytes(),
                            queue_depth=self._queue_depth())
        return derive(self.base, pressure)

    def budget_view(self) -> dict:
        budget, notes = self.effective()
        return {
            'schema': SCHEMA,
            'revision': self.revision,
            'base': self.base.to_dict(),
            'effective': budget.to_dict(),
            'notes': notes,
            'derivation': ('预算只收缩不放大：cgroup/亲和性 CPU 少于总预算、RSS 超过软预算 85%、'
                           '磁盘延迟超过 40 ms 时自动降低，不因一次空闲采样而放宽。'),
            'applies': dict(APPLIES),
        }

    def status(self, actor: dict, probe: str = '') -> dict:
        from ..app import VERSION
        budget, notes = self.effective()
        jobs = self._jobs()
        counts = {'by_state': {}, 'by_kind': {}}
        for job in jobs:
            counts['by_state'][job['state']] = counts['by_state'].get(job['state'], 0) + 1
            counts['by_kind'][job['kind']] = counts['by_kind'].get(job['kind'], 0) + 1
        backlog = sum(count for job in jobs
                      if job['state'] in QUEUE_STATES for count in [1])
        runtime = {
            'schema': SCHEMA,
            'generatedAt': _stamp(),
            'uptimeSeconds': int(time.time() - self.started),
            'version': VERSION,
            'budget': self.budget_view(),
            'pools': self._pools(budget),
            'resources': self._resources(probe),
            'jobs': {'counts': counts, 'queue': jobs[:100], 'queued': backlog,
                     'truncated': len(jobs) > 100},
            'index': self._index(budget),
            'queries': self.app.queries.metrics_snapshot(),
            'capabilities': self._capabilities(),
            'notes': notes,
        }
        return runtime

    # -------------------------------------------------------------------- pools
    def _pools(self, budget: Budget) -> dict:
        pool = self.app.index_jobs.pool
        snapshot = pool.snapshot() if hasattr(pool, 'snapshot') else {}
        upload_store = getattr(getattr(self.app, 'uploads', None), 'store', None)
        transfers = self._transfers()
        return {
            'parse': {
                'workers': len(snapshot.get('workers') or []),
                'wanted': snapshot.get('wanted', pool.workers_wanted),
                'budget_slots': budget.parse_slots,
                'available': snapshot.get('available'),
                'heap_mb': snapshot.get('heap_mb'),
                'protocol': snapshot.get('protocol'),
                'stats': snapshot.get('stats') or {},
                'detail': snapshot.get('workers') or [],
            },
            'query': {
                'budget_slots': budget.query_slots,
                'gate': self.app.queries.gate.snapshot(),
                'executor_threads': 8 if self.app.queries.executor_pool is not None else 0,
                'mode': 'async' if self.app.queries.executor_pool is not None else 'inline',
            },
            'upload': {
                'budget_slots': budget.upload_slots,
                'max_active_global': getattr(upload_store, 'max_active_global', None),
                'max_active_per_principal': getattr(upload_store, 'max_active_per_principal', None),
                'open_sessions': self._upload_sessions(),
            },
            'transfer': transfers,
        }

    def _transfers(self) -> dict:
        collectors = getattr(self.app, 'collectors', None)
        scheduler = getattr(collectors, 'scheduler', None) if collectors is not None else None
        if scheduler is None or not hasattr(scheduler, 'snapshot'):
            return {'state': 'not_running', 'running': None, 'queued': None,
                    'reason': '采集调度器尚未启动（未配置远程源）。'}
        try:
            return dict(scheduler.snapshot())
        except Exception as exc:  # noqa: BLE001 - status must not fail because of it
            return {'state': 'error', 'reason': str(exc)[:200]}

    def _upload_sessions(self) -> Optional[int]:
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT COUNT(*) AS n FROM upload_sessions "
                                 "WHERE state NOT IN ('expired','cancelled','sealed')").fetchone()
            return int(row['n'])
        except Exception:  # noqa: BLE001 - an older schema simply has no sessions
            return None

    # ---------------------------------------------------------------- resources
    def _resources(self, probe: str = '') -> dict:
        rss = _rss_bytes()
        disk = _disk(self.app.config.state_dir)
        out = {
            'rss_bytes': rss,
            'rss_source': _rss_source(),
            'cpu_count': os.cpu_count(),
            'affinity': _cpu_limit(),
            'memory_bytes': _memory_bytes(),
            'mem_soft_bytes': self.effective()[0].mem_soft_bytes,
            'disk': disk,
        }
        if probe == 'disk':
            out['disk']['latency_ms'] = _disk_latency(self.app.config.state_dir)
        else:
            out['disk']['latency_ms'] = None
            out['disk']['latency_note'] = '未采样：加 ?probe=disk 做一次 4 KiB 写入测量。'
        return out

    def _index(self, budget: Budget) -> dict:
        with self.store.connect() as db:
            rows = db.execute('SELECT kind,state,COUNT(*) AS n FROM jobs GROUP BY kind,state').fetchall()
            failed = db.execute("SELECT id,error,updated_at FROM jobs WHERE state='failed' "
                                "ORDER BY updated_at DESC LIMIT 5").fetchall()
        by_kind: dict = {}
        for row in rows:
            by_kind.setdefault(row['kind'], {})[row['state']] = row['n']
        index_states = by_kind.get('index', {})
        backlog = sum(index_states.get(state, 0) for state in QUEUE_STATES)
        recent = []
        for row in failed:
            error = {}
            if row['error']:
                try:
                    error = json.loads(row['error'])
                except ValueError:
                    error = {'message': str(row['error'])[:200]}
            recent.append({'jobId': row['id'], 'code': error.get('code'),
                           'message': (error.get('message') or '')[:200],
                           'at': row['updated_at']})
        return {'backlog': backlog, 'by_state': index_states, 'recentFailures': recent,
                'history_slots': budget.history_slots, 'writer_slots': budget.writer_slots}

    def _capabilities(self) -> dict:
        from ..capabilities import derive_features, probe_cpu, probe_disk, probe_node, probe_sqlite
        directory = self.app.config.data_dir
        probes = {'sqlite': probe_sqlite(directory), 'disk': probe_disk(directory),
                  'node': probe_node(), 'cpu': probe_cpu()}
        features = derive_features(probes)
        return {
            'server_index': features['server_index'],
            'private_upload': features['private_upload'],
            'remote_collect': features['remote_collect'],
            'sqlite': {'version': probes['sqlite'].get('version'),
                       'fts5': probes['sqlite'].get('fts5'),
                       'journal': probes['sqlite'].get('journal_policy')},
            'node': {'state': probes['node'].get('state'),
                     'reason': probes['node'].get('reason'),
                     'version': probes['node'].get('version')},
            'publish': {'state': features.get('local_reading', {}).get('state', 'enabled')},
        }

    # --------------------------------------------------------------------- jobs
    def _jobs(self) -> list:
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200').fetchall()
            checkpoints = db.execute(
                'SELECT job_id,MAX(epoch) AS epoch FROM job_checkpoints GROUP BY job_id').fetchall()
        epochs = {row['job_id']: row['epoch'] for row in checkpoints}
        out = []
        for row in rows:
            payload = {}
            try:
                payload = json.loads(row['payload'] or '{}')
            except ValueError:
                payload = {}
            progress = {}
            try:
                progress = json.loads(row['progress'] or '{}')
            except ValueError:
                progress = {}
            error = None
            if row['error']:
                try:
                    error = json.loads(row['error'])
                except ValueError:
                    error = {'message': str(row['error'])[:200]}
            name = payload.get('rel_path') or payload.get('path') or payload.get('file_id')
            out.append({
                'id': row['id'], 'kind': row['kind'], 'state': row['state'],
                'priority': row['priority'],
                'source': _source_of(row['kind'], payload),
                'file': Path(str(name)).name if name else '—',
                'principal': row['principal'],
                'execution': 'browser' if row['kind'] == 'query' and row['state'] == 'queued'
                             else ('server' if row['kind'] in ('index', 'query', 'upload', 'collect')
                                   else 'server'),
                'progress': progress,
                'watermark': progress.get('watermark') or {},
                'error': error,
                'revision': row['revision'],
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
                'epoch': epochs.get(row['id']),
                'retryable': row['state'] in TERMINAL_FAILED,
                'pausable': row['state'] == 'uploading',
                'cancellable': row['state'] not in ('indexed', 'failed', 'cancelled'),
            })
        return out

    def _queue_depth(self) -> int:
        try:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE state IN (%s)" %
                    ','.join('?' * len(QUEUE_STATES)), QUEUE_STATES).fetchone()
            return int(row['n'])
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------- budget
    def update_budget(self, actor: dict, payload: dict) -> dict:
        from ..app import integer
        expected = integer(payload.get('expectedRevision'), '版本号')
        if expected != self.revision:
            raise RuntimeError_('revision_conflict', '预算已被其他管理员修改，请刷新后重试。',
                                expected=expected, current=self.revision)
        values = payload.get('value')
        if not isinstance(values, dict):
            raise RuntimeError_('invalid_budget', '预算需要 value 对象。')
        try:
            candidate = _budget_from({**self.base.to_dict(), **values}).validate()
        except (ValueError, TypeError) as exc:
            raise RuntimeError_('invalid_budget', str(exc))
        before = self.base.to_dict()
        revision = self._save(candidate, actor, before)
        self.base = candidate
        applied = self._apply(candidate)
        budget, notes = self.effective()
        return {'schema': SCHEMA, 'revision': revision, 'base': candidate.to_dict(),
                'effective': budget.to_dict(), 'applied': applied, 'notes': notes,
                'applies': dict(APPLIES)}

    def _apply(self, budget: Budget) -> dict:
        """Push the parts that can change in a running process. Report the rest."""
        report = {'parse': None, 'query': None, 'upload': None, 'deferred': []}
        pool = self.app.index_jobs.pool
        try:
            if pool.workers or pool.workers_wanted:
                # Only resize a pool that has been started; announcing a size to a
                # pool that has never spawned is not evidence that concurrency changed.
                report['parse'] = pool.resize(budget.parse_slots) if pool.workers else {
                    'workers': 0, 'wanted': budget.parse_slots, 'spawned': 0, 'stopped': 0,
                    'busy': [], 'note': '解析池尚未启动，新槽位在首个索引任务启动时生效。'}
                if not pool.workers:
                    pool.workers_wanted = budget.parse_slots
        except Exception as exc:  # noqa: BLE001 - report, do not pretend
            report['parse'] = {'error': str(exc)[:200]}
        try:
            report['query'] = self.app.queries.set_slots(budget.query_slots)
        except Exception as exc:  # noqa: BLE001
            report['query'] = {'error': str(exc)[:200]}
        store = getattr(getattr(self.app, 'uploads', None), 'store', None)
        if store is not None:
            store.max_active_global = budget.upload_slots
            store.max_active_per_principal = budget.per_principal_uploads
            report['upload'] = {'max_active_global': store.max_active_global,
                                'max_active_per_principal': store.max_active_per_principal}
        for field, mode in APPLIES.items():
            if mode == 'next_restart':
                report['deferred'].append({'field': field, 'applies': mode})
        return report

    # ------------------------------------------------------------ diagnostics
    def diagnostics(self, actor: dict, q) -> dict:
        from ..app import Response
        runtime = self.status(actor)
        report = {
            'schema': SCHEMA,
            'generatedAt': runtime['generatedAt'],
            'version': runtime['version'],
            'uptimeSeconds': runtime['uptimeSeconds'],
            'budget': {'revision': runtime['budget']['revision'],
                       'effective': runtime['budget']['effective'],
                       'notes': runtime['budget']['notes']},
            'pools': {name: {key: value for key, value in pool.items()
                             if key in ('workers', 'wanted', 'budget_slots', 'gate',
                                        'mode', 'open_sessions', 'max_active_global')}
                      for name, pool in runtime['pools'].items()},
            'resources': {'rss_bytes': runtime['resources']['rss_bytes'],
                          'cpu_count': runtime['resources']['cpu_count'],
                          'affinity': runtime['resources']['affinity'],
                          'disk': {key: runtime['resources']['disk'].get(key)
                                   for key in ('free_bytes', 'total_bytes', 'writable',
                                               'latency_ms')}},
            'jobs': {'counts': runtime['jobs']['counts'],
                     'queued': runtime['jobs']['queued']},
            'index': {'backlog': runtime['index']['backlog'],
                      'by_state': runtime['index']['by_state'],
                      'recentFailures': runtime['index']['recentFailures']},
            'queries': {key: runtime['queries'].get(key) for key in
                        ('created', 'cancelled', 'failed', 'first_page_p50_ms',
                         'first_page_p95_ms', 'first_page_samples', 'active', 'slots')},
            'capabilities': runtime['capabilities'],
            'redaction': ['不含日志原文', '不含文件路径与文件名', '不含完整搜索词', '不含凭据与环境变量'],
        }
        body = json.dumps(report, ensure_ascii=False, indent=1).encode('utf-8')
        import io
        return Response(headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Disposition': 'attachment; filename="logbench-diagnostics.json"',
        }, stream=io.BytesIO(body), length=len(body))


# --------------------------------------------------------------------- helpers
def _budget_from(values: dict) -> Budget:
    allowed = {field for field in Budget.__dataclass_fields__}
    clean = {}
    for field, value in (values or {}).items():
        if field not in allowed:
            raise ValueError('未知预算字段：%s' % field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError('预算字段 %s 必须是整数。' % field)
        clean[field] = value
    return Budget(**clean)


def _cpu_limit() -> int:
    """Effective CPU limit: affinity and the cgroup quota, whichever is smaller.

    On Linux `sched_getaffinity` reports a cpuset but not a CPU quota, so a
    quota-only container (`--cpus=2` on a 32-core host) would otherwise be told it
    owns 32 cores and schedule far too many jobs. Where neither probe answers,
    the logical core count is the last resort — the same behaviour as before.
    """
    limits = [value for value in (procmetrics.cpu_affinity_count(),
                                  procmetrics.cgroup_cpu_quota()) if value]
    return min(limits) if limits else (os.cpu_count() or 1)


def _rss_bytes() -> Optional[int]:
    """Peak RSS in bytes, or `None` when the platform cannot measure it."""
    return procmetrics.peak_rss_bytes()


def _rss_source() -> str:
    return procmetrics.rss_source()


def _memory_bytes() -> Optional[int]:
    return procmetrics.physical_memory_bytes()


def _disk(directory) -> dict:
    target = _usable_dir(directory)
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {'free_bytes': None, 'total_bytes': None, 'writable': False,
                'path': str(target), 'error': exc.__class__.__name__}
    return {'free_bytes': usage.free, 'total_bytes': usage.total,
            'writable': os.access(str(target), os.W_OK), 'path': str(target)}


def _disk_latency(directory) -> Optional[float]:
    """One 4 KiB write + fsync, measured. Bounded by the payload size, not by luck."""
    target = _usable_dir(directory)
    handle = None
    try:
        fd, path = tempfile.mkstemp(prefix='.latency-', dir=str(target))
        started = time.perf_counter()
        with os.fdopen(fd, 'wb') as handle:
            handle.write(b'\0' * 4096)
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = (time.perf_counter() - started) * 1000
        os.unlink(path)
        return round(elapsed, 2)
    except OSError:
        return None


def _usable_dir(directory) -> Path:
    candidate = Path(directory) if directory else Path(tempfile.gettempdir())
    for parent in [candidate] + list(candidate.parents):
        if parent.is_dir():
            return parent
    return Path(tempfile.gettempdir())


def _source_of(kind: str, payload: dict) -> str:
    if kind == 'collect':
        return payload.get('source_id') or 'remote'
    if kind == 'upload':
        return 'private'
    if kind == 'index':
        return payload.get('root_id') or 'legacy'
    if kind == 'query':
        return payload.get('file_id') or 'server'
    return kind


def _stamp() -> str:
    from ..app import now
    return now()
