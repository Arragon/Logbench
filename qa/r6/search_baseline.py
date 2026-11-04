#!/usr/bin/env python3
"""R6-006: measure the real v3.0.3 import and search path in a real browser.

What this does *not* do: it never edits the served bundle, never raises the
1.8s budget and never treats a Node micro-benchmark as a browser result. It
observes the running page from the outside by wrapping four primitives that the
production code already calls:

* ``structuredClone``  -> the "clone the whole record set" cost
* ``JSON.stringify``   -> the postMessage payload size
* ``new Worker``       -> how many workers one search creates
* ``TextDecoder.decode`` -> how much of the wall time is decoding

Long tasks come from ``PerformanceObserver({entryTypes:['longtask']})``.

Usage::

    env -u PYTHONPATH python3.9 qa/r6/search_baseline.py \
        --base-url http://127.0.0.1:50902 \
        --files tests/fixtures/r6/scale/*.log --out qa/evidence/R6-006
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

INIT_SCRIPT = r"""
(() => {
  const state = {
    worker_created: 0,
    clone_calls: 0, clone_ms: 0,
    post_calls: 0, post_ms: 0, post_records: 0, post_chars_estimate: 0,
    stringify_calls: 0, stringify_chars: 0, stringify_ms: 0,
    decode_calls: 0, decode_ms: 0, decode_bytes: 0,
    long_tasks: [], started: performance.now()
  };
  window.__lbProbe = state;
  const now = () => performance.now();

  const realClone = window.structuredClone;
  if (typeof realClone === 'function') {
    window.structuredClone = function (value, options) {
      const t0 = now();
      try { return realClone.call(this, value, options); }
      finally { state.clone_calls += 1; state.clone_ms += now() - t0; }
    };
  }

  const realStringify = JSON.stringify;
  JSON.stringify = function (value, replacer, space) {
    const t0 = now();
    try {
      const out = realStringify.call(this, value, replacer, space);
      state.stringify_calls += 1;
      if (typeof out === 'string') state.stringify_chars += out.length;
      return out;
    } finally { state.stringify_ms += now() - t0; }
  };

  const RealWorker = window.Worker;
  if (typeof RealWorker === 'function') {
    const Wrapped = function (url, options) {
      state.worker_created += 1;
      return new RealWorker(url, options);
    };
    Wrapped.prototype = RealWorker.prototype;
    window.Worker = Wrapped;
  }

  /* postMessage performs a synchronous structured clone on the main thread.
     That is where "copy 130k records" actually shows up, not in window.structuredClone. */
  const realPost = RealWorker && RealWorker.prototype && RealWorker.prototype.postMessage;
  if (typeof realPost === 'function') {
    RealWorker.prototype.postMessage = function (message, transfer) {
      const t0 = now();
      try { return realPost.call(this, message, transfer); }
      finally {
        state.post_calls += 1;
        state.post_ms += now() - t0;
        const records = message && message.records;
        if (Array.isArray(records)) {
          state.post_records += records.length;
          const sample = Math.min(records.length, 200);
          let chars = 0;
          for (let i = 0; i < sample; i++) {
            const record = records[i] || {};
            chars += (record.message ? record.message.length : 0) + (record.body ? record.body.length : 0);
          }
          state.post_chars_estimate += records.length * (sample ? chars / sample : 0);
        }
      }
    };
  }

  const realDecode = TextDecoder.prototype.decode;
  TextDecoder.prototype.decode = function (input, options) {
    const t0 = now();
    try { return realDecode.call(this, input, options); }
    finally {
      state.decode_calls += 1;
      state.decode_ms += now() - t0;
      if (input && typeof input.byteLength === 'number') state.decode_bytes += input.byteLength;
    }
  };

  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) state.long_tasks.push(Math.round(entry.duration));
    }).observe({ entryTypes: ['longtask'] });
  } catch (error) { state.long_tasks_unsupported = true; }

  window.__lbSnapshot = () => ({
    worker_created: state.worker_created,
    clone_calls: state.clone_calls,
    clone_ms: Math.round(state.clone_ms * 10) / 10,
    post_calls: state.post_calls,
    post_ms: Math.round(state.post_ms * 10) / 10,
    post_records: state.post_records,
    post_chars_estimate: Math.round(state.post_chars_estimate),
    stringify_calls: state.stringify_calls,
    stringify_chars: state.stringify_chars,
    stringify_ms: Math.round(state.stringify_ms * 10) / 10,
    decode_calls: state.decode_calls,
    decode_ms: Math.round(state.decode_ms * 10) / 10,
    decode_bytes: state.decode_bytes,
    long_task_count: state.long_tasks.length,
    long_task_max_ms: state.long_tasks.length ? Math.max(...state.long_tasks) : 0,
    long_task_total_ms: state.long_tasks.reduce((a, b) => a + b, 0)
  });
})();
"""

DOM_PROBE = r"""
() => {
  const status = (document.querySelector('.app-status') || document.body).innerText || '';
  const foot = (document.querySelector('.page-foot') || {}).innerText || '';
  const rows = document.querySelectorAll('tr.log-row, .record-item').length;
  const table = document.querySelector('.log-table, .log-virtual');
  const match = foot.match(/([\d,]+)\s*条记录/);
  return {
    status: status.replace(/\s+/g, ' ').slice(0, 160),
    foot: foot.replace(/\s+/g, ' ').slice(0, 120),
    records: match ? parseInt(match[1].replace(/,/g, ''), 10) : null,
    mounted_rows: rows,
    scroll_height: table ? table.scrollHeight : 0,
    has_timeout_notice: /超时|timeout|过慢|失败/i.test(status + ' ' + foot)
  };
}
"""


def wait_for_change(page, previous, timeout=30.0):
    """Wait until the rendered record count actually changes from `previous`."""
    deadline = time.time() + timeout
    info = page.evaluate(DOM_PROBE)
    while time.time() < deadline:
        info = page.evaluate(DOM_PROBE)
        if info['records'] != previous:
            return info
        page.wait_for_timeout(100)
    return info


def wait_stable(page, timeout=30.0, settle=0.6):
    """Poll the rendered record count until it stops changing."""
    deadline = time.time() + timeout
    last = None
    stable_since = None
    info = page.evaluate(DOM_PROBE)
    while time.time() < deadline:
        info = page.evaluate(DOM_PROBE)
        key = (info['records'], info['mounted_rows'])
        if key == last:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= settle:
                return info
        else:
            last = key
            stable_since = None
        page.wait_for_timeout(120)
    return info


def pick_files(pattern: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in pattern:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix == '.log'))
        else:
            files.append(path)
    return files


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    files = pick_files(args.files)
    if not files:
        raise SystemExit('no fixture files matched')
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(f.stat().st_size for f in files)

    report = {
        'schema': 'logbench.search-baseline/1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'base_url': args.base_url,
        'corpus': {
            'files': len(files),
            'bytes': total_bytes,
            'mib': round(total_bytes / 1048576, 1),
            'names_redacted': True,
        },
        'environment': {},
        'import': {},
        'searches': [],
        'targets': {
            'ordinary_search_budget_ms': 1800,
            'note': '目标值来自 R5 的硬超时；实测值单列，不用于证明达标。',
        },
        'notes': [
            '本报告不含文件名、日志原文或完整搜索词。',
            'clone/stringify/decode 为浏览器内包装真实调用的累计耗时。',
        ],
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        page = browser.new_page(viewport={'width': 1366, 'height': 768})
        page.add_init_script(INIT_SCRIPT)
        page.goto(args.base_url + '/', wait_until='load')
        page.wait_for_selector('.nav-button', timeout=30000)
        page.wait_for_timeout(400)

        report['environment'] = {
            'user_agent': page.evaluate('() => navigator.userAgent'),
            'hardware_concurrency': page.evaluate('() => navigator.hardwareConcurrency'),
            'device_memory_gb': page.evaluate('() => navigator.deviceMemory || null'),
            'secure_context': page.evaluate('() => window.isSecureContext'),
            'indexeddb': page.evaluate("() => typeof indexedDB !== 'undefined'"),
            'opfs': page.evaluate("() => !!(navigator.storage && navigator.storage.getDirectory)"),
            'cache_state': 'cold (fresh browser profile per run)',
        }

        page.evaluate('() => { window.__lbProbe.clone_calls = 0; window.__lbProbe.clone_ms = 0; }')
        before = page.evaluate('() => window.__lbSnapshot()')

        started = time.time()
        page.set_input_files('#file-input', [str(f) for f in files])
        page.wait_for_selector('.record-item', timeout=int(args.import_timeout * 1000))
        dom = wait_stable(page, timeout=args.import_timeout, settle=3.0)
        import_seconds = time.time() - started
        after = page.evaluate('() => window.__lbSnapshot()')

        report['import'] = {
            'wall_ms': round(import_seconds * 1000),
            'bytes': total_bytes,
            'mib_per_second': round(total_bytes / 1048576 / max(import_seconds, 0.001), 2),
            'decode_ms': round(after['decode_ms'] - before['decode_ms'], 1),
            'clone_ms': round(after['clone_ms'] - before['clone_ms'], 1),
            'clone_calls': after['clone_calls'] - before['clone_calls'],
            'stringify_ms': round(after['stringify_ms'] - before['stringify_ms'], 1),
            'stringify_chars': after['stringify_chars'] - before['stringify_chars'],
            'worker_created': after['worker_created'] - before['worker_created'],
            'long_task_count': after['long_task_count'],
            'long_task_max_ms': after['long_task_max_ms'],
            'records': dom['records'],
            'mounted_rows': dom['mounted_rows'],
            'scroll_height': dom['scroll_height'],
            'status': dom['status'],
            'timeout_notice': dom['has_timeout_notice'],
        }

        baseline = page.evaluate(DOM_PROBE)

        for label, query in args.query:
            page.fill('#main-search', '')
            wait_stable(page, timeout=20)
            base = page.evaluate('() => window.__lbSnapshot()')
            started = time.time()
            page.type('#main-search', query, delay=15)
            typed_at = time.time()
            wait_for_change(page, baseline['records'], timeout=args.search_timeout)
            info = wait_stable(page, timeout=args.search_timeout)
            finished = time.time()
            now = page.evaluate('() => window.__lbSnapshot()')
            report['searches'].append({
                'label': label,
                'query': {'length': len(query), 'tokens': len(query.split())},
                'typing_ms': round((typed_at - started) * 1000),
                'search_ms': round((finished - typed_at) * 1000),
                'wall_ms': round((finished - started) * 1000),
                'clone_ms': round(now['clone_ms'] - base['clone_ms'], 1),
                'post_ms': round(now['post_ms'] - base['post_ms'], 1),
                'post_records': now['post_records'] - base['post_records'],
                'post_chars_estimate': now['post_chars_estimate'] - base['post_chars_estimate'],
                'clone_calls': now['clone_calls'] - base['clone_calls'],
                'stringify_ms': round(now['stringify_ms'] - base['stringify_ms'], 1),
                'stringify_chars': now['stringify_chars'] - base['stringify_chars'],
                'worker_created': now['worker_created'] - base['worker_created'],
                'long_task_max_ms': now['long_task_max_ms'],
                'records_before': baseline['records'],
                'records_after': info['records'],
                'mounted_rows': info['mounted_rows'],
                'status': info['status'],
                'timeout_notice': info['has_timeout_notice'],
                'within_1800ms': round((finished - typed_at) * 1000) <= 1800,
            })
            baseline = info

        browser.close()

    payload = json.dumps(report, ensure_ascii=False, indent=1)
    (out_dir / 'search-baseline.json').write_text(payload + '\n', 'utf-8')
    print(json.dumps({k: report[k] for k in ('corpus', 'import')}, ensure_ascii=False, indent=1))
    for item in report['searches']:
        print('search %-12s search=%6dms typing=%5dms post=%7.1fms records=%d -> %s workers=%d within1800=%s'
              % (item['label'], item['search_ms'], item['typing_ms'], item['post_ms'],
                 item['post_records'], item['records_after'], item['worker_created'],
                 item['within_1800ms']))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='R6-006 search baseline')
    parser.add_argument('--base-url', default='http://127.0.0.1:50902')
    parser.add_argument('--files', nargs='+', default=[str(ROOT / 'tests/fixtures/r6/scale')])
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-006'))
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--import-timeout', type=float, default=300.0)
    parser.add_argument('--search-timeout', type=float, default=60.0,
                        help='give up waiting for a stable result count after this many seconds')
    parser.add_argument('--query', nargs=2, action='append', metavar=('LABEL', 'TEXT'),
                        default=None,
                        help='zero|multi|few hit queries; defaults cover all three')
    args = parser.parse_args()
    if not args.query:
        args.query = [
            ['zero', 'zzz_no_such_token_9f3a'],
            ['multi', 'energy'],
            ['few', 'RuntimeError: periodic'],
        ]
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
