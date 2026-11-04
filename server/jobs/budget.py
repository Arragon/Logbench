"""Resource budget (R6-034).

The budget is a value object, not a runtime knob: `derive()` takes measured facts
(cgroup CPU limit, RSS, disk latency) and returns the limits that apply now. Tests
call it with plain numbers, so "the pool shrinks on a 4-CPU cgroup" is an
assertion rather than a hope.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

GiB = 1024 ** 3


@dataclass(frozen=True)
class Budget:
    parse_slots: int = 4
    query_slots: int = 2
    upload_slots: int = 4
    total_cpu: int = 8
    transfer_connections: int = 4
    per_host_connections: int = 2
    per_principal_uploads: int = 2
    per_principal_queries: int = 1
    history_slots: int = 2
    mem_soft_bytes: int = 16 * GiB
    hot_cache_bytes: int = 4 * GiB
    # Reserved for interactive work even when history/rebuild is queued.
    interactive_reserve: int = 1
    writer_slots: int = 4

    def validate(self) -> 'Budget':
        if self.parse_slots < 1 or self.query_slots < 1:
            raise ValueError('解析与查询槽位至少各 1 个。')
        if self.parse_slots + self.query_slots > self.total_cpu:
            raise ValueError('解析与查询槽位之和不能超过总 CPU 预算。')
        if self.history_slots > self.parse_slots:
            raise ValueError('历史补采槽位不能多于解析槽位。')
        if self.interactive_reserve < 0 or self.interactive_reserve > self.parse_slots:
            raise ValueError('交互保留槽位不合法。')
        if self.hot_cache_bytes > self.mem_soft_bytes:
            raise ValueError('热缓存不能超过内存软预算。')
        if self.per_host_connections > self.transfer_connections:
            raise ValueError('单主机连接数不能超过总连接数。')
        return self

    def to_dict(self) -> dict:
        return {
            'parse_slots': self.parse_slots, 'query_slots': self.query_slots,
            'upload_slots': self.upload_slots, 'total_cpu': self.total_cpu,
            'transfer_connections': self.transfer_connections,
            'per_host_connections': self.per_host_connections,
            'per_principal_uploads': self.per_principal_uploads,
            'per_principal_queries': self.per_principal_queries,
            'history_slots': self.history_slots,
            'mem_soft_bytes': self.mem_soft_bytes,
            'hot_cache_bytes': self.hot_cache_bytes,
            'interactive_reserve': self.interactive_reserve,
            'writer_slots': self.writer_slots,
        }


@dataclass
class Pressure:
    """Measured facts that may shrink the budget."""
    cpu_limit: Optional[int] = None          # cgroup quota or affinity count
    rss_bytes: Optional[int] = None
    disk_latency_ms: Optional[float] = None
    queue_depth: int = 0
    reason: str = ''


def derive(base: Budget, pressure: Pressure) -> tuple[Budget, list[str]]:
    """
    Shrink-only. A measurement can lower a limit, never raise it: growing because
    a single sample looked idle is how a machine ends up swapping at 3 a.m.
    """
    budget = base.validate()
    notes = []
    if pressure.cpu_limit:
        if pressure.cpu_limit < base.total_cpu:
            cpu = max(2, int(pressure.cpu_limit))
            parse = max(1, min(base.parse_slots, cpu - 1))
            query = max(1, min(base.query_slots, cpu - parse))
            budget = replace(budget, total_cpu=cpu, parse_slots=parse, query_slots=query)
            notes.append('cpu_limit=%d' % pressure.cpu_limit)
        if pressure.cpu_limit <= 2:
            budget = replace(budget, history_slots=1, writer_slots=2)
            notes.append('small_cgroup')
    if pressure.rss_bytes and pressure.rss_bytes > budget.mem_soft_bytes * 0.85:
        budget = replace(budget, history_slots=0,
                         hot_cache_bytes=budget.hot_cache_bytes // 2)
        notes.append('memory_high')
    if pressure.disk_latency_ms is not None and pressure.disk_latency_ms > 40:
        budget = replace(budget, writer_slots=max(1, min(budget.writer_slots, 2)))
        notes.append('disk_slow')
    if pressure.queue_depth > 64:
        notes.append('queue_backlog')
    budget.validate()
    return budget, notes


def slot_for(budget: Budget, kind: str) -> str:
    """Which pool a job kind draws from."""
    if kind in ('index', 'collect'):
        return 'parse'
    if kind == 'query':
        return 'query'
    if kind == 'upload':
        return 'upload'
    raise ValueError('未知任务类型：' + kind)


def total_cpu_of(budget: Budget) -> int:
    """The pools may not exceed this together; asserted by the scheduler tests."""
    return budget.parse_slots + budget.query_slots
