#!/usr/bin/env python3
"""R6-013: prove the local store works in a real browser, including persistence.

Serves the repository over plain HTTP and drives qa/r6/idb_harness.html. Plain
HTTP matters: a private-network deployment is not a secure context, and OPFS is
therefore unavailable while IndexedDB must still work.

    env -u PYTHONPATH python3.9 qa/r6/idb_store.py --out qa/evidence/R6-013
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the evidence output readable
        pass


def lan_ip() -> str:
    """Prefer a real interface address; a VPN/tun address is not reachable."""
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
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('10.255.255.255', 1))
        value = probe.getsockname()[0]
        probe.close()
        return value
    except OSError:
        return '127.0.0.1'


def serve(directory: Path):
    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(('0.0.0.0', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    server = serve(ROOT)
    port = server.server_address[1]
    host = lan_ip() if args.lan else '127.0.0.1'
    base = 'http://%s:%d' % (host, port)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {
        'schema': 'logbench.idb-store-evidence/1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'base_url': base,
        'note': '在真实 Chrome 中以普通 HTTP 打开，验证 IndexedDB 基线路径。',
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=args.channel, headless=True,
                args=['--no-proxy-server', '--disable-features=IsolateOrigins'])
            context = browser.new_context(viewport={'width': 900, 'height': 700})
            page = context.new_page()
            page.goto(base + '/' + args.page, wait_until='load')
            page.wait_for_function('() => window.__result !== undefined', timeout=args.timeout * 1000)
            result = page.evaluate('() => window.__result')
            page.screenshot(path=str(out / (args.name + '-harness.png')), full_page=True)
            browser.close()
        report['browser'] = result.get('origin')
        report['secure_context'] = result.get('secure')
        report['checks'] = result.get('checks', [])
        report['counts'] = result.get('counts', {})
        report['ok'] = bool(result.get('ok'))
        if result.get('error'):
            report['error'] = result['error']
    finally:
        server.shutdown()

    (out / (args.name + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    for item in report.get('checks', []):
        print('%-6s %-30s %s' % ('PASS' if item['ok'] else 'FAIL', item['name'], item['detail']))
    print('secure_context=%s origin=%s' % (report.get('secure_context'), report.get('browser')))
    failures = [item for item in report.get('checks', []) if not item['ok']]
    return 1 if failures or not report.get('ok') else 0


def main() -> int:
    parser = argparse.ArgumentParser(description='R6-013 IndexedDB evidence')
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-013'))
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--timeout', type=float, default=90.0)
    parser.add_argument('--page', default='qa/r6/idb_harness.html')
    parser.add_argument('--name', default='idb-store')
    parser.add_argument('--lan', action='store_true',
                        help='用本机局域网 IP 打开，获得非安全上下文（私网 HTTP）')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
