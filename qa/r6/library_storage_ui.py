#!/usr/bin/env python3
"""R6-056 evidence · unified library and storage roots on the restored /r6 DOM.

Intent preserved: the workbench navigation stays compact, roots/library come
from the real API, no viewport forces horizontal scrolling, the data area has
a real height, and a non-admin gets read-only rather than hidden dead links.

    python3 qa/r6/library_storage_ui.py --base-url http://127.0.0.1:50950 --out qa/evidence/R6-056
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m8_common import Checks  # noqa: E402
from r6_ui_common import api, close, open_r6, run_args  # noqa: E402


def run() -> bool:
    args = run_args('R6-056')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = Checks('library_storage_ui', out)
    pw, browser, page, errors = open_r6(args.base_url)
    try:
        roots = api(page, 'GET', '/api/v2/storage-roots')
        checks.equal('存储位置接口可用', roots['status'], 200)
        items = (roots['value'] or {}).get('items', [])
        checks.check('服务端返回存储位置', isinstance(items, list), len(items))
        for row in items:
            checks.check('登记含 id 与绝对路径: ' + str(row.get('id')),
                         bool(row.get('id')) and str(row.get('path', '')).startswith('/'),
                         {k: row.get(k) for k in ('id', 'label', 'path', 'state')})

        # Storage tab lives inside the collection centre in the restored design.
        page.evaluate("() => document.querySelector('[data-r6=\"tab\"][data-id=\"storage\"]')?.click()")
        page.wait_for_timeout(200)
        ui_rows = page.locator('.r6-root-table tbody tr').count()
        checks.equal('存储位置表格行数与服务端一致', ui_rows, len(items))
        first = page.locator('.r6-root-table tbody tr').first.inner_text() if ui_rows else ''
        if items:
            checks.check('表格显示真实位置 ID',
                         str(items[0].get('id')) in first, first[:120])
        checks.check('离线位置不被当成空库清除',
                     '0 个文件' not in first or ui_rows == 0, first[:120])

        # Library view: unified shell, real catalogue (no fixture filenames).
        page.evaluate("() => window.__r6.showView('files')")
        page.wait_for_timeout(250)
        shell = page.evaluate("""({shell: !!document.querySelector('.library-shell'),
            folders: !!document.querySelector('.folder-pane'),
            main: !!document.querySelector('.library-main'),
            heading: document.querySelector('.page-heading h1')?.textContent})""")
        checks.check('统一文件库外壳完整',
                     shell['shell'] and shell['folders'] and shell['main'], shell)
        checks.check('唯一文件表格，没有两套文件库切换',
                     page.locator('.file-table').count() == 1 and
                     page.locator('[data-r6=\"local-library\"]').count() == 0 and
                     page.locator('.r6-filebar').count() == 0)
        checks.check('规则显示没有对象字符串',
                     '[object Object]' not in page.locator('.library-shell').inner_text())
        files = page.evaluate("window.LogBenchPrototype.getFiles().length")
        library = api(page, 'GET', '/api/v2/library')
        api_count = len((library['value'] or {}).get('items', []))
        checks.equal('文件语料来自真实目录而非内置 fixture', files, api_count)

        for width, height in ((1366, 768), (1024, 768), (768, 1024), (375, 812)):
            page.set_viewport_size({'width': width, 'height': height})
            page.wait_for_timeout(220)
            box = page.evaluate("""({scroll: document.documentElement.scrollWidth,
                client: document.documentElement.clientWidth,
                pane: Math.round((document.querySelector('.library-shell') ||
                    document.querySelector('.r6-pane')).getBoundingClientRect().height),
                reachable: (()=>{const e=document.querySelector('.file-table-wrap');
                    return !e || e.scrollHeight <= e.clientHeight ||
                    (e.scrollTop = e.scrollHeight, e.scrollTop > 0);})()})""")
            checks.check('视口 %dx%d 无横向溢出且数据区可见' % (width, height),
                         box['scroll'] == box['client'] and box['pane'] > 0 and box['reachable'], box)

        page.set_viewport_size({'width': 1366, 'height': 768})
        page.screenshot(path=str(out / 'library-1366.png'))
        checks.check('页面无脚本错误', not errors, errors[:3])

        # Read-only identity: writes must be blocked, not silently hidden.
        member = {
            'user': {'ip': '127.0.0.1', 'name': 'member', 'role': 'member',
                     'source': 'local', 'id': 'member-1'},
            'capabilities': {'read': True, 'admin': False},
            'csrfToken': 'test', 'version': '3.0.3', 'identityMode': 'local'
        }
        page.route('**/api/v1/me', lambda route, request: route.fulfill(
            status=200, content_type='application/json',
            body=json.dumps(member, ensure_ascii=False)))
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function("() => window.LB && window.LB.state.scenario !== 'loading'", timeout=30000)
        page.wait_for_timeout(250)
        readonly = page.evaluate("""({admin: window.LB.state.admin,
            addDisabled: !!document.querySelector('.r6-head [data-r6="new-root"]')?.disabled,
            status: document.querySelector('.app-status .row')?.textContent.trim()})""")
        checks.check('非管理员降级为只读且状态可读',
                     not readonly['admin'] and '只读' in (readonly['status'] or ''), readonly)
        return checks.write({'roots': len(items), 'library_files': files})
    finally:
        close(pw, browser)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
