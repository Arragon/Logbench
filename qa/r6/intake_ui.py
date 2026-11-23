#!/usr/bin/env python3
"""R6-068 evidence · collection centre against the restored /r6 DOM.

Intent preserved from the pre-restore script: the table is the primary
surface, the detail drawer must not squeeze it, a never-collected source shows
real zeros (not a synthetic percentage), all four tabs exist, and 1366x768 /
375x812 keep the data area usable.

    python3 qa/r6/intake_ui.py --base-url http://127.0.0.1:50950 --out qa/evidence/R6-068
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m8_common import Checks  # noqa: E402 - report writer, no old DOM dependency
from r6_ui_common import api, close, open_r6, run_args  # noqa: E402


def run() -> bool:
    args = run_args('R6-068')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = Checks('intake_ui', out)
    pw, browser, page, errors = open_r6(args.base_url)
    try:
        intake = api(page, 'GET', '/api/v2/intake')
        checks.equal('采集状态接口可用', intake['status'], 200)
        items = (intake['value'] or {}).get('items', [])
        checks.check('服务端返回源列表', isinstance(items, list), len(items))
        if items:
            raw = items[0]
            checks.check('未采集源如实报告 0 字节',
                         raw.get('archivedBytes') == 0 and raw.get('files') == 0,
                         {'bytes': raw.get('archivedBytes'), 'files': raw.get('files')})

        page.evaluate("() => document.querySelector('[data-r6=\"tab\"][data-id=\"sources\"]')?.click()")
        page.wait_for_timeout(200)
        rows = page.locator('.r6-table tbody tr').count()
        checks.check('日志源表格渲染出真实行', rows == len(items), {'ui': rows, 'api': len(items)})

        before = page.locator('.r6-table-scroll').bounding_box()
        page.evaluate("() => document.querySelector('.r6-table tbody [data-r6=\"source-detail\"]')?.click()")
        page.wait_for_timeout(250)
        drawer = page.locator('aside.r6-drawer').count()
        after = page.locator('.r6-table-scroll').bounding_box()
        checks.check('详情抽屉悬浮且不挤压表格', drawer == 1 and
                     bool(before) and bool(after) and abs(before['width'] - after['width']) < 2,
                     {'before': before, 'after': after})
        checks.check('抽屉显示真实采集链路',
                     page.locator('aside.r6-drawer .r6-steps li').count() == 3)

        page.evaluate("() => document.querySelector('[data-r6=\"close-detail\"]')?.click()")
        page.wait_for_timeout(150)

        tabs = page.locator('.r6-tabs .r6-tab').all_text_contents()
        checks.check('四个采集 tab 均存在', len(tabs) == 4, tabs)
        for tab in ('storage', 'cache', 'events', 'sources'):
            page.evaluate("t => document.querySelector('[data-r6=\"tab\"][data-id=\"'+t+'\"]')?.click()", tab)
            page.wait_for_timeout(120)
            checks.check('tab 可切换: ' + tab,
                         page.evaluate("document.querySelector('.r6-tab.active')?.dataset.id") == tab)

        height = page.evaluate("Math.round(document.querySelector('.r6-pane').getBoundingClientRect().height)")
        checks.check('1366×768 数据区可用', height >= 550, height)
        page.screenshot(path=str(out / 'intake-1366.png'))
        checks.check('页面无脚本错误', not errors, errors[:3])

        page.set_viewport_size({'width': 375, 'height': 812})
        page.wait_for_timeout(300)
        narrow = page.evaluate("""({scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth,
            main: Math.round(document.querySelector('main.main').getBoundingClientRect().height)})""")
        checks.check('375×812 无横向溢出且主内容可见',
                     narrow['scroll'] == narrow['client'] and narrow['main'] > 0, narrow)
        page.screenshot(path=str(out / 'intake-375.png'))
        return checks.write({'sources': len(items), 'data_area': height})
    finally:
        close(pw, browser)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
