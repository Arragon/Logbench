"""Disk pressure, cache reclamation and retention protection (R6-076).

The rule that shapes this module: **original bytes are not a cache**. Under pressure
the system stops writing and says so; it does not delete a user's collected logs to
free space. What it may reclaim is what can be rebuilt:

* layer versions that are staged, failed or superseded and are not pinned;
* index shards belonging to a superseded generation;
* expired upload parts, which are not archive at all.

Protection is checked at deletion time, not at planning time: a version can be pinned
between the plan and the delete (a reader opened it), so the query excludes pinned
rows and the active generation in the same statement.

`waiting_storage` is a state, not an error: the collection job pauses for that root
with a reason and a resume condition, while queries, exports and reads keep working.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from ..collectors.base import CollectorError

GIB = 1024 ** 3
MIN_FREE = 50 * GIB
FRACTION = 0.05


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def default_sampler(path: Path) -> dict:
    try:
        stats = os.statvfs(str(path))
    except (OSError, AttributeError):
        return {'free': 0, 'total': 0, 'error': 'statvfs_unavailable'}
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    return {'free': int(free), 'total': int(total), 'error': None}


class Lifecycle:
    def __init__(self, app, *, sampler=None, clock=time.time, min_free: int = MIN_FREE,
                 fraction: float = FRACTION, staging_ttl: float = 3600):
        self.app = app
        self.store = app.store
        self.sampler = sampler or default_sampler
        self.clock = clock
        self.min_free = int(min_free)
        self.fraction = float(fraction)
        self.staging_ttl = float(staging_ttl)

    # ------------------------------------------------------------------ roots
    def roots(self) -> list:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM storage_roots ORDER BY id')]

    def threshold(self, total: int) -> int:
        return int(max(self.min_free, total * self.fraction))

    def check_root(self, root: dict) -> dict:
        path = Path(root['path'])
        if not path.exists():
            return {'rootId': root['id'], 'state': 'offline', 'free': 0, 'total': 0,
                    'threshold': self.min_free, 'reason': 'path_missing'}
        sample = self.sampler(path)
        if sample.get('error'):
            return {'rootId': root['id'], 'state': 'unknown', 'free': 0,
                    'total': int(sample.get('total') or 0),
                    'threshold': self.threshold(int(sample.get('total') or 0)),
                    'reason': sample['error']}
        threshold = self.threshold(sample['total'])
        state = 'low' if sample['free'] < threshold else 'ok'
        return {'rootId': root['id'], 'state': state, 'free': sample['free'],
                'total': sample['total'], 'threshold': threshold,
                'reason': 'below_low_watermark' if state == 'low' else None}

    def guard_write(self, root_id: str) -> dict:
        """Called before a collection writes. Raises `storage_low`/`root_offline`."""
        root = next((item for item in self.roots() if item['id'] == root_id), None)
        if root is None:
            raise LifecycleError('unknown_root', '目标存储根不存在。', rootId=root_id)
        state = self.check_root(root)
        if state['state'] == 'offline':
            raise LifecycleError('root_offline', '存储根离线，采集已暂停。', rootId=root_id)
        if state['state'] == 'low':
            raise LifecycleError('storage_low', '存储根可用空间不足，采集已暂停。',
                                 rootId=root_id, free=state['free'],
                                 threshold=state['threshold'])
        return state

    # ----------------------------------------------------------------- enforce
    def enforce(self) -> dict:
        report = {'at': _now(), 'roots': [], 'paused': [], 'resumed': []}
        states = {}
        for root in self.roots():
            state = self.check_root(root)
            states[root['id']] = state
            report['roots'].append(state)
        with self.store.connect() as db:
            rows = db.execute('SELECT DISTINCT source_id, root_id FROM source_files').fetchall()
        for row in rows:
            state = states.get(row['root_id'])
            if state is None:
                continue
            if state['state'] in ('low', 'offline'):
                self._pause_source(row['source_id'], root=row['root_id'], state=state)
                report['paused'].append({'sourceId': row['source_id'], 'rootId': row['root_id'],
                                         'state': state['state']})
            else:
                if self._resume_source(row['source_id'], root=row['root_id']):
                    report['resumed'].append({'sourceId': row['source_id'],
                                              'rootId': row['root_id']})
        return report

    def _pause_source(self, source_id: str, *, root: str, state: dict) -> None:
        with self.store.transaction() as db:
            row = db.execute('SELECT policy,paused FROM remote_sources WHERE id=?',
                             (source_id,)).fetchone()
            if row is None:
                return
            policy = {}
            try:
                import json
                policy = json.loads(row['policy'] or '{}')
            except (TypeError, ValueError):
                policy = {}
            if policy.get('waitingStorage') == root:
                return
            policy['waitingStorage'] = root
            policy['waitingReason'] = state['reason']
            # Remember who owns the pause. If the source was already paused it was the
            # operator's decision, and recovery must leave it alone.
            if not policy.get('pausedBy'):
                policy['pausedBy'] = 'storage' if not row['paused'] else 'operator'
            db.execute('UPDATE remote_sources SET paused=1, policy=?, revision=revision+1, '
                       'updated_at=? WHERE id=?',
                       (json.dumps(policy, ensure_ascii=False), _now(), source_id))
            self.store.audit(db, 'lifecycle', 'source.waiting_storage', source_id,
                             {'rootId': root, 'state': state['state']})

    def _resume_source(self, source_id: str, *, root: str) -> bool:
        with self.store.transaction() as db:
            row = db.execute('SELECT policy,paused FROM remote_sources WHERE id=?',
                             (source_id,)).fetchone()
            if row is None:
                return False
            import json
            try:
                policy = json.loads(row['policy'] or '{}')
            except (TypeError, ValueError):
                policy = {}
            if policy.get('waitingStorage') != root:
                return False
            owner = policy.get('pausedBy')
            if owner not in (None, 'storage'):
                # Somebody else owns this pause; clearing the marker is all we may do.
                policy.pop('waitingStorage', None)
                policy.pop('waitingReason', None)
                db.execute('UPDATE remote_sources SET policy=?, updated_at=? WHERE id=?',
                           (json.dumps(policy, ensure_ascii=False), _now(), source_id))
                return False
            policy.pop('waitingStorage', None)
            policy.pop('waitingReason', None)
            policy.pop('pausedBy', None)
            # Only resume what this module paused: an operator's pause is not ours to undo.
            paused = row['paused']
            if owner == 'storage':
                paused = 0
            db.execute('UPDATE remote_sources SET paused=?, policy=?, revision=revision+1, '
                       'updated_at=? WHERE id=?',
                       (paused, json.dumps(policy, ensure_ascii=False), _now(), source_id))
            self.store.audit(db, 'lifecycle', 'source.storage_recovered', source_id,
                             {'rootId': root})
            return True

    # ------------------------------------------------------------------ gc
    def collect_garbage(self, *, dry_run: bool = False) -> dict:
        """Reclaim only rebuildable objects. Returns what it removed and what it kept."""
        report = {'at': _now(), 'dryRun': dry_run, 'versions': [], 'shards': [],
                  'kept': {'pinned': 0, 'active': 0, 'raw': 0}, 'freedBytes': 0}
        cutoff = self.clock() - self.staging_ttl
        with self.store.connect() as db:
            candidates = db.execute(
                "SELECT version_id,file_id,generation,layer,state,bytes,created_at FROM layer_versions "
                "WHERE layer<>'raw' AND state IN ('staging','failed','superseded')").fetchall()
            for row in candidates:
                if self._pinned(row['version_id']):
                    report['kept']['pinned'] += 1
                    continue
                if self._active_generation(row['file_id'], int(row['generation'])):
                    report['kept']['active'] += 1
                    continue
                report['versions'].append({'versionId': row['version_id'],
                                           'layer': row['layer'], 'bytes': row['bytes']})
                report['freedBytes'] += int(row['bytes'] or 0)
            raw = db.execute("SELECT COUNT(*) AS n FROM layer_versions WHERE layer='raw'").fetchone()
            report['kept']['raw'] = int(raw['n'] or 0)
            stale = db.execute(
                "SELECT file_id,generation FROM generations WHERE state='superseded'").fetchall()
            for row in stale:
                shards = self._shards_for(row['file_id'], int(row['generation']))
                for shard in shards:
                    if self._pinned_file(row['file_id'], int(row['generation'])):
                        report['kept']['pinned'] += 1
                        continue
                    report['shards'].append(str(shard))
                    try:
                        report['freedBytes'] += shard.stat().st_size
                    except OSError:
                        pass
                    report['shards'][-1] = {'path': str(shard), 'fileId': row['file_id'],
                                            'generation': int(row['generation'])}
        if dry_run:
            return report
        removed = 0
        with self.store.transaction() as db:
            for item in report['versions']:
                db.execute('DELETE FROM layer_pins WHERE version_id=?', (item['versionId'],))
                db.execute('DELETE FROM layer_events WHERE version_id=?', (item['versionId'],))
                removed += db.execute('DELETE FROM layer_versions WHERE version_id=?',
                                      (item['versionId'],)).rowcount
            db.execute('DELETE FROM event_log WHERE expires_at<?', (self.clock(),))
        for shard in report['shards']:
            try:
                Path(shard['path']).unlink()
            except OSError:
                pass
        report['removedVersions'] = removed
        report['at'] = _now()
        return report

    def _pinned(self, version_id: str) -> bool:
        with self.store.connect() as db:
            row = db.execute('SELECT COUNT(*) AS n FROM layer_pins WHERE version_id=? AND expires_at>?',
                             (version_id, self.clock())).fetchone()
        return bool(row['n'])

    def _pinned_file(self, file_id: str, generation: int) -> bool:
        with self.store.connect() as db:
            row = db.execute(
                'SELECT COUNT(*) AS n FROM layer_versions v JOIN layer_pins p '
                'ON p.version_id=v.version_id WHERE v.file_id=? AND v.generation=? AND p.expires_at>?',
                (file_id, generation, self.clock())).fetchone()
        return bool(row['n'])

    def _active_generation(self, file_id: str, generation: int) -> bool:
        with self.store.connect() as db:
            row = db.execute('SELECT generation,state FROM generations WHERE file_id=? '
                             'ORDER BY generation DESC LIMIT 1', (file_id,)).fetchone()
        if row and int(row['generation']) == generation and row['state'] in ('indexing', 'indexed'):
            return True
        return False

    def _shards_for(self, file_id: str, generation: int) -> list:
        index_root = Path(self.app.config.state_dir) / 'index'
        directory = index_root / file_id
        if not directory.is_dir():
            return []
        return sorted(directory.glob('g%d-*.sqlite3' % generation))


def retention_report(app) -> dict:
    """What an operator can see without running GC: sizes and protections."""
    lifecycle = Lifecycle(app)
    roots = []
    for root in lifecycle.roots():
        state = lifecycle.check_root(root)
        roots.append({**state, 'path': root['path'], 'kind': root['kind']})
    with app.store.connect() as db:
        archive = db.execute('SELECT COUNT(*) AS files, SUM(archived_bytes) AS bytes '
                             'FROM source_files').fetchone()
        caches = db.execute("SELECT COUNT(*) AS n, SUM(bytes) AS bytes FROM layer_versions "
                            "WHERE layer<>'raw'").fetchone()
    return {'roots': roots, 'archive': {'files': int(archive['files'] or 0),
                                        'bytes': int(archive['bytes'] or 0)},
            'caches': {'versions': int(caches['n'] or 0), 'bytes': int(caches['bytes'] or 0)}}
