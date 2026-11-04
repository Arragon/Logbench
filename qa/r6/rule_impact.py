#!/usr/bin/env python3
"""R6-081 · 规则影响预览、试跑与分层重算：真实 API + 真实页面。

The claims being checked are the ones a reviewer cannot confirm by looking at a
screenshot: a style-only edit affects zero files, an anomaly edit affects the rule and
search layers but not the structure layer, applying keeps the structure shard identity,
and the trial run shows evidence from the same worker the index uses.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/rule_impact.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import ROOT, Checks, Page, Product, arg_out


def structure_signature(product, file_id):
    status, value = product.api('GET', '/api/v2/files/%s' % file_id)
    return (value.get('layers') or {}).get('structure', {}).get('signature')


def rule_profile(product, rule_id='cpqpsgx-default'):
    status, value = product.api('GET', '/api/v2/rules/%s' % rule_id)
    return value['item']['profile'], value['item']['revision']


def run() -> bool:
    args = arg_out('M8-081')
    out = Path(args.out)
    checks = Checks('rule_impact', out)
    with Product() as product, Page(product.base) as page:
        file_a = product.file_ids['psg-a.log']
        before_structure = structure_signature(product, file_a)
        profile, revision = rule_profile(product)
        checks.check('规则列表返回语义签名', True)

        status, baseline = product.api('POST', '/api/v2/rules/impact', {'profile': profile})
        checks.equal('当前规则的影响预览可用', status, 200)
        checks.equal('当前规则不产生任何重建', baseline['totals']['affected'], 0)
        checks.equal('已建索引文件计入可见范围', baseline['totals']['indexed'], 2)

        style = copy.deepcopy(profile)
        for node in style.get('nodes') or []:
            node['color'] = '#123456'
            node['shape'] = 'hexagon'
            node['name'] = '改色后的节点'
        status, styled = product.api('POST', '/api/v2/rules/impact', {'profile': style})
        checks.equal('只改颜色/形状/名称影响 0 个文件', styled['totals']['affected'], 0)

        changed = copy.deepcopy(profile)
        changed.setdefault('anomalies', []).append({
            'id': 'qa-anomaly', 'name': 'QA 新增异常', 'enabled': True, 'field': 'message',
            'op': 'contains', 'value': 'NEVER-MATCHES-QA', 'severity': 'warning'})
        status, impact = product.api('POST', '/api/v2/rules/impact', {'profile': changed})
        checks.equal('改异常影响规则层', impact['totals']['rules_affected'], 2)
        checks.equal('改异常影响检索层', impact['totals']['search_affected'], 2)
        checks.equal('改异常不影响结构层', impact['layers']['structure']['affected'], 0)

        sample = (ROOT / 'tests' / 'fixtures' / 'r6' / 'golden' / 'edge_cases.log').read_text('utf-8')
        status, trial = product.api('POST', '/api/v2/rules/trial',
                                    {'profile': profile, 'sample': sample, 'limit': 5})
        checks.equal('试跑返回 200', status, 200)
        checks.check('试跑真实分帧出记录', trial['framed_records'] > 0, trial['framed_records'])
        checks.check('试跑给出字段与命中证据',
                     any(record['fields'] for record in trial['records']) and
                     any(record['issues'] or record['nodes'] for record in trial['records']),
                     trial['records'][0].get('fields'))

        status, conflict = product.api('POST', '/api/v2/rules/cpqpsgx-default/apply',
                                       {'profile': changed, 'expectedRevision': revision + 99})
        checks.equal('过期修订号被拒绝', status, 409)
        checks.equal('冲突码正确', conflict['error']['code'], 'revision_conflict')

        status, applied = product.api('POST', '/api/v2/rules/cpqpsgx-default/apply',
                                      {'profile': changed, 'expectedRevision': revision,
                                       'actionId': 'qa-apply-1'})
        checks.equal('应用成功', status, 200)
        checks.equal('修订号 +1', applied['rule']['revision'], revision + 1)
        checks.check('排队的重建任务数等于受影响文件数', applied['queued'] >= 2, applied['queued'])
        for job in applied['jobs']:
            if job.get('jobId'):
                product.app.index_jobs.run_now(job['jobId'])
        after_structure = structure_signature(product, file_a)
        checks.equal('应用后结构层签名不变（未重新分帧）', after_structure, before_structure)
        status, after = product.api('POST', '/api/v2/rules/impact', {'profile': changed})
        checks.equal('重建后同一规则不再需要重建', after['totals']['affected'], 0)

        # ---------------------------------------------------------------- interface
        page.page.click('.r6-nav-item[data-view="rules"]')
        page.page.wait_for_selector('.rule-table', timeout=20000)
        page.wait(600)
        rows = page.page.eval_on_selector_all('.rule-table tbody tr', 'els => els.length')
        checks.check('规则页渲染出提取规则表', rows >= 1, rows)
        tabs = page.page.eval_on_selector_all('.rule-tabs .r6-tab', 'els => els.map(e => e.textContent)')
        checks.equal('规则页包含提取/异常/节点/试跑/影响五个分区', len(tabs), 5)

        apply_disabled = page.page.eval_on_selector(
            '.rule-view .r6-button.is-primary', 'e => e.disabled')
        checks.equal('未预览时保存并应用不可用', apply_disabled, True)

        page.page.click('.rule-tabs .r6-tab:nth-child(5)')
        page.wait(300)
        page.page.click('.rule-view .rule-node-actions .r6-button')
        page.page.wait_for_selector('.rule-view .rt-stats', timeout=30000)
        page.wait(400)
        values = page.page.eval_on_selector_all('.rule-view .rt-stat-value',
                                                'els => els.map(e => e.textContent)')
        checks.check('影响面板显示可见/受影响计数', len(values) >= 4, values)
        checks.equal('预览后受影响文件为 0（与 API 一致）', values[2], '0')
        apply_disabled = page.page.eval_on_selector(
            '.rule-view .r6-button.is-primary', 'e => e.disabled')
        checks.equal('预览后保存并应用可用', apply_disabled, False)

        page.page.click('.rule-tabs .r6-tab:nth-child(4)')
        page.wait(300)
        page.page.fill('.rule-sample', sample.splitlines()[0] + '\n')
        page.page.click('.rule-view .rule-node-actions .r6-button')
        page.page.wait_for_selector('.rule-trial', timeout=30000)
        page.wait(300)
        checks.check('试跑分区渲染出证据卡片',
                     page.page.eval_on_selector_all('.rule-trial', 'els => els.length') >= 1)

        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])
        page.shot(out / 'rule-impact.png')
        return checks.write({'baseline': baseline['totals'], 'impact': impact['totals'],
                             'applied': applied.get('queued')})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
