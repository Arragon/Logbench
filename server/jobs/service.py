"""Job control policy: authorization, idempotent actions, audit (R6-033).

Repository does SQL and fencing. This layer decides *who may* do what, and turns
the frozen state table into explicit answers instead of exceptions leaking to
HTTP.

Two deliberate refusals:

* **pause** is only allowed from `uploading`, because that is the only edge the
  frozen task machine has into `paused`. For a running index job the API says
  `pause_not_supported_in_state` and points at cancel+retry. Inventing an edge
  here would make the contract describe a system that does not exist.
* **retry** of a terminal job creates a *new* job. Terminal states have no
  outgoing edges, so mutating the old row would violate the same table.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..models import Job, TransitionError, assert_transition, dumps, is_terminal, new_id
from .repository import JobRepository, LeaseError

PAUSABLE = ('uploading',)
ACTIONS = ('pause', 'resume', 'cancel', 'retry')


class JobService:
    def __init__(self, store):
        self.store = store
        self.repo = JobRepository(store)

    # ------------------------------------------------------------- authorization
    @staticmethod
    def may_control(actor: dict, job: Job) -> bool:
        return actor.get('role') == 'admin' or job.principal == actor.get('id')

    def _authorised(self, actor: dict, job_id: str) -> Job:
        job = self.repo.get(job_id)
        if not job:
            raise LeaseError('unknown_job', '任务不存在。', job_id=job_id)
        if not self.may_control(actor, job):
            # Same shape as "missing" on purpose: an outsider must not learn that
            # the id exists by getting a different error.
            raise LeaseError('unknown_job', '任务不存在。', job_id=job_id)
        return job

    # --------------------------------------------------------------------- queries
    def list(self, actor: dict, *, states: Optional[list[str]] = None, limit: int = 100) -> list[dict]:
        principal = None if actor.get('role') == 'admin' else actor.get('id')
        return [job.to_dict() for job in self.repo.list(principal, states, limit)]

    def get(self, actor: dict, job_id: str, *, include_payload: bool = False) -> dict:
        job = self._authorised(actor, job_id)
        value = job.to_dict()
        if include_payload:
            value['payload'] = job.payload
            value['checkpoints'] = self.repo.checkpoints(job_id)
        lease = self.repo.lease(job_id)
        value['lease'] = lease.to_dict() if lease else None
        return value

    # --------------------------------------------------------------------- actions
    def action(self, actor: dict, job_id: str, action: str, payload: dict) -> dict:
        if action not in ACTIONS:
            raise LeaseError('unknown_action', '不支持的任务动作：' + str(action))
        action_id = payload.get('actionId') or payload.get('action_id')
        if not isinstance(action_id, str) or not action_id.strip():
            raise LeaseError('missing_action_id', '控制动作必须携带 actionId 以便幂等。')
        action_id = action_id.strip()[:128]

        cached = self._cached_action(action_id)
        if cached is not None:
            # Same action id → same answer, no second transition (C9).
            return cached

        job = self._authorised(actor, job_id)
        expected = payload.get('expectedRevision')
        if expected is not None and int(expected) != job.revision:
            raise LeaseError('revision_conflict', '任务已被修改，请刷新后重试。',
                             expected=int(expected), current=job.revision)

        if action == 'cancel':
            result = self._cancel(actor, job)
        elif action == 'pause':
            result = self._pause(actor, job)
        elif action == 'resume':
            result = self._resume(actor, job)
        else:
            result = self._retry(actor, job)

        self._remember_action(action_id, job.id, action, result)
        return result

    # ------------------------------------------------------------------- internals
    def _cancel(self, actor: dict, job: Job) -> dict:
        if is_terminal('task', job.state):
            # Cancelling a finished job is a no-op, not a conflict: the user's
            # intent ("it should not run") is already true.
            return {'job': job.to_dict(), 'cancelled': False, 'reason': 'already_terminal'}
        updated = self.repo.set_state(job.id, 'cancelled', reason='user_cancelled')
        self._audit(actor, 'job.cancel', job.id, {'from': job.state, 'reason': 'user_cancelled'})
        # C8: cancelling keeps the raw bytes and any committed checkpoint.
        return {'job': updated.to_dict(), 'cancelled': True, 'raw_kept': True}

    def _pause(self, actor: dict, job: Job) -> dict:
        if job.state == 'paused':
            return {'job': job.to_dict(), 'paused': True, 'reason': 'already_paused'}
        if job.state not in PAUSABLE:
            raise LeaseError(
                'pause_not_supported_in_state',
                '该阶段不支持暂停（契约只允许上传阶段进入 paused）；请取消后用相同参数重试。',
                state=job.state, pausable=list(PAUSABLE))
        updated = self.repo.set_state(job.id, 'paused', reason='user_paused')
        self._audit(actor, 'job.pause', job.id, {'from': job.state})
        return {'job': updated.to_dict(), 'paused': True}

    def _resume(self, actor: dict, job: Job) -> dict:
        if job.state != 'paused':
            raise LeaseError('illegal_transition', '只有 paused 任务可以继续。', state=job.state)
        # paused → uploading is the only resume edge in the frozen table.
        updated = self.repo.set_state(job.id, 'uploading', reason=None)
        self._audit(actor, 'job.resume', job.id, {'to': 'uploading'})
        return {'job': updated.to_dict(), 'resumed': True}

    def _retry(self, actor: dict, job: Job) -> dict:
        if job.state not in ('failed', 'cancelled'):
            raise LeaseError('illegal_transition', '只有失败或已取消的任务可以重试。', state=job.state)
        clone = self.repo.create(job.kind, actor.get('id') or job.principal, job.payload,
                                 priority=job.priority)
        self._audit(actor, 'job.retry', job.id, {'new_job': clone.id})
        return {'job': clone.to_dict(), 'retried_from': job.id}

    def _cached_action(self, action_id: str):
        with self.store.connect() as db:
            row = db.execute('SELECT result FROM job_actions WHERE action_id=?', (action_id,)).fetchone()
        return json.loads(row['result']) if row else None

    def _remember_action(self, action_id: str, job_id: str, action: str, result: dict) -> None:
        with self.store.transaction() as db:
            db.execute(
                'INSERT OR IGNORE INTO job_actions(action_id,job_id,action,result,created_at) '
                'VALUES(?,?,?,?,?)',
                (action_id, job_id, action, dumps(result), _stamp()))

    def _audit(self, actor: dict, action: str, target: str, detail: dict) -> None:
        # Detail never contains log text: an audit trail is not a content store.
        with self.store.transaction() as db:
            self.store.audit(db, actor.get('id') or actor.get('ip') or 'unknown', action, target, detail)


def _stamp() -> str:
    from ..app import now
    return now()
