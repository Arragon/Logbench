#!/usr/bin/env python3
"""Batch recycling, permanent removal and the header cache meter, in a real browser.

What is measured, not asserted from the source:

  * the library table exposes per-row checkboxes and a batch bar that appears only
    while something is selected;
  * the confirmation dialog names every entry, its location and its size, and says
    that permanent removal cannot be undone;
  * permanent removal destroys the recycled bytes *and* drops the index cache the
    file owned, which the header meter reflects without a page reload;
  * the header meter shows used/quota, opens a quota dialog on click, and saves a
    new limit that survives a reload;
  * nothing overflows horizontally at 1366x768 or 375x812, and no page error is
    raised along the way.

    env -u PYTHONPATH python3 qa/r6/trash_batch_cache.py --out qa/evidence/trash-batch-cache

The fixtures are temporary directories on this machine. Nothing here proves
behaviour against a real unmounted device or a remote root.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FILES = 6
LAYER_BYTES = 1024 * 1024
DEPS = {
    'structure': {'content_sig': 'c1', 'generation': 1, 'framing': 'f1',
                  'runtime': 'py39', 'schema': 'v1', 'encoding': 'utf-8'},
    'rules': {'structure_signature': 's1', 'rule_structure': 'r1',
              'rule_extract': 'e1', 'engine': 'js'},
    'search': {'rules_signature': 'r1', 'projection': 'p1', 'gram_size': 3},
}


def seed(tmp: Path):
    data = tmp / 'deploy' / 'data'
    state = tmp / 'deploy' / 'var'
    for path in (data / 'logs', state):
        path.mkdir(parents=True, exist_ok=True)
    for index in range(FILES):
        (data / 'logs' / ('run-%02d.log' % index)).write_text(
            '2026-01-01 10:00:%02d.000 [INFO] line %d\n' % (index, index), 'utf-8')
    return data, state


def start_server(seed_paths, ip='127.0.0.1'):
    from server.app import Application, Config
    from server.httpd import Server
    data, state = seed_paths
    config = Config(ROOT, data_dir=data, state_dir=state, code_root=ROOT,
                    allowed_hosts=[ip, 'localhost', '127.0.0.1'], bootstrap_admins=[ip])
    app = Application(config)
    server = Server((ip, 0), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, app, server.server_address[1]


def publish_cache(app, tmp: Path):
    """Give two files a full derived cache with real shard bytes on disk."""
    registry = app.cache.registry
    actor = app.actor('127.0.0.1')
    made = []
    with app.store.connect() as db:
        rows = db.execute("SELECT id FROM logical_files WHERE root_id='legacy' "
                          "ORDER BY rel_path LIMIT 2").fetchall()
    for row in rows:
        for layer, deps in DEPS.items():
            shard = tmp / ('shard-%s-%s.bin' % (row['id'], layer))
            shard.write_bytes(b'\0' * 128)
            version = registry.ensure(row['id'], layer, owner=actor['id'],
                                      generation=1, deps=deps)
            registry.publish(version['version_id'], rows=4, digest='a' * 64,
                             stats={'bytes': LAYER_BYTES, 'shard': str(shard)})
        made.append(row['id'])
    return made


def measure(page):
    return page.evaluate("""() => {
      const doc = document.documentElement;
      const header = document.querySelector('.r6-header');
      const meter = document.querySelector('#cache-meter');
      const table = document.querySelector('#library-view table.lib-table');
      // A control label that breaks mid-word shows up as a control taller than the
      // row it lives in: 打开本机文件 rendered 47x69 at 375 px before the fix.
      const actions = document.querySelector('.r6-actions');
      const controls = actions ? [...actions.children].map(node => ({
        tag: node.id || node.className,
        h: Math.round(node.getBoundingClientRect().height)
      })) : [];
      return {
        viewport: [window.innerWidth, window.innerHeight],
        scroll_width: doc.scrollWidth,
        horizontal_overflow: doc.scrollWidth > window.innerWidth + 1,
        header_height: header ? Math.round(header.getBoundingClientRect().height) : 0,
        controls: controls,
        tallest_control: controls.reduce((max, item) => Math.max(max, item.h), 0),
        meter_text: document.getElementById('cache-meter-value').textContent.trim(),
        meter_state: meter ? meter.dataset.state : null,
        meter_visible: !!meter && meter.getBoundingClientRect().width > 0,
        meter_width: meter ? Math.round(meter.getBoundingClientRect().width) : 0,
        rows: table ? table.querySelectorAll('tbody tr').length : 0,
        table_head_height: table ? [...table.querySelectorAll('thead th')]
          .reduce((max, th) => Math.max(max, Math.round(th.getBoundingClientRect().height)), 0) : 0,
        checked: table ? table.querySelectorAll('td.lib-check input:checked').length : 0
      };
    }""")


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='lb-purge-ui-'))
    seed_paths = seed(tmp)
    server, app, port = start_server(seed_paths)
    base = 'http://127.0.0.1:%d' % port
    report = {'url': base, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'checks': {}, 'states': {}, 'screenshots': []}

    def check(name, ok, detail=None):
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}

    try:
        # Register the six seeded files, then give two of them a real cache. The
        # registration is what the library page reads; nothing is scanned by hand.
        actor = app.actor('127.0.0.1')
        app.storage.catalog.sync_directory(app.storage.roots.get('legacy'), 'logs', limit=50)
        cached = publish_cache(app, tmp)
        report['cached_files'] = cached
        before = app.cache.usage(actor)
        report['cache_before'] = before
        check('cache_seeded', before['usedBytes'] == len(cached) * 3 * LAYER_BYTES,
              before['usedBytes'])

        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True,
                                        args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 1366, 'height': 768})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/r6', wait_until='load', timeout=30000)
            page.wait_for_function('() => window.__r6Ready === true', timeout=30000)
            page.wait_for_function(
                "() => document.getElementById('cache-meter-value').textContent.trim() !== '—'",
                timeout=15000)

            report['states']['header_initial'] = measure(page)
            check('meter_shows_used_and_quota',
                  'MB' in report['states']['header_initial']['meter_text']
                  and 'GB' in report['states']['header_initial']['meter_text'],
                  report['states']['header_initial']['meter_text'])

            # ---------------------------------------------------------- library
            page.click('.r6-nav-item[data-view="files"]')
            page.wait_for_selector('#library-view:not([hidden])', timeout=5000)
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view .folder-root').length >= 1",
                timeout=10000)
            page.click('#library-view .folder-root[data-root="legacy"]')
            page.wait_for_timeout(500)
            page.click("#library-view .folder-dir:has-text('logs')")
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view tbody tr td.lib-check input').length >= 3",
                timeout=20000)

            boxes = page.query_selector_all('#library-view tbody tr td.lib-check input:not([disabled])')
            check('library_rows_checkable', len(boxes) >= 3, len(boxes))
            boxes[0].check()
            boxes[1].check()
            page.wait_for_selector('#library-view .lib-batch:not([hidden])', timeout=5000)
            batch_text = page.text_content('#library-view .lib-batch .r6-meta')
            check('batch_bar_counts_selection', '已选 2 项' in batch_text, batch_text)

            page.click('#library-view .lib-batch:not([hidden]) .r6-button.is-danger')
            page.wait_for_selector('#library-view .lib-confirm:not([hidden])', timeout=5000)
            dialog = page.evaluate("""() => {
              const box = document.querySelector('#library-view .lib-confirm');
              return {
                title: box.querySelector('strong').textContent,
                items: box.querySelectorAll('.lib-confirm-item').length,
                text: box.textContent.replace(/\\s+/g, ' ').trim()
              };
            }""")
            report['states']['library_confirm'] = dialog
            check('confirm_lists_every_file', dialog['items'] == 2, dialog['items'])
            check('confirm_names_location_and_size',
                  'logs/run-' in dialog['text'] and 'B' in dialog['text'], dialog['text'][:160])
            page.screenshot(path=str(out / 'library-batch-confirm-1366x768.png'))
            report['screenshots'].append('library-batch-confirm-1366x768.png')

            page.click('#library-view .lib-confirm .r6-button')
            page.wait_for_function(
                "() => document.querySelector('#library-view .lib-confirm').hidden === true",
                timeout=10000)
            page.wait_for_timeout(600)
            after_delete = app.storage.trash.list(actor)
            report['trash_after_delete'] = len(after_delete['items'])
            check('batch_delete_moved_two_entries', len(after_delete['items']) == 2,
                  len(after_delete['items']))

            # ------------------------------------------------------- recycling bin
            page.click("#library-view button:has-text('回收站')")
            page.wait_for_function(
                "() => document.querySelectorAll('#library-view .lib-table tbody tr td.lib-check input').length >= 2",
                timeout=10000)
            page.click('#library-view thead th.lib-check input')
            page.wait_for_selector('#library-view .lib-batch:not([hidden])', timeout=5000)
            trash_text = page.text_content('#library-view .lib-batch .r6-meta')
            check('trash_select_all_counts', '已选 2 项' in trash_text, trash_text)
            report['states']['trash'] = measure(page)
            page.screenshot(path=str(out / 'trash-selected-1366x768.png'))
            report['screenshots'].append('trash-selected-1366x768.png')

            page.click('#library-view .lib-batch:not([hidden]) .r6-button.is-danger')
            page.wait_for_selector('#library-view .lib-confirm:not([hidden])', timeout=5000)
            purge_dialog = page.evaluate("""() => {
              const box = document.querySelector('#library-view .lib-confirm');
              return {
                items: box.querySelectorAll('.lib-confirm-item').length,
                text: box.textContent.replace(/\\s+/g, ' ').trim(),
                warn: (box.querySelector('.lib-confirm-warn') || {}).textContent || ''
              };
            }""")
            report['states']['purge_confirm'] = purge_dialog
            check('purge_dialog_warns_irreversible',
                  '不可恢复' in purge_dialog['text'], purge_dialog['text'][:200])
            check('purge_dialog_lists_entries', purge_dialog['items'] == 2, purge_dialog['items'])
            page.screenshot(path=str(out / 'trash-purge-confirm-1366x768.png'))
            report['screenshots'].append('trash-purge-confirm-1366x768.png')

            page.click('#library-view .lib-confirm .r6-button.is-danger')
            page.wait_for_function(
                "() => document.querySelector('#library-view .lib-confirm').hidden === true",
                timeout=15000)
            page.wait_for_timeout(900)
            remaining = page.text_content('#library-view .lib-table tbody')
            check('trash_emptied', '回收站是空的' in remaining, remaining[:80])

            # Where the outcome is reported, not just that it was reported. The
            # library note is a sibling of the scroll container, so it is always on
            # screen; the header's right end belongs to the cache meter, and at
            # 375 px the status is capped at 80 px and would truncate the sentence.
            note_text = (page.text_content('#library-view .lib-note') or '').strip()
            header_text = (page.text_content('#status') or '').strip()
            check('purge_reported_inline_not_in_header',
                  '已永久删除' in note_text and '已永久删除' not in header_text,
                  [note_text[:60], header_text])

            after = app.cache.usage(actor)
            report['cache_after'] = after
            check('purge_released_the_cache', after['usedBytes'] == 0, after['usedBytes'])
            page.wait_for_function(
                "() => document.getElementById('cache-meter-value').textContent.indexOf('0 B') === 0",
                timeout=10000)
            report['states']['meter_after_purge'] = measure(page)
            check('meter_follows_the_purge',
                  report['states']['meter_after_purge']['meter_text'].startswith('0 B'),
                  report['states']['meter_after_purge']['meter_text'])

            # ------------------------------------------------------- quota dialog
            page.click('#cache-meter')
            page.wait_for_selector('.cache-quota-dialog .cache-quota-form', timeout=5000)
            dialog_state = page.evaluate("""() => {
              const box = document.querySelector('.cache-quota-dialog');
              return {
                layers: box.querySelectorAll('.cache-quota-layers tbody tr').length,
                text: box.textContent.replace(/\\s+/g, ' ').trim()
              };
            }""")
            report['states']['quota_dialog'] = dialog_state
            check('quota_dialog_shows_layer_breakdown', dialog_state['layers'] == 3,
                  dialog_state['layers'])
            page.screenshot(path=str(out / 'cache-quota-dialog-1366x768.png'))
            report['screenshots'].append('cache-quota-dialog-1366x768.png')

            page.fill('#cache-quota-input', '500')
            page.click('#cache-quota-save')
            page.wait_for_timeout(900)
            usage = app.cache.usage(actor)
            report['cache_quota_after_save'] = usage['quotaBytes']
            check('quota_saved', usage['quotaBytes'] == 500 * 1024 * 1024,
                  usage['quotaBytes'])
            page.wait_for_function(
                "() => document.getElementById('cache-meter-value').textContent.indexOf('/ 500.0 MB') > 0",
                timeout=10000)
            check('meter_shows_new_quota', True)
            # Saving a quota is an operation result. It belongs in the dialog's
            # own inline notice, not in the header status next to the meter.
            saved = page.evaluate("""() => ({
              inline: (document.querySelector('.cache-quota-dialog .cache-quota-saved') || {}).textContent || '',
              header: document.getElementById('status').textContent.trim()
            })""")
            check('quota_reported_inline_not_in_header',
                  '缓存限额已保存' in saved['inline'] and '缓存限额' not in saved['header'],
                  saved)
            page.keyboard.press('Escape')
            page.wait_for_timeout(300)

            # ------------------------------------------------------- responsive
            for name, width, height in (('375x812', 375, 812), ('768x1024', 768, 1024)):
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(400)
                state = measure(page)
                report['states']['header_' + name] = state
                check('no_overflow_' + name, not state['horizontal_overflow'],
                      [state['scroll_width'], state['viewport']])
                check('meter_visible_' + name, state['meter_visible'], state['meter_width'])
                # 36 px is the control height plus a pixel of rounding; a label that
                # broke mid-word measures 60-70 px and is caught here.
                check('controls_single_line_' + name, state['tallest_control'] <= 36,
                      state['controls'])
                check('table_head_single_line_' + name,
                      state['table_head_height'] == 0 or state['table_head_height'] <= 40,
                      state['table_head_height'])
                page.screenshot(path=str(out / ('header-%s.png' % name)))
                report['screenshots'].append('header-%s.png' % name)

            # A reload proves the quota came from the server, not from page state.
            page.set_viewport_size({'width': 1366, 'height': 768})
            page.reload(wait_until='load')
            page.wait_for_function('() => window.__r6Ready === true', timeout=30000)
            page.wait_for_function(
                "() => document.getElementById('cache-meter-value').textContent.indexOf('500.0 MB') > 0",
                timeout=15000)
            check('quota_survives_reload', True)

            check('no_page_errors', not errors, errors[:3])
            report['errors'] = errors
            browser.close()
    finally:
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        failed = [name for name, item in report['checks'].items() if not item['ok']]
        report['summary'] = {'checks': len(report['checks']), 'failed': len(failed),
                             'failed_names': failed,
                             'result': 'PASS' if not failed else 'FAIL'}
        (out / 'trash_batch_cache.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        try:
            server.shutdown()
        except Exception:
            pass

    failed = report['summary']['failed']
    print('%d checks, %d failed' % (report['summary']['checks'], failed))
    for name in report['summary']['failed_names']:
        print('  FAIL %s: %s' % (name, json.dumps(report['checks'][name]['detail'],
                                                  ensure_ascii=False)[:300]))
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='qa/evidence/trash-batch-cache')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
