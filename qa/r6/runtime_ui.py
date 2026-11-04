#!/usr/bin/env python3
"""R6-083 · 运行记录、真实并发变更、审计与脱敏诊断：真实 API + 真实页面。

The only interesting claim is that a budget edit changes what the process actually does:
the parse pool spawns real Node workers, the query gate's slot count changes, and the
audit row records the before/after. A form that stores a number is not evidence.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/runtime_ui.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out


def run() -> bool:
    args = arg_out('M8-083')
    out = Path(args.out)
    checks = Checks('runtime_ui', out)
    with Product() as product, Page(product.base) as page:
        status, runtime = product.api('GET', '/api/v2/runtime')
        checks.equal('运行状态接口可用', status, 200)
        checks.check('解析池已启动并报告真实进程数',
                     runtime['pools']['parse']['workers'] >= 1,
                     runtime['pools']['parse']['workers'])
        checks.check('报告主机资源（RSS/磁盘）',
                     (runtime['resources']['rss_bytes'] or 0) > 0 and
                     (runtime['resources']['disk']['total_bytes'] or 0) > 0)
        checks.check('未请求 probe 时磁盘延迟为 null 而不是 0',
                     runtime['resources']['disk']['latency_ms'] is None)
        checks.check('任务列表带来源与文件', bool(runtime['jobs']['queue']) and
                     runtime['jobs']['queue'][0]['file'].endswith('.log'),
                     runtime['jobs']['queue'][0]['file'])
        checks.check('查询指标为空时报告 null 而不是 0',
                     runtime['queries']['first_page_p50_ms'] is None,
                     runtime['queries']['first_page_p50_ms'])
        checks.check('查询结果缓存如实标注未实现',
                     runtime['queries']['result_cache']['state'] == 'not_implemented')

        status, budget = product.api('GET', '/api/v2/runtime/budget')
        revision = budget['revision']
        baseline_workers = runtime['pools']['parse']['workers']
        status, updated = product.api('PUT', '/api/v2/runtime/budget',
                                      {'value': {'parse_slots': baseline_workers + 1},
                                       'expectedRevision': revision})
        checks.equal('预算保存成功', status, 200)
        checks.equal('服务端确认解析池进程数变化',
                     updated['applied']['parse']['workers'], baseline_workers + 1)
        status, runtime2 = product.api('GET', '/api/v2/runtime')
        checks.equal('再次读取确实是新进程数',
                     runtime2['pools']['parse']['workers'], baseline_workers + 1)

        status, updated2 = product.api('PUT', '/api/v2/runtime/budget',
                                       {'value': {'query_slots': 3},
                                        'expectedRevision': updated['revision']})
        checks.equal('查询槽位保存成功', status, 200)
        checks.equal('查询闸门槽位即时变化',
                     runtime2['queries']['slots']['slots'] if False else
                     updated2['applied']['query']['slots'], 3)

        status, conflict = product.api('PUT', '/api/v2/runtime/budget',
                                       {'value': {'query_slots': 1},
                                        'expectedRevision': revision})
        checks.equal('过期修订号被拒绝', status, 409)
        status, impossible = product.api('PUT', '/api/v2/runtime/budget',
                                         {'value': {'parse_slots': 7, 'query_slots': 7,
                                                    'total_cpu': 8},
                                          'expectedRevision': updated2['revision']})
        checks.equal('超出总预算被拒绝', status, 400)
        checks.equal('拒绝原因明确', impossible['error']['code'], 'invalid_budget')

        status, text = product.api('GET', '/api/v2/runtime/diagnostics')
        body = json.dumps(text, ensure_ascii=False)
        checks.equal('诊断导出可用', status, 200)
        checks.check('诊断不含文件名', 'psg-a.log' not in body)
        checks.check('诊断不含数据目录路径', str(product.data) not in body)
        checks.check('诊断声明脱敏范围', '不含日志原文' in body)

        # ------------------------------------------------------------------ interface
        page.page.click('.r6-nav-item[data-view="runtime"]')
        page.page.wait_for_selector('.rt-stats', timeout=20000)
        page.wait(600)
        stats = page.page.eval_on_selector_all('.runtime-view .rt-stat-value',
                                              'els => els.map(e => e.textContent)')
        checks.check('运行页显示队列/积压/查询指标', len(stats) >= 6, stats)
        job_rows = page.page.eval_on_selector_all('.rt-table tbody tr', 'els => els.length')
        checks.check('任务表渲染出任务行', job_rows >= 1, job_rows)

        page.page.click('.runtime-view .r6-tab:nth-child(2)')
        page.page.wait_for_selector('.rt-field', timeout=20000)
        page.wait(600)
        fields = page.page.eval_on_selector_all('.rt-field', 'els => els.length')
        checks.equal('预算表单包含 13 个字段', fields, 13)
        labels = page.page.eval_on_selector_all('.rt-apply', 'els => els.map(e => e.textContent)')
        checks.check('每个字段标注生效范围',
                     all(label in ('即时', '下个任务', '需重启', '—') for label in labels),
                     labels)
        checks.check('至少一个字段标注需重启（不假装全部即时）',
                     '需重启' in labels, labels)

        page.page.fill('#rt-query_slots', '4')
        page.page.dispatch_event('#rt-query_slots', 'input')
        page.wait(300)
        diff = page.page.eval_on_selector('.rt-diff', 'e => e.hidden ? "" : e.textContent')
        checks.check('保存前显示更改摘要', 'query_slots' in diff, diff)
        page.page.click('.rt-actions .r6-button.is-primary')
        page.page.wait_for_selector('.rt-result:not([hidden])', timeout=20000)
        page.wait(500)
        result = page.page.eval_on_selector('.rt-result', 'e => e.textContent')
        checks.check('保存后显示服务端确认的生效结果',
                     '查询并发' in result and '4' in result, result[:120])
        status, runtime3 = product.api('GET', '/api/v2/runtime')
        checks.equal('页面保存确实改变了服务端槽位',
                     runtime3['queries']['slots']['slots'], 4)

        with product.app.store.connect() as db:
            rows = db.execute("SELECT detail FROM audit WHERE action='runtime.budget' "
                              "ORDER BY at DESC LIMIT 1").fetchone()
        detail = json.loads(rows['detail']) if rows else {}
        checks.check('审计记录包含修改前后值',
                     bool(detail.get('before')) and bool(detail.get('after')), detail.get('after'))

        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])
        page.shot(out / 'runtime.png')
        return checks.write({'workers_after': updated['applied']['parse'],
                             'query_slots': runtime3['queries']['slots']})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
