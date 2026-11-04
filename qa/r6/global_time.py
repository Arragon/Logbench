#!/usr/bin/env python3
"""R6-079 · 跨来源全局时间筛选：真实浏览器 UI，含保存/恢复阅读视角。

Driven through the interface, not through the module API, because the failure modes
worth catching are in the wiring: a dialog that opens but cannot apply, a toolbar
entry that pushes the reader down a row, a saved view that forgets which folders were
selected, and a reload that restores nothing.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/global_time.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out, local_file

RANGE = ('2026-05-01T09:59', '2026-05-01T10:01')


def set_range(page):
    page.page.fill('#gt-from-date', RANGE[0][:10])
    page.page.fill('#gt-from-clock', RANGE[0][11:])
    page.page.fill('#gt-to-date', RANGE[1][:10])
    page.page.fill('#gt-to-clock', RANGE[1][11:])
    page.page.dispatch_event('#gt-to-clock', 'change')


def run() -> bool:
    args = arg_out('M8-079')
    out = Path(args.out)
    checks = Checks('global_time', out)
    with Product() as product, Page(product.base) as page:
        local = local_file(Path(product.tmp) / 'browser-only.log')
        page.import_local(local)
        # The dialog accepts a prompt for a view name; auto-accept it.
        page.page.on('dialog', lambda dialog: dialog.accept('联调视角'))

        toolbar_height = page.page.evaluate(
            "() => document.getElementById('toolbar').getBoundingClientRect().height")
        checks.check('时间入口不独占一行（工具条 ≤ 44px）', toolbar_height <= 44,
                     toolbar_height)

        page.page.click('#time-scope')
        page.page.wait_for_selector('#time-dialog', state='visible', timeout=10000)
        page.wait(400)
        roots = page.page.eval_on_selector_all('#time-dialog .gt-node-row', 'els => els.length')
        checks.check('树中同时列出本机与服务器来源', roots >= 3, roots)

        # Select the shared folder: both server files, descendants included.
        page.page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#time-dialog .gt-node-row')];
          const target = rows.find((row) => row.textContent.trim().startsWith('服务器总库'));
          if (!target) throw new Error('shared root missing');
          target.querySelector('input').click();
        }""")
        page.wait(200)
        selected = page.page.eval_on_selector('#time-dialog .gt-foot .r6-meta',
                                             'e => e.textContent')
        checks.check('选择目录后文件数为 2', '已选 2 个文件' in selected, selected)

        # Cancel one file inside the selected folder: partial selection must be visible.
        page.page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#time-dialog .gt-node-row')];
          const file = rows.find((row) => row.textContent.trim().startsWith('psg-b.log'));
          if (!file) throw new Error('psg-b.log missing');
          file.querySelector('input').click();
        }""")
        page.wait(200)
        partial = page.page.eval_on_selector('#time-dialog .gt-foot .r6-meta',
                                            'e => e.textContent')
        checks.check('取消单个文件后为 1 个文件且标注部分选择',
                     '已选 1 个文件' in partial and '部分选择' in partial, partial)

        # Re-select it, set the range, apply.
        page.page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#time-dialog .gt-node-row')];
          const file = rows.find((row) => row.textContent.trim().startsWith('psg-b.log'));
          file.querySelector('input').click();
        }""")
        set_range(page)
        page.wait(300)
        checks.check('合法范围使“应用范围”可用',
                     not page.page.eval_on_selector('#time-dialog .r6-button.is-primary',
                                                    'e => e.disabled'))
        page.page.click('#time-dialog .r6-button.is-primary')
        page.page.wait_for_function(
            "() => window.__r6.state.mode === 'scope' && window.__r6.state.scopeRows.length > 0",
            timeout=30000)
        rows = page.page.eval_on_selector_all('#rows .r6-row', 'els => els.length')
        text = page.page.eval_on_selector('#search-count', 'e => e.textContent')
        checks.check('应用后阅读区渲染出记录', rows >= 5, rows)
        checks.check('计数文案给出条数与完成度', ('共' in text) or ('已载入' in text), text)
        checks.check('范围状态包含来源计数',
                     '本机' in page.page.eval_on_selector('#status', 'e => e.textContent'), '')

        # Inverted range must be refused, and the applied result must not change.
        before_rows = page.page.evaluate("() => window.__r6.state.scopeRows.length")
        page.page.click('#time-scope')
        page.page.wait_for_selector('#time-dialog', state='visible')
        page.page.fill('#gt-from-date', RANGE[1][:10])
        page.page.fill('#gt-from-clock', RANGE[1][11:])
        page.page.fill('#gt-to-date', RANGE[0][:10])
        page.page.fill('#gt-to-clock', RANGE[0][11:])
        page.page.dispatch_event('#gt-to-clock', 'change')
        page.wait(200)
        error_text = page.page.eval_on_selector('#time-dialog .r6-errorbox',
                                               'e => e.hidden ? "" : e.textContent')
        apply_disabled = page.page.eval_on_selector('#time-dialog .r6-button.is-primary',
                                                    'e => e.disabled')
        checks.check('起点晚于终点时给出错误并禁用应用',
                     ('开始时间不能晚于结束时间' in error_text) and apply_disabled, error_text)
        page.page.keyboard.press('Escape')
        page.wait(200)
        checks.equal('取消不改变当前结果',
                     page.page.evaluate("() => window.__r6.state.scopeRows.length"),
                     before_rows)

        # Save a view, reload the page, restore it.
        page.page.click('#time-scope')
        page.page.wait_for_selector('#time-dialog', state='visible')
        set_range(page)
        page.wait(200)
        page.page.click('#time-dialog .r6-button:has-text("保存视角")')
        page.wait(400)
        saved = page.page.evaluate(
            "() => JSON.parse(localStorage.getItem('logbench-r6-time-views') || '{}')")
        checks.check('视角已保存到本机（含目录选择与时间）',
                     bool(saved.get('views')) and bool((saved.get('views') or [{}])[0].get('include')), saved)

        page.page.reload(wait_until='load')
        page.page.wait_for_function('() => window.__r6Ready === true', timeout=45000)
        page.page.click('#time-scope')
        page.page.wait_for_selector('#time-dialog', state='visible')
        page.wait(300)
        page.page.click('#time-dialog .gt-list-item:has-text("联调视角")')
        page.wait(300)
        restored = page.page.eval_on_selector('#time-dialog .gt-foot .r6-meta',
                                             'e => e.textContent')
        checks.check('刷新后恢复视角并重新解析出 2 个文件',
                     '已选 2 个文件' in restored, restored)
        page.page.click('#time-dialog .r6-button.is-primary')
        page.page.wait_for_function(
            "() => window.__r6.state.mode === 'scope' && window.__r6.state.scopeRows.length > 0",
            timeout=30000)
        checks.check('恢复后的范围同样产生记录',
                     page.page.eval_on_selector_all('#rows .r6-row', 'els => els.length') >= 5)

        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])
        page.shot(out / 'global-time.png')
        return checks.write({'toolbar_height': toolbar_height, 'saved_views': len(saved.get('views', []))})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
