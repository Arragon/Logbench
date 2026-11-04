"""Parallel indexing without breaking source order (R6-039).

Two levels of parallelism, and a hard rule for each:

* **between files** — one file is one unit. Files are independent, so the only
  cost of concurrency is CPU, which the scheduler budget already accounts for.
* **inside a file, only for record-local work** — extractors, anomalies and
  timeline nodes are per-record functions, so batches of an already-framed file
  can be enriched by several workers. Everything that carries streaming state
  (framing, traceback merging, checkpoints) stays on the single sequential
  producer, and results are committed through `OrderedReducer` in source order.

Framing is never split by byte offset: a record boundary is not a byte boundary,
and a "split at 4 MiB" scheme is exactly how a traceback gets torn in half.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterable, Optional

from .layers import _RangeReader
from .ordered import OrderedReducer, ReducerOverflow
from .pipeline import Pipeline

DEFAULT_SLOTS = 4


class ParallelIndexer:
    def __init__(self, store, pool, index_root, *, parse_slots: int = DEFAULT_SLOTS,
                 max_buffer: int = 16):
        self.store = store
        self.pool = pool
        self.index_root = Path(index_root)
        self.parse_slots = max(1, parse_slots)
        self.max_buffer = max(2, max_buffer)

    # ------------------------------------------------------------------ per file
    def index_files(self, entries: Iterable[dict], *, profile: Optional[dict] = None,
                    signature: Optional[str] = None, on_progress=None) -> dict:
        """Index independent files concurrently. Each file stays sequential."""
        entries = list(entries)
        results, failures = {}, {}
        with ThreadPoolExecutor(max_workers=self.parse_slots, thread_name_prefix='lb-file') as pool:
            futures = {}
            for entry in entries:
                pipeline = Pipeline(self.store, self.pool, self.index_root)
                futures[pool.submit(pipeline.run, entry['job'], Path(entry['path']),
                                    profile=profile, signature=signature,
                                    on_progress=on_progress)] = entry['job']
            for future in futures:
                job = futures[future]
                try:
                    results[job] = future.result()
                except Exception as exc:  # noqa: BLE001 - one file must not cancel the rest
                    failures[job] = {'code': getattr(exc, 'code', 'index_failed'),
                                     'message': str(exc)[:300]}
        digest = hashlib.sha256()
        parts = []
        for job in sorted(results):
            item = results[job]
            # The digest represents *content*, not scheduling: job ids embed the
            # run, so hashing them would report a difference for identical output.
            content = hashlib.sha256()
            try:
                from .shards import ShardReader
                for record in ShardReader(Path(item['shard'])).page(0, 2000):
                    content.update(json.dumps(
                        [record['ordinal'], record['lines'], record['ts'], record['level'],
                         record['message']], sort_keys=True, ensure_ascii=True).encode('utf-8'))
            except Exception:  # noqa: BLE001 - a missing shard is reported as a failure elsewhere
                content.update(b'missing')
            parts.append([item['content_sig'], item['stats']['records'], content.hexdigest()])
        for part in sorted(parts):
            digest.update(('%s\0%d\0%s\n' % (part[0], part[1], part[2])).encode('utf-8'))
        return {'files': len(entries), 'ok': len(results), 'failed': failures,
                'records': sum(item['stats']['records'] for item in results.values()),
                'digest': digest.hexdigest(), 'results': results}

    # --------------------------------------------------------------- rule fanout
    def apply_parallel(self, records_by_batch: Iterable[list], *, profile: Optional[dict],
                       job: str, lanes: Optional[int] = None,
                       on_batch=None) -> dict:
        """Enrich batches with `lanes` workers, commit in seq order.

        `records_by_batch` yields already-framed record lists in source order; the
        caller decides how many to prepare at once. `on_batch(seq, records)` is
        called at commit time, in order, so a writer can persist incrementally.
        """
        lanes = max(1, min(lanes or self.parse_slots, 16))
        reducer = OrderedReducer(max_buffer=self.max_buffer)
        digest = hashlib.sha256()
        state = {'committed': 0}
        lane_ids = ['%s#%d' % (job, index) for index in range(lanes)]

        def handle(seq: int, applied: list):
            outcome = reducer.accept({'seq': seq, 'records': applied,
                                      'boundary': {'batch': seq}})
            for batch in outcome['commit']:
                for record in batch['records']:
                    digest.update(json.dumps(
                        [record.get('ordinal'), record.get('fields'), record.get('issue'),
                         record.get('issueSeverity'), record.get('event')],
                        sort_keys=True, ensure_ascii=True).encode('utf-8'))
                    digest.update(b'\0')
                state['committed'] += len(batch['records'])
                if on_batch:
                    on_batch(batch['seq'], batch['records'])

        with ThreadPoolExecutor(max_workers=lanes, thread_name_prefix='lb-rules') as executor:
            source = enumerate(records_by_batch)
            pending: dict = {}
            inflight = max(lanes * 2, 2)
            exhausted = False
            while pending or not exhausted:
                # Top up only while the ordered buffer is not under pressure — unless
                # nothing is in flight, because then only new work can fill the gap.
                while (not exhausted and len(pending) < inflight
                       and (not reducer.pressure() or not pending)):
                    try:
                        seq, records = next(source)
                    except StopIteration:
                        exhausted = True
                        break
                    pending[executor.submit(self.pool.apply, lane_ids[seq % lanes],
                                            records, profile)] = seq
                if not pending:
                    continue
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    handle(pending.pop(future), future.result().get('records') or [])
        if reducer.pending():
            raise ReducerOverflow(reducer.next_seq, reducer.pending())
        return {'rows': state['committed'], 'digest': digest.hexdigest(), 'lanes': lanes,
                'stats': dict(reducer.stats), 'checkpoint': reducer.checkpoint()}

    # --------------------------------------------------------------- shard layer
    def rebuild_rules_parallel(self, shard_path: Path, *, source_path: Path, pool,
                               profile: Optional[dict], version: str, job: str = 'rules-parallel',
                               page: int = 500, lanes: Optional[int] = None) -> dict:
        """Same output as `build_rules_layer`, with the enrichment fanned out."""
        from .shards import ShardReader, ShardWriter
        reader = ShardReader(shard_path)
        meta = reader.meta()
        writer = ShardWriter(Path(shard_path), file_id=meta.get('file_id', 'file'),
                             generation=int(meta.get('generation', 1) or 1),
                             signature=meta.get('signature', 'default')).open()
        ranges = _RangeReader(source_path)
        total = reader.count()
        buffer: list = []
        batch_size = page

        def producer():
            for offset in range(0, total, batch_size):
                records = reader.page(offset, batch_size)
                for record in records:
                    record['raw'] = ranges.read(record.get('bytes') or {})
                yield records

        def commit(seq: int, records: list):
            buffer.extend(records)

        try:
            report = self.apply_parallel(producer(), profile=profile, job=job,
                                         lanes=lanes, on_batch=commit)
            if len(buffer) != total:
                from .shards import ShardError
                raise ShardError('coverage_mismatch', '并行规则层覆盖不完整。',
                                 expected=total, actual=len(buffer))
            writer.write_rule_rows(version, buffer, reset=True)
            writer.write_layer('rules', version, rows=len(buffer), digest=report['digest'])
            return {'layer': 'rules', 'version': version, 'rows': len(buffer),
                    'digest': report['digest'],
                    'stats': {'parses': 0, 'source_bytes': ranges.bytes_read,
                              'records': len(buffer), 'lanes': report['lanes']}}
        finally:
            ranges.close()
            writer.close()
