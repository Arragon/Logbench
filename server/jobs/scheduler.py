"""Deterministic job scheduler (R6-034).

No threads and no sleeping: `tick(now)` returns the jobs that *would* be started,
and the caller (a worker runtime) acts on that list. That is what makes fairness
testable — the same queue and the same clock always produce the same schedule.

Policy, in order:

1. hard caps per pool derived from the budget;
2. one query slot per principal, so a single user's regex cannot occupy the pool;
3. a reserve for interactive work, so history/rebuild never fills every slot;
4. round-robin between principals within a priority class, so "first in" is not
   "always first".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .budget import Budget, slot_for

# Lower number = more urgent. The names are the ticket's vocabulary.
PRIORITY = {
    'interactive': 0,
    'current_file': 1,
    'realtime': 2,
    'history': 3,
    'rebuild': 4,
}
HISTORY_CLASSES = ('history', 'rebuild')


@dataclass
class Waiting:
    job_id: str
    kind: str
    principal: str
    priority: int = PRIORITY['realtime']
    reason: str = ''
    since: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        for label, value in PRIORITY.items():
            if value == self.priority:
                return label
        return 'unknown'


class Scheduler:
    def __init__(self, budget: Budget):
        self.budget = budget.validate()
        self.waiting: list[Waiting] = []
        self.running: dict[str, Waiting] = {}
        self.rotation: dict[str, int] = {}
        self.notes: list[str] = []
        self.paused_low: list[dict] = []

    # ------------------------------------------------------------------ submission
    def submit(self, job_id: str, kind: str, principal: str, priority: Optional[int] = None,
               now: float = 0.0, meta: Optional[dict] = None, reason: str = '') -> Waiting:
        slot_for(self.budget, kind)
        entry = Waiting(job_id=job_id, kind=kind, principal=principal,
                        priority=PRIORITY['realtime'] if priority is None else priority,
                        since=now, meta=meta or {}, reason=reason)
        if any(item.job_id == job_id for item in self.waiting) or job_id in self.running:
            raise ValueError('任务已在队列中：' + job_id)
        self.waiting.append(entry)
        return entry

    def running_count(self, slot: str) -> int:
        return sum(1 for job in self.running.values() if slot_for(self.budget, job.kind) == slot)

    def running_by_principal(self, principal: str, kind: str) -> int:
        return sum(1 for job in self.running.values()
                   if job.principal == principal and job.kind == kind)

    # ----------------------------------------------------------------------- tick
    def tick(self, now: float = 0.0, limit: Optional[int] = None) -> list[Waiting]:
        """Return jobs to start now, in the order they should start."""
        started: list[Waiting] = []
        cap = limit if limit is not None else self.budget.total_cpu
        for entry in self._ordered(now):
            if len(started) >= cap:
                break
            decision = self._admit(entry, started)
            if decision is not None:
                entry.reason = decision
                continue
            self.waiting.remove(entry)
            self.running[entry.job_id] = entry
            self.rotation[entry.principal] = self.rotation.get(entry.principal, 0) + 1
            started.append(entry)
        return started

    def _ordered(self, now: float) -> list[Waiting]:
        # Priority, then whoever has the fewest jobs running (the user with an
        # idle slot goes first), then a rotation index so a busy principal cannot
        # sit at the head forever, then arrival time for stability.
        def running_for(principal: str) -> int:
            return sum(1 for job in self.running.values() if job.principal == principal)

        return sorted(self.waiting, key=lambda item: (
            item.priority,
            running_for(item.principal),
            self.rotation.get(item.principal, 0),
            item.since,
            item.job_id,
        ))

    def _admit(self, entry: Waiting, starting: list[Waiting]) -> Optional[str]:
        slot = slot_for(self.budget, entry.kind)
        # Jobs admitted in this tick are already in `self.running`; counting them
        # again through `starting` was double-counting and made the second slot
        # look occupied.
        used = self.running_count(slot)

        if slot == 'parse':
            if used >= self.budget.parse_slots:
                return 'parse_slots_full'
            if entry.priority in (PRIORITY['history'], PRIORITY['rebuild']):
                history_used = sum(1 for job in self.running.values()
                                   if job.priority in (PRIORITY['history'], PRIORITY['rebuild']))
                budget_for_history = self.budget.history_slots
                if budget_for_history == 0:
                    return 'history_paused_memory'
                if history_used >= budget_for_history:
                    return 'history_slots_full'
                # Keep a slot free for interactive work.
                if used >= self.budget.parse_slots - self.budget.interactive_reserve:
                    return 'interactive_reserve'
        elif slot == 'query':
            if used >= self.budget.query_slots:
                return 'query_slots_full'
            if self.running_by_principal(entry.principal, 'query') >= \
                    self.budget.per_principal_queries:
                return 'principal_query_quota'
        elif slot == 'upload':
            if used >= self.budget.upload_slots:
                return 'upload_slots_full'

        if len(self.running) >= self.budget.total_cpu:
            return 'cpu_budget'
        return None

    # ---------------------------------------------------------------------- events
    def finish(self, job_id: str) -> Optional[Waiting]:
        return self.running.pop(job_id, None)

    def apply_budget(self, budget: Budget, notes: Iterable[str] = ()) -> list[dict]:
        """
        Shrink the budget and pause the lowest-priority work that no longer fits.
        The caller keeps checkpoints, so a paused job resumes where it stopped.
        """
        self.budget = budget.validate()
        self.notes = list(notes)
        paused = []
        while self.running_count('parse') > self.budget.parse_slots:
            victim = max(self.running.values(), key=lambda job: (job.priority, -job.since),
                         default=None)
            if victim is None or slot_for(self.budget, victim.kind) != 'parse':
                break
            self.running.pop(victim.job_id, None)
            victim.reason = 'budget_shrunk'
            self.waiting.append(victim)
            paused.append({'job_id': victim.job_id, 'reason': 'budget_shrunk',
                           'priority': victim.name})
        self.paused_low = paused
        return paused

    def snapshot(self) -> dict:
        return {
            'budget': self.budget.to_dict(),
            'running': [job.job_id for job in self.running.values()],
            'waiting': [{'job_id': job.job_id, 'priority': job.name, 'reason': job.reason or 'waiting'}
                        for job in self.waiting],
            'by_principal': dict(self.rotation),
            'notes': self.notes,
        }
