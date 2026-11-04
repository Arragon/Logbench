#!/usr/bin/env python3
"""R6-084 · Gate M8：跨来源筛选、时间对比、规则与管理 UI 的产品级验收。

This is the gate, not another unit test. It drives the product surface in a real
browser over the real server, and it deliberately re-checks the R5 capabilities that
this milestone touched indirectly (import → search → highlight → drawer → timeline),
because "the new pages work" is not the same claim as "the old reading path still
does".

It also re-asserts the two promises that are easiest to break while adding pages:

  * a partial or failed child never becomes an exact count of zero;
  * nothing that leaves the page names a local file or carries its text.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/product_workflows.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out, local_file

SUBSCRIPT_REPORTS = ('mixed_scope', 'global_time', 'compare_windows', 'rule_impact',
                     'cache_manager', 'runtime_ui')


def apply_scope(page, from_clock='09:59', to_clock='10:01'):
    page.page.click('#time-scope')
    page.page.wait_for_selector('#time-dialog', state='visible', timeout=10000)
    page.wait(500)
    page.page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#time-dialog .gt-node-row')];
      const root = rows.find((row) => row.textContent.trim().startsWith('服务器总库'));
      root.querySelector('input').click();
    }""")
    page.page.fill('#gt-from-date', '2026-05-01')
    page.page.fill('#gt-from-clock', from_clock)
    page.page.fill('#gt-to-date', '2026-05-01')
    page.page.fill('#gt-to-clock', to_clock)
    page.page.dispatch_event('#gt-to-clock', 'change')
    page.wait(200)
    page.page.click('#time-dialog .r6-button.is-primary')
    page.page.wait_for_function(
        "() => window.__r6.state.mode === 'scope' && window.__r6.state.scopeRows.length > 0",
        timeout=30000)


def run() -> bool:
    args = arg_out('M8-gate')
    out = Path(args.out)
    checks = Checks('product_workflows', out)
    with Product() as product, Page(product.base) as page:
        local = local_file(Path(product.tmp) / 'browser-only.log')
        marker = 'LOCALFILE-XYZZY'
        imported = page.import_local(local)
        checks.check('R5：本机文件通过真实文件输入导入', imported >= 1, imported)

        # ---------------------------------------------------------- R5 reading path
        page.page.evaluate("() => { const file = document.querySelector('.r6-nav-file'); file.click(); }")
        page.page.wait_for_function("() => window.__r6.state.total > 0", timeout=30000)
        page.wait(600)
        rows = page.page.eval_on_selector_all('#rows .r6-row', 'els => els.length')
        checks.check('R5：打开文件后渲染出记录行', rows >= 2, rows)
        timeline = page.page.eval_on_selector_all('#timeline-nodes .r6-node', 'els => els.length')
        checks.check('R5：时间线渲染出节点', timeline >= 1, timeline)

        page.page.fill('#search', 'LOCALFILE')
        page.page.dispatch_event('#search', 'input')
        page.page.press('#search', 'Enter')
        page.page.wait_for_function(
            "() => (document.querySelector('#search-count').textContent || '').includes('条')",
            timeout=20000)
        page.wait(900)
        marks = page.page.eval_on_selector_all('#rows mark', 'els => els.length')
        checks.check('R5：搜索命中并只对可见行高亮', marks >= 1, marks)

        page.page.evaluate("() => document.querySelector('#rows .r6-row').click()")
        page.page.wait_for_function("() => !document.getElementById('drawer').hidden", timeout=15000)
        drawer = page.page.eval_on_selector('#drawer-body', 'e => e.textContent')
        checks.check('R5：悬浮详情显示记录内容与证据', '时间' in drawer and len(drawer) > 20,
                     drawer[:60])
        page.page.keyboard.press('Escape')

        # ------------------------------------------------------- cross-source scope
        page.page.fill('#search', '')
        page.page.dispatch_event('#search', 'input')
        apply_scope(page)
        scope_rows = page.page.eval_on_selector_all('#rows .r6-row', 'els => els.length')
        count_text = page.page.eval_on_selector('#search-count', 'e => e.textContent')
        checks.check('M8：混合范围渲染记录', scope_rows >= 5, scope_rows)
        checks.check('M8：范围计数声明完成度而不是伪精确',
                     ('共' in count_text) or ('已载入' in count_text), count_text)

        # A scope that names a file the server cannot authorize must not become "0 条".
        partial = page.page.evaluate("""async () => {
          const csrf = (await (await fetch('/api/v1/me')).json()).csrfToken;
          const combined = LBCombinedProvider.createCombinedProvider({
            local: window.__r6.provider,
            localFactory: (id) => LBLocalProvider.createProvider({ client: window.__r6.client }),
            serverFactory: (id) => LBServerProvider.createProvider({ csrfToken: csrf, datasetId: id }),
            scope: { local: [], server: ['f_ffffffffffffffff'] }
          });
          await combined.search({ match_all: true, limit: 10, mode: 'time' });
          return combined.wait({ timeout_ms: 8000 });
        }""")
        checks.check('M8：无权限文件不产生精确计数（unknown/at_least，绝不是 exact）',
                     partial['count']['relation'] in ('unknown', 'at_least'),
                     partial['count'])
        checks.check('M8：失败文件在 coverage 中指名',
                     bool(partial['coverage']['failed']) and
                     partial['coverage']['failed'][0]['code'] in ('not_found', 'http_404'),
                     partial['coverage']['failed'])

        # --------------------------------------------------------------- compare
        page.page.click('#time-compare')
        page.page.wait_for_selector('.cmp-pane', timeout=20000)
        page.wait(800)
        panes = page.page.eval_on_selector_all('.cmp-pane', 'els => els.length')
        checks.equal('M8：时间对比为两个 pane', panes, 2)
        checks.check('M8：对比出现空槽（缺记录不补前值）',
                     page.page.eval_on_selector_all('.cmp-row.is-empty', 'els => els.length') >= 1)
        page.page.click('.r6-nav-item[data-view="logs"]')

        # ----------------------------------------------------------- admin pages
        for view, selector, label in (('rules', '.rule-table', '规则'),
                                      ('cache', '.cm-table tbody tr', '解析缓存'),
                                      ('runtime', '.rt-stats', '运行')):
            page.page.click('.r6-nav-item[data-view="%s"]' % view)
            page.page.wait_for_selector(selector, timeout=25000)
            page.wait(400)
            checks.check('M8：%s页在管理员身份下可用' % label,
                         page.page.eval_on_selector_all(selector, 'els => els.length') >= 1)

        # Non-admin refusal is decided by the API, not by the hidden nav item.
        viewer = product.app.handle('GET', '/api/v2/runtime', {
            'Host': 'localhost', 'Content-Type': 'application/json'}, b'', '10.0.0.9')
        checks.equal('M8：非管理员读取运行状态被拒绝', viewer.status, 403)
        viewer_rules = product.app.handle('POST', '/api/v2/rules/impact', {
            'Host': 'localhost', 'Content-Type': 'application/json',
            'Origin': 'http://localhost', 'X-LogBench-CSRF': product.app.csrf('10.0.0.9')},
            json.dumps({'profile': {}}).encode(), '10.0.0.9')
        checks.equal('M8：非管理员预览规则影响被拒绝', viewer_rules.status, 403)

        # ------------------------------------------------------- privacy & errors
        leaked = [item['url'] for item in page.requests
                  if marker in ((item.get('url') or '') + (item.get('body') or ''))
                  or 'browser-only.log' in ((item.get('url') or '') + (item.get('body') or ''))]
        checks.check('M8：网络请求不含本机文件名或原文', not leaked, leaked[:3])
        # The 404 above is provoked on purpose; the browser logs every failed
        # resource load, so only unexpected script errors count here.
        unexpected = [item for item in page.errors()
                      if 'Failed to load resource' not in item['text']]
        checks.check('M8：页面无（非预期）脚本错误', not unexpected, unexpected[:3])

        page.shot(out / 'product-workflows.png')

        # ---------------------------------------------------------------- aggregate
        prior = {}
        for name in SUBSCRIPT_REPORTS:
            # Each script writes into its own evidence directory; the gate reads them
            # from there rather than from its own, so the numbers stay traceable.
            candidates = [out / (name + '.json'),
                          out.parent / ('M8-' + name) / (name + '.json')]
            for path in candidates:
                if path.is_file():
                    payload = json.loads(path.read_text('utf-8'))
                    prior[name] = {'ok': payload.get('ok'), 'total': payload.get('total'),
                                   'failed': payload.get('failed'),
                                   'source': str(path.relative_to(out.parent.parent))}
                    break
        missing = [name for name in SUBSCRIPT_REPORTS if name not in prior]
        checks.check('Gate 汇总了 6 个 M8 子脚本结果', not missing, missing)
        checks.check('所有子脚本通过', all(item['ok'] for item in prior.values()),
                     {name: item['ok'] for name, item in prior.items()})
        return checks.write({'sub_reports': prior, 'missing': missing})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
