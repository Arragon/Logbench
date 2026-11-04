#!/usr/bin/env python3
"""R6 fixture generator: deterministic golden corpus and large-scale log files.

Standard library only, Python 3.9.11 compatible.

Two presets:
  golden  small hand-shaped files that pin R5 parser edge cases. Byte-stable.
  scale   streamed N x MiB files for performance work. Never fully resident.

Determinism contract: same preset + seed + sizes produce byte-identical files and
a byte-identical manifest. No timestamps are written unless --stamp is passed.

Usage:
  python3.9 tools/generate_log_fixture.py --preset golden --out tests/fixtures/r6/golden
  python3.9 tools/generate_log_fixture.py --preset scale --files 5 --mib 20 --out /tmp/r6-scale
  python3.9 tools/generate_log_fixture.py --preset scale --files 1 --mib 1024 --out /tmp/r6-1g
"""
from __future__ import annotations
import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = 'logbench.r6.fixture-manifest/1'
DEFAULT_SEED = 20261102
BASE_TIME = datetime(2020, 1, 1, 11, 39, 15)
LEVELS = ('EVENT', 'EVENT', 'EVENT', 'EVENT', 'WARNING', 'ERROR', 'FATAL', 'DEBUG')
LOGGERS = ('APP-A', 'APP-B', 'APP-C', 'CTRL-A')
FUNCS = ('initialize()', 'get_setpoint()', 'optimize()', 'set_recipe()', 'get_camera_frame()',
         'best_index()', 'flush_buffer()', 'health_check()')
HEADER = '[{ts}] [{level}] [{logger}] [{pid}] [{tid}] [{src}:{line}] [100] [0:0:0] {fn} <TAG_A> {msg}'


def stamp(seconds: float, digits: int = 9) -> str:
    """BASE_TIME + seconds, rendered with fixed fractional digits (7/8/9 supported)."""
    whole = int(seconds)
    frac = seconds - whole
    moment = BASE_TIME + timedelta(seconds=whole)
    scale = 10 ** digits
    micro = int(round(frac * scale))
    if micro >= scale:
        micro = scale - 1
    return moment.strftime('%Y-%m-%d %H:%M:%S') + '.' + str(micro).rjust(digits, '0')


def header(ts: str, level: str, logger: str, pid: str, tid: str, src: str, line: int, fn: str,
           msg: str, tag: bool = True) -> str:
    text = HEADER.format(ts=ts, level=level, logger=logger, pid=pid, tid=tid, src=src,
                         line=line, fn=fn, msg=msg)
    if not tag:
        text = text.replace(' <TAG_A>', '')
    return text


# ---------------------------------------------------------------- golden preset

def golden_files() -> list:
    """(name, bytes, notes) triples. Bytes are written verbatim so the corpus is reviewable."""
    files = []

    # 1. header and value edge cases; intentionally mixes 7/8/9 digit fractions,
    #    a missing tag, duplicate keys, zero/false/null/empty values and a 19-digit integer.
    rows = [
        header(stamp(0.1234567, 7), 'EVENT', 'APP-A', '1001', '1041', 'algorithm.py', 1241, 'get_setpoint()',
               'GLOBAL_Delay: 3000ns energy: 9.430 converged: false'),
        header(stamp(0.12345678, 8), 'EVENT', 'APP-A', '1001', '1041', 'recipe.py', 140, 'set_recipe()',
               'recipe: axis_01=100.0, axis_02=200.5 key: first key: second'),
        header(stamp(0.123456789, 9), 'EVENT', 'APP-A', '1001', '1041', 'acquisition.py', 188, 'get_camera_frame()',
               'frame_id: 12004 exposure_us: 100 status: ready', tag=False),
        header(stamp(1.1, 9), 'WARNING', 'APP-B', '2002', '2042', 'device.py', 166, 'set_setpoint_now()',
               'retry: 0 enabled: false payload: null note: '),
        header(stamp(1.2, 9), 'ERROR', 'APP-B', '2002', '2042', 'rpc.py', 88, 'flush_buffer()',
               'rpc_success: False err_code: 0x0 detail: 中文全角：值，后续'),
        header(stamp(2.3, 9), 'EVENT', 'CTRL-A', '3003', '3003', 'counter.py', 12, 'health_check()',
               'burstStartTime: 1234567890123456789 burstEndTime: 1234567890173456789'),
        header(stamp(3.4, 9), 'EVENT', 'APP-C', '4004', '4044', 'buffer.py', 77, 'flush_buffer()',
               'depth: 0 ratio: 0.0 flag: true name: ""'),
        '',
        # A line without a timestamp must stay as unparsed text, not be glued to the record above.
        'orphan line without any bracket header',
        header(stamp(4.5, 9), 'DEBUG', 'APP-C', '4004', '4044', 'buffer.py', 78, 'flush_buffer()',
               'after orphan: ok'),
        # Descending time is legal input; parser must keep physical order.
        header(stamp(3.0, 9), 'EVENT', 'APP-A', '1001', '1041', 'algorithm.py', 947, 'optimize()',
               'iteration: 9 energy: 9.100 converged: true'),
        # Duplicate timestamp with a different record body.
        header(stamp(3.0, 9), 'ERROR', 'APP-A', '1001', '1041', 'algorithm.py', 948, 'optimize()',
               'iteration: 9 err_code: 5 rpc_success: false'),
    ]
    files.append(('edge_cases.log', '\n'.join(rows) + '\n',
                  'fraction digits, missing tag, duplicate keys, 0/false/null/empty, 19-digit int, '
                  'orphan line, descending and duplicate timestamps'))

    # 2. Traceback shapes: cause chain, context chain, prefixed and per-line stamped frames,
    #    truncation (no terminal exception) and ExceptionGroup.
    rows = [
        header(stamp(10.0), 'ERROR', 'APP-A', '1001', '1041', 'runner.py', 50, 'run()',
               'job failed -> module: demo'),
        'Traceback (most recent call last):',
        '  File "/opt/APP-A/runner.py", line 47, in run',
        '    return worker.execute()',
        '  File "/opt/APP-A/worker.py", line 88, in execute',
        '    raise ValueError("bad recipe")',
        'ValueError: bad recipe',
        '',
        'The above exception was the direct cause of the following exception:',
        '',
        'Traceback (most recent call last):',
        '  File "/opt/APP-A/job.py", line 12, in main',
        '    run()',
        'RuntimeError: wrapper failed',
        header(stamp(11.0), 'ERROR', 'APP-B', '2002', '2042', 'runner.py', 51, 'run()',
               '| Traceback (most recent call last):'),
        header(stamp(11.1), 'ERROR', 'APP-B', '2002', '2042', 'runner.py', 52, 'run()',
               '|   File "/opt/APP-B/x.py", line 3, in <module>'),
        header(stamp(11.2), 'ERROR', 'APP-B', '2002', '2042', 'runner.py', 53, 'run()',
               '| KeyError: \'missing\''),
        header(stamp(12.0), 'ERROR', 'APP-C', '4004', '4044', 'trunc.py', 9, 'flush_buffer()',
               'truncated traceback follows'),
        'Traceback (most recent call last):',
        '  File "/opt/APP-C/trunc.py", line 8, in flush_buffer',
        '    raise OSError("no terminal line")',
        header(stamp(13.0), 'ERROR', 'CTRL-A', '3003', '3003', 'group.py', 21, 'health_check()',
               'ExceptionGroup raised'),
        '  + Exception Group Traceback (most recent call last):',
        '  |   File "/opt/CTRL-A/group.py", line 20, in health_check',
        '  | ExceptionGroup: grouped failures (2 sub-exceptions)',
        '  +-+---------------- 1 ----------------',
        '    | ValueError: first',
        '    +---------------- 2 ----------------',
        '    | KeyError: second',
        '    +------------------------------------',
    ]
    files.append(('traceback_chain.log', '\n'.join(rows) + '\n',
                  'cause/context chain, per-line stamped prefix, truncated traceback without terminal '
                  'line, ExceptionGroup body preserved raw'))

    # 3. BOM + CRLF + Chinese full width, written with real CRLF bytes.
    rows = [
        header(stamp(20.0), 'EVENT', 'APP-A', '1001', '1041', '中文模块.py', 7, 'initialize()',
               '参数：轴1＝100.0，状态：就绪'),
        header(stamp(20.5), 'WARNING', 'APP-A', '1001', '1041', '中文模块.py', 8, 'initialize()',
               '告警：温度偏高（６５℃）'),
        header(stamp(21.0), 'EVENT', 'APP-A', '1001', '1041', '中文模块.py', 9, 'initialize()',
               'Ünïcode: ÄÖÜ ß ﾊﾝｶｸ'),
    ]
    body = '\r\n'.join(rows) + '\r\n'
    files.append(('unicode_crlf_bom.log', '\ufeff' + body,
                  'UTF-8 BOM, CRLF line endings, full-width punctuation, mixed Latin diacritics'))

    # 4. Multi-line body with no trailing newline at end of file.
    rows = [
        header(stamp(30.0), 'EVENT', 'APP-A', '1001', '1041', 'algorithm.py', 1284, 'best_index()',
               'burstEuvEnergyMean: 9.414220809936523'),
        'burstEuvEnergy3sigma: 0.21338501572608948',
        'burstDropletCount: 420',
        'isValid: true',
        '',
        header(stamp(31.0), 'EVENT', 'APP-A', '1001', '1041', 'algorithm.py', 1285, 'best_index()',
               'best_index_mask: 0'),
    ]
    files.append(('no_trailing_newline.log', '\n'.join(rows),
                  'multi-line continuation body, blank continuation line, file ends without newline'))

    return files


# ---------------------------------------------------------------- scale preset

MESSAGE_POOL = (
    'iteration: {i} energy: {e:.3f} converged: false',
    'frame_id: {i} exposure_us: 100 status: ready',
    'GLOBAL_Delay: 3000ns setpoint: {e:.2f}',
    'depth: {i} ratio: {r:.4f} flag: true',
    'rpc_success: false err_code: {i} detail: 调用失败，重试中',
    'burstDropletCount: {i} isValid: true',
    'cache: hit={i} miss=0 evicted: null',
)


def write_scale_file(path: Path, target_bytes: int, seed: int, index: int, digits: int = 9) -> dict:
    rng = random.Random('%s-%s' % (seed, index))
    digest = hashlib.sha256()
    written = 0
    records = 0
    physical = 0
    first_key = None
    last_key = None
    seconds = 0.0
    with path.open('wb') as handle:
        while written < target_bytes:
            seconds += rng.choice((0.00000037, 0.00000041, 0.001, 0.25, 1.0, 1.5))
            ts = stamp(seconds, digits)
            level = rng.choice(LEVELS)
            logger = rng.choice(LOGGERS)
            fn = rng.choice(FUNCS)
            msg = rng.choice(MESSAGE_POOL).format(i=records, e=rng.uniform(8.0, 10.0), r=rng.uniform(0, 1))
            line = header(ts, level, logger, str(1000 + index), str(1040 + index), 'scale.py',
                          records + 1, fn, msg)
            tail = ''
            extra_lines = 0
            if records % 5000 == 4999:
                tail = ('\nTraceback (most recent call last):\n'
                        '  File "/opt/scale/%d.py", line %d, in %s\n'
                        '    raise RuntimeError("periodic")\n'
                        'RuntimeError: periodic') % (index, records % 900 + 1, fn[:-2])
                extra_lines = 4
            payload = (line + tail + '\n').encode('utf-8')
            handle.write(payload)
            digest.update(payload)
            written += len(payload)
            physical += 1 + extra_lines
            records += 1
            if first_key is None:
                first_key = ts
            last_key = ts
    return {'name': path.name, 'bytes': written, 'sha256': digest.hexdigest(), 'records': records,
            'physical_lines': physical, 'first_key': first_key, 'last_key': last_key,
            'target_bytes': target_bytes}


# ---------------------------------------------------------------- manifest

def build_manifest(preset: str, seed: int, entries: list, extra: dict) -> dict:
    totals = {'files': len(entries), 'bytes': sum(e['bytes'] for e in entries),
              'records': sum(e['records'] for e in entries),
              'physical_lines': sum(e['physical_lines'] for e in entries)}
    manifest = {'schema': SCHEMA, 'generator': 'tools/generate_log_fixture.py', 'generator_version': 1,
                'preset': preset, 'seed': seed, 'files': entries, 'totals': totals}
    manifest.update(extra)
    return manifest


def dump(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def count_golden(raw: bytes) -> dict:
    text = raw.decode('utf-8-sig')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    records = sum(1 for line in lines if line.startswith('['))
    keys = [line[1:line.index(']')] for line in lines if line.startswith('[')]
    return {'records': records, 'physical_lines': len(lines),
            'first_key': keys[0] if keys else None, 'last_key': keys[-1] if keys else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Generate R6 log fixtures')
    parser.add_argument('--preset', choices=('golden', 'scale'), default='golden')
    parser.add_argument('--out', required=True)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--files', type=int, default=5)
    parser.add_argument('--mib', type=int, default=20)
    parser.add_argument('--digits', type=int, default=9, choices=(7, 8, 9))
    parser.add_argument('--stamp', action='store_true', help='include generation timestamp (breaks byte determinism)')
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    if args.preset == 'golden':
        for name, text, notes in golden_files():
            raw = text.encode('utf-8')
            path = out / name
            path.write_bytes(raw)
            entry = {'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                     'notes': notes}
            entry.update(count_golden(raw))
            entries.append(entry)
        extra = {'rule_profile': 'cpqpsgx-default', 'format': 'bracket'}
    else:
        for index in range(args.files):
            path = out / ('scale_%02d_%02dMiB.log' % (index + 1, args.mib))
            entry = write_scale_file(path, args.mib * 1024 * 1024, args.seed, index, args.digits)
            entry['notes'] = 'streamed synthetic records; traceback every 5000 records'
            entries.append(entry)
        extra = {'rule_profile': 'cpqpsgx-default', 'format': 'bracket', 'digits': args.digits}

    manifest = build_manifest(args.preset, args.seed, entries, extra)
    if args.stamp:
        from datetime import datetime, timezone
        manifest['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    dump(out / 'manifest.json', manifest)
    totals = manifest['totals']
    print('%s: %d files, %.1f MiB, %d records, manifest=%s'
          % (args.preset, totals['files'], totals['bytes'] / 1048576.0, totals['records'],
             out / 'manifest.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
