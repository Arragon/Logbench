#!/usr/bin/env python3
"""R6-082 · 解析缓存分层状态、选择性重建与“清缓存不动原文”。

The destructive half is the point: after purging the derived layers the source file must
be byte-identical, the raw layer must still exist, and the structure layer must still be
published. A page that says "已清理" while the raw bytes are gone would be worse than no
page at all.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/cache_manager.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> bool:
    args = arg_out('M8-082')
    out = Path(args.out)
    checks = Checks('cache_manager', out)
    with Product() as product, Page(product.base) as page:
        target = product.paths[0]
        file_id = product.file_ids[target.name]
        before = digest(target)

        status, listing = product.api('GET', '/api/v2/cache?limit=10')
        checks.equal('缓存列表可用', status, 200)
        checks.equal('两个文件都出现在列表中', len(listing['items']), 2)
        item = [row for row in listing['items'] if row['fileId'] == file_id][0]
        checks.equal('结构/规则/检索三层均已完成',
                     [item['layers'][layer]['state'] for layer in ('structure', 'rules', 'search')],
                     ['complete', 'complete', 'complete'])
        checks.check('索引分片大小已测量', (item['shardBytes'] or 0) > 0, item['shardBytes'])
        checks.equal('可清理层只包含派生层', item['purgeable'], ['rules', 'search'])

        status, refreshed = product.api('POST', '/api/v2/cache/rebuild',
                                        {'fileId': file_id, 'layers': ['rules', 'search'],
                                         'actionId': 'qa-cache-rebuild'})
        checks.equal('重建请求被接受', status, 200)
        job = refreshed['item']
        product.app.index_jobs.run_now(job['id'])
        status, after_rebuild = product.api('GET', '/api/v2/cache?limit=10')
        again = [row for row in after_rebuild['items'] if row['fileId'] == file_id][0]
        checks.equal('重建后三层仍为完成',
                     [again['layers'][layer]['state'] for layer in ('structure', 'rules', 'search')],
                     ['complete', 'complete', 'complete'])

        status, purged = product.api('POST', '/api/v2/cache/purge',
                                     {'fileId': file_id, 'layers': ['rules', 'search'],
                                      'confirm': 'purge-derived'})
        checks.equal('清理返回 200', status, 200)
        checks.equal('清理了两层', len(purged['removed']), 2)
        checks.equal('清理声明保留原文', purged['rawKept'], True)
        checks.equal('原文逐字节未变', digest(target), before)
        status, after_purge = product.api('GET', '/api/v2/cache?limit=10')
        purged_item = [row for row in after_purge['items'] if row['fileId'] == file_id][0]
        checks.equal('结构层仍在（没有连带清掉）',
                     purged_item['layers']['structure']['state'], 'complete')
        checks.check('规则层变为待重建/未建',
                     purged_item['layers']['rules']['state'] in ('pending', 'failed', 'stale'),
                     purged_item['layers']['rules']['state'])
        checks.equal('覆盖退回到结构层', purged_item['coverage'], 'structure')

        status, refused = product.api('POST', '/api/v2/cache/purge',
                                      {'fileId': file_id, 'layers': ['raw'],
                                       'confirm': 'purge-derived'})
        checks.equal('原文层不可通过清理删除', status, 400)
        checks.equal('拒绝码明确', refused['error']['code'], 'raw_not_purgeable')

        # ------------------------------------------------------------------ interface
        page.page.click('.r6-nav-item[data-view="cache"]')
        page.page.wait_for_selector('.cm-table tbody tr', timeout=20000)
        page.wait(600)
        rows = page.page.eval_on_selector_all('.cm-table tbody tr', 'els => els.length')
        checks.check('缓存页渲染出文件行', rows >= 2, rows)
        badges = page.page.eval_on_selector_all('.cm-table .r6-badge', 'els => els.map(e => e.textContent)')
        checks.check('每行展示三层的状态徽标', len(badges) >= 6, badges[:6])
        checks.check('状态用文字而不是仅颜色表达',
                     all(any(word in badge for word in ('完成', '未建', '重建中', '失败', '待重建'))
                         for badge in badges),
                     badges[:6])

        page.page.click('.cm-table tbody tr .r6-link')
        page.wait(300)
        detail = page.page.eval_on_selector_all('.cm-detail-line', 'els => els.length')
        checks.check('展开后可看到分层依据', detail >= 3, detail)
        page.page.click('.cm-table tbody tr .r6-link')

        page.page.click('.cm-table tbody tr .r6-icon')
        page.page.wait_for_selector('.cm-dialog .cm-dialog-head', timeout=10000)
        dialog_text = page.page.eval_on_selector('.cm-dialog', 'e => e.textContent')
        checks.check('重建确认对话框说明复用结构层',
                     '不会重新分帧' in dialog_text, dialog_text[:80])
        page.page.click('.cm-dialog .r6-dialog-actions .r6-button.is-primary')
        page.wait(1500)
        status, jobs = product.api('GET', '/api/v2/jobs?limit=20')
        kinds = [row['kind'] for row in jobs['items']]
        checks.check('界面重建确实创建了索引任务', kinds.count('index') >= 2, kinds)

        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])
        page.shot(out / 'cache-manager.png')
        return checks.write({'file': target.name, 'digest_unchanged': digest(target) == before})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
