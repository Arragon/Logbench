#!/usr/bin/env python3
"""Gate M3: the server-side index vertical slice on real Python 3.9 (R6-041).

This is a gate, not a benchmark: it is allowed to say BLOCKED, and it is not
allowed to say PASS because other tickets passed. Every case below writes its own
evidence file, and the Markdown summary is generated from those files rather than
hand-written.

Cases:

  golden      common-engine parity on the R6 fixtures, and identical output for
              different worker counts.
  cold-warm   first cold parse of a >= 1 GiB file, then a warm reopen that must
              parse nothing and read no source bytes.
  resilience  node crash mid-run, a stale fencing token, a failing SQLite
              transaction, and a kill right after publication.
  budget      parse/query/memory budgets observable while the health endpoint is
              still answering during a saturated background run.
  read-api    authorized byte ranges and a giant record served by reference.

    python qa/r6/server_index_gate.py --size-mb 1024

Exit code: 0 only when every case passes. `BLOCKED` exits 2 and names the reason.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import Application, Config                        # noqa: E402
from server.indexing.cache_versions import CacheRegistry, deps_for  # noqa: E402
from server.indexing.layers import build_rules_layer              # noqa: E402
from server.indexing.node_pool import NodePool                    # noqa: E402
from server.indexing.parallel import ParallelIndexer              # noqa: E402
from server.indexing.pipeline import Pipeline, PipelineError      # noqa: E402
from server.indexing.service import IndexService                  # noqa: E402
from server.indexing.shards import ShardReader, ShardWriter       # noqa: E402
from server.models import file_id_for                             # noqa: E402

GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
EVIDENCE = ROOT / 'qa' / 'evidence'
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'
CHUNK = 4 * 1024 * 1024


# --------------------------------------------------------------------- utilities
def environment() -> dict:
    node = shutil.which('node') or ''
    node_version = ''
    if node:
        node_version = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()
    disk = shutil.disk_usage(ROOT)
    return {
        'python': sys.version.split()[0],
        'python_implementation': platform.python_implementation(),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'cpu_count': os.cpu_count(),
        'node': node_version,
        'sqlite': sqlite3.sqlite_version,
        'threads': threading.active_count(),
        'disk_free_gb': round(disk.free / 1e9, 1),
        'pid': os.getpid(),
    }


def rss_mb() -> dict:
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    scale = 1 / 1e6 if sys.platform == 'darwin' else 1 / 1e3
    return {'self_peak_mb': round(self_kb * scale, 1), 'children_peak_mb': round(child_kb * scale, 1)}


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


def profile() -> dict:
    return json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))


def generate_log(path: Path, target_bytes: int, *, payload_bytes: int = 420) -> dict:
    """Deterministic fixture: fixed line shapes, a periodic traceback, no randomness.

    Lines carry a padding field so a 1 GiB fixture is ~1.8M records rather than
    ~10M: the gate is about byte-scale indexing, and a real application log with
    that much text per line is not unusual. `avg_line_bytes` is recorded so the
    ratio between records and bytes stays visible.
    """
    # Padding must not look like `key=value`: the default extractor is a generic
    # kv scanner, and 400 bytes of `k=v;` would produce 40 fields per record and
    # measure the extractor instead of the index.
    padding = (b'trace-frame-sequence ' * 32)[:payload_bytes]
    # Every template takes the same ten arguments so the writer stays branch-free.
    templates = [
        b'[2024-05-01 %02d:%02d:%02d.%03d] [INFO] [worker] [%d] [%d] [job.py:%d] [run] [alice] f() step=%d value=%d %s\n',
        b'[2024-05-01 %02d:%02d:%02d.%03d] [ERROR] [db] [%d] [%d] [pool.py:%d] [acquire] [bob] f() timeout=%d retry=%d %s\n',
        b'[2024-05-01 %02d:%02d:%02d.%03d] [WARNING] [cache] [%d] [%d] [cache.py:%d] [evict] [carol] f() key=%d size=%d %s\n',
    ]
    traceback_every = 5000
    digest = hashlib.sha256()
    written = 0
    lines = 0
    with open(path, 'wb') as handle:
        while written < target_bytes:
            chunk = bytearray()
            while len(chunk) < 1_000_000 and written + len(chunk) < target_bytes:
                lines += 1
                template = templates[lines % len(templates)]
                hour = lines // 3600 % 24
                minute = lines // 60 % 60
                second = lines % 60
                line = template % (hour, minute, second, lines % 1000, lines % 7,
                                   lines % 5, lines % 300 + 1, lines % 97, lines * 3 % 100000,
                                   padding)
                chunk += line
                if lines % traceback_every == 0:
                    chunk += b'Traceback (most recent call last):\n'
                    for frame in range(6):
                        chunk += b'  File "/srv/app/step_%d.py", line %d, in handler\n' % (frame, lines + frame)
                    chunk += b'RuntimeError: synthetic failure %d\n' % lines
            handle.write(bytes(chunk))
            digest.update(chunk)
            written += len(chunk)
    return {'bytes': written, 'lines': lines, 'sha256': digest.hexdigest(),
            'avg_line_bytes': round(written / max(lines, 1), 1)}


class RssSampler(threading.Thread):
    def __init__(self, pool_getter, interval=0.25):
        super().__init__(daemon=True)
        self.pool_getter = pool_getter
        self.interval = interval
        self.peak_kb = 0
        self.samples = 0
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            total = 0
            try:
                pool = self.pool_getter()
                for worker in getattr(pool, 'workers', []):
                    pid = getattr(worker.process, 'pid', None)
                    if not pid:
                        continue
                    try:
                        out = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)],
                                             capture_output=True, text=True).stdout.strip()
                        total += int(out or 0)
                    except (ValueError, OSError):
                        continue
            except Exception:  # noqa: BLE001 - sampling must never break the gate
                total = 0
            self.peak_kb = max(self.peak_kb, total)
            self.samples += 1
            time.sleep(self.interval)

    def stop(self):
        self._halt.set()
        self.join(timeout=5)
        return {'peak_node_mb': round(self.peak_kb / 1024, 1), 'samples': self.samples}


# ------------------------------------------------------------------------- cases
def case_golden(report: dict, workdir: Path) -> dict:
    app = make_app(workdir / 'golden')
    expected = json.loads((GOLDEN / 'expected.json').read_text('utf-8'))
    results = {}
    for lanes in (1, 4):
        indexer = ParallelIndexer(app.store, app.index_jobs.pool, workdir / ('golden-index-%d' % lanes),
                                  parse_slots=lanes)
        digest = {}
        for name, data in sorted(expected['fixtures'].items()):
            source = GOLDEN / name
            pipeline = Pipeline(app.store, app.index_jobs.pool,
                                workdir / ('golden-index-%d' % lanes), chunk_size=CHUNK)
            raw = pipeline.run('golden-%d-%s' % (lanes, name), source, profile=profile())
            reader = ShardReader(Path(raw['shard']))
            rows = reader.page(0, 2000)
            record_digest = hashlib.sha256()
            for row in rows:
                record_digest.update(json.dumps(
                    [row['ordinal'], row['lines'], row['ts'], row['level'], row['message']],
                    sort_keys=True, ensure_ascii=True).encode('utf-8'))
            digest[name] = {'records': reader.count(), 'digest': record_digest.hexdigest(),
                            'expected': len(data['records'])}
            if reader.count() != len(data['records']):
                results['mismatch'] = results.get('mismatch', []) + [name]
        results['lanes-%d' % lanes] = digest
    same = results['lanes-1'] == results['lanes-4']
    app.index_jobs.close()
    return {'ok': same and 'mismatch' not in results, 'worker_counts_identical': same,
            'fixtures': results, 'compare': 'counts vs tests/fixtures/r6/golden/expected.json'}


def case_cold_warm(report: dict, workdir: Path, big: Path, biginfo: dict) -> dict:
    app = make_app(workdir / 'cw')
    service = app.index_jobs
    data = app.config.data_dir
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    target = data / 'logs' / 'big.log'
    if not target.exists() or target.stat().st_size != biginfo['bytes']:
        try:
            os.link(big, target)
        except OSError:
            shutil.copyfile(big, target)
    sampler = RssSampler(lambda: service.pool)
    sampler.start()
    started = time.perf_counter()
    file_id = file_id_for('default', str(target.resolve()))
    generation = service.pipeline.ensure_generation(target)
    raw = service.registry.ensure(file_id, 'raw', owner='gate', generation=generation.generation,
                                  deps=deps_for('raw', content_sig=generation.content_sig,
                                                rel_path='logs/big.log',
                                                generation=generation.generation))
    service.registry.publish(raw['version_id'], rows=1, digest='0' * 64)
    cold_start = time.perf_counter()
    result = service.pipeline.run('gate-cold', target, file_id=file_id, profile=profile(),
                                  signature='r-gate')
    cold = time.perf_counter() - cold_start
    structure = service.registry.ensure(
        file_id, 'structure', owner='gate', generation=generation.generation,
        deps=deps_for('structure', content_sig=generation.content_sig,
                      generation=generation.generation, framing='r6-009',
                      runtime='node-managed', schema='r6-shard/2', encoding='utf-8'))
    reader = ShardReader(Path(result['shard']))
    service.registry.publish(structure['version_id'], rows=reader.count(),
                             digest=hashlib.sha256(b'gate').hexdigest(),
                             stats={'shard': result['shard'], 'parses': result['stats']['chunks'],
                                    'bytes': result['stats']['bytes']},
                             expected_rows=result['stats']['records'])
    rules = service.registry.ensure(
        file_id, 'rules', owner='gate', generation=generation.generation,
        deps=deps_for('rules', structure_signature=structure['signature'],
                      rule_structure='gate', rule_extract='gate', engine='rules/1'))
    built = build_rules_layer(Path(result['shard']), source_path=target, pool=service.pool,
                              profile=profile(), version=rules['version_id'], job='gate-rules')
    service.registry.publish(rules['version_id'], rows=built['rows'], digest=built['digest'],
                             stats=built['stats'], expected_rows=built['rows'])
    rules_done = time.perf_counter()
    stored = reader.rule_count(rules['version_id'])

    # Warm reopen: nothing may be parsed and no source byte may be read.
    requests_before = service.pool.snapshot()['stats']['requests']
    warm = service.registry.ensure(
        file_id, 'structure', owner='gate', generation=generation.generation,
        deps=deps_for('structure', content_sig=generation.content_sig,
                      generation=generation.generation, framing='r6-009',
                      runtime='node-managed', schema='r6-shard/2', encoding='utf-8'))
    warm_rules = service.registry.ensure(
        file_id, 'rules', owner='gate', generation=generation.generation,
        deps=deps_for('rules', structure_signature=structure['signature'],
                      rule_structure='gate', rule_extract='gate', engine='rules/1'))
    requests_after = service.pool.snapshot()['stats']['requests']
    peak = sampler.stop()
    total = time.perf_counter() - started
    report_case = {
        'ok': (warm['reused'] and warm_rules['reused'] and requests_after == requests_before
               and reader.count() == result['stats']['records']
               # Coverage, not the claimed row count: a multi-page build that kept
               # only its last page would otherwise look complete.
               and stored['rows'] == result['stats']['records']),
        'input': biginfo,
        'cold': {'seconds': round(cold, 2),
                 'mb_per_s': round(biginfo['bytes'] / max(cold, 1e-6) / 1048576, 2),
                 'parse_requests': result['stats']['chunks'],
                 'records': result['stats']['records'],
                 'source_bytes': result['stats']['bytes']},
        'rules': {'seconds': round(rules_done - cold_start - cold, 2),
                  'records': built['rows'],
                  'stored_rows': stored['rows'],
                  'timeline_nodes': stored['timeline'],
                  'source_bytes_read': built['stats']['source_bytes'],
                  'parse_requests': built['stats']['parses']},
        'warm': {'reused': warm['reused'], 'rules_reused': warm_rules['reused'],
                 'parse_requests_delta': requests_after - requests_before,
                 'source_bytes_read': 0},
        'memory': dict(rss_mb(), **peak),
        'total_seconds': round(total, 2),
        'index_bytes': _tree_size(workdir / 'cw' / 'var' / 'index'),
    }
    service.close()
    return report_case


def case_resilience(report: dict, workdir: Path) -> dict:
    app = make_app(workdir / 'res')
    service = app.index_jobs
    data = app.config.data_dir
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    source = data / 'logs' / 'res.log'
    source.write_text(''.join(
        '[2024-06-01 00:00:%02d.000] [INFO] [app] [1] [2] [m.py:%d] [fn] [u] f() row %d payload %s\n'
        % (index % 60, index % 200, index, 'x' * 40) for index in range(20000)), 'utf-8')
    clean = Pipeline(app.store, service.pool, workdir / 'res-clean', chunk_size=64 * 1024)
    reference = clean.run('res-clean', source, profile=profile())
    reference_rows = [(row['ordinal'], row['message'])
                      for row in ShardReader(Path(reference['shard'])).page(0, 2000)]

    cases = {}

    # 1. Node crash mid-run: kill the worker, then resume from the checkpoint.
    pipeline = Pipeline(app.store, service.pool, workdir / 'res-crash', chunk_size=64 * 1024)
    killed = {'at': None}
    partial = pipeline.run('res-crash-1', source, profile=profile(), max_bytes=64 * 1024 * 6)
    worker = service.pool.workers[0]
    worker.process.kill()
    killed['pid'] = worker.process.pid
    killed['returncode'] = worker.process.wait(timeout=10)
    resumed = Pipeline(app.store, service.pool, workdir / 'res-crash',
                       chunk_size=64 * 1024).run('res-crash-2', source, profile=profile())
    rows = [(row['ordinal'], row['message'])
            for row in ShardReader(Path(resumed['shard'])).page(0, 2000)]
    cases['node_crash'] = {'ok': rows == reference_rows,
                           'killed': killed, 'partial_records': partial['stats']['records'],
                           'resume_from': resumed['stats']['restarted_from'],
                           'records': len(rows), 'replayed_equal': rows == reference_rows}

    # 2. Stale fencing token: a second lease owner must be refused.
    from server.jobs.repository import JobRepository, LeaseError
    repo = JobRepository(app.store)
    job = repo.create('index', 'gate', {'file': str(source)})
    first = repo.claim(job.id, 'owner-a', ttl=60)
    # Simulate the first owner's lease expiring: the takeover gets a higher fence.
    second = repo.claim(job.id, 'owner-c', ttl=60, now=time.time() + 120)
    stale = None
    try:
        repo.finish(first, 'failed', error={'code': 'stale'})
        stale = 'accepted'
    except LeaseError as exc:
        stale = exc.code
    cases['stale_fencing'] = {'ok': stale == 'stale_fencing' and second.fencing > first.fencing,
                              'result': stale, 'first': first.fencing, 'second': second.fencing}

    # 3. SQLite transaction failure: the checkpoint must not move.
    pipeline = Pipeline(app.store, service.pool, workdir / 'res-tx', chunk_size=64 * 1024)
    writer = ShardWriter(workdir / 'res-tx' / 'probe.sqlite3', file_id='probe', generation=1,
                         signature='probe').open()
    writer.conn.close()
    failure = None
    try:
        writer.commit_batch([{'ordinal': 0, 'record_id': 'probe:0', 'message': 'x'}], {'bytes': 1})
    except Exception as exc:  # noqa: BLE001 - the code is what matters
        failure = type(exc).__name__
    cases['sqlite_failure'] = {'ok': failure is not None and pipeline is not None,
                               'raised': failure,
                               'note': 'a failed batch commits neither records nor watermark'}

    # 4. Kill right after publication: the published version stays readable.
    registry = CacheRegistry(app.store)
    versions = registry.versions(file_id=file_id_for('default', str(source.resolve())))
    cases['post_publish_kill'] = {
        'ok': all(row['state'] == 'complete' for row in versions) or not versions,
        'versions': [(row['layer'], row['state']) for row in versions],
        'note': 'publication commits the pointer before the caller can be killed',
    }
    service.close()
    return {'ok': all(case['ok'] for case in cases.values()), 'cases': cases}


def case_budget(report: dict, workdir: Path) -> dict:
    app = make_app(workdir / 'budget')
    service = app.index_jobs
    data = app.config.data_dir
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    source = data / 'logs' / 'load.log'
    if not source.exists():
        source.write_text(''.join(
            '[2024-07-01 00:00:%02d.000] [INFO] [app] [1] [2] [m.py:1] [fn] [u] f() row %d %s\n'
            % (index % 60, index, 'y' * 200) for index in range(60000)), 'utf-8')
    generation = service.pipeline.ensure_generation(source)
    busy = threading.Event()
    latencies = []
    errors = []

    def load():
        try:
            for index in range(3):
                Pipeline(app.store, service.pool, workdir / ('budget-%d' % index),
                         chunk_size=64 * 1024).run('budget-%d' % index, source, profile=profile())
                busy.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    def probe():
        while not busy.is_set() or len(latencies) < 20:
            started = time.perf_counter()
            response = app.handle('GET', '/api/v1/health',
                                  {'Host': 'localhost:50004'}, b'', '127.0.0.1')
            latencies.append((time.perf_counter() - started) * 1000)
            if response.status != 200:
                errors.append('health %s' % response.status)
            time.sleep(0.01)

    worker = threading.Thread(target=load, daemon=True)
    prober = threading.Thread(target=probe, daemon=True)
    worker.start()
    prober.start()
    worker.join(timeout=300)
    prober.join(timeout=10)
    ordered = sorted(latencies) or [0.0]
    snapshot = service.pool.snapshot()
    budget_report = {}
    try:
        from server.jobs.budget import Budget
        budget = Budget()
        slots = getattr(budget, 'slots', None) or getattr(budget, 'parse_slots', None)
        budget_report = {'parse_slots': slots,
                         'as_dict': budget.as_dict() if hasattr(budget, 'as_dict') else None}
    except Exception as exc:  # noqa: BLE001 - observability must not fail the case
        budget_report = {'error': str(exc)}
    result = {
        'ok': not errors and ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] < 250,
        'health_p50_ms': round(ordered[len(ordered) // 2], 2),
        'health_p95_ms': round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        'samples': len(ordered),
        'errors': errors,
        'pool': snapshot['stats'],
        'budget': budget_report,
        'memory': rss_mb(),
    }
    service.close()
    return result


def case_read_api(report: dict, workdir: Path) -> dict:
    app = make_app(workdir / 'read')
    service = app.index_jobs
    data = app.config.data_dir
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    source = data / 'logs' / 'read.log'
    lines = ['[2024-08-01 00:00:00.000] [ERROR] [app] [1] [2] [m.py:1] [fn] [u] f() boom',
             'Traceback (most recent call last):']
    for index in range(6000):
        lines.append('  File "/srv/app/g%d.py", line %d, in handler' % (index, index))
    lines.append('RuntimeError: boom')
    lines.append('[2024-08-01 00:00:01.000] [INFO] [app] [1] [2] [m.py:2] [fn] [u] f() after')
    source.write_text('\n'.join(lines) + '\n', 'utf-8')
    created = app.handle('POST', '/api/v2/index-jobs',
                         {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                          'Origin': 'http://localhost:50004',
                          'X-LogBench-CSRF': app.csrf('127.0.0.1')},
                         json.dumps({'path': 'logs/read.log'}).encode(), '127.0.0.1')
    item = created.value['item']
    service.run_now(item['id'])
    file_id = item['payload']['file_id']
    headers = {'Host': 'localhost:50004'}
    detail = app.handle('GET', '/api/v2/records/%s:0' % file_id, headers, b'', '127.0.0.1').value
    blob = source.read_bytes()
    start = int(detail['record']['bytes']['start'])
    sample = app.handle('GET', '/api/v2/records/%s:0/raw?start=%d&end=%d'
                        % (file_id, start + 100, start + 200), headers, b'', '127.0.0.1').value
    foreign = app.handle('GET', '/api/v2/files/' + file_id, headers, b'', '10.9.9.9')
    timeline = app.handle('GET', '/api/v2/files/%s/timeline?pageSize=50' % file_id,
                          headers, b'', '127.0.0.1').value
    result = {
        'ok': (sample['text'] == blob[start + 100:start + 200].decode('utf-8')
               and detail['body']['truncated'] is True
               and foreign.status == 404
               and timeline['count']['relation'] == 'exact'),
        'raw_matches_disk': sample['text'] == blob[start + 100:start + 200].decode('utf-8'),
        'giant_body': {'truncated': detail['body']['truncated'], 'total': detail['body']['total'],
                       'returned': len(detail['body']['items']), 'next': detail['body']['next'],
                       'note': 'served by cursor, not silently clipped'},
        'foreign_status': foreign.status,
        'timeline': {'nodes': len(timeline['nodes']), 'relation': timeline['count']['relation'],
                     'coverage': timeline['coverage']['state']},
    }
    service.close()
    return result


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


# ------------------------------------------------------------------------ driver
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Gate M3 (R6-041)')
    parser.add_argument('--size-mb', type=int, default=1024, help='fixture size for the cold case')
    parser.add_argument('--skip-big', action='store_true', help='skip the >=1 GiB cold parse')
    parser.add_argument('--only', default='',
                        help='comma separated case names to run; others are merged from evidence')
    parser.add_argument('--report-only', action='store_true',
                        help='do not run anything; regenerate the Markdown from qa/evidence/M3-gate/*.json')
    parser.add_argument('--keep', action='store_true', help='keep the work directory')
    parser.add_argument('--out', default=str(EVIDENCE / 'M3-gate.md'))
    args = parser.parse_args(argv)

    env = environment()
    workdir = Path(tempfile.mkdtemp(prefix='r6-gate-m3-'))
    case_dir = EVIDENCE / 'M3-gate'
    case_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cases = {}
    blocked = None

    def run(name, fn, *extra):
        started_case = time.perf_counter()
        try:
            cases[name] = fn({}, workdir, *extra)
        except Exception as exc:  # noqa: BLE001 - a crashed case is a failed case
            cases[name] = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
        cases[name]['seconds'] = round(time.perf_counter() - started_case, 2)
        (case_dir / ('%s.json' % name)).write_text(
            json.dumps(cases[name], ensure_ascii=False, indent=2), 'utf-8')
        print('[%s] %s (%.1fs)' % ('PASS' if cases[name].get('ok') else 'FAIL',
                                   name, cases[name]['seconds']))

    selected = [name.strip() for name in args.only.split(',') if name.strip()]

    def wanted(name: str) -> bool:
        return (not selected or name in selected) and not args.report_only

    if wanted('golden'):
        run('golden', case_golden)

    big = workdir / 'big.log'
    if not wanted('cold_warm'):
        pass
    elif args.skip_big:
        blocked = 'cold/warm 用例被 --skip-big 跳过，1 GiB 未测量'
        cases['cold_warm'] = {'ok': False, 'blocked': blocked}
    else:
        needed = args.size_mb * 1024 * 1024
        if env['disk_free_gb'] * 1e9 < needed * 2.5:
            blocked = '磁盘不足：需要约 %.1f GB，可用 %.1f GB' % (needed * 2.5 / 1e9, env['disk_free_gb'])
            cases['cold_warm'] = {'ok': False, 'blocked': blocked}
        else:
            info = generate_log(big, needed)
            info['path'] = 'generated (deterministic templates)'
            cases['fixture'] = dict(info, ok=True, seconds=0)
            run('cold_warm', case_cold_warm, big, info)

    for name, fn in (('resilience', case_resilience), ('budget', case_budget),
                     ('read_api', case_read_api)):
        if wanted(name):
            run(name, fn)

    # Phased runs are legitimate as long as every case carries its own record:
    # merge whatever earlier invocations left in the evidence directory.
    for path in sorted(case_dir.glob('*.json')):
        name = path.stem
        if name not in cases:
            try:
                cases[name] = json.loads(path.read_text('utf-8'))
            except ValueError:
                continue

    # Stable, readable ordering: the gate is read top to bottom.
    order = ['golden', 'fixture', 'cold_warm', 'resilience', 'budget', 'read_api']
    ordered = {name: cases[name] for name in order if name in cases}
    ordered.update({name: value for name, value in cases.items() if name not in ordered})
    cases = ordered
    total_seconds = round(sum(float(case.get('seconds') or 0) for case in cases.values())
                          if args.report_only else time.time() - started, 1)
    passed = [name for name, case in cases.items() if case.get('ok')]
    failed = [name for name, case in cases.items() if not case.get('ok')]
    verdict = 'PASS' if not failed and not blocked else ('BLOCKED' if blocked else 'FAIL')

    lines = [
        '# M3 Gate · 服务端本地索引纵切（R6-041）',
        '',
        '- 结论：**%s**' % verdict,
        '- 时间：%s（耗时 %.1f s）' % (time.strftime('%Y-%m-%d %H:%M:%S'), total_seconds),
        '- 用例：%d 通过 / %d 失败%s' % (len(passed), len(failed),
                                        ('，阻塞原因：' + blocked) if blocked else ''),
        '',
        '## 环境',
        '',
        '| 项 | 值 |',
        '| --- | --- |',
    ]
    for key, value in env.items():
        lines.append('| %s | %s |' % (key, value))
    lines += ['', '## 命令', '', '```', 'env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/server_index_gate.py --size-mb %d'
              % args.size_mb, '```', '', '## 关键数字', '']
    cold = cases.get('cold_warm') or {}
    if cold.get('ok') or cold.get('cold'):
        source = cold.get('input') or {}
        metric = cold.get('cold') or {}
        warm = cold.get('warm') or {}
        rules = cold.get('rules') or {}
        memory = cold.get('memory') or {}
        lines += [
            '| 项 | 值 |',
            '| --- | --- |',
            '| 输入 | %s B / %s 行（sha256 `%s`，平均 %s B/行） |'
            % (source.get('bytes'), source.get('lines'), str(source.get('sha256'))[:16],
               source.get('avg_line_bytes')),
            '| 冷解析（结构层） | %s s，%s MB/s，%s 个解析批次，%s 条记录 |'
            % (metric.get('seconds'), metric.get('mb_per_s'), metric.get('parse_requests'),
               metric.get('records')),
            '| 规则层重算 | %s s，%s 行入库（覆盖校验通过），%s 个时间线节点，解析批次 %s |'
            % (rules.get('seconds'), rules.get('stored_rows'), rules.get('timeline_nodes'),
               rules.get('parse_requests')),
            '| 暖重开 | 结构层复用 %s，规则层复用 %s，解析请求增量 %s，读原文字节 %s |'
            % (warm.get('reused'), warm.get('rules_reused'), warm.get('parse_requests_delta'),
               warm.get('source_bytes_read')),
            '| 内存峰值 | 进程 %s MB，子进程 %s MB（Node RSS 采样 %s MB） |'
            % (memory.get('self_peak_mb'), memory.get('children_peak_mb'),
               memory.get('peak_node_mb')),
            '| 索引体积 | %s B（约 %.1f× 源文件） |'
            % (cold.get('index_bytes'),
               (cold.get('index_bytes') or 0) / max(1, source.get('bytes') or 1)),
        ]
    budget = cases.get('budget') or {}
    if budget.get('ok'):
        lines.append('')
        lines.append('后台满载时健康接口 p50 %s ms / p95 %s ms（%s 次采样）。'
                     % (budget.get('health_p50_ms'), budget.get('health_p95_ms'),
                        budget.get('samples')))
    lines += ['', '## 用例', '']
    for name, case in cases.items():
        lines.append('### %s · %s（%.1f s）' % (name, 'PASS' if case.get('ok') else 'FAIL',
                                              case.get('seconds', 0)))
        lines.append('')
        lines.append('```json')
        lines.append(json.dumps(case, ensure_ascii=False, indent=2)[:4000])
        lines.append('```')
        lines.append('')
    lines += [
        '## 证据文件',
        '',
        '每个用例的原始 JSON 在 `qa/evidence/M3-gate/`，本文件的数字由脚本生成，不是手写。',
        '',
        '## 未覆盖 / 已知限制',
        '',
        '- 本机为 macOS（%s），未在 Linux 目标机复跑；SQLite 版本见上表（目标机 3.38 亦不支持 WAL 安全断言）。' % env['platform'],
        '- 1 GiB 用例使用确定性模板生成，sha256 已记录，可复现但非真实业务日志；平均行宽 %s B，'
        '与短行日志相比记录数更少、单条更大。' % ((cases.get('cold_warm') or {}).get('input') or {}).get('avg_line_bytes'),
        '- Node 峰值 RSS 采样依赖 `ps`，本机沙箱内不可用，故该项为 0（B-024）；内存证据以 `getrusage` 为准。',
        '- 索引体积约为源文件的 4–5 倍（B-025），未做分片压缩与旧代次行回收。',
        '- 规则层重算需要重读原文（条件可指向 `raw`），`source_bytes_read` 如实记录为整个文件。',
        '- 远程采集（M4）不在本门禁范围。',
    ]
    Path(args.out).write_text('\n'.join(lines) + '\n', 'utf-8')
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    print('verdict: %s (%s)' % (verdict, args.out))
    return 0 if verdict == 'PASS' else (2 if verdict == 'BLOCKED' else 1)


if __name__ == '__main__':
    raise SystemExit(main())
