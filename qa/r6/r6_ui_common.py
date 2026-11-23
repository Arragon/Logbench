#!/usr/bin/env python3
"""Shared harness for the R6 UI evidence scripts after the design restore.

The old m8_common.Page waited for `window.__r6Ready` and drove the pre-restore
DOM (`#intake-view`, `.r6-nav-item[data-view=...]`, `.runtime-view`). The
restored /r6 uses the prototype's DOM and boots through window.LB, so these
three evidence scripts share a small, explicit harness instead.

Every script takes --base-url and --out, talks to a running server, and writes
the same Checks-shaped evidence JSON as before.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def run_args(evidence_name: str):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:50004')
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / evidence_name))
    return parser.parse_args()


def open_r6(base_url: str, viewport=(1366, 768)):
    """Return (playwright, browser, page, page_errors). Caller closes all."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(executable_path=CHROME, headless=True,
                                         args=['--no-proxy-server'])
    page = browser.new_page(viewport={'width': viewport[0], 'height': viewport[1]})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)[:300]))
    page.goto(base_url.rstrip('/') + '/r6', wait_until='domcontentloaded', timeout=45000)
    # Never sample the pre-boot frame: it intentionally has no data.
    page.wait_for_function(
        "() => window.LB && window.LB.state.scenario !== 'loading'", timeout=30000)
    page.wait_for_timeout(250)
    return playwright, browser, page, errors


def close(playwright, browser):
    try:
        browser.close()
    finally:
        playwright.stop()


def api(page, method: str, path: str, body=None):
    """Same-origin JSON call with the page's own CSRF token."""
    return page.evaluate("""async ({method, path, body}) => {
        const token = (window.LB && window.LB.state.csrfToken) || '';
        const init = {method, credentials: 'same-origin', headers: {}};
        if (body !== null && body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        if (method !== 'GET') init.headers['X-LogBench-CSRF'] = token;
        const r = await fetch(path, init);
        let value = null;
        try { value = await r.json(); } catch (e) { value = null; }
        return {status: r.status, value: value};
    }""", {'method': method, 'path': path, 'body': body})
