#!/usr/bin/env python3
"""R6-078 · 混合范围 CombinedProvider：真实本机 IDB + 真实 HTTP 服务端，网络隐私断言。

What is proven here, and cannot be proven by a unit test:

  * a scope with one local file and one indexed server file merges by timestamp, in
    the real browser, through the real Worker and the real query API;
  * a record that exists only in the local file is present in the merged result;
  * **no request that left the page carried the local file's name or its text** —
    asserted against the browser's own request log, not against our intentions;
  * a pure-local scope performs zero server queries.

    env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/mixed_scope.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8_common import Checks, Page, Product, arg_out, local_file

SCRIPT = r'''
async (serverFileId) => {
  const csrf = (await (await fetch('/api/v1/me')).json()).csrfToken;
  const client = window.__r6.client;
  const makeLocal = (fileId) => LBLocalProvider.createProvider({ client: client, datasetId: 'local' });
  const makeServer = (fileId) => LBServerProvider.createProvider({ csrfToken: csrf, datasetId: fileId });
  const localFiles = (await window.__r6.provider.listFiles()).files || [];
  if (!localFiles.length) return { error: 'no_local_file' };
  const localId = localFiles[0].file_id;
  const combined = LBCombinedProvider.createCombinedProvider({
    local: window.__r6.provider,
    localFactory: makeLocal,
    serverFactory: makeServer,
    scope: { local: [localId], server: [serverFileId] }
  });
  const range = { from_ns: String(BigInt(Date.parse('2026-05-01T09:59:00Z')) * 1000000n),
                  to_ns: String(BigInt(Date.parse('2026-05-01T10:01:00Z')) * 1000000n) };
  await combined.search({ match_all: true, time_range: range, limit: 200, mode: 'time' });
  // The server query is asynchronous; the merged answer is only worth reading once
  // the children have settled (or the bounded wait expired).
  const result = await combined.wait({ timeout_ms: 15000 });
  const rows = result.items.map((row) => ({
    source: row.source, file_id: row.file_id, ordinal: row.ordinal,
    ts: row.ts, level: row.level, message: row.message
  }));
  // Pure local scope: must not touch the query API at all.
  const before = (window.__m8Requests || []).filter((u) => u.indexOf('/api/v2/queries') === 0).length;
  const localOnly = LBCombinedProvider.createCombinedProvider({
    local: window.__r6.provider, localFactory: makeLocal,
    scope: { local: [localId], server: [] }
  });
  await localOnly.search({ match_all: true, limit: 50, mode: 'search' });
  const localResult = await localOnly.wait({ timeout_ms: 5000 });
  const after = (window.__m8Requests || []).filter((u) => u.indexOf('/api/v2/queries') === 0).length;
  return {
    local_id: localId,
    count: result.count,
    coverage: result.coverage,
    order_final: result.order_final,
    excluded: result.excluded,
    rows: rows,
    local_only: { rows: localResult.items.length, server_queries: localResult.stats.server_queries,
                  delta_queries: after - before },
    stats: result.stats
  };
}
'''


def run() -> bool:
    args = arg_out('M8-078')
    out = Path(args.out)
    checks = Checks('mixed_scope', out)
    marker = 'LOCALFILE-XYZZY'
    with Product() as product, Page(product.base) as page:
        page.page.evaluate("() => { window.__m8Requests = []; const open = XMLHttpRequest.prototype.open; "
                           "XMLHttpRequest.prototype.open = function (method, url) { "
                           "window.__m8Requests.push(String(url)); return open.apply(this, arguments); }; "
                           "const f = window.fetch; window.fetch = function (input, init) { "
                           "const url = typeof input === 'string' ? input : input.url; "
                           "window.__m8Requests.push(String(url)); return f.apply(this, arguments); }; }")
        local = local_file(Path(product.tmp) / 'browser-only.log')
        imported = page.import_local(local)
        checks.check('本机文件已通过真实文件输入导入', imported >= 1, imported)

        server_file = product.file_ids.get('psg-a.log')
        checks.check('服务端文件已建立索引', bool(server_file), server_file)

        page.requests.clear()
        result = page.page.evaluate(SCRIPT, server_file)
        checks.check('混合查询没有报错', not result.get('error'), result.get('error'))

        rows = result.get('rows') or []
        sources = {row['source'] for row in rows}
        checks.equal('合并结果同时包含两个来源', sources, {'local', 'server'})
        checks.check('合并结果包含只存在于本机的记录',
                     any(marker in (row['message'] or '') for row in rows),
                     [row['message'][:40] for row in rows][:6])
        keys = [(row['ts'], row['source'], row['ordinal']) for row in rows]
        ordered = [row['ts'] for row in rows]
        checks.equal('结果按时间升序', ordered, sorted(ordered), ordered[:6])
        same_ms = [row for row in rows if row['source'] == 'server'
                   and str(row['ts']).replace('T', ' ').startswith('2026-05-01 10:00:10')]
        checks.equal('相同时间点保留两条重复记录', len(same_ms), 2,
                     [(row['ts'], row['source']) for row in rows])
        checks.equal('全部子查询完成时 order_final 为 true', result.get('order_final'), True)
        checks.equal('计数关系为 exact', (result.get('count') or {}).get('relation'), 'exact')

        local_only = result.get('local_only') or {}
        checks.equal('纯本机范围不发起服务端查询', local_only.get('server_queries'), 0)
        checks.equal('纯本机范围没有新增 /api/v2/queries 请求', local_only.get('delta_queries'), 0)
        checks.check('纯本机范围仍返回记录', (local_only.get('rows') or 0) >= 2,
                     local_only.get('rows'))

        # Privacy: nothing that left the page mentions the local file or its text.
        leaked = []
        for item in page.requests:
            blob = (item.get('url') or '') + ' ' + (item.get('body') or '')
            if marker in blob or 'browser-only.log' in blob:
                leaked.append(item['url'])
        checks.check('离开页面的请求不含本机文件名或原文', not leaked, leaked[:3])
        server_queries = [item for item in page.requests
                          if item.get('method') == 'POST'
                          and (item.get('url') or '').endswith('/api/v2/queries')]
        checks.check('服务端查询只针对服务端文件',
                     bool(server_queries) and all(
                         server_file in (item.get('body') or '') for item in server_queries),
                     [item['body'][:160] for item in server_queries][:2])
        checks.check('页面无脚本错误', not page.errors(), page.errors()[:3])

        page.shot(out / 'mixed-scope.png')
        return checks.write({'rows': rows[:12], 'server_queries': len(server_queries),
                             'marker': marker})


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
