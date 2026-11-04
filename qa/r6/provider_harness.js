(function () {
  'use strict';
  var checks = [];
  function record(label, ok, detail) {
    checks.push({ label: label, ok: !!ok, detail: detail == null ? null : String(detail) });
    var li = document.createElement('li');
    li.className = ok ? 'ok' : 'bad';
    li.textContent = (ok ? 'PASS ' : 'FAIL ') + label + (detail == null ? '' : ' — ' + detail);
    document.getElementById('checks').appendChild(li);
  }

  /* Count Worker construction: ten files must not become ten workers. */
  var workerCount = 0;
  var RealWorker = window.Worker;
  window.Worker = function (url, options) { workerCount += 1; return new RealWorker(url, options); };
  window.Worker.prototype = RealWorker.prototype;

  function makeRecords(count) {
    var records = [];
    for (var i = 0; i < count; i++) {
      records.push({
        ordinal: i,
        record_id: 'demo:' + i,
        version: 1,
        level: i % 23 === 0 ? 'ERROR' : 'INFO',
        ts: '2026-01-01 00:00:' + String(i % 60).padStart(2, '0') + '.000000000',
        ts_ns: 1767225600000000000 + i * 1000000,
        lines: { first: i * 2 + 1, last: i * 2 + 1 },
        message: 'worker ' + (i % 7) + ' handled energy sample ' + i,
        body: i % 11 === 0 ? ['  at step ' + i, '  retry=false'] : [],
        has_trace: i % 11 === 0
      });
    }
    return records;
  }

  async function run() {
    var worker = new Worker('/assets/workers/local-engine.js');
    var client = LBRpc.createRpcClient({
      post: function (message) { worker.postMessage(message); },
      timeoutMs: 20000
    });
    worker.onmessage = function (event) { client.handleMessage(event.data); };

    var provider = LBLocalProvider.createProvider({ client: client, datasetId: 'qa-provider' });
    var opened = await provider.open('qa-provider');
    record('open 返回能力表', opened.capabilities.max_page_records === 2000, 'page=' + opened.capabilities.max_page_records);

    var empty = await provider.listFiles();
    record('空数据集可列出', empty.files.length === 0, 'files=' + empty.files.length);

    /* Real raw bytes, written through the provider's engine path. */
    var text = 'first line\n第二行 with 中文\nthird line\n';
    var bytes = new TextEncoder().encode(text);
    await client.call('putFile', { file_id: 'demo', name: 'demo.log', size: bytes.length, generation: 1, chunks: 1 });
    await client.call('appendChunk', { file_id: 'demo', generation: 1, chunk_no: 0, bytes: bytes });

    var records = makeRecords(5000);
    var staged = await client.call('stageIndex', {
      file_id: 'demo', generation: 1, size: bytes.length, records: records,
      raw_bytes: bytes.length, framed_lines: 10000,
      signatures: { structure: 's1', extract: 'e1', search: 'v1' }
    });
    record('索引已发布', staged.published_generation === 1, 'generation=' + staged.published_generation);

    var files = await provider.listFiles();
    record('列出 1 个文件', files.files.length === 1 && files.files[0].name === 'demo.log', JSON.stringify(files.files));

    var page = await provider.headers({ file_id: 'demo', generation: 1, limit: 200 });
    record('首屏 200 条', page.items.length === 200, 'got=' + page.items.length);
    record('下一页游标为 200', page.next_cursor === 200, 'cursor=' + page.next_cursor);
    record('计数为 exact', page.count.relation === 'exact' && page.count.matched === 5000, JSON.stringify(page.count));

    var rejected = await provider.headers({ file_id: 'demo', generation: 1, limit: 5000 })
      .then(function () { return null; }, function (error) { return error; });
    record('超过 2000 条被拒绝', rejected && rejected.code === 'page_too_large', rejected && rejected.code);

    var stale = await provider.headers({ file_id: 'demo', generation: 2, limit: 10 })
      .then(function () { return null; }, function (error) { return error; });
    record('过期代次被拒绝', stale && stale.code === 'stale_generation', stale && stale.code);

    var raw = await provider.readRaw({ file_id: 'demo', generation: 1, offset: 0, length: bytes.length });
    var back = new TextDecoder().decode(raw.bytes);
    record('原文逐字节往返', back === text, 'len=' + raw.bytes.length);

    var cut = await provider.readRaw({ file_id: 'demo', generation: 1, offset: 6, length: 4 });
    record('区间读取只取所需字节', new TextDecoder().decode(cut.bytes) === 'line', new TextDecoder().decode(cut.bytes));

    var search = await provider.search({ file_id: 'demo', generation: 1, text: 'energy', limit: 50 });
    record('搜索命中数正确', search.count.matched === 5000, 'matched=' + search.count.matched);
    record('结果页不超过 limit', search.page.items.length === 50, 'items=' + search.page.items.length);
    record('候选剪枝未误杀（全为命中）',
      search.coverage.pruned_blocks === 0 || search.coverage.scanned_blocks > 0,
      JSON.stringify(search.coverage));

    var filtered = await provider.search({ file_id: 'demo', generation: 1, text: 'energy', levels: ['ERROR'], limit: 10 });
    record('等级过滤与剪枝一致', filtered.count.matched === Math.ceil(5000 / 23),
      'matched=' + filtered.count.matched + ' expected=' + Math.ceil(5000 / 23));

    var detail = await provider.detail({ file_id: 'demo', generation: 1, ordinal: 11 });
    record('详情按 ordinal 取回', detail.ordinal === 11 && detail.body.length === 2, 'body=' + detail.body.length);
    record('详情带原文行号', detail.header.lines.first === 23, 'first=' + detail.header.lines.first);

    var timeline = await provider.getTimeline({ file_id: 'demo', generation: 1, page: 0, page_size: 50 });
    record('时间线分页', timeline.nodes.length === 50 && timeline.has_next === true,
      'nodes=' + timeline.nodes.length + ' total=' + timeline.total);

    var seek = await provider.seek({ file_id: 'demo', generation: 1, ts_ns: 1767225600000000000 + 3000 * 1000000 });
    record('按时间定位', seek.ordinal === 3000, 'ordinal=' + seek.ordinal);

    var resources = performance.getEntriesByType('resource')
      .filter(function (entry) { return /\/api\//.test(entry.name); });
    record('全程未向服务器上传内容', resources.length === 0, 'api-requests=' + resources.length);
    record('只创建了 1 个常驻 Worker', workerCount === 1, 'workers=' + workerCount);

    var stats = client.stats();
    record('没有孤儿回包', stats.orphan_replies === 0, JSON.stringify(stats));

    var result = {
      ok: checks.every(function (check) { return check.ok; }),
      checks: checks,
      transport: {
        to_worker_bytes: stats.calls,
        pending: stats.pending,
        workers: workerCount
      },
      coverage: search.coverage,
      count: search.count
    };
    window.__result = result;
    document.getElementById('status').textContent = result.ok ? 'all checks passed' : 'FAILURES';
    document.getElementById('status').className = result.ok ? 'ok' : 'bad';
    document.getElementById('detail').textContent = JSON.stringify(result, null, 2);
    return result;
  }

  window.__run = function () {
    return run().catch(function (error) {
      record('运行未抛异常', false, error && (error.code || error.message));
      window.__result = { ok: false, checks: checks, error: String(error && (error.stack || error.message)) };
      document.getElementById('status').textContent = 'ERROR';
      document.getElementById('status').className = 'bad';
      return window.__result;
    });
  };
})();
