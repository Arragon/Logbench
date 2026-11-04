"""Ordered commit queue for parallel batches (R6-039).

Mirror of `src/shared/ordered-reducer.js`. Two implementations exist because the
page and the server are in different languages; `tests/test_r6_parallel_pipeline.py`
drives both over the same schedules and compares the commit traces, so a drift
between them is a test failure rather than a mystery in production.

Semantics, identical on both sides:

* `seq < next_seq`           -> `stale` (already committed; never re-committed)
* `seq` already buffered     -> `duplicate`
* `token` older than current -> `old_token`
* `seq == next_seq`          -> commit it, then drain contiguous successors
* otherwise                  -> buffer, bounded by `max_buffer`
"""
from __future__ import annotations

from typing import Optional

OLD_TOKEN = 'old_token'
STALE = 'stale'
DUPLICATE = 'duplicate'


class ReducerOverflow(RuntimeError):
    def __init__(self, seq: int, size: int):
        super().__init__('有序缓冲已满（%d 批），seq=%d 落后过多。' % (size, seq))
        self.code = 'reducer_overflow'
        self.seq = seq
        self.size = size


class OrderedReducer:
    def __init__(self, *, max_buffer: int = 16, start_seq: int = 0,
                 token: Optional[int] = None):
        self.max_buffer = max(1, max_buffer)
        self.token = token
        self.next_seq = start_seq
        self.buffer: dict = {}
        self.last_boundary = None
        self.stats = {'committed': 0, 'buffered': 0, 'duplicates': 0, 'stale': 0,
                      'old_token': 0, 'pressure': 0, 'overflow': 0, 'max_buffered': 0}

    # -------------------------------------------------------------------- accept
    def accept(self, batch: dict) -> dict:
        seq = batch.get('seq')
        if not isinstance(seq, int):
            raise TypeError('batch needs an integer seq')
        if self.token is not None and batch.get('token') is not None and batch['token'] < self.token:
            self.stats['old_token'] += 1
            return {'commit': [], 'dropped': OLD_TOKEN, 'pressure': self.pressure()}
        if seq < self.next_seq:
            self.stats['stale'] += 1
            return {'commit': [], 'dropped': STALE, 'pressure': self.pressure()}
        if seq in self.buffer:
            self.stats['duplicates'] += 1
            return {'commit': [], 'dropped': DUPLICATE, 'pressure': self.pressure()}

        commit = []
        if seq == self.next_seq:
            commit.append(batch)
            self.last_boundary = batch.get('boundary') or self.last_boundary
            self.next_seq += 1
            while self.next_seq in self.buffer:
                ready = self.buffer.pop(self.next_seq)
                commit.append(ready)
                self.last_boundary = ready.get('boundary') or self.last_boundary
                self.next_seq += 1
        else:
            self.buffer[seq] = batch
            self.stats['max_buffered'] = max(self.stats['max_buffered'], len(self.buffer))
            if len(self.buffer) > self.max_buffer:
                self.stats['overflow'] += 1
                raise ReducerOverflow(seq, len(self.buffer))
        self.stats['committed'] += len(commit)
        self.stats['buffered'] = len(self.buffer)
        if self.pressure():
            self.stats['pressure'] += 1
        return {'commit': commit, 'dropped': None, 'pressure': self.pressure()}

    def adopt_token(self, token: int) -> bool:
        if self.token is None or token > self.token:
            self.token = token
            return True
        return False

    # --------------------------------------------------------------------- views
    def pressure(self) -> bool:
        return len(self.buffer) * 2 >= self.max_buffer

    def checkpoint(self) -> dict:
        return {'seq': self.next_seq - 1, 'boundary': self.last_boundary}

    def pending(self) -> int:
        return len(self.buffer)
