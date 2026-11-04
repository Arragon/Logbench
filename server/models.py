"""Shared control-plane entities (R6-032).

This module owns three things and nothing else:

* opaque identifiers — a file id must not be a path, so a caller cannot probe
  the library by guessing names;
* content signatures — a generation changes when the bytes changed, not when a
  timestamp did;
* the frozen task/query transition tables, mirrored from
  `contracts/r6/state-machine.json`. `tests/test_r6_migrations.py` asserts the
  mirror equals the JSON, so a contract edit that is not applied here fails the
  build instead of drifting.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

# Mirrors contracts/r6/state-machine.json. Do not add edges here first.
TASK_TRANSITIONS = {
    'queued': ['reading', 'failed', 'cancelled'],
    'reading': ['framing', 'uploading', 'failed', 'cancelled'],
    'framing': ['indexing', 'failed', 'cancelled'],
    'uploading': ['uploaded', 'paused', 'failed', 'cancelled'],
    'uploaded': ['indexing', 'failed', 'cancelled'],
    'indexing': ['indexed', 'failed', 'cancelled'],
    'paused': ['uploading', 'cancelled', 'failed'],
    'indexed': [],
    'failed': [],
    'cancelled': [],
}
TASK_TERMINAL = ('indexed', 'failed', 'cancelled')

QUERY_TRANSITIONS = {
    'queued': ['running', 'cancelled', 'failed'],
    'running': ['paused', 'partial', 'done', 'cancelled', 'failed'],
    'paused': ['running', 'cancelled', 'failed'],
    'partial': ['running', 'done', 'cancelled', 'failed'],
    'done': [],
    'cancelled': [],
    'failed': [],
}
QUERY_TERMINAL = ('done', 'cancelled', 'failed')

LAYERS = ('structure', 'rules', 'search')
JOB_KINDS = ('index', 'query', 'upload', 'collect')


class TransitionError(ValueError):
    def __init__(self, kind: str, current: str, target: str):
        super().__init__('非法状态迁移：%s %s → %s' % (kind, current, target))
        self.kind, self.current, self.target = kind, current, target


def transitions_for(kind: str) -> dict:
    if kind == 'task':
        return TASK_TRANSITIONS
    if kind == 'query':
        return QUERY_TRANSITIONS
    raise ValueError('未知状态机：' + kind)


def assert_transition(kind: str, current: str, target: str) -> None:
    table = transitions_for(kind)
    if current not in table:
        raise TransitionError(kind, current, target)
    if target not in table[current]:
        raise TransitionError(kind, current, target)


def is_terminal(kind: str, state: str) -> bool:
    return state in (TASK_TERMINAL if kind == 'task' else QUERY_TERMINAL)


def new_id(prefix: str) -> str:
    """Opaque, URL-safe, 128 bits. Never derived from a path."""
    return '%s_%s' % (prefix, secrets.token_hex(16))


def file_id_for(root_id: str, rel_path: str) -> str:
    """Stable id for a logical file. The path is an input, never the output."""
    digest = hashlib.sha256(('file:%s:%s' % (root_id, rel_path)).encode('utf-8')).hexdigest()
    return 'f_' + digest[:16]


def content_signature(size: int, mtime_ns: int, head: bytes, tail: bytes) -> str:
    """
    Generation identity: size + mtime + the first and last 64 KiB.

    A pure timestamp check misses `cp new old`. A pure size check misses an
    in-place rewrite of equal length. Sampling both ends catches the truncation
    and prepend cases cheaply; the full-content hash is the index job's business,
    not the identity check's.
    """
    digest = hashlib.sha256()
    digest.update(b'sig1\0')
    digest.update(str(size).encode('ascii') + b'\0')
    digest.update(str(mtime_ns).encode('ascii') + b'\0')
    digest.update(head or b'')
    digest.update(b'\0')
    digest.update(tail or b'')
    return digest.hexdigest()[:32]


@dataclass
class Job:
    id: str
    kind: str
    state: str
    principal: str
    priority: int = 0
    revision: int = 1
    payload: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    error: Optional[dict] = None
    resume_reason: Optional[str] = None
    created_at: str = ''
    updated_at: str = ''

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'kind': self.kind,
            'state': self.state,
            'principal': self.principal,
            'priority': self.priority,
            'revision': self.revision,
            'progress': self.progress,
            'error': self.error,
            'resume_reason': self.resume_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> 'Job':
        return cls(
            id=row['id'], kind=row['kind'], state=row['state'], principal=row['principal'],
            priority=row['priority'], revision=row['revision'],
            payload=json.loads(row['payload'] or '{}'),
            progress=json.loads(row['progress'] or '{}'),
            error=json.loads(row['error']) if row['error'] else None,
            resume_reason=row['resume_reason'],
            created_at=row['created_at'], updated_at=row['updated_at'],
        )


@dataclass
class Lease:
    job_id: str
    epoch: int
    owner: str
    fencing: int
    expires_at: float

    def to_dict(self) -> dict:
        return {'job_id': self.job_id, 'epoch': self.epoch, 'owner': self.owner,
                'fencing': self.fencing, 'expires_at': self.expires_at}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
