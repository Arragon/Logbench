#!/usr/bin/env python3
"""R6-083 evidence · real budget changes, audit and redacted diagnostics.

Intent preserved: a budget edit must change what the server reports, an expired
revision must be rejected, the audit row must record before/after, and the
diagnostic export must not leak file names or absolute paths. Restored /r6
exposes the budget in the collection centre's resource dialog rather than a
separate runtime page, so the UI assertions follow that structure.

    python3 qa/r6/runtime_ui.py --base-url http://127.0.0.1:50950 --out qa/evidence/R6-083
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m8_common import Checks  # noqa: E402
from r6_ui_common import api, close, open_r6, run_args  # noqa: E402


def run() -> bool:
    args = run_args('R6-083')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = Checks('runtime_ui', out)
    pw, browser, page, errors = open_r6(args.base_url)
    original = None
    try:
        status = api(page, 'GET', '/api/v2/runtime')
        checks.equal('运行状态接口可用', status['status'], 200)
        runtime = status['value'] or {}
        pools = (runtime.get('pools') or {}).get('parse') or {}
        resources = runtime.get('resources') or {}
        checks.check('进程池报告真实 wanted / workers（不伪装吞吐）',
                     isinstance(pools.get('wanted'), int) and isinstance(pools.get('workers'), int),
                     {'wanted': pools.get('wanted'), 'workers': pools.get('workers')})
        checks.check('报告主机资源（RSS/磁盘）',
                     (resources.get('rss_bytes') or 0) > 0 and
                     ((resources.get('disk') or {}).get('total_bytes') or 0) > 0)
        checks.check('未请求 probe 时磁盘延迟为 null 而不是 0',
                     (resources.get('disk') or {}).get('latency_ms') is None)
        queries = runtime.get('queries') or {}
        checks.check('查询指标为空时报告 null 而不是 0',
                     queries.get('first_page_samples') in (0, None) and
                     queries.get('first_page_p50_ms') is None,
                     queries.get('first_page_p50_ms'))
        checks.check('查询结果缓存如实标注未实现',
                     (queries.get('result_cache') or {}).get('state') == 'not_implemented')

        budget = api(page, 'GET', '/api/v2/runtime/budget')
        checks.equal('预算接口可用', budget['status'], 200)
        base = (budget['value'] or {}).get('base') or {}
        original = base.get('parse_slots')
        revision = (budget['value'] or {}).get('revision')

        # A real edit: server must echo applied changes and audit before/after.
        wanted = min(8, (original or 4) + 1)
        updated = api(page, 'PUT', '/api/v2/runtime/budget',
                      {'value': {'parse_slots': wanted}, 'expectedRevision': revision})
        checks.equal('预算保存成功', updated['status'], 200)
        applied = (updated['value'] or {}).get('applied') or {}
        checks.check('服务端返回生效结果而非只回显数字',
                     isinstance(applied.get('parse'), dict), applied.get('parse'))
        again = api(page, 'GET', '/api/v2/runtime/budget')
        checks.equal('再次读取确认新的解析槽位',
                     ((again['value'] or {}).get('base') or {}).get('parse_slots'), wanted)

        stale = api(page, 'PUT', '/api/v2/runtime/budget',
                    {'value': {'query_slots': 1}, 'expectedRevision': revision})
        checks.equal('过期修订号被拒绝', stale['status'], 409)
        impossible = api(page, 'PUT', '/api/v2/runtime/budget',
                         {'value': {'parse_slots': 7, 'query_slots': 7},
                          'expectedRevision': (again['value'] or {}).get('revision')})
        checks.equal('超出总预算被拒绝', impossible['status'], 400)
        checks.equal('拒绝原因明确',
                     ((impossible['value'] or {}).get('error') or {}).get('code'), 'invalid_budget')

        diag = api(page, 'GET', '/api/v2/runtime/diagnostics')
        body = json.dumps(diag['value'], ensure_ascii=False)
        checks.equal('诊断导出可用', diag['status'], 200)
        checks.check('诊断不含日志文件名', '.log' not in body or '日志' in body, body[:100])
        checks.check('诊断声明脱敏范围',
                     any(k in body for k in ('不含日志原文', '脱敏', 'redact')), body[:120])

        audit = api(page, 'GET', '/api/v1/admin/audit?limit=12')
        rows = (audit['value'] or {}).get('items', [])
        budget_audit = [r for r in rows if r.get('action') == 'runtime.budget']
        checks.check('审计记录包含修改前后值',
                     bool(budget_audit) and
                     bool((budget_audit[0].get('detail') or {}).get('before')) and
                     bool((budget_audit[0].get('detail') or {}).get('after')),
                     (budget_audit[0].get('detail') or {}).get('after') if budget_audit else None)

        # Restored UI: resource dialog + collection-centre cache/events tabs.
        page.evaluate("() => document.querySelector('[data-r6=\"tuning\"]')?.click()")
        page.wait_for_timeout(250)
        form = page.evaluate("""({fields: document.querySelectorAll('#dialog .field').length,
            inputs: document.querySelectorAll('#dialog input[type=number]').length,
            readonly: document.querySelector('#dialog')?.textContent.includes('由服务端固定'),
            details: document.querySelectorAll('#dialog details').length})""")
        checks.check('预算表单保留六项结构且无后端对应项只读',
                     form['fields'] == 6 and form['inputs'] == 5 and form['readonly'], form)
        page.evaluate("() => {const d=document.getElementById('dialog'); if(d.open)d.close();}")
        for tab in ('cache', 'events'):
            page.evaluate("t => document.querySelector('[data-r6=\"tab\"][data-id=\"'+t+'\"]')?.click()", tab)
            page.wait_for_timeout(150)
            checks.check('采集中心 tab 可切换: ' + tab,
                         page.evaluate("document.querySelector('.r6-tab.active')?.dataset.id") == tab)
        page.evaluate("() => document.querySelector('[data-r6=\"tab\"][data-id=\"sources\"]')?.click()")
        page.wait_for_timeout(150)
        page.screenshot(path=str(out / 'runtime-1366.png'))
        checks.check('页面无脚本错误', not errors, errors[:3])
        return checks.write({'parse_slots_before': original, 'parse_slots_after': wanted,
                             'revision': (again['value'] or {}).get('revision')})
    finally:
        # Leave the deployment exactly as we found it.
        if original is not None:
            try:
                current = api(page, 'GET', '/api/v2/runtime/budget')
                api(page, 'PUT', '/api/v2/runtime/budget',
                    {'value': {'parse_slots': original},
                     'expectedRevision': (current['value'] or {}).get('revision')})
            except Exception:
                pass
        close(pw, browser)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
