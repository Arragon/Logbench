#!/usr/bin/env python3
"""R6-019: drive the resident local engine through a real Worker and real IndexedDB.

The server that serves `/assets/workers/local-engine.js` is the product server
itself, started in-process, because the Worker must be same-origin. The page is
opened over the LAN address so `isSecureContext` is false — the deployment case.

    env -u PYTHONPATH python3.9 qa/r6/provider_local.py --out qa/evidence/R6-019
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


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


def start_server(root: Path):
    """Start the product server on an ephemeral port with a throwaway data dir."""
    import sys
    sys.path.insert(0, str(root))
    from server.app import Application, Config
    from server.httpd import Server

    tmp = tempfile.mkdtemp(prefix='lb-provider-')
    data = Path(tmp) / 'data'
    state = Path(tmp) / 'var'
    data.mkdir()
    state.mkdir()
    ip = lan_ip()
    config = Config(root, data_dir=data, state_dir=state, code_root=root,
                    allowed_hosts=[ip, 'localhost', '127.0.0.1'], bootstrap_admins=[ip])
    application = Application(config)
    server = Server(('0.0.0.0', 0), application)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], tmp


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ['LOGBENCH_DEV_ASSETS'] = '1'

    server, port, tmp = start_server(ROOT)
    base = 'http://%s:%d' % (lan_ip() if args.lan else '127.0.0.1', port)
    report = {'url': base, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'checks': []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            page = browser.new_page(viewport={'width': 1366, 'height': 768})
            console = []
            page.on('console', lambda message: console.append(message.text))
            page.goto(base + '/assets/qa/r6/provider_harness.html', wait_until='load', timeout=30000)
            page.wait_for_function('() => typeof window.__run === "function"', timeout=15000)
            result = page.evaluate('() => window.__run()')
            report['secure_context'] = page.evaluate('() => window.isSecureContext')
            report['checks'] = result.get('checks', [])
            report['ok'] = bool(result.get('ok'))
            report['coverage'] = result.get('coverage')
            report['count'] = result.get('count')
            report['error'] = result.get('error')
            report['console'] = console[-20:]
            page.screenshot(path=str(out / 'provider-harness.png'), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    (out / 'provider-local.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    for check in report['checks']:
        print(('PASS ' if check['ok'] else 'FAIL ') + check['label'] +
              ('' if check.get('detail') is None else ' — ' + str(check['detail'])))
    print('ok=%s total=%d failed=%d' % (report['ok'], len(report['checks']),
                                        sum(1 for c in report['checks'] if not c['ok'])))
    return 0 if report['ok'] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-019'))
    parser.add_argument('--lan', action='store_true', help='用局域网地址打开，得到非安全上下文')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
