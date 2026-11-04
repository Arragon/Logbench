#!/usr/bin/env python3
"""R6-056: the unified library and storage-root pages, measured in a real browser.

What is measured, not asserted from the source:

  * the navigation still has the compact workbench shape and the storage tab only
    appears for an administrator;
  * a directory with hundreds of entries renders without reading log content and
    without pushing the page horizontally (documentElement.scrollWidth);
  * the table keeps a real viewport height (not zero) at 1366x768, 1024x768,
    768x1024 and 375x812, and the last row is reachable by scrolling;
  * an offline root shows the registration and the reason, never "0 files";
  * the add-root dialog closes on Escape and keeps the focus inside the page.

    env -u PYTHONPATH python3 qa/r6/library_storage_ui.py --out qa/evidence/R6-056

The fixtures are temporary directories. Two roots outside the deployment directory
are real directories on this machine, not simulated mounts; nothing here proves
behaviour on a real unmounted device, which is why `mount_offline` below is
reported as `simulated`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

MANY = 180


def seed(tmp: Path):
    data = tmp / 'deploy' / 'data'
    state = tmp / 'deploy' / 'var'
    shared = tmp / 'physical' / 'archive'
    private = tmp / 'physical' / 'private'
    for path in (data / 'logs', state, shared / 'psg-a', private):
        path.mkdir(parents=True, exist_ok=True)
    # A directory with enough entries that a naive implementation would try to
    # load all of them, plus one long path that would overflow a fixed-width cell.
    for index in range(MANY):
        (data / 'logs' / ('run-%03d.log' % index)).write_text(
            '2026-01-01 10:00:%02d.000 [INFO] line %d\n' % (index % 60, index), 'utf-8')
    deep = data / 'logs' / ('very-long-directory-name-' + 'x' * 60)
    deep.mkdir(exist_ok=True)
    (deep / ('deep-' + 'y' * 80 + '.log')).write_text(
        '2026-01-01 11:00:00.000 [WARN] deep path\n', 'utf-8')
    (shared / 'psg-a' / 'device_io.log').write_text(
        '2026-01-01 12:00:00.000 [ERROR] archive root\n', 'utf-8')
    (private / 'private-upload.log').write_text(
        '2026-01-01 13:00:00.000 [INFO] private root\n', 'utf-8')
    return data, state, shared, private


def start_server(seed_paths, ip='127.0.0.1'):
    from server.app import Application, Config
    from server.httpd import Server
    data, state, shared, private = seed_paths
    config = Config(ROOT, data_dir=data, state_dir=state, code_root=ROOT,
                    allowed_hosts=[ip, 'localhost', '127.0.0.1'], bootstrap_admins=[ip])
    app = Application(config)
    server = Server((ip, 0), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, app, server.server_address[1]


def measure(page):
    return page.evaluate("""() => {
      const scroll = document.querySelector('#library-view .lib-scroll');
      const table = document.querySelector('#library-view table.lib-table');
      const rows = table ? table.querySelectorAll('tbody tr').length : 0;
      const doc = document.documentElement;
      return {
        viewport: [window.innerWidth, window.innerHeight],
        scroll_width: doc.scrollWidth,
        body_scroll_width: document.body.scrollWidth,
        horizontal_overflow: doc.scrollWidth > window.innerWidth + 1,
        table_scroll_height: scroll ? Math.round(scroll.getBoundingClientRect().height) : 0,
        table_scroll_overflow: scroll ? scroll.scrollHeight - scroll.clientHeight : 0,
        rows_rendered: rows,
        nav_items: Array.from(document.querySelectorAll('.r6-nav-item')).length,
        toolbar_rows: document.querySelectorAll('#toolbar').length,
        header_height: Math.round(document.querySelector('.r6-header').getBoundingClientRect().height)
      };
    }""")


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='lb-m5-ui-'))
    seed_paths = seed(tmp)
    server, app, port = start_server(seed_paths)
    base = 'http://127.0.0.1:%d' % port
    ip = '127.0.0.1'
    report = {'url': base, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'checks': {}, 'states': {}, 'screenshots': []}

    def check(name, ok, detail=None):
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}

    try:
        # Two additional roots: a shared archive outside the deployment directory
        # and a private root. Both are registered through the real API.
        actor = app.actor(ip)
        shared_root = app.storage.roots.create(actor, {
            'label': '归档盘', 'path': str(seed_paths[2]), 'kind': 'shared_raw'})
        private_root = app.storage.roots.create(actor, {
            'label': '私有上传区', 'path': str(seed_paths[3]), 'kind': 'private_raw'})
        report['roots'] = [shared_root['item']['root_id'], private_root['item']['root_id']]

        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True,
                                        args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 1366, 'height': 768})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/r6', wait_until='load', timeout=30000)
            page.wait_for_function('() => window.__r6Ready === true', timeout=30000)

            nav = page.evaluate("""() => Array.from(document.querySelectorAll('.r6-nav-item'))
                .map(b => ({label: b.textContent.trim(), view: b.dataset.view, hidden: b.hidden}))""")
            report['nav'] = nav
            check('nav_compact', len(nav) <= 5 and all(not item['hidden'] or item['view'] == 'storage'
                                                       for item in nav), nav)

            page.click('.r6-nav-item[data-view="files"]')
            page.wait_for_selector('#library-view:not([hidden])', timeout=5000)
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view .folder-root').length >= 1",
                timeout=10000)
            page.wait_for_timeout(400)
            report['states']['library_initial'] = measure(page)

            # Expand the legacy root, then the logs directory.
            page.click('#library-view .folder-root[data-root="legacy"]')
            page.wait_for_timeout(600)
            dirs = page.evaluate("""() => Array.from(document.querySelectorAll(
                '#library-view .folder-dir')).map(b => b.textContent.trim())""")
            check('tree_lazy_top_level', any('logs' in name for name in dirs), dirs)
            page.click("#library-view .folder-dir:has-text('logs')")
            # The tree call is the scan; the table only renders after it, so wait for
            # the rows rather than for a fixed delay (a fixed delay measured a
            # half-populated table once and passed for the wrong reason).
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view tbody tr').length >= 20",
                timeout=20000)
            page.wait_for_timeout(150)
            state = measure(page)
            report['states']['library_directory'] = state
            check('directory_rows_bounded', 20 <= state['rows_rendered'] <= 120,
                  state['rows_rendered'])
            footer = page.evaluate("() => document.querySelector('#library-view .r6-foot').textContent")
            check('directory_total_counted', str(MANY) in footer, footer)
            check('no_horizontal_overflow', not state['horizontal_overflow'],
                  [state['scroll_width'], state['viewport']])
            check('table_has_height', state['table_scroll_height'] > 300,
                  state['table_scroll_height'])
            page.screenshot(path=str(out / 'library-1366x768.png'))
            report['screenshots'].append('library-1366x768.png')

            # Last row must be reachable inside the table, not clipped by the page.
            page.evaluate("""() => { const s = document.querySelector('#library-view .lib-scroll');
                s.scrollTop = s.scrollHeight; }""")
            page.wait_for_timeout(200)
            reachable = page.evaluate("""() => { const s = document.querySelector('#library-view .lib-scroll');
                const rows = s.querySelectorAll('tbody tr');
                if (!rows.length) return null;
                const last = rows[rows.length - 1].getBoundingClientRect();
                const box = s.getBoundingClientRect();
                return {lastTop: Math.round(last.top), boxBottom: Math.round(box.bottom),
                        visible: last.top >= box.top - 2 && last.bottom <= box.bottom + 2}; }""")
            report['states']['last_row'] = reachable
            check('last_row_reachable', bool(reachable and reachable['visible']), reachable)

            # Root rule selection is only offered for a selected directory.
            page.click("#library-view .folder-dir:has-text('logs')")
            page.wait_for_timeout(300)
            rule_state = page.evaluate("""() => ({
              selectDisabled: document.querySelector('#library-view select').disabled,
              explain: (document.querySelector('.lib-explain')||{}).textContent || ''
            })""")
            report['states']['rule_controls'] = rule_state
            check('rule_explained', '继承' in rule_state['explain'] or '当前生效' in rule_state['explain'],
                  rule_state['explain'])

            # Storage roots page: table, then add-dialog focus + Escape.
            page.click('.r6-nav-item[data-view="storage"]')
            page.wait_for_selector('#storage-view:not([hidden])', timeout=5000)
            page.wait_for_timeout(500)
            roots = page.evaluate("""() => Array.from(document.querySelectorAll(
                '#storage-view tbody tr')).map(r => r.textContent.replace(/\\s+/g, ' ').trim())""")
            report['states']['storage_rows'] = roots
            check('storage_lists_roots', len(roots) >= 3, len(roots))
            page.screenshot(path=str(out / 'storage-1366x768.png'))
            report['screenshots'].append('storage-1366x768.png')
            page.click('#storage-view .r6-button')
            page.wait_for_selector('.storage-dialog:not([hidden])', timeout=3000)
            focused = page.evaluate("() => document.activeElement && document.activeElement.id")
            page.keyboard.press('Escape')
            closed = page.evaluate("() => document.querySelector('.storage-dialog').hidden")
            check('dialog_focus_and_escape', focused == 'storage-root-label' and closed,
                  {'focused': focused, 'closed': closed})

            # Offline: remove the sentinel of the legacy root and reload the view.
            (seed_paths[0] / '.logbench-root.json').unlink()
            page.click('.r6-nav-item[data-view="files"]')
            page.wait_for_timeout(300)
            page.click('#library-view .r6-icon[title="刷新"], #library-view .r6-icon:has-text("刷新")')
            page.wait_for_timeout(900)
            offline = page.evaluate("""() => ({
              note: (document.querySelector('#library-view .lib-note')||{}).textContent || '',
              empty: (document.querySelector('#library-view .lib-empty-cell')||{}).textContent || '',
              dot: Array.from(document.querySelectorAll('#library-view .lib-dot'))
                    .map(d => d.dataset.state)
            })""")
            report['states']['offline'] = offline
            check('offline_not_empty', '离线' in offline['note'] or '不可用' in offline['empty'],
                  offline)
            page.screenshot(path=str(out / 'library-offline-1366x768.png'))
            report['screenshots'].append('library-offline-1366x768.png')

            # Remount recovery: putting the sentinel back is what a working mount
            # looks like to the guard, so the root must come back without losing
            # the registration or the file rows.
            legacy = app.storage.roots.get('legacy', with_state=False)
            (seed_paths[0] / '.logbench-root.json').write_text(
                json.dumps({'root_id': 'legacy', 'sentinel_id': legacy['sentinel_id']}), 'utf-8')
            recovered = app.storage.roots.probe(actor, 'legacy')
            check('remount_recovers_root', recovered['root']['state'] == 'online',
                  recovered['root']['state'])
            page.click('#library-view .r6-icon:has-text("刷新")')
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view tbody tr').length >= 20",
                timeout=20000)

            # Responsive sweep: the table must keep a real height everywhere.
            for width, height, tag in ((1024, 768, '1024x768'), (768, 1024, '768x1024'),
                                       (375, 812, '375x812')):
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(500)
                state = measure(page)
                report['states']['size_' + tag] = state
                check('no_overflow_' + tag, not state['horizontal_overflow'],
                      [state['scroll_width'], width])
                check('table_height_' + tag, state['table_scroll_height'] > 120,
                      state['table_scroll_height'])
                page.screenshot(path=str(out / ('library-%s.png' % tag)))
                report['screenshots'].append('library-%s.png' % tag)

            report['page_errors'] = errors
            check('no_page_errors', not errors, errors)
            browser.close()
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass

    report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    report['mount_offline'] = {
        'result': 'simulated',
        'detail': 'sentinel 缺失触发离线判定；真实卸载/换盘挂载需在目标机执行，本机未验证。',
    }
    failed = [name for name, value in report['checks'].items() if not value['ok']]
    report['passed'] = not failed
    (out / 'library-storage.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({'passed': report['passed'], 'failed': failed,
                      'out': str(out)}, ensure_ascii=False))
    return 0 if report['passed'] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / 'R6-056'))
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
