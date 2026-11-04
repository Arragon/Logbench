#!/usr/bin/env python3
"""Shared harness for the M8 acceptance scripts (R6-078 … R6-084).

One server, one browser, one fixture generator — so the seven scripts measure the
same product rather than seven slightly different ones. Nothing here asserts
anything; each script owns its checks and its own JSON report.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    # The scripts are run as `python3.9 qa/r6/x.py`, so the package root is not on the
    # path yet; adding it here keeps every script's invocation identical.
    sys.path.insert(0, str(ROOT))
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


# --------------------------------------------------------------------- fixtures
def line(ts: str, level: str, func: str, message: str, logger: str = 'APP-A') -> str:
    """One bracket-format record the default rule can parse."""
    return ('[%s] [%s] [%s] [1001] [1041] [%s.py:100] [100] [0:0:0] %s() <TAG> %s'
            % (ts, level, logger, func, func, message))


def write_log(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(rows) + '\n', 'utf-8')
    return path


def two_aligned_files(data: Path) -> list:
    """Two files on one clock, with a deliberate gap and one duplicate timestamp.

    a: 10:00:00, 10:00:05, 10:00:10, 10:00:10 (duplicate), 10:00:20
    b: 10:00:00, 10:00:10, 10:00:15, 10:00:20, 10:00:25
    → the 10:00:05 slot is empty in b, 10:00:15 empty in a, 10:00:10 has two rows in a.
    """
    a = write_log(data / 'sw' / 'psg-a.log', [
        line('2026-05-01 10:00:00.000000', 'INFO', 'start', 'boot ok energy: 1'),
        line('2026-05-01 10:00:05.000000', 'ERROR', 'setpoint', 'rpc_success: False err_code: 0x1'),
        line('2026-05-01 10:00:10.000000', 'INFO', 'tick', 'tick 10 iteration: 10'),
        line('2026-05-01 10:00:10.000000', 'WARNING', 'tick2', 'same ms iteration: 11'),
        line('2026-05-01 10:00:20.000000', 'INFO', 'done', 'finish iteration: 20'),
    ])
    b = write_log(data / 'sw' / 'psg-b.log', [
        line('2026-05-01 10:00:00.000000', 'INFO', 'start', 'boot ok energy: 2', logger='APP-B'),
        line('2026-05-01 10:00:10.000000', 'ERROR', 'flush', 'flush failed err_code: 0x9',
             logger='APP-B'),
        line('2026-05-01 10:00:15.000000', 'INFO', 'mid', 'mid iteration: 15', logger='APP-B'),
        line('2026-05-01 10:00:20.000000', 'INFO', 'done', 'finish iteration: 20', logger='APP-B'),
        line('2026-05-01 10:00:25.000000', 'INFO', 'tail', 'tail iteration: 25', logger='APP-B'),
    ])
    return [a, b]


def local_file(path: Path) -> Path:
    """A file that only ever lives in the browser (imported through the file input)."""
    return write_log(path, [
        line('2026-05-01 10:00:02.000000', 'INFO', 'local', 'local-only marker LOCALFILE-XYZZY'),
        line('2026-05-01 10:00:12.000000', 'ERROR', 'localerr', 'local error LOCALFILE-XYZZY'),
    ])


# ----------------------------------------------------------------------- server
class Product:
    def __init__(self, *, index=True, files=None, dev_assets=True):
        from server.app import Application, Config
        from server.httpd import Server

        self.tmp = Path(tempfile.mkdtemp(prefix='lb-m8-'))
        (self.tmp / 'rules').mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                    self.tmp / 'rules' / 'cpqpsgx-default.json')
        self.data = self.tmp / 'data'
        self.data.mkdir(parents=True, exist_ok=True)
        self.paths = two_aligned_files(self.data) if files is None else files
        if dev_assets:
            os.environ['LOGBENCH_DEV_ASSETS'] = '1'
        config = Config(self.tmp, data_dir=self.data, state_dir=self.tmp / 'var',
                        code_root=ROOT, allowed_hosts=['127.0.0.1', 'localhost'],
                        bootstrap_admins=['127.0.0.1'])
        self.app = Application(config)
        self.app.index_jobs.executor = None
        self.server = Server(('127.0.0.1', 0), self.app)
        self.port = self.server.server_address[1]
        self.base = 'http://127.0.0.1:%d' % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.file_ids = {}
        if index:
            for path in self.paths:
                self.file_ids[path.name] = self.index(path.relative_to(self.data).as_posix())

    # ------------------------------------------------------------------ helpers
    def csrf(self) -> str:
        return self.app.csrf('127.0.0.1')

    def index(self, rel: str) -> str:
        created = self.app.handle('POST', '/api/v2/index-jobs', {
            'Host': 'localhost:%d' % self.port, 'Content-Type': 'application/json',
            'Origin': 'http://localhost:%d' % self.port, 'X-LogBench-CSRF': self.csrf()},
            json.dumps({'path': rel}).encode(), '127.0.0.1')
        if created.status != 200:
            raise AssertionError('index %s failed: %s' % (rel, created.value))
        item = created.value['item']
        self.app.index_jobs.run_now(item['id'])
        return item['payload']['file_id']

    def api(self, method: str, path: str, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={'Host': 'localhost:%d' % self.port,
                     'Origin': 'http://localhost:%d' % self.port,
                     'Content-Type': 'application/json',
                     'X-LogBench-CSRF': self.csrf()})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode('utf-8')
                return response.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as error:
            text = error.read().decode('utf-8')
            return error.code, (json.loads(text) if text else {})

    def close(self):
        try:
            self.app.index_jobs.close()
            self.app.queries.close()
        finally:
            self.server.shutdown()
            self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------- browser
class Page:
    """System Chrome, same-origin page, console capture, request log."""

    def __init__(self, base: str, *, viewport=(1366, 768), url='/r6'):
        from playwright.sync_api import sync_playwright
        self.console = []
        self.requests = []
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(channel='chrome', headless=True,
                                                        args=['--no-proxy-server'])
        self.page = self.browser.new_page(viewport={'width': viewport[0],
                                                    'height': viewport[1]})
        self.page.on('console', self._on_console)
        self.page.on('pageerror', lambda error: self.console.append(
            {'type': 'pageerror', 'text': str(error)}))
        self.page.on('request', lambda request: self.requests.append({
            'url': request.url, 'method': request.method,
            'body': request.post_data or ''}))
        self.base = base
        self.page.goto(base + url, wait_until='load', timeout=45000)
        self.page.wait_for_function('() => window.__r6Ready === true', timeout=45000)

    def _on_console(self, message):
        self.console.append({'type': message.type, 'text': message.text})

    def wait(self, ms: int):
        self.page.wait_for_timeout(ms)

    def shot(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=False)

    def import_local(self, path: Path):
        """Import through the real file input, then wait for the file list to show it."""
        self.page.set_input_files('#file-input', str(path))
        self.page.wait_for_function(
            "() => (window.__r6.provider.listFiles) && window.__r6.state.fileId !== null || "
            "document.querySelectorAll('.r6-nav-file').length > 0", timeout=45000)
        return self.page.evaluate("() => document.querySelectorAll('.r6-nav-file').length")

    def errors(self):
        return [item for item in self.console
                if item['type'] in ('error', 'pageerror')]

    def close(self):
        try:
            self.browser.close()
        finally:
            self._playwright.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ----------------------------------------------------------------------- report
class Checks:
    def __init__(self, name: str, out: Path):
        self.name = name
        self.out = Path(out)
        self.items = []

    def check(self, label: str, ok: bool, detail=None):
        self.items.append({'label': label, 'ok': bool(ok), 'detail': detail})
        print(('PASS ' if ok else 'FAIL ') + label +
              ('' if detail is None else ' — ' + str(detail)[:180]))
        return ok

    def equal(self, label: str, actual, expected, detail=None):
        note = 'actual=%r expected=%r' % (actual, expected)
        if detail is not None:
            note += ' sample=%r' % (detail,)
        return self.check(label, actual == expected, note)

    def write(self, extra=None) -> bool:
        self.out.mkdir(parents=True, exist_ok=True)
        payload = {
            'name': self.name,
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'ok': all(item['ok'] for item in self.items),
            'total': len(self.items),
            'failed': sum(1 for item in self.items if not item['ok']),
            'checks': self.items,
        }
        if extra:
            payload.update(extra)
        path = self.out / (self.name + '.json')
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), 'utf-8')
        print('ok=%s total=%d failed=%d → %s' % (payload['ok'], payload['total'],
                                                 payload['failed'], path))
        return payload['ok']


def main_guard(fn):
    """One exit code convention for every script."""
    def wrapper():
        ok = fn()
        raise SystemExit(0 if ok else 1)
    return wrapper


def arg_out(default: str):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / default))
    return parser.parse_args()
