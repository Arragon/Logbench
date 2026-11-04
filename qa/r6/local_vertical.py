#!/usr/bin/env python3
"""R6-031: the M2 vertical slice, measured in a real browser at LAN address.

What this script refuses to do: report a target as met when it was not. Every
number below comes from `performance.now()` inside the page, and a missed target
is written into `report['targets']` as `met: false` rather than smoothed away.

    env -u PYTHONPATH python3.9 qa/r6/local_vertical.py --out qa/evidence/M2-gate
    env -u PYTHONPATH python3.9 qa/r6/local_vertical.py --big     # 10x100 MiB

Phases:
    1  import 5x20 MiB: panel latency, first readable batch, total time
    2  search 30 runs: zero / high / low hits, CJK, whole word, cross-line, OR
    3  reader: 40k jump, DOM count, drawer geometry, anchor, four themes
    4  (--big) 10x100 MiB import and index size
    5  reload: index reused, preferences restored
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def lan_ip() -> str:
    for interface in ('en0', 'en1', 'en2'):
        try:
            done = subprocess.run(['ipconfig', 'getifaddr', interface],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
            value = done.stdout.decode().strip()
            if value:
                return value
        except (OSError, subprocess.SubprocessError):
            continue
    return '127.0.0.1'


def start_server():
    from server.app import Application, Config
    from server.httpd import Server
    tmp = tempfile.mkdtemp(prefix='lb-m2-')
    data = Path(tmp) / 'data'
    state = Path(tmp) / 'var'
    data.mkdir()
    state.mkdir()
    ip = lan_ip()
    config = Config(ROOT, data_dir=data, state_dir=state, code_root=ROOT,
                    allowed_hosts=[ip, 'localhost', '127.0.0.1'], bootstrap_admins=[ip])
    server = Server(('0.0.0.0', 0), Application(config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], tmp


JS_IMPORT = r"""
async (paths) => {
  // Reading the bytes from the server stands in for the file picker: a local
  // selection has no network cost, so the timing that matters starts at the
  // moment the page hands File objects to the queue.
  const blobs = [];
  for (const path of paths) {
    const res = await fetch(path);
    const buf = await res.arrayBuffer();
    blobs.push(new File([buf], path.split('/').pop(), { lastModified: 1767225600000 }));
  }
  // Exactly what the change handler does, with the same synchronous calls.
  const t0 = performance.now();
  const tasks = window.__r6.queue.add(blobs);
  window.__r6.panel.onFilesChosen(tasks.length);
  const registered = performance.now() - t0;
  // First paint of the queue, not just the state change.
  const painted = await new Promise(resolve => requestAnimationFrame(() => resolve(performance.now() - t0)));
  window.__t0 = performance.now();
  return { register_ms: registered, first_paint_ms: painted, tasks: tasks.length };
}
"""


def summarise(values):
    if not values:
        return {'n': 0, 'p50': None, 'p95': None, 'max': None}
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return {
        'n': len(values),
        'p50': round(statistics.median(ordered), 1),
        'p95': round(ordered[index], 1),
        'max': round(ordered[-1], 1),
        'min': round(ordered[0], 1),
    }


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ['LOGBENCH_DEV_ASSETS'] = '1'
    server, port, tmp = start_server()
    host = lan_ip() if args.lan else '127.0.0.1'
    base = 'http://%s:%d' % (host, port)
    report = {
        'url': base,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'phases': {},
        'targets': {},
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 1366, 'height': 768})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/r6', wait_until='load', timeout=30000)
            page.wait_for_function('() => window.__r6Ready === true', timeout=30000)
            report['secure_context'] = page.evaluate('() => window.isSecureContext')
            report['user_agent'] = page.evaluate('() => navigator.userAgent')
            report['hardware_concurrency'] = page.evaluate('() => navigator.hardwareConcurrency')

            # ---------------------------------------------------------- phase 1
            files = sorted((ROOT / 'tests/fixtures/r6/scale').glob('*.log'))
            paths = ['/fixtures/' + file.name for file in files]
            page.route('**/fixtures/*', lambda route: route.fulfill(
                path=str(ROOT / 'tests/fixtures/r6/scale' / route.request.url.split('/')[-1])))
            launch = page.evaluate(JS_IMPORT, paths)
            page.wait_for_selector('#jobs:not([hidden])', timeout=5000)
            report['phases']['import_5x20'] = {
                'panel_visible_ms': round(launch['first_paint_ms'], 1),
                'register_ms': round(launch['register_ms'], 1),
                'files': launch['tasks'],
            }
            page.wait_for_function(
                "() => window.__r6.queue.tasks().some(t => t.phase === 'indexed')", timeout=180000)
            first_ms = page.evaluate('() => performance.now() - window.__t0')
            page.wait_for_function(
                "() => window.__r6.queue.tasks().every(t => ['indexed','failed','cancelled'].includes(t.phase))",
                timeout=600000)
            total_s = page.evaluate('() => (performance.now() - window.__t0) / 1000')
            tasks = page.evaluate("() => window.__r6.queue.tasks().map(t => ({phase: t.phase, records: t.records, bytes: t.bytes_done}))")
            report['phases']['import_5x20'].update({
                'first_readable_ms': round(first_ms, 1),
                'total_s': round(total_s, 2),
                'records': sum(task['records'] for task in tasks),
                'files_indexed': sum(1 for task in tasks if task['phase'] == 'indexed'),
                'files_failed': sum(1 for task in tasks if task['phase'] == 'failed'),
            })

            # ---------------------------------------------------------- phase 2
            page.evaluate("""async () => {
              const files = (await window.__r6.provider.listFiles()).files;
              const sorted = files.slice().sort((a, b) => b.size - a.size || a.name.localeCompare(b.name));
              await window.__r6.openFile(sorted[0]);
            }""")
            queries = [
                ('high', 'energy'), ('high2', 'worker'), ('low', 'periodic'),
                ('zero', 'zzz_absent_token'), ('cjk2', '设备'), ('cjk1', '电'),
                ('whole', 'run()'), ('crossline', '^$'), ('literal_pct', '100%')
            ]
            runs = []
            for index in range(max(30, args.search_runs)):
                label, text = queries[index % len(queries)]
                spec = {'text': text, 'flags': {'whole_word': label == 'whole'}}
                if label == 'crossline':
                    spec = {'text': 'energy.*record', 'flags': {'regex': True}}
                result = page.evaluate("""async ({spec}) => {
                  const started = performance.now();
                  const state = window.__r6.state;
                  const r = await window.__r6.provider.search({
                    file_id: state.fileId, generation: state.generation,
                    limit: 200, projection_version: 'v1', ...spec
                  });
                  // Poll to completion the same way the controller does.
                  let current = r;
                  for (let i = 0; i < 200 && ['queued','running'].includes(current.state); i++) {
                    await new Promise(res => setTimeout(res, 25));
                    current = await window.__r6.provider.poll(r.query_id);
                  }
                  return {
                    elapsed_ms: performance.now() - started,
                    state: current.state,
                    relation: current.count ? current.count.relation : null,
                    matched: current.count ? current.count.matched : null,
                    scanned: current.coverage ? current.coverage.scanned_blocks : null,
                    total_blocks: current.coverage ? current.coverage.total_blocks : null
                  };
                }""", {'spec': spec})
                result['label'] = label
                result['query'] = text
                runs.append(result)
            elapsed = [run['elapsed_ms'] for run in runs]
            report['phases']['search'] = {
                'runs': len(runs),
                'latency_ms': summarise(elapsed),
                'by_label': {},
                'zero_reported_without_full_scan': [
                    run for run in runs
                    if run['relation'] == 'exact' and run['matched'] == 0 and
                    run['scanned'] != run['total_blocks']
                ],
            }
            for run in runs:
                bucket = report['phases']['search']['by_label'].setdefault(
                    run['label'], {'n': 0, 'latency_ms': [], 'relations': {}})
                bucket['n'] += 1
                bucket['latency_ms'].append(round(run['elapsed_ms'], 1))
                bucket['relations'][run['relation']] = bucket['relations'].get(run['relation'], 0) + 1
            for bucket in report['phases']['search']['by_label'].values():
                bucket['latency_ms'] = summarise(bucket['latency_ms'])

            # ---------------------------------------------------------- phase 3
            page.wait_for_selector('.r6-row', timeout=60000)
            page.wait_for_function(
                "() => document.getElementById('spacer').style.height !== ''", timeout=30000)
            page.select_option('#page-size', '50000')
            page.wait_for_timeout(200)
            page.evaluate("() => { const r = document.getElementById('reader'); r.scrollTop = 40000 * 28; }")
            page.wait_for_timeout(1200)
            geometry = page.evaluate("""() => {
              const pick = () => {
                const rows = [...document.querySelectorAll('.r6-row')];
                const row = rows.find(el => el.getBoundingClientRect().top >= 0);
                return row ? { ordinal: row.dataset.ordinal, top: Math.round(row.getBoundingClientRect().top) } : null;
              };
              window.__probe = pick;
              const box = selector => {
                const el = document.querySelector(selector);
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
              };
              return {
                rows: document.querySelectorAll('.r6-row').length,
                reader: box('#reader'),
                timeline: box('#timeline'),
                first_ordinal: document.querySelector('.r6-row') ? document.querySelector('.r6-row').dataset.ordinal : null,
                first_top: pick() ? pick().top : null,
                probe: pick(),
                scroll_top: Math.round(document.getElementById('reader').scrollTop)
              };
            }""")
            # Click the exact row by identity: a positional click can scroll it
            # into view first, which would move the very anchor being measured.
            target_ordinal = geometry['probe']['ordinal'] if geometry.get('probe') else None
            page.evaluate("(ordinal) => document.querySelector('[data-ordinal=\"' + ordinal + '\"]').click()",
                          target_ordinal)
            page.wait_for_selector('#drawer:not([hidden])', timeout=5000)
            page.wait_for_timeout(300)
            page.evaluate("(ordinal) => { window.__target = ordinal; }", target_ordinal)
            after = page.evaluate("""() => {
              const box = selector => {
                const el = document.querySelector(selector);
                const rect = el.getBoundingClientRect();
                return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
              };
              const row = document.querySelector('[data-ordinal="' + window.__target + '"]');
              return {
                reader: box('#reader'), timeline: box('#timeline'),
                drawer: box('#drawer'),
                first_ordinal: row ? row.dataset.ordinal : null,
                first_top: row ? Math.round(row.getBoundingClientRect().top) : null,
                scroll_top: Math.round(document.getElementById('reader').scrollTop),
                rows: document.querySelectorAll('.r6-row').length
              };
            }""")
            page.keyboard.press('Escape')
            report['phases']['reader'] = {
                'jump': geometry,
                'drawer_open': after,
                'reader_delta': {
                    'x': after['reader']['x'] - geometry['reader']['x'],
                    'w': after['reader']['w'] - geometry['reader']['w'],
                    'h': after['reader']['h'] - geometry['reader']['h'],
                },
                'timeline_delta': {
                    'x': after['timeline']['x'] - geometry['timeline']['x'],
                    'w': after['timeline']['w'] - geometry['timeline']['w'],
                },
                'anchor_shift_px': (abs(after['first_top'] - geometry['probe']['top'])
                                    if geometry.get('probe') and after['first_ordinal'] == geometry['probe']['ordinal']
                                    else None),
                'anchor_same_record': bool(geometry.get('probe')) and
                                      after['first_ordinal'] == geometry['probe']['ordinal'],
                'anchor_ordinal': geometry.get('probe', {}).get('ordinal'),
            }

            themes = {}
            for theme in ('ink', 'paper', 'graphite', 'nord'):
                page.select_option('#theme', theme)
                page.wait_for_timeout(200)
                shot = out / ('theme-%s.png' % theme)
                page.screenshot(path=str(shot))
                themes[theme] = page.evaluate("""() => {
                  // Measure inside the token scope: body sits outside .ud-app and
                  // would report the same colour for every theme.
                  const app = document.getElementById('app');
                  const row = document.querySelector('.r6-row');
                  const style = getComputedStyle(app);
                  return {
                    theme: app.dataset.theme,
                    ink: style.color, bg: style.backgroundColor,
                    row_color: row ? getComputedStyle(row).color : null,
                    reader_bg: getComputedStyle(document.getElementById('reader')).backgroundColor,
                    row_visible: !!row
                  };
                }""")
                themes[theme]['screenshot'] = shot.name
            report['phases']['themes'] = themes

            # ---------------------------------------------------------- phase 4
            if args.big:
                big_dir = Path('/tmp/r6-big')
                if not big_dir.exists():
                    subprocess.run([sys.executable, str(ROOT / 'tools/generate_log_fixture.py'),
                                    '--preset', 'scale', '--files', '10', '--mib', '100',
                                    '--out', str(big_dir)], check=True)
                big_files = sorted(big_dir.glob('*.log'))
                before = time.time()
                # The file picker is the honest path for 100 MiB files: Chrome hands
                # the page Blob handles backed by disk, so the harness never holds
                # 1 GiB in ArrayBuffers (which is what crashed the first attempt).
                page.set_input_files('#file-input', [str(file) for file in big_files])
                page.wait_for_function(
                    "() => window.__r6.queue.tasks().length >= %d" % (len(big_files) + 5), timeout=60000)
                page.wait_for_function(
                    "() => window.__r6.queue.tasks().every(t => ['indexed','failed','cancelled'].includes(t.phase))",
                    timeout=3600000)
                report['phases']['import_10x100'] = {
                    'total_s': round(time.time() - before, 2),
                    'files': len(big_files),
                    'phases': page.evaluate(
                        "() => window.__r6.queue.tasks().map(t => ({phase: t.phase, records: t.records, error: t.error}))"),
                }

            # ---------------------------------------------------------- phase 5
            page.reload(wait_until='load')
            page.wait_for_function('() => window.__r6Ready === true', timeout=30000)
            reload_state = page.evaluate("""async () => {
              const files = await window.__r6.provider.listFiles();
              return {
                files: files.files.length,
                theme: document.documentElement.dataset.theme,
                page_size: document.getElementById('page-size').value,
                tasks: window.__r6.queue.tasks().length
              };
            }""")
            report['phases']['reload'] = reload_state
            report['page_errors'] = errors
            browser.close()
    except Exception as exc:  # noqa: BLE001 - evidence must survive a browser crash
        report['fatal'] = '%s: %s' % (type(exc).__name__, exc)
        print('FATAL', report['fatal'])
    finally:
        server.shutdown()
        server.server_close()

    # ---------------------------------------------------------------- targets
    imported = report['phases'].get('import_5x20', {})
    search_phase = report['phases'].get('search', {})
    reader_phase = report['phases'].get('reader', {})
    reload_state = report['phases'].get('reload', {})

    report['targets'] = {
        'select_feedback_100ms': {
            'target': 100,
            'actual': imported.get('panel_visible_ms'),
            'met': (imported.get('panel_visible_ms') or 1e9) <= 100,
        },
        'first_batch_3000ms': {
            'target': 3000,
            'actual': imported.get('first_readable_ms'),
            'met': (imported.get('first_readable_ms') or 1e9) <= 3000,
        },
        'search_p95_1800ms': {
            'target': 1800,
            'actual': search_phase.get('latency_ms', {}).get('p95'),
            'met': (search_phase.get('latency_ms', {}).get('p95') or 1e9) <= 1800,
        },
        'dom_under_300': {
            'target': 300,
            'actual': reader_phase.get('jump', {}).get('rows'),
            'met': (reader_phase.get('jump', {}).get('rows') or 1e9) < 300,
        },
        'drawer_no_layout_shift': {
            'target': 0,
            'actual': reader_phase.get('reader_delta'),
            'met': reader_phase.get('reader_delta', {}).get('w') == 0 and
                   reader_phase.get('timeline_delta', {}).get('w') == 0,
        },
        'anchor_within_2px': {
            'target': 2,
            'actual': reader_phase.get('anchor_shift_px'),
            'met': reader_phase.get('anchor_shift_px') is not None and
                   reader_phase.get('anchor_shift_px') <= 2,
        },
        'reload_reuses_index': {
            'target': 5,
            'actual': reload_state.get('files'),
            'met': (reload_state.get('files') or 0) >= 5 and reload_state.get('tasks') == 0,
        },
        'no_false_zero': {
            'target': 0,
            'actual': len(search_phase.get('zero_reported_without_full_scan', [])),
            'met': len(search_phase.get('zero_reported_without_full_scan', [])) == 0,
        },
    }

    (out / 'local-vertical.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    for name, target in report['targets'].items():
        print('%-28s target=%-6s actual=%-22s %s'
              % (name, target['target'], target['actual'], 'PASS' if target['met'] else 'MISS'))
    print('errors:', report.get('page_errors'))
    print('evidence:', out / 'local-vertical.json')
    return 0 if all(item['met'] for item in report['targets'].values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/M2-gate'))
    parser.add_argument('--lan', action='store_true', default=True)
    parser.add_argument('--search-runs', type=int, default=30)
    parser.add_argument('--big', action='store_true', help='额外导入 10×100 MiB')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
