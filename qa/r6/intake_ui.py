"""R6-068 evidence: 采集中心 list, detail drawer and real status, in a real browser.

The script drives `/r6` against a running server. It asserts the properties the
ticket names, and it refuses to pass on a page that shows numbers nobody measured:

* the table is the primary surface and the detail drawer does not move it;
* a source that has never collected shows 0 bytes, not a synthetic percentage;
* pause/scan actions post with an idempotent action id and surface the outcome;
* the four tabs exist, and the two that belong to M5/M8 say so instead of
  inventing content;
* 1366×768 keeps a usable table height, and 375×812 does not overflow the body.

Usage:
    python qa/r6/intake_ui.py --base-url http://127.0.0.1:50950 --out qa/evidence/R6-068
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'qa' / 'evidence' / 'R6-068'
VIEWPORTS = [(1366, 768), (1920, 1080), (768, 1024), (375, 812)]


def csrf(page):
    return page.evaluate("""async () => {
        const r = await fetch('/api/v1/me', {credentials: 'same-origin'});
        const v = await r.json();
        return v.csrfToken;
    }""")


def api(page, method, path, body=None):
    token = csrf(page)
    return page.evaluate("""async ([method, path, body, token]) => {
        const init = {method, credentials: 'same-origin', headers: {'X-LogBench-CSRF': token}};
        if (body !== null) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
        const response = await fetch(path, init);
        return {status: response.status, value: await response.json().catch(() => ({}))};
    }""", [method, path, body, token])


def seed(page, base_url):
    created = api(page, 'POST', '/api/v2/sources', {
        'adapter': 'http',
        'label': 'QA · 本机 HTTP 源',
        'endpoint': base_url + '/var/log/app.log',
        'policy': {'allowedHosts': ['127.0.0.1'], 'allowLoopback': True},
        'mappings': [{'remoteDir': '/var/log/', 'rootId': 'legacy',
                      'targetSubdir': 'qa/http', 'mode': 'scheduled'}],
    })
    if created['status'] == 200:
        return {'status': 200, 'item': created['value']['item'], 'created': True}
    listed = api(page, 'GET', '/api/v2/sources?limit=50')
    items = listed.get('value', {}).get('items', []) if listed.get('value') else []
    existing = next((entry for entry in items if entry.get('label') == 'QA · 本机 HTTP 源'), None)
    if existing:
        return {'status': 200, 'item': existing, 'created': False,
                'first': created}
    return {'status': created['status'], 'item': None, 'first': created}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:50950')
    parser.add_argument('--out', default=str(OUT))
    parser.add_argument('--headful', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    report = {'base_url': args.base_url, 'viewports': {}, 'checks': [], 'failures': []}

    def check(name, ok, detail=None):
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['failures'].append({'name': name, 'detail': detail})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=not args.headful)
        for width, height in VIEWPORTS:
            context = browser.new_context(viewport={'width': width, 'height': height},
                                          device_scale_factor=1)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            label = '%dx%d' % (width, height)
            page.goto(args.base_url + '/r6', wait_until='domcontentloaded')
            page.wait_for_selector('#reader', timeout=15000)
            ready = False
            for _ in range(80):
                if page.evaluate('() => Boolean(window.__r6Ready)'):
                    ready = True
                    break
                page.wait_for_timeout(250)
            check(label + ' 页面初始化', ready, None)
            page.get_by_role('button', name='采集中心').click()
            page.wait_for_selector('#intake-view .r6-intake', timeout=10000)
            page.wait_for_timeout(400)

            entry = {'console_errors': errors}
            entry['tabs'] = page.locator('#intake-view .r6-tab').all_text_contents()
            check(label + ' 四标签存在', len(entry['tabs']) == 4, entry['tabs'])

            table = page.locator('#intake-view .r6-table-wrap')
            box = table.bounding_box() if table.count() else None
            entry['table_box'] = box
            check(label + ' 表格可见', bool(box and box['height'] > 80), box)

            body_width = page.evaluate('document.body.scrollWidth')
            check(label + ' 无横向溢出', body_width <= width + 2,
                  {'scrollWidth': body_width, 'viewport': width})

            # Seed one source through the real API, then read the page again.
            if width == 1366:
                created = seed(page, args.base_url)
                entry['seed'] = created
                check('创建源成功', created['status'] == 200 and bool(created['item']), created)
                page.get_by_role('button', name='刷新').click()
                page.wait_for_timeout(600)
                rows = page.locator('#intake-view .r6-table tbody tr')
                entry['rows'] = rows.count()
                check('源出现在列表', rows.count() >= 1, {'rows': rows.count()})
                seeded = rows.filter(has_text='QA · 本机 HTTP 源').first
                row_text = seeded.inner_text() if seeded.count() else ''
                entry['row_text'] = row_text
                check('未启用的源不显示完成态',
                      '未启用' in row_text or '已暂停' in row_text, row_text)
                check('归档量为真实 0', '0 B' in row_text, row_text)
                check('页面没有伪造百分比', '%' not in page.inner_text('#intake-view'),
                      page.inner_text('#intake-view')[:400])

                page.wait_for_selector('#intake-view .r6-table tbody tr', timeout=8000)
                page.wait_for_timeout(300)
                before = page.locator('#intake-view .r6-table-wrap').bounding_box()
                seeded.get_by_role('button', name='查看').click()
                page.wait_for_selector('#intake-view .r6-source-drawer:not([hidden])', timeout=5000)
                page.wait_for_timeout(400)
                after = page.locator('#intake-view .r6-table-wrap').bounding_box()
                entry['drawer_shift'] = {
                    'before': before, 'after': after,
                    'dx': round(after['x'] - before['x'], 2),
                    'dw': round(after['width'] - before['width'], 2)
                }
                check('详情浮层不移动表格',
                      abs(after['x'] - before['x']) <= 1 and abs(after['width'] - before['width']) <= 1,
                      entry['drawer_shift'])
                drawer_text = page.inner_text('#intake-view .r6-source-drawer')
                entry['drawer_text'] = drawer_text[:600]
                page.screenshot(path=str(out / ('intake-detail-%s.png' % label)))
                check('详情说明能力而非承诺', '连接能力' in drawer_text, drawer_text[:200])
                check('详情不显示密钥', 'super-secret' not in drawer_text, None)
                page.keyboard.press('Escape')
                page.wait_for_timeout(200)
                check('Esc 关闭详情', page.locator('#intake-view .r6-source-drawer[hidden]').count() == 1)

                # A disabled source cannot be paused: the button is disabled, and the
                # API refuses it even if the request is forged from the console.
                rows_locator = page.locator('#intake-view .r6-table tbody tr')
                failures = []
                inspected = 0
                for index in range(rows_locator.count()):
                    row = rows_locator.nth(index)
                    if 'QA · 本机 HTTP 源' not in row.inner_text():
                        continue
                    inspected += 1
                    check('种子源显示未启用', '未启用' in row.inner_text(), row.inner_text()[:120])
                    toggles = row.locator('.r6-row-actions button').filter(
                        has_text=re.compile('暂停|继续'))
                    for position in range(toggles.count()):
                        if not toggles.nth(position).is_disabled():
                            failures.append({'row': index, 'position': position})
                entry['disabled_row_toggles'] = {'inspected': inspected, 'enabled': failures}
                check('未启用源的启停按钮不可用', inspected > 0 and not failures,
                      entry['disabled_row_toggles'])
                forged = api(page, 'POST', '/api/v2/sources/%s/actions' % created['item']['id'],
                             {'action': 'pause',
                              'actionId': 'qa-forged-%d' % int(time.time() * 1000)})
                entry['forged_pause'] = forged
                check('伪造暂停请求被后端拒绝',
                      forged['status'] == 409 and forged['value']['error']['code'] == 'source_disabled',
                      forged)

                # Storage and cache tabs must describe their real owner.
                page.get_by_role('tab', name='远程日志源').click()
                page.wait_for_timeout(300)
                page.screenshot(path=str(out / 'intake-1366x768.png'))
                page.get_by_role('tab', name='存储位置').click()
                page.wait_for_timeout(200)
                storage_text = page.inner_text('#intake-view .r6-intake-body')
                entry['storage_text'] = storage_text[:300]
                check('存储位置标签不重复实现根管理', 'R6-056' in storage_text, storage_text[:120])
                page.get_by_role('tab', name='解析缓存').click()
                page.wait_for_timeout(200)
                cache_text = page.inner_text('#intake-view .r6-intake-body')
                check('解析缓存标签标明归属 M8', 'R6-082' in cache_text, cache_text[:120])
                page.get_by_role('tab', name='运行记录').click()
                page.wait_for_timeout(400)
                jobs_text = page.inner_text('#intake-view .r6-intake-body')
                entry['jobs_text'] = jobs_text[:300]

            if width != 1366:
                page.screenshot(path=str(out / ('intake-%s.png' % label)), full_page=False)
            entry['height_used'] = page.evaluate(
                "document.querySelector('#intake-view .r6-table-wrap')?.getBoundingClientRect().height || 0")
            report['viewports'][label] = entry
            context.close()
        browser.close()

    (out / 'intake-ui.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    if report['failures']:
        print(json.dumps(report['failures'], ensure_ascii=False, indent=2))
        return 1
    print('intake UI checks passed: %d' % len(report['checks']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
