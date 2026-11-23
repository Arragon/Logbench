#!/usr/bin/env python3
"""Large-file index probe (R6-036).

Smaller and more focused than the M3 gate: index one file with the real Node pool
and the real shard writer, measure read volume / records / elapsed / RSS, then
kill the run halfway and resume, comparing the resumed shard against a clean run
record for record.

    python qa/r6/index_large.py --size-mb 512
    python qa/r6/index_large.py --file /path/to/app.log --chunk-mb 2

The checkpoint is read from the shard, not from memory: the point of the exercise
is that the durable watermark is what resumes, not whatever the last process
happened to hold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

try:  # Unix-only: report "n/a" rather than failing to import on Windows.
    import resource
except ImportError:  # pragma: no cover - exercised on Windows only
    resource = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.app import Application, Config                # noqa: E402
from server.indexing.node_pool import NodePool            # noqa: E402
from server.indexing.pipeline import Pipeline             # noqa: E402
from server.indexing.shards import ShardReader            # noqa: E402
import server_index_gate as gate                          # noqa: E402


def rss_mb() -> Optional[float]:
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1e6 if sys.platform == 'darwin' else 1e3), 1)


def rows_of(shard: Path, limit: int = 2000) -> list:
    return [(row['ordinal'], row['lines'], row['message'])
            for row in ShardReader(shard).page(0, limit)]


def run(path: Path, *, chunk_mb: float, kill_at: float, out: Path) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix='r6-large-'))
    (workdir / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', workdir / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(workdir, bootstrap_admins=['127.0.0.1']))
    profile = json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))
    pool = NodePool(ROOT / 'src' / 'workers' / 'server-engine.cjs', workers=1, request_timeout=120)
    pool.start()
    chunk = int(chunk_mb * 1024 * 1024)
    size = path.stat().st_size
    report = {'file': str(path), 'bytes': size, 'chunk_mb': chunk_mb}
    try:
        first = Pipeline(app.store, pool, workdir / 'index', chunk_size=chunk)
        started = time.perf_counter()
        partial = first.run('large-1', path, profile=profile,
                            max_bytes=int(size * kill_at) if 0 < kill_at < 1 else None)
        report['cold'] = {'seconds': round(time.perf_counter() - started, 2),
                          'records': partial['stats']['records'],
                          'bytes': partial['stats']['bytes'],
                          'chunks': partial['stats']['chunks'],
                          'shard': partial['shard'],
                          'checkpoint': ShardReader(Path(partial['shard'])).checkpoint('parsed')}
        report['cold']['mb_per_s'] = round(report['cold']['bytes'] / max(report['cold']['seconds'], 1e-6)
                                           / 1048576, 2)
        report['rss_mb'] = rss_mb()

        if 0 < kill_at < 1:
            resumed = Pipeline(app.store, pool, workdir / 'index', chunk_size=chunk)
            started = time.perf_counter()
            second = resumed.run('large-2', path, profile=profile)
            report['resume'] = {'seconds': round(time.perf_counter() - started, 2),
                                'restarted_from': second['stats']['restarted_from'],
                                'records': second['stats']['records'],
                                'shard': second['shard']}
            clean = Pipeline(app.store, pool, workdir / 'index-clean', chunk_size=chunk)
            fresh = clean.run('large-clean', path, profile=profile)
            report['clean'] = {'records': fresh['stats']['records'], 'shard': fresh['shard']}
            report['resume']['identical'] = rows_of(Path(second['shard'])) == rows_of(Path(fresh['shard']))
            report['resume']['no_duplicates'] = report['resume']['identical']
        report['ok'] = report.get('resume', {}).get('identical', True)
        report['node_stats'] = pool.snapshot()['stats']
        digest = hashlib.sha256()
        digest.update(str(report['cold']['records']).encode())
        digest.update(path.read_bytes()[:65536] if size <= 1 << 20 else str(size).encode())
        report['digest'] = digest.hexdigest()
    finally:
        pool.close(drain=False)
        shutil.rmtree(workdir, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='R6 large-file index probe')
    parser.add_argument('--file', default='')
    parser.add_argument('--size-mb', type=int, default=512)
    parser.add_argument('--chunk-mb', type=float, default=4.0)
    parser.add_argument('--kill-at', type=float, default=0.5,
                        help='fraction of the file to index before pretending to crash (0 disables)')
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / 'index-large.json'))
    args = parser.parse_args(argv)

    temp = None
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print('file not found: %s' % path, file=sys.stderr)
            return 2
    else:
        temp = Path(tempfile.mkdtemp(prefix='r6-large-fixture-'))
        path = temp / 'big.log'
        info = gate.generate_log(path, args.size_mb * 1024 * 1024)
        print('fixture: %s (%s)' % (path, info))
    try:
        report = run(path, chunk_mb=args.chunk_mb, kill_at=args.kill_at, out=Path(args.out))
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:2000])
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
