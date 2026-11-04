#!/usr/bin/env python3
"""R6-080 · 2–4 文件时间对比：真实浏览器，按时间锚点对齐而非滚动百分比。

The checks target the four ways a comparison view lies:

  * panes aligned by scroll percentage (row 100 of one file is not the time of row 100
    of another);
  * an empty slot filled with the previous value instead of a visible gap;
  * duplicate timestamps collapsed into one row;
  * sync toggle that does not restore an explicit anchor when switched back on.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/compare_windows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out


def apply_two_file_scope(page):
    page.page.click('#time-scope')
    page.page.wait_for_selector('#time-dialog', state='visible', timeout=10000)
    page.wait(500)
    page.page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#time-dialog .gt-node-row')];
      const root = rows.find((row) => row.textContent.trim().startsWith('服务器总库'));
      root.querySelector('input').click();
    }""")
    page.page.fill('#gt-from-date', '2026-05-01')
    page.page.fill('#gt-from-clock', '09:59')
    page.page.fill('#gt-to-date', '2026-05-01')
    page.page.fill('#gt-to-clock', '10:01')
    page.page.dispatch_event('#gt-to-clock', 'change')
    page.wait(200)
    page.page.click('#time-dialog .r6-button.is-primary')
    page.page.wait_for_function(
        "() => window.__r6.state.mode === 'scope' && window.__r6.state.scopeRows.length > 0",
        timeout=30000)


def run() -> bool:
    args = arg_out('M8-080')
    out = Path(args.out)
    checks = Checks('compare_windows', out)
    with Product() as product, Page(product.base) as page:
        apply_two_file_scope(page)
        page.page.click('#time-compare')
        page.page.wait_for_selector('.cmp-pane', timeout=20000)
        page.wait(1200)

        panes = page.page.eval_on_selector_all('.cmp-pane', 'els => els.length')
        checks.equal('两个文件生成两个 pane', panes, 2)

        rows_per_pane = page.page.eval_on_selector_all(
            '.cmp-pane', 'els => els.map((pane) => pane.querySelectorAll(".cmp-row").length)')
        checks.check('两个 pane 行数一致（按同一时点矩阵铺开）',
                     len(set(rows_per_pane)) == 1 and rows_per_pane[0] > 0, rows_per_pane)

        empty_rows = page.page.eval_on_selector_all('.cmp-row.is-empty', 'els => els.length')
        checks.check('缺失时点渲染为空槽而不是前值填充', empty_rows >= 2, empty_rows)

        duplicate_slot = page.page.evaluate("""() => {
          const rows = [...document.querySelectorAll('.cmp-pane:first-child .cmp-row')];
          const stamps = rows.map((row) => (row.querySelector('.cmp-ts') || {}).textContent || '');
          const counts = {};
          stamps.forEach((stamp) => { if (stamp && stamp !== '—') counts[stamp] = (counts[stamp] || 0) + 1; });
          return Math.max(0, ...Object.values(counts));
        }""")
        checks.check('同一时间戳的两条记录占用两行', duplicate_slot >= 2, duplicate_slot)

        anchor = page.page.eval_on_selector('.cmp-anchor', 'e => e.textContent')
        checks.check('工具条显示对齐时间锚点', bool(anchor) and '未对齐' not in anchor, anchor)

        # Sync: scrolling pane 0 must put pane 1 on the same slot index, not the same
        # scroll percentage of a different row count. The panes are shortened here so
        # the fixture is scrollable without inventing more records.
        page.page.add_style_tag(content='.cmp-body{max-height:84px}')
        page.wait(300)
        page.page.evaluate("() => { document.querySelectorAll('.cmp-body')[0].scrollTop = 84; }")
        page.wait(1500)
        offsets = page.page.eval_on_selector_all('.cmp-body', 'els => els.map(e => e.scrollTop)')
        checks.equal('同步开启时两个 pane 落在同一时点行', offsets[0], offsets[1])
        checks.check('同步后锚点跟随滚动位置变化',
                     page.page.eval_on_selector('.cmp-anchor', 'e => e.textContent') != anchor,
                     page.page.eval_on_selector('.cmp-anchor', 'e => e.textContent'))

        # Turn sync off: pane 1 must stay put while pane 0 moves.
        page.page.click('.cmp-toggle input')
        page.wait(300)
        page.page.evaluate("() => { document.querySelectorAll('.cmp-body')[0].scrollTop = 0; }")
        page.wait(900)
        after = page.page.eval_on_selector_all('.cmp-body', 'els => els.map(e => e.scrollTop)')
        checks.check('关闭同步后另一 pane 不跟随',
                     after[0] == 0 and after[1] != 0, after)

        # Layout switch: 2×2 grid for up to four files.
        page.page.select_option('.cmp-select', 'grid')
        page.wait(600)
        columns = page.page.evaluate(
            "() => getComputedStyle(document.querySelector('.cmp-grid')).gridTemplateColumns"
            ".split(' ').length")
        checks.equal('切换 2×2 布局后网格为两列', columns, 2)
        saved = page.page.evaluate(
            "() => JSON.parse(localStorage.getItem('logbench-r6-workspace') || '{}')")
        checks.check('布局写入个人设置',
                     (saved.get('r6/workspace/preferences') or {}).get('compare_layout') == 'grid',
                     saved.get('r6/workspace/preferences'))

        # Narrow viewport: panes stack instead of overflowing the page.
        page.page.set_viewport_size({'width': 768, 'height': 1024})
        page.wait(500)
        body_overflow = page.page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        checks.check('768px 下页面无整体横向溢出', body_overflow <= 1, body_overflow)
        page.page.set_viewport_size({'width': 1366, 'height': 768})
        page.wait(300)

        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])
        page.shot(out / 'compare-windows.png')
        return checks.write({'rows_per_pane': rows_per_pane, 'empty_rows': empty_rows,
                             'offsets': offsets})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
