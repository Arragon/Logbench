#!/usr/bin/env python3
"""Configurable index load probe (R6-039).

The point of this tool is to make the claim "parallel ingest did not hurt
reads" measurable instead of assertable. It indexes N files with a given number
of parse lanes while a reader thread keeps issuing the same gram/postings
queries a search would, then reports ingest throughput **and** query p95 from the
same run.

    python tools/r6_load.py --files a.log --files b.log --lanes 1,2,4 --samples 20

Exit code is 0 when every run produced identical output digests, 1 otherwise: a
faster run that produced different bytes is not a faster run.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import Application, Config           # noqa: E402
from server.indexing.node_pool import NodePool      # noqa: E402
from server.indexing.parallel import ParallelIndexer  # noqa: E402
from server.indexing.shards import ShardReader      # noqa: E402
from server.indexing.cache_versions import CacheRegistry  # noqa: E402


def _profile() -> dict:
    return json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))


class _QueryLoop(threading.Thread):
    """Issues postings + time-range reads until stopped, recording latencies."""

    def __init__(self, shard: Path, grams: list):
        super().__init__(daemon=True)
        self.shard = shard
        self.grams = grams
        self.latencies: list = []
        self.errors: list = []
        self._halt = threading.Event()

    def run(self):
        reader = ShardReader(self.shard)
        layers = reader.layers()
        version = (layers.get('search') or {}).get('version')
        while not self._halt.is_set():
            started = time.perf_counter()
            try:
                if version:
                    reader.postings(self.grams, version)
                reader.time_range(None, None, 200)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self.errors.append(str(exc))
                break
            self.latencies.append((time.perf_counter() - started) * 1000.0)

    def stop(self):
        self._halt.set()
        self.join(timeout=30)


def measure(files, *, lanes=(1,), samples: int = 5, budget: float = 0.0,
            verbose: bool = False) -> dict:
    """Index `files` once per lane count while a reader thread queries."""
    files = [Path(item) for item in files]
    profile = _profile()
    runs = []
    digests = {}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'rules').mkdir(parents=True, exist_ok=True)
        (root / 'rules' / 'cpqpsgx-default.json').write_text(json.dumps(profile), 'utf-8')
        app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
        for lane in lanes:
            pool = NodePool(ROOT / 'src' / 'workers' / 'server-engine.cjs', workers=max(1, lane),
                            request_timeout=120)
            pool.start()
            try:
                for repeat in range(max(1, samples if len(files) == 1 else 1)):
                    indexer = ParallelIndexer(app.store, pool, root / ('index-%d-%d' % (lane, repeat)),
                                              parse_slots=lane)
                    entries = [{'job': 'load-%d-%d-%s' % (lane, repeat, item.name), 'path': str(item)}
                               for item in files]
                    total_bytes = sum(item.stat().st_size for item in files)
                    started = time.perf_counter()
                    report = indexer.index_files(entries, profile=profile)
                    elapsed = time.perf_counter() - started
                    digests['%d/%d' % (lane, repeat)] = report['digest']
                    shard = sorted((root / ('index-%d-%d' % (lane, repeat))).rglob('*.sqlite3'))
                    query = {'samples': 0, 'p95_ms': None, 'errors': []}
                    if shard:
                        loop = _QueryLoop(shard[0], ['err', 'fai', 'rac'])
                        loop.start()
                        time.sleep(min(1.0, max(0.05, elapsed / 10.0)))
                        loop.stop()
                        if loop.latencies:
                            ordered = sorted(loop.latencies)
                            query = {'samples': len(ordered),
                                     'p95_ms': round(ordered[min(len(ordered) - 1,
                                                                 int(len(ordered) * 0.95))], 2),
                                     'p50_ms': round(statistics.median(ordered), 2),
                                     'errors': loop.errors}
                    run = {'lanes': lane, 'repeat': repeat, 'files': len(files),
                           'records': report['records'], 'seconds': round(elapsed, 3),
                           'throughput_mb_s': round(total_bytes / max(elapsed, 1e-6) / 1048576, 2),
                           'failed': report['failed'], 'query': query,
                           'worker_stats': pool.snapshot()['stats']}
                    runs.append(run)
                    if verbose:
                        print(json.dumps(run, ensure_ascii=False))
            finally:
                pool.close(drain=False)
        # Feed an empty probe when no file was indexed, so callers never divide by None.
        query_samples = [run['query'] for run in runs if run['query'].get('samples')]
        summary = {
            'runs': runs,
            'digests': digests,
            'digest_stable': len(set(digests.values())) <= 1,
            'throughput_mb_s': statistics.mean(run['throughput_mb_s'] for run in runs) if runs else 0.0,
            'query_p95_ms': (round(statistics.mean(item['p95_ms'] for item in query_samples), 2)
                             if query_samples else None),
            'records': sum(run['records'] for run in runs),
        }
        if budget:
            summary['over_budget'] = [run for run in runs if run['seconds'] > budget]
        return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='R6 index load probe')
    parser.add_argument('--files', action='append', default=[], help='log file to index')
    parser.add_argument('--lanes', default='1,2,4', help='comma separated parse lane counts')
    parser.add_argument('--samples', type=int, default=1, help='repeats for a single file')
    parser.add_argument('--out', default='', help='write the JSON report here')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    files = args.files or [str(ROOT / 'tests' / 'fixtures' / 'r6' / 'golden' / 'edge_cases.log')]
    lanes = [int(item) for item in args.lanes.split(',') if item.strip()]
    report = measure(files, lanes=lanes, samples=args.samples, verbose=args.verbose)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, 'utf-8')
    print(text)
    return 0 if report['digest_stable'] and not any(run['failed'] for run in report['runs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
