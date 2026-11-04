#!/usr/bin/env python3
"""R6-024: prove a catastrophic regex is terminated in a real Worker.

The pattern `(a+)+$` over `aaa…!` has no yield point that a cooperative loop
could reach, so the only thing that can stop it is `terminate()` from outside.
This script checks that the executor is really destroyed, that the main thread
stayed responsive while it was stuck, and that the result is `partial` rather
than a zero.

    env -u PYTHONPATH python3.9 qa/r6/regex_isolation.py --lan --out qa/evidence/R6-024
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def lan_ip() -> str:
    import subprocess
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
    tmp = tempfile.mkdtemp(prefix='lb-regex-')
    data = Path(tmp) / 'data'
    state = Path(tmp) / 'var'
    data.mkdir()
    state.mkdir()
    ip = lan_ip()
    config = Config(ROOT, data_dir=data, state_dir=state, code_root=ROOT,
                    allowed_hosts=[ip, 'localhost', '127.0.0.1'], bootstrap_admins=[ip])
    server = Server(('0.0.0.0', 0), Application(config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ['LOGBENCH_DEV_ASSETS'] = '1'
    server, port = start_server()
    host = lan_ip() if args.lan else '127.0.0.1'
    base = 'http://%s:%d' % (host, port)
    report = {'url': base, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'checks': []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            page = browser.new_page(viewport={'width': 1366, 'height': 768})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/assets/qa/r6/regex_harness.html', wait_until='load', timeout=30000)
            page.wait_for_function('() => typeof window.__run === "function"', timeout=15000)
            result = page.evaluate('() => window.__run()')
            report.update({
                'ok': bool(result.get('ok')),
                'checks': result.get('checks', []),
                'workers_created': result.get('workers_created'),
                'workers_terminated': result.get('workers_terminated'),
                'max_timer_gap_ms': result.get('max_timer_gap_ms'),
                'hostile_elapsed_ms': result.get('hostile_elapsed_ms'),
                'supervisor': result.get('supervisor'),
                'page_errors': errors,
                'secure_context': page.evaluate('() => window.isSecureContext'),
            })
            page.screenshot(path=str(out / 'regex-harness.png'), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    (out / 'regex-isolation.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    for check in report['checks']:
        print(('PASS ' if check['ok'] else 'FAIL ') + check['label'] +
              ('' if check.get('detail') is None else ' — ' + str(check['detail'])))
    failed = sum(1 for check in report['checks'] if not check['ok'])
    print('ok=%s total=%d failed=%d' % (report['ok'], len(report['checks']), failed))
    return 0 if report['ok'] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-024'))
    parser.add_argument('--lan', action='store_true')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
