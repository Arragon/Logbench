"""Job persistence: create/claim/heartbeat/checkpoint/finish (R6-033).

Every state-changing call takes a `Lease`. The lease is checked **inside the
same transaction** as the write it guards, because the alternative — check at
task start, trust it later — is exactly how two workers write one generation.

Fencing is monotonic per job: claiming bumps `epoch` and `fencing`, and a worker
holding the old values is refused rather than silently overwriting.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

from ..models import (Job, Lease, assert_transition, dumps, is_terminal,
                      new_id, TransitionError)

SCHEMA_HINT = 'R6-032'


class LeaseError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class JobRepository:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ create
    def create(self, kind: str, principal: str, payload: dict, *, priority: int = 0,
               action_key: Optional[str] = None, state: str = 'queued') -> Job:
        """
        Idempotent by `action_key` (C9): a double-click returns the first job, it
        does not create a second scan.
        """
        with self.store.transaction() as db:
            if action_key:
                existing = db.execute('SELECT * FROM jobs WHERE action_key=?', (action_key,)).fetchone()
                if existing:
                    return Job.from_row(existing)
            job_id = new_id('j')
            stamp = _stamp()
            db.execute(
                'INSERT INTO jobs(id,kind,state,principal,priority,revision,action_key,payload,progress,'
                "error,resume_reason,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?,NULL,NULL,?,?)",
                (job_id, kind, state, principal, priority, action_key, dumps(payload),
                 dumps({'phase': state, 'watermark': {}}), stamp, stamp))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return Job.from_row(row)

    # --------------------------------------------------------------------- read
    def get(self, job_id: str) -> Optional[Job]:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list(self, principal: Optional[str] = None, states: Optional[Iterable[str]] = None,
             limit: int = 100) -> list[Job]:
        sql = 'SELECT * FROM jobs'
        clauses, params = [], []
        if principal:
            clauses.append('principal=?')
            params.append(principal)
        if states:
            states = list(states)
            clauses.append('state IN (%s)' % ','.join('?' * len(states)))
            params.extend(states)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC LIMIT ?'
        params.append(max(1, min(500, limit)))
        with self.store.connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()
        return [Job.from_row(row) for row in rows]

    def checkpoints(self, job_id: str, epoch: Optional[int] = None) -> dict:
        sql = 'SELECT epoch,stage,watermark,updated_at FROM job_checkpoints WHERE job_id=?'
        params: list[Any] = [job_id]
        if epoch is not None:
            sql += ' AND epoch=?'
            params.append(epoch)
        with self.store.connect() as db:
            rows = db.execute(sql + ' ORDER BY epoch DESC, stage', tuple(params)).fetchall()
        return {
            'epoch': rows[0]['epoch'] if rows else None,
            'stages': {row['stage']: json.loads(row['watermark']) for row in rows},
            'updated_at': rows[0]['updated_at'] if rows else None,
        }

    def lease(self, job_id: str) -> Optional[Lease]:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM job_leases WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            return None
        return Lease(job_id=row['job_id'], epoch=row['epoch'], owner=row['owner'],
                     fencing=row['fencing'], expires_at=row['expires_at'])

    # -------------------------------------------------------------------- claim
    def claim(self, job_id: str, owner: str, ttl: float = 30.0, now: Optional[float] = None) -> Lease:
        """
        Take the lease, or refuse. A live lease held by someone else is a
        `lease_held`; an expired one is taken over with a higher fencing token.
        """
        moment = time.time() if now is None else now
        with self.store.transaction() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise LeaseError('unknown_job', '任务不存在。', job_id=job_id)
            if is_terminal('task', job['state']):
                raise LeaseError('job_finished', '任务已结束，不能领取。', state=job['state'])
            current = db.execute('SELECT * FROM job_leases WHERE job_id=?', (job_id,)).fetchone()
            if current and current['expires_at'] > moment and current['owner'] != owner:
                raise LeaseError('lease_held', '任务已被其他进程持有。',
                                 owner=current['owner'], expires_at=current['expires_at'])
            epoch = (current['epoch'] + 1) if current else 1
            fencing = (current['fencing'] + 1) if current else 1
            expires = moment + ttl
            db.execute(
                'INSERT INTO job_leases(job_id,epoch,owner,fencing,expires_at,updated_at) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(job_id) DO UPDATE SET epoch=excluded.epoch,owner=excluded.owner,'
                'fencing=excluded.fencing,expires_at=excluded.expires_at,updated_at=excluded.updated_at',
                (job_id, epoch, owner, fencing, expires, _stamp()))
            db.execute('UPDATE jobs SET revision=revision+1, updated_at=? WHERE id=?', (_stamp(), job_id))
            return Lease(job_id=job_id, epoch=epoch, owner=owner, fencing=fencing, expires_at=expires)

    # ------------------------------------------------------------------ lease io
    def heartbeat(self, lease: Lease, ttl: float = 30.0, now: Optional[float] = None) -> Lease:
        moment = time.time() if now is None else now
        with self.store.transaction() as db:
            self._require_lease(db, lease, moment)
            expires = moment + ttl
            db.execute('UPDATE job_leases SET expires_at=?,updated_at=? WHERE job_id=?',
                       (expires, _stamp(), lease.job_id))
        return Lease(job_id=lease.job_id, epoch=lease.epoch, owner=lease.owner,
                     fencing=lease.fencing, expires_at=expires)

    def checkpoint(self, lease: Lease, stage: str, watermark: dict,
                   now: Optional[float] = None) -> dict:
        with self.store.transaction() as db:
            self._require_lease(db, lease, now)
            db.execute(
                'INSERT INTO job_checkpoints(job_id,epoch,stage,watermark,updated_at) VALUES(?,?,?,?,?) '
                'ON CONFLICT(job_id,epoch,stage) DO UPDATE SET watermark=excluded.watermark,'
                'updated_at=excluded.updated_at',
                (lease.job_id, lease.epoch, stage, dumps(watermark), _stamp()))
            db.execute('UPDATE jobs SET progress=?,updated_at=? WHERE id=?',
                       (dumps({'phase': stage, 'watermark': watermark}), _stamp(), lease.job_id))
        return {'job_id': lease.job_id, 'epoch': lease.epoch, 'stage': stage, 'watermark': watermark}

    def finish(self, lease: Lease, state: str, error: Optional[dict] = None,
               progress: Optional[dict] = None, now: Optional[float] = None) -> Job:
        with self.store.transaction() as db:
            row = self._require_lease(db, lease, now)
            try:
                assert_transition('task', row['state'], state)
            except TransitionError as exc:
                raise LeaseError('illegal_transition', str(exc),
                                 current=row['state'], target=state)
            db.execute(
                'UPDATE jobs SET state=?,error=?,progress=COALESCE(?,progress),'
                'revision=revision+1,updated_at=? WHERE id=?',
                (state, dumps(error) if error else None,
                 dumps(progress) if progress else None, _stamp(), lease.job_id))
            db.execute('DELETE FROM job_leases WHERE job_id=?', (lease.job_id,))
            updated = db.execute('SELECT * FROM jobs WHERE id=?', (lease.job_id,)).fetchone()
            return Job.from_row(updated)

    # ---------------------------------------------------------------- control
    def set_state(self, job_id: str, state: str, *, expected_revision: Optional[int] = None,
                  error: Optional[dict] = None, reason: Optional[str] = None) -> Job:
        """Control-plane transition (pause/resume/retry/cancel), no lease needed."""
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise LeaseError('unknown_job', '任务不存在。', job_id=job_id)
            if expected_revision is not None and row['revision'] != expected_revision:
                raise LeaseError('revision_conflict', '任务已被修改，请刷新后重试。',
                                 expected=expected_revision, current=row['revision'])
            try:
                assert_transition('task', row['state'], state)
            except TransitionError as exc:
                raise LeaseError('illegal_transition', str(exc),
                                 current=row['state'], target=state)
            db.execute('UPDATE jobs SET state=?,error=?,resume_reason=?,revision=revision+1,updated_at=? '
                       'WHERE id=?',
                       (state, dumps(error) if error else row['error'], reason, _stamp(), job_id))
            if is_terminal('task', state):
                db.execute('DELETE FROM job_leases WHERE job_id=?', (job_id,))
            updated = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return Job.from_row(updated)

    def reclaim_expired(self, now: Optional[float] = None) -> list[dict]:
        """
        A crashed worker leaves a lease behind. The job goes back to `paused` with
        a reason, never to `failed`: the work is resumable, and the checkpoint is
        still there.
        """
        moment = time.time() if now is None else now
        reclaimed = []
        with self.store.transaction() as db:
            rows = db.execute(
                'SELECT l.job_id,l.owner,l.fencing,l.expires_at,j.state FROM job_leases l '
                'JOIN jobs j ON j.id=l.job_id WHERE l.expires_at<=?', (moment,)).fetchall()
            for row in rows:
                if is_terminal('task', row['state']):
                    db.execute('DELETE FROM job_leases WHERE job_id=?', (row['job_id'],))
                    continue
                # Do not delete the row: the fencing counter lives in it. Deleting
                # would let the next claim start at fencing=1 again, so a worker that
                # was declared dead could still match the new lease (R6-070).
                db.execute('UPDATE job_leases SET expires_at=0,updated_at=? WHERE job_id=?',
                           (_stamp(), row['job_id']))
                db.execute(
                    'UPDATE jobs SET state=?,resume_reason=?,revision=revision+1,updated_at=? '
                    'WHERE id=? AND state NOT IN (\'indexed\',\'failed\',\'cancelled\')',
                    ('paused', 'lease_expired', _stamp(), row['job_id']))
                reclaimed.append({'job_id': row['job_id'], 'owner': row['owner'],
                                  'fencing': row['fencing'], 'reason': 'lease_expired'})
        return reclaimed

    # ------------------------------------------------------------------ helpers
    def _require_lease(self, db, lease: Lease, now: Optional[float] = None):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (lease.job_id,)).fetchone()
        if not row:
            raise LeaseError('unknown_job', '任务不存在。', job_id=lease.job_id)
        current = db.execute('SELECT * FROM job_leases WHERE job_id=?', (lease.job_id,)).fetchone()
        if not current:
            raise LeaseError('no_lease', '任务没有有效租约，写入被拒绝。', job_id=lease.job_id)
        if current['fencing'] != lease.fencing or current['epoch'] != lease.epoch:
            raise LeaseError('stale_fencing', '租约已被接管，本次写入被拒绝。',
                             given=lease.fencing, current=current['fencing'])
        if current['expires_at'] <= (time.time() if now is None else now):
            raise LeaseError('lease_expired', '租约已过期，请重新领取。', job_id=lease.job_id)
        return row


def _stamp() -> str:
    from ..app import now
    return now()
