"""Query execution: candidate scan, exact match, counts and budgets (R6-044).

The executor is deliberately boring. It walks candidates in ordinal order, asks the
shared JS matcher which of them actually match, appends the hits to the snapshot in
sequence order, and stops when the budget runs out — at which point the query is
`paused` with a resume token rather than "finished with 0 results".

Three correctness rules, all of them things a query layer usually gets wrong:

* **"complete" is about the answer, "evaluated" is about the work.** C4 of the
  contract says `complete === scanned_bytes >= total_bytes`, so a finished pruned
  scan reports `scanned_bytes = total_bytes` — the index proved the rest cannot
  match. The bytes actually read are reported separately as `evaluated_bytes`, so
  the fast path cannot be mistaken for the slow one in the metrics.
* **a stopped scan is `at_least`, never `exact`.** C3 in the contract.
* **cancel keeps the snapshot.** The hits found so far stay readable, and the
  relation becomes `unknown`, because a cancelled scan is not a count.

The matcher runs in the Node worker (`query-eval`), which loads the same
`src/shared/query.js` the page uses. There is no second implementation of "does
this record match".
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from ..indexing.node_pool import PoolError
from ..indexing.shards import ShardReader
from . import planner as planner_module

DEFAULT_BATCH = 500
DEFAULT_BUDGET_MS = 1500
SNAPSHOT_TTL = 900.0


class Executor:
    def __init__(self, store, pool, shard_resolver: Callable[[str, int], Path], snapshots,
                 *, batch: int = DEFAULT_BATCH, clock=time.time):
        self.store = store
        self.pool = pool
        self.shard_resolver = shard_resolver
        self.snapshots = snapshots
        self.batch = batch
        self.clock = clock
        self._tokens: dict = {}

    # ------------------------------------------------------------------- tokens
    def open_token(self, query_id: str, spec: dict) -> dict:
        opened = self.pool.query_open(spec, key='q-%s' % query_id)
        self._tokens[query_id] = opened['qid']
        return opened

    def close_token(self, query_id: str) -> None:
        qid = self._tokens.pop(query_id, None)
        if not qid:
            return
        try:
            self.pool.query_close(qid, key='q-%s' % query_id)
        except PoolError:
            pass

    # ------------------------------------------------------------------- driver
    def run(self, query_id: str, *, principal: str, start_ordinal: int = -1,
            budget_ms: Optional[int] = None) -> dict:
        started = self.clock()
        row = self.snapshots.get(query_id, principal)
        payload = row['payload']
        shard = self.shard_resolver(row['file_id'], int(row['generation']))
        reader = ShardReader(shard)
        budget = int(budget_ms if budget_ms is not None else
                     (payload.get('budget_ms') or DEFAULT_BUDGET_MS))
        deadline = started + budget / 1000.0

        plan = planner_module.plan_query(
            shard, dict(payload['spec'], terms=payload.get('terms')),
            postings_version=self._postings_version(reader),
            postings_complete=bool(self._postings_version(reader)),
            total_records=reader.count())
        payload['plan'] = plan.report()

        self.snapshots.update(query_id, state='running')
        total_bytes = _file_bytes(self.store, row['file_id'])
        count = payload.get('count') or {}
        matched = int(count.get('matched') or 0)
        evaluated_bytes = 0
        evaluated_records = 0
        last_ordinal = int(start_ordinal)
        position = int(payload.get('position') or 0)
        paused = False

        candidates = planner_module.iter_candidates(shard, plan, start_after=start_ordinal)
        while True:
            if self.clock() >= deadline:
                paused = True
                break
            ordinals = []
            for _ in range(self.batch):
                try:
                    ordinals.append(next(candidates))
                except StopIteration:
                    break
            if not ordinals:
                break
            records = reader.records_for(ordinals)
            if not records:
                continue
            evaluated_records += len(records)
            evaluated_bytes += sum(_record_bytes(record) for record in records)
            hits = self._evaluate(query_id, records, payload)
            if hits:
                position = self.snapshots.append_hits(query_id, hits, start_seq=position)
                matched += len(hits)
            last_ordinal = ordinals[-1]

        elapsed_ms = int((self.clock() - started) * 1000)
        complete = not paused
        coverage = {
            # C4: complete is a statement about the answer, so a finished pruned
            # scan has seen the whole file *through the index*.
            'scanned_bytes': str(total_bytes if complete and plan.superset else min(evaluated_bytes,
                                                                                   total_bytes)),
            'total_bytes': str(total_bytes),
            'complete': bool(complete and plan.superset),
            'evaluated_bytes': str(evaluated_bytes),
            'scanned_records': evaluated_records,
            'mode': plan.mode,
        }
        # C3/C5: a budget stop is a floor (`at_least`), a cancel is not a count
        # (`unknown`), and only a finished scan may claim `exact`.
        relation = 'exact' if coverage['complete'] else 'at_least'
        state = 'done' if complete else 'paused'
        payload.update({
            'count': {'matched': matched, 'returned': matched, 'relation': relation},
            'coverage': coverage,
            'position': position,
            'elapsed_ms': int(payload.get('elapsed_ms') or 0) + elapsed_ms,
            'resume_token': (self.snapshots.resume_token(row, principal, position, last_ordinal)
                             if paused else None),
            'last_ordinal': last_ordinal,
        })
        self.snapshots.update(query_id, state=state, payload=payload,
                              expires_at=self.clock() + SNAPSHOT_TTL)
        return {'query_id': query_id, 'state': state, 'count': payload['count'],
                'coverage': coverage, 'elapsed_ms': elapsed_ms,
                'resume_token': payload['resume_token'], 'plan': plan.report()}

    # ------------------------------------------------------------------ matching
    def _evaluate(self, query_id: str, records: list, payload: dict) -> list:
        qid = self._tokens.get(query_id)
        if qid:
            try:
                reply = self.pool.query_eval(qid, records, key='q-%s' % query_id)
                return list(reply.get('hits') or [])
            except PoolError as exc:
                if exc.code not in ('unknown_query', 'worker_dead', 'worker_timeout'):
                    raise
                self._tokens.pop(query_id, None)
        # The worker that held the AST is gone: reopen from the stored spec, which
        # is deterministic, so the semantics cannot drift.
        opened = self.open_token(query_id, payload['spec'])
        reply = self.pool.query_eval(opened['qid'], records, key='q-%s' % query_id)
        return list(reply.get('hits') or [])

    def _postings_version(self, reader: ShardReader) -> Optional[str]:
        try:
            layer = reader.layer('search')
        except Exception:  # noqa: BLE001 - a shard without layers is a full scan
            return None
        return (layer or {}).get('version')

    # ------------------------------------------------------------------- cancel
    def cancel(self, query_id: str, *, principal: str) -> dict:
        row = self.snapshots.get(query_id, principal)
        payload = row['payload']
        count = payload.get('count') or {}
        payload['count'] = {'matched': None, 'returned': count.get('returned') or 0,
                            'relation': 'unknown'}
        coverage = payload.get('coverage') or {}
        coverage['complete'] = False
        payload['coverage'] = coverage
        payload['resume_token'] = None
        self.snapshots.update(query_id, state='cancelled', payload=payload)
        self.close_token(query_id)
        return {'query_id': query_id, 'state': 'cancelled'}


def _record_bytes(record: dict) -> int:
    try:
        return max(0, int(record['bytes']['end'] or 0) - int(record['bytes']['start'] or 0))
    except (KeyError, TypeError, ValueError):
        return len(record.get('message') or '')


def _file_bytes(store, file_id: str) -> int:
    with store.connect() as db:
        row = db.execute('SELECT size FROM logical_files WHERE id=?', (file_id,)).fetchone()
    return int(row['size']) if row else 0


def state_of(row: dict) -> dict:
    """The QueryState superset the UI reads (see qa/evidence/R6-042.md)."""
    payload = row['payload']
    complete = row['state'] == 'done'
    count = dict(payload.get('count') or {})
    relation = count.get('relation') or 'unknown'
    if relation == 'unknown':
        count['matched'] = None
    return {
        'query_id': row['id'],
        'dataset_id': row['file_id'],
        'state': row['state'],
        'count': {'matched': count.get('matched'), 'returned': int(count.get('returned') or 0),
                  'relation': relation},
        'coverage': payload.get('coverage') or {'scanned_bytes': '0', 'total_bytes': '0',
                                                'complete': False},
        'index_version': int(row['generation']),
        'budget_ms': int(payload.get('budget_ms') or 0),
        'elapsed_ms': int(payload.get('elapsed_ms') or 0),
        'resume_token': payload.get('resume_token'),
        'error': payload.get('error'),
        'signature': row['signature'],
        'plan': payload.get('plan'),
        'order_final': complete,
    }
