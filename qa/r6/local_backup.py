#!/usr/bin/env python3
"""R6-017: build a backup, write it to disk, restore it from those files.

The point is the round trip through the filesystem: volumes are produced in the
browser, saved as real files, served back over HTTP, and only then restored into
a staging generation.

    env -u PYTHONPATH python3.9 qa/r6/local_backup.py --lan --out qa/evidence/R6-017
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from idb_store import lan_ip, serve, ROOT  # noqa: E402


class CorsHandler(SimpleHTTPRequestHandler):
    """The restore page runs on another port, so the file server must allow CORS."""

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_message(self, *args):
        pass


def serve_files(directory: Path):
    handler = partial(CorsHandler, directory=str(directory))
    server = ThreadingHTTPServer(('0.0.0.0', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    repo_server = serve(ROOT)
    repo_port = repo_server.server_address[1]
    host = lan_ip() if args.lan else '127.0.0.1'
    page_url = 'http://%s:%d/qa/r6/backup_harness.html' % (host, repo_port)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files_dir = out / 'backup-files'
    files_dir.mkdir(parents=True, exist_ok=True)
    checks = []
    report = {
        'schema': 'logbench.backup-evidence/1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.channel, headless=True, args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 960, 'height': 720}, accept_downloads=True)
            page = context.new_page()
            page.goto(page_url, wait_until='load')
            page.wait_for_function('() => window.__ready === true', timeout=30000)

            built = page.evaluate('async () => await window.__backup.build()')
            checks.extend(built['checks'])

            manifest = built['manifest']
            (files_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), 'utf-8')
            for index, payload in enumerate(built['volumes']):
                (files_dir / ('volume-%d.bin' % index)).write_bytes(base64.b64decode(payload))
            total_bytes = sum(len(base64.b64decode(payload)) for payload in built['volumes'])
            checks.append({
                'name': '备份文件已落盘',
                'ok': all((files_dir / ('volume-%d.bin' % i)).exists() for i in range(len(built['volumes']))),
                'detail': '%d volumes, %d bytes' % (len(built['volumes']), total_bytes),
            })

            file_server = serve_files(files_dir)
            file_port = file_server.server_address[1]
            source = 'http://%s:%d' % (host, file_port)
            try:
                restored = page.evaluate('async (src) => await window.__backup.restore(src)', source)
                checks.extend(restored['checks'])
            finally:
                file_server.shutdown()

            page.screenshot(path=str(out / 'backup-harness.png'), full_page=True)
            secure = page.evaluate('() => window.isSecureContext')
            browser.close()
    finally:
        repo_server.shutdown()

    report['secure_context'] = secure
    report['checks'] = checks
    report['ok'] = all(item['ok'] for item in checks)
    (out / 'backup.json').write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    for item in checks:
        print('%-6s %-32s %s' % ('PASS' if item['ok'] else 'FAIL', item['name'], item['detail']))
    print('secure_context=%s' % secure)
    return 0 if report['ok'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='R6-017 backup round trip')
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-017'))
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--lan', action='store_true')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
