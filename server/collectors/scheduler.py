"""Persistent collection scheduling (R6-072).

One pass over one source, and the pieces it must not confuse:

* **discovery and activity are separate clocks.** A directory listing every 60 s and a
  tail check every 2 s are different costs; using one interval for both either hammers
  the remote or notices appends late.
* **only new bytes are read.** The pass starts at `contiguous_bytes`; the amount per
  run is bounded, so a 40 GiB backlog cannot monopolise a transfer slot.
* **a failed mapping is not an empty directory.** An adapter error marks the file, sets
  `retry_at` with jitter for retryable errors, and stops for a human on credential,
  host-key, permission or offline-root errors.
* **pause stops new work, not the archive.** No path here deletes archived bytes.

The scheduler does not own a process loop for tests: `run_source()` is callable
directly, and `start()` only adds a thread that calls `tick()`.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from ..models import content_signature
from . import base as adapters
from .archive import Archive, ArchiveError
from .base import CollectorError, error_class
from .generations import (Observed, Previous, apply_decision, decide, is_compressed,
                          previous_from_row)
from .sources import SourceService

HEAD = 64 * 1024
DEFAULTS = {
    'discovery_interval': 60,
    'active_interval': 2,
    'global_quota': 4,
    'host_quota': 2,
    'max_bytes_per_run': 8 * 1024 * 1024,
    'max_files_per_run': 50,
    'base_backoff': 5,
    'max_backoff': 300,
}


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Quota:
    """Global and per-host transfer slots, with counters the UI can read."""

    def __init__(self, total: int = 4, per_host: int = 2):
        self.total = max(1, int(total))
        self.per_host = max(1, int(per_host))
        self._lock = threading.Lock()
        self._active = 0
        self._hosts: dict = {}
        self.peak = 0

    def acquire(self, host: str, *, blocking: bool = False) -> bool:
        with self._lock:
            if self._active >= self.total:
                return False
            if self._hosts.get(host, 0) >= self.per_host:
                return False
            self._active += 1
            self._hosts[host] = self._hosts.get(host, 0) + 1
            self.peak = max(self.peak, self._active)
            return True

    def release(self, host: str) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            if self._hosts.get(host):
                self._hosts[host] -= 1

    def snapshot(self) -> dict:
        with self._lock:
            return {'active': self._active, 'total': self.total, 'perHost': self.per_host,
                    'hosts': dict(self._hosts), 'peak': self.peak}


def backoff(attempts: int, *, base: int = 5, cap: int = 300, rng=random.random) -> float:
    """Exponential with jitter, clamped. Never returns a fixed interval."""
    value = min(cap, base * (2 ** max(0, attempts - 1)))
    return round(value * (0.5 + rng() * 0.5), 2)


class CollectScheduler:
    def __init__(self, app, *, archive=None, indexer=None, clock=time.time,
                 rng=random.random, settings: Optional[dict] = None, adapters_factory=None):
        self.app = app
        self.store = app.store
        self.archive = archive or Archive(app)
        self.indexer = indexer
        self.clock = clock
        self.rng = rng
        self.settings = dict(DEFAULTS)
        self.settings.update(settings or {})
        self.quota = Quota(self.settings['global_quota'], self.settings['host_quota'])
        self.adapters_factory = adapters_factory
        self.sources = SourceService(app)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.tick_seconds: Optional[float] = None
        self.last_tick: Optional[dict] = None

    # ------------------------------------------------------------------ adapter
    def _adapter(self, source: dict):
        if self.adapters_factory is not None:
            return self.adapters_factory(source)
        return self.sources._adapter(source)

    # --------------------------------------------------------------------- run
    def run_source(self, source_id: str, *, job_id: Optional[str] = None,
                   max_bytes: Optional[int] = None, max_files: Optional[int] = None) -> dict:
        started = self.clock()
        report = {'sourceId': source_id, 'startedAt': _now(), 'mappings': [],
                  'files': 0, 'bytes': 0, 'readBytes': 0, 'errors': [], 'skipped': None,
                  'index': []}
        row = self._source_row(source_id)
        if row is None:
            raise CollectorError('unknown_source', '远程源不存在。')
        if not row['enabled']:
            report['skipped'] = 'disabled'
            return report
        if row['paused']:
            report['skipped'] = 'paused'
            return report

        lease = None
        if job_id:
            from ..jobs.repository import JobRepository
            repo = JobRepository(self.store)
            lease = repo.claim(job_id, 'collector:%d' % os.getpid(), ttl=600)
            for state in ('reading', 'framing', 'indexing'):
                try:
                    repo.set_state(job_id, state)
                except Exception:  # noqa: BLE001 - a resumed job may already be past it
                    pass

        adapter = self._adapter(row)
        budget = int(max_bytes or self.settings['max_bytes_per_run'])
        file_limit = int(max_files or self.settings['max_files_per_run'])
        host = self._host(row)
        try:
            for mapping in self.sources.mappings(source_id):
                if not mapping['enabled']:
                    continue
                if not self.quota.acquire(host):
                    report['errors'].append({'mapping': mapping['id'], 'code': 'quota_full'})
                    break
                try:
                    self.quota_budget = budget
                    report['mappings'].append(
                        self._run_mapping(row, mapping, adapter, lease,
                                          budget=budget, file_limit=file_limit,
                                          report=report))
                except CollectorError as exc:
                    report['errors'].append({'mapping': mapping['id'], **exc.to_dict()})
                    self._record_error(row['id'], mapping['id'], exc)
                finally:
                    self.quota.release(host)
        finally:
            adapter.close()
            if job_id:
                self._finish_job(job_id, report)
        report['duration'] = round(self.clock() - started, 3)
        report['quota'] = self.quota.snapshot()
        return report

    def _run_mapping(self, source, mapping, adapter, lease, *, budget, file_limit, report) -> dict:
        result = {'mapping': mapping['id'], 'listed': 0, 'archived': 0, 'bytes': 0,
                  'generations': []}
        listing = adapter.list(mapping['remoteDir'])
        entries = [entry for entry in listing.entries if not entry.is_dir]
        entries = _filter(entries, mapping['include'], mapping['exclude'])
        entries.sort(key=lambda entry: entry.path)
        result['listed'] = len(entries)
        result['truncated'] = listing.truncated
        for entry in entries[:file_limit]:
            if is_compressed(entry.name):
                # A compressed rotation is a sealed snapshot. Indexing inside it needs an
                # unpack budget that this release does not implement (R6-071 documents it).
                result['generations'].append({'path': entry.path, 'action': 'compressed-snapshot',
                                              'indexed': False})
                continue
            if report['bytes'] >= budget:
                result['budget_exhausted'] = True
                break
            try:
                detail = self._sync_file(source, mapping, adapter, entry, lease,
                                         budget=budget - report['bytes'])
            except CollectorError as exc:
                result['generations'].append({'path': entry.path, 'action': 'error',
                                              **exc.to_dict()})
                # Route the failure through the error policy so the file records a
                # retry time or a stop reason; a silent `continue` would leave it
                # looking idle.
                self._record_error(source['id'], mapping['id'], exc)
                continue
            # `bytes` is what was committed to the archive; `readBytes` also counts
            # the head sample used for the generation decision.
            report['bytes'] += detail.get('bytesCommitted', 0)
            report['readBytes'] = report.get('readBytes', 0) + detail.pop('bytesRead', 0)
            report['files'] += 1
            result['archived'] += detail.get('bytesCommitted', 0)
            result['generations'].append(detail)
            if detail.get('indexed') is not None:
                report['index'].append({'fileId': detail.get('fileId'),
                                        'generation': detail.get('generation'),
                                        'indexed': detail.get('indexed')})
        result['generations'].extend(
            self._mark_missing(source, mapping, {entry.path for entry in entries}))
        return result

    def _mark_missing(self, source: dict, mapping: dict, listed: set) -> list:
        """Files this mapping used to watch that the listing no longer shows.

        A vanished file is a state, not an error: the archive stays, the row says
        `disappeared`, and the reader can still page through what was collected. Not
        noticing at all would leave a dead source looking healthy.
        """
        with self.store.connect() as db:
            rows = db.execute('SELECT id,remote_path,file_id,generation,state FROM source_files '
                              'WHERE mapping_id=?', (mapping['id'],)).fetchall()
        gone = []
        for row in rows:
            if row['remote_path'] in listed:
                continue
            if row['state'] in ('disappeared', 'sealed'):
                continue
            with self.store.transaction() as db:
                db.execute('UPDATE source_files SET state=?, last_seen_at=? WHERE id=?',
                           ('disappeared', _now(), row['id']))
            gone.append({'path': row['remote_path'], 'action': 'disappeared',
                         'bytesRead': 0, 'bytesCommitted': 0, 'indexed': None,
                         'fileId': row['file_id'], 'generation': row['generation'],
                         'reason': 'not-in-listing'})
            self._publish('rotation', source['id'], row['file_id'], row['generation'],
                          {'action': 'disappeared', 'path': row['remote_path'],
                           'reason': 'not-in-listing'})
        return gone

    # ------------------------------------------------------------------- file
    def _sync_file(self, source, mapping, adapter, entry, lease, *, budget: int) -> dict:
        stat = adapter.stat(entry.path)
        plan = self.archive.plan(source=source, mapping=mapping, remote_path=entry.path,
                                 name=entry.name, generation=1)
        row = self._source_file_row(mapping['id'], entry.path)
        local_head = self._local_head(row) if row else b''
        previous = previous_from_row(row, self.archive.watermark(row['file_id'], row['generation'])
                                     if row and row['file_id'] else None,
                                     local_head) if row and row['file_id'] else None
        head = adapter.read_range(entry.path, offset=0,
                                  length=min(stat.size, HEAD),
                                  identity={'size': stat.size, **stat.identity}).data \
            if stat.size else b''
        observed = Observed(size=stat.size, mtime_ns=stat.mtime_ns, identity=stat.identity,
                            head=head, exists=True, name=entry.name)
        decision = decide(previous, observed,
                          rotation_candidate=bool(row and row['state'] == 'rotated'))
        generation = decision.generation
        applied = None
        if row is None:
            self.archive.register(plan, source_id=source['id'], mapping_id=mapping['id'],
                                  size=stat.size,
                                  evidence={'identity': stat.identity, 'mtimeNs': stat.mtime_ns})
            row = self._source_file_row(mapping['id'], entry.path)
        elif decision.action in ('new',):
            pass
        if decision.action in ('rewritten', 'truncated', 'replaced'):
            # A new generation gets its own path: the old generation's confirmed bytes
            # must stay on disk and readable.
            plan = self.archive.plan(source=source, mapping=mapping,
                                     remote_path=entry.path, name=entry.name,
                                     generation=generation)
            self.archive.register(plan, source_id=source['id'], mapping_id=mapping['id'],
                                  size=stat.size,
                                  evidence={'identity': stat.identity, 'mtimeNs': stat.mtime_ns})
            with self.store.transaction() as db:
                fresh = self._source_file_row(mapping['id'], entry.path)
                applied = apply_decision(db, fresh, decision, observed=observed,
                                         source_id=source['id'])
            row = self._source_file_row(mapping['id'], entry.path)
        elif decision.action in ('append', 'unchanged', 'new'):
            with self.store.transaction() as db:
                fresh = self._source_file_row(mapping['id'], entry.path)
                applied = apply_decision(db, fresh, decision, observed=observed,
                                         source_id=source['id'])
            row = self._source_file_row(mapping['id'], entry.path)
        elif decision.action == 'unknown':
            raise CollectorError('generation_unknown', '无法确认文件代次，已停止该文件。',
                                 action=decision.action, reason=decision.reason)
        elif decision.action in ('disappeared', 'rotated'):
            with self.store.transaction() as db:
                db.execute('UPDATE source_files SET state=?, last_seen_at=? WHERE id=?',
                           (decision.action, _now(), row['id']))
            # A rotation is a reader-visible event: the tail of the old file stops
            # growing and the new one starts at zero, so the view must re-anchor.
            self._publish('rotation', source['id'], row['file_id'], generation,
                          {'action': decision.action, 'path': entry.path,
                           'reason': decision.reason})
            return {'path': entry.path, 'action': decision.action, 'bytesRead': 0,
                    'bytesCommitted': 0, 'indexed': None}

        if applied and applied.get('gapState', 'none') != 'none':
            # A hole is stated, never smoothed over: the reader must show that the
            # archive is missing a range instead of rendering a silent join.
            self._publish('gap', source['id'], row['file_id'], generation,
                          {'state': applied['gapState'], 'archiveBytes': int(row['archived_bytes'] or 0),
                           'reason': decision.reason})

        if decision.action in ('rewritten', 'truncated', 'replaced'):
            # A new generation is a rotation from the reader's point of view: the old
            # generation stays readable, but the tail moved to a new one.
            self._publish('rotation', source['id'], row['file_id'], generation,
                          {'action': decision.action, 'reason': decision.reason,
                           'generation': generation})

        contiguous = int(row['contiguous_bytes'] or 0)
        want = max(0, min(budget, stat.size - contiguous))
        committed = 0
        while want > 0:
            length = min(want, int(adapter.limits['readWindow']), budget - committed)
            if length <= 0:
                break
            chunk = adapter.read_range(entry.path, offset=contiguous + committed,
                                       length=length,
                                       identity={'size': stat.size, **stat.identity})
            if not chunk.data:
                break
            result = self.archive.append(row['file_id'], generation,
                                         offset=contiguous + committed, data=chunk.data,
                                         lease=lease, writer='collector:' + source['id'])
            committed += result['bytes']
            want -= result['bytes']
            self._publish('archive_commit', source['id'], plan['file_id'], generation,
                          {'offset': contiguous + committed,
                           'committed': result.get('committed', False)})
        detail = {'path': entry.path, 'action': decision.action,
                  'generation': generation, 'bytesRead': len(head) + committed,
                  'bytesCommitted': committed, 'fileId': row['file_id'],
                  'indexed': None, 'reason': decision.reason}
        if committed and self.indexer is not None:
            try:
                sync = self.indexer.sync(row['file_id'], generation, row['rel_path'], lease=lease)
                detail['indexed'] = sync.get('state')
                detail['records'] = sync.get('records')
                # References only: the reader learns *where* new records live, never
                # their text. A provisional tail record is announced as a revision of
                # the same ordinal range so the client can update in place.
                self._publish('record_revision', source['id'], row['file_id'], generation,
                              {'fromBytes': contiguous,
                               'toBytes': contiguous + committed,
                               'newRecords': sync.get('newRecords', 0),
                               'provisional': True})
                self._publish('index_commit', source['id'], row['file_id'], generation,
                              {'state': sync.get('state'), 'mode': sync.get('mode'),
                               'records': sync.get('records'),
                               'parsedBytes': sync.get('parsed'),
                               'archiveBytes': sync.get('archive')})
            except Exception as exc:  # noqa: BLE001 - archive correctness outranks indexing
                detail['indexError'] = repr(exc)[:200]
                self._publish('index_commit', source['id'], row['file_id'], generation,
                              {'state': 'failed', 'error': detail['indexError']})
        return detail

    # ------------------------------------------------------------------ helpers
    def _source_row(self, source_id: str):
        with self.store.connect() as db:
            return db.execute('SELECT * FROM remote_sources WHERE id=?', (source_id,)).fetchone()

    def _source_file_row(self, mapping_id: str, remote_path: str):
        with self.store.connect() as db:
            return db.execute('SELECT * FROM source_files WHERE mapping_id=? AND remote_path=?',
                              (mapping_id, remote_path)).fetchone()

    def _local_head(self, row) -> bytes:
        if not row or not row['file_id']:
            return b''
        try:
            root = self.archive.root_path(row['root_id'])
            path = Path(root) / row['rel_path']
            with open(path, 'rb') as handle:
                return handle.read(HEAD)
        except (OSError, ArchiveError):
            return b''

    @staticmethod
    def _host(row) -> str:
        from .security import Endpoint
        try:
            return Endpoint.parse(row['endpoint']).host
        except Exception:  # noqa: BLE001
            return str(row['endpoint'])[:120]

    def _record_error(self, source_id: str, mapping_id: str, exc: CollectorError) -> None:
        klass = error_class(exc.code)
        attempts = 1
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM source_files WHERE mapping_id=? AND state=?',
                              (mapping_id, 'error')).fetchall()
            attempts = len(rows) + 1
            state = {'retry': 'retry', 'manual': 'stopped', 'file': 'error'}.get(klass, 'error')
            retry_at = self.clock() + backoff(attempts, base=self.settings['base_backoff'],
                                              cap=self.settings['max_backoff'],
                                              rng=self.rng) if klass == 'retry' else None
            db.execute('UPDATE source_files SET state=?, last_error=?, retry_at=? '
                       'WHERE mapping_id=? AND state<>?',
                       (state, json.dumps(exc.to_dict(), ensure_ascii=False), retry_at,
                        mapping_id, 'sealed'))
            self.store.audit(db, 'collector', 'collect.error', source_id, exc.to_dict())
        self._publish('source_error', source_id, None, None,
                      {'code': exc.code, 'mapping': mapping_id, 'retryAt': retry_at})

    def _finish_job(self, job_id: str, report: dict) -> None:
        from ..jobs.repository import JobRepository
        repo = JobRepository(self.store)
        lease = repo.lease(job_id)
        if lease is None:
            return
        code = report['errors'][0]['code'] if report['errors'] else None
        klass = error_class(code) if code else 'fatal'
        try:
            if code and klass != 'file':
                repo.finish(lease, 'failed', error={'code': code, 'message': '采集未完成'},
                            progress={'phase': 'failed', 'bytes': report['bytes']})
            else:
                repo.finish(lease, 'indexed', progress={'phase': 'indexed',
                                                        'bytes': report['bytes'],
                                                        'files': report['files']})
        except Exception:  # noqa: BLE001 - already terminal
            pass
        # The job panel follows this event instead of polling the job list for a
        # progress bar that has not moved.
        self._publish('task_progress', report['sourceId'], None, None,
                      {'jobId': job_id,
                       'state': 'failed' if (code and klass != 'file') else 'indexed',
                       'phase': 'failed' if (code and klass != 'file') else 'indexed',
                       'bytes': report['bytes'], 'files': report['files']})

    def _publish(self, kind: str, source_id: str, file_id, generation, payload: dict) -> None:
        broker = getattr(self.app, 'events', None)
        if broker is None:
            return
        broker.publish(kind=kind, payload=dict(payload, sourceId=source_id,
                                               fileId=file_id, generation=generation))

    # --------------------------------------------------------------------- tick
    def due_sources(self, *, limit: int = 8) -> list:
        """Enabled, unpaused sources whose next check is due."""
        rows = self.sources.list({'role': 'admin', 'ip': 'system', 'id': 'system'}, limit=200)
        due = []
        now = self.clock()
        for source in rows:
            if not source['enabled'] or source['paused']:
                continue
            policy = source.get('policy') or {}
            interval = int(policy.get('activeInterval') or self.settings['active_interval'])
            last = self._last_run(source['id'])
            if last is None or now - last >= interval:
                due.append(source['id'])
        return due[:limit]

    def _last_run(self, source_id: str) -> Optional[float]:
        with self.store.connect() as db:
            row = db.execute('SELECT MAX(last_seen_at) AS seen FROM source_files WHERE source_id=?',
                             (source_id,)).fetchone()
        if not row or not row['seen']:
            return None
        try:
            import datetime
            return datetime.datetime.strptime(row['seen'], '%Y-%m-%dT%H:%M:%SZ') \
                .replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return None

    def tick(self) -> dict:
        report = {'at': _now(), 'sources': [], 'quota': self.quota.snapshot()}
        for source_id in self.due_sources():
            try:
                report['sources'].append(self.run_source(source_id))
            except CollectorError as exc:
                report['sources'].append({'sourceId': source_id, 'error': exc.to_dict()})
        self.last_tick = report
        return report

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, interval: float = 1.0) -> dict:
        if self.running():
            return {'running': True}
        self._stop.clear()
        self.tick_seconds = float(interval)

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - a scheduler must survive a pass
                    self.last_tick = {'at': _now(), 'error': repr(exc)[:200]}

        self._thread = threading.Thread(target=loop, name='lb-collector', daemon=True)
        self._thread.start()
        return {'running': True}

    def stop(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=5)
        self._thread = None
        self.tick_seconds = None
        return {'running': False}


def _filter(entries, include, exclude):
    import fnmatch
    result = []
    for entry in entries:
        name = entry.name
        if include and not any(fnmatch.fnmatch(name, pattern) for pattern in include):
            continue
        if exclude and any(fnmatch.fnmatch(name, pattern) for pattern in exclude):
            continue
        result.append(entry)
    return result
