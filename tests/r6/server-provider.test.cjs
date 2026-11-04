'use strict';
/*
 * R6-045: the server provider maps HTTP onto the same surface as the local one.
 *
 * `fetch` is stubbed here on purpose: this file is about the adapter's contract —
 * token guards, error mapping, page limits, query versioning — not about Python.
 * The real HTTP path is exercised end to end by qa/r6/private_acceleration_gate.py.
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const { createProvider, CAPABILITIES } = require(path.join(ROOT, 'src', 'providers', 'server.js'));

function stubFetch(routes) {
  const calls = [];
  const impl = (url, init) => {
    const method = (init && init.method) || 'GET';
    calls.push({ url: url, method: method, init: init });
    const key = method + ' ' + url.split('?')[0];
    const handler = routes[key] || routes[method + ' *'];
    if (!handler) {
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'no route' } })) });
    }
    const result = typeof handler === 'function' ? handler(url, init) : handler;
    const status = result.status == null ? 200 : result.status;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status: status,
      text: () => Promise.resolve(result.text != null ? result.text : JSON.stringify(result.body || {}))
    });
  };
  return { impl: impl, calls: calls };
}

function makeProvider(routes, extra) {
  const stub = stubFetch(routes);
  const provider = createProvider(Object.assign({ fetchImpl: stub.impl, csrfToken: 'csrf-1' }, extra || {}));
  return { provider: provider, calls: stub.calls };
}

test('capabilities say server, and page limits match the contract', () => {
  assert.strictEqual(CAPABILITIES.kind, 'server');
  assert.strictEqual(CAPABILITIES.max_page_records, 2000);
  assert.strictEqual(CAPABILITIES.max_page_bytes, 4194304);
  assert.strictEqual(CAPABILITIES.write, true);
});

test('open reads the index status and exposes coverage', async () => {
  const { provider } = makeProvider({
    'GET /api/v2/files/f_1': {
      body: { file_id: 'f_1', generation: 2, coverage: { state: 'complete', record_count: 9 },
              layers: { structure: { state: 'complete' } } }
    }
  });
  const opened = await provider.open('f_1');
  assert.strictEqual(opened.dataset_id, 'f_1');
  assert.strictEqual(opened.index.coverage.state, 'complete');
  assert.strictEqual(opened.capabilities.kind, 'server');
});

test('a page request over 2000 rows is rejected before it is sent', async () => {
  const { provider, calls } = makeProvider({});
  await assert.rejects(() => provider.headers({ file_id: 'f_1', limit: 5000 }), /2000/);
  assert.strictEqual(calls.length, 0, 'an oversized page must not reach the server');
});

test('writes carry the csrf header and reads do not', async () => {
  const { provider, calls } = makeProvider({
    'POST /api/v2/queries': { body: { item: { query_id: 'q-1', state: 'running', count: { matched: 0, returned: 0, relation: 'at_least' }, coverage: {}, dataset_id: 'f_1' } } },
    'GET /api/v2/files/f_1/records': { body: { items: [], next_cursor: null } }
  });
  await provider.search({ file_id: 'f_1', text: 'x' });
  await provider.headers({ file_id: 'f_1' });
  const write = calls.find(call => call.method === 'POST');
  const read = calls.find(call => call.method === 'GET');
  assert.strictEqual(write.init.headers['X-LogBench-CSRF'], 'csrf-1');
  assert.strictEqual(read.init.headers['X-LogBench-CSRF'], undefined);
});

test('a server error becomes an error with the server code', async () => {
  const { provider } = makeProvider({
    'GET /api/v2/files/f_missing': { status: 404, body: { error: { code: 'NOT_FOUND', message: '文件不存在。' } } }
  });
  await assert.rejects(() => provider.open('f_missing'), (error) => {
    assert.strictEqual(error.code, 'NOT_FOUND');
    return true;
  });
});

test('a reply that arrives after open() is discarded', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let calls = 0;
  const respond = () => ({
    ok: true, status: 200,
    text: () => Promise.resolve(JSON.stringify({ file_id: 'f_2', items: [], next_cursor: null }))
  });
  const provider = createProvider({
    // Only the first request is held, so open() can overtake it.
    fetchImpl: () => {
      calls += 1;
      return calls === 1 ? gate.then(respond) : Promise.resolve(respond());
    },
    csrfToken: 'c'
  });
  const pending = provider.headers({ file_id: 'f_1', limit: 10 });
  await provider.open('f_2');
  release();
  await assert.rejects(() => pending, (error) => {
    assert.strictEqual(error.code, 'dataset_replaced');
    return true;
  });
});

test('an older query cannot publish its result', async () => {
  const answers = [
    { body: { item: { query_id: 'q-1', state: 'running', count: { matched: 0, returned: 0, relation: 'at_least' }, coverage: {}, dataset_id: 'f_1' } } },
    { body: { item: { query_id: 'q-2', state: 'running', count: { matched: 0, returned: 0, relation: 'at_least' }, coverage: {}, dataset_id: 'f_1' } } },
    { body: { item: { query_id: 'q-1', state: 'done', count: { matched: 3, returned: 3, relation: 'exact' }, coverage: {}, dataset_id: 'f_1' } } }
  ];
  let index = 0;
  const provider = createProvider({
    fetchImpl: () => Promise.resolve({
      ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(answers[index++]))
    }),
    csrfToken: 'c'
  });
  const first = await provider.search({ file_id: 'f_1', text: 'a' });
  await provider.search({ file_id: 'f_1', text: 'b' });
  // Whatever the code, the old query must not publish a result into the newer one.
  await assert.rejects(() => provider.poll(first.query_id), (error) => {
    assert.ok(['stale_query', 'unknown_query'].indexOf(error.code) !== -1, error.code);
    return true;
  });
});

test('poll/resume/cancel address the snapshot they were created with', async () => {
  const { provider, calls } = makeProvider({
    'POST /api/v2/queries': { body: { item: { query_id: 'q-9', state: 'paused', resume_token: 'tok', count: { matched: 1, returned: 1, relation: 'at_least' }, coverage: {}, dataset_id: 'f_1' } } },
    'POST /api/v2/queries/q-9/continue': { body: { item: { query_id: 'q-9', state: 'running', count: { matched: 1, returned: 1, relation: 'at_least' }, coverage: {}, dataset_id: 'f_1' } } },
    'DELETE /api/v2/queries/q-9': { body: { item: { query_id: 'q-9', state: 'cancelled', count: { matched: null, returned: 1, relation: 'unknown' }, coverage: {}, dataset_id: 'f_1' } } }
  });
  const state = await provider.search({ file_id: 'f_1', text: 'x' });
  assert.strictEqual(state.state, 'paused');
  assert.strictEqual(state.provider, 'server');
  const resumed = await provider.resume('q-9', state.resume_token);
  assert.strictEqual(resumed.state, 'running');
  const cancelled = await provider.cancel('q-9');
  assert.strictEqual(cancelled.count.relation, 'unknown');
  assert.ok(calls.some(call => call.method === 'DELETE'));
});

test('the provider refuses unknown query ids instead of guessing', async () => {
  const { provider } = makeProvider({});
  await assert.rejects(() => provider.poll('q-nope'), (error) => {
    assert.strictEqual(error.code, 'unknown_query');
    return true;
  });
});

test('ensureIndex asks the server by file id and never sends a path', async () => {
  const { provider, calls } = makeProvider({
    'POST /api/v2/files/f_1/index': { status: 200, body: { item: { id: 'j_1', state: 'queued' } } }
  });
  const job = await provider.ensureIndex('f_1');
  assert.strictEqual(job.item.state, 'queued');
  const request = calls[0];
  assert.strictEqual(request.url, '/api/v2/files/f_1/index');
  assert.strictEqual(request.init.body, undefined, 'no path in the body');
});

test('detail and raw reads use the opaque record id', async () => {
  const { provider, calls } = makeProvider({
    'GET /api/v2/records/f_1:7': { body: { record: { record_id: 'f_1:7' }, version: { rules: 'ver-1' } } },
    'GET /api/v2/records/f_1:7/raw': { body: { text: 'abc', truncated: false, range: { start: 10, end: 13 } } }
  });
  const detail = await provider.detail({ file_id: 'f_1', ordinal: 7 });
  assert.strictEqual(detail.record.record_id, 'f_1:7');
  const raw = await provider.readRaw({ file_id: 'f_1', ordinal: 7, offset: 10, length: 3 });
  assert.strictEqual(raw.text, 'abc');
  assert.ok(calls.every(call => call.url.indexOf('/api/v2/records/') === 0));
});

test('close makes every later call fail as closed', async () => {
  const { provider } = makeProvider({});
  await provider.close();
  await assert.rejects(() => provider.headers({ file_id: 'f_1' }), (error) => {
    assert.strictEqual(error.code, 'provider_closed');
    return true;
  });
});

test('poll, cancel and resume work on the query that was just created', async () => {
  // The server's state object does not carry `query_version`; reading it from there
  // used to make every poll of its own query answer `stale_query`.
  const state = { query_id: 'q-1', state: 'running', count: { relation: 'at_least', matched: 3 },
    coverage: { complete: false } };
  const provider = createProvider({
    fetchImpl: stubFetch({
      'POST /api/v2/queries': { body: { item: state, accepted: true } },
      'GET /api/v2/queries/q-1': { body: { item: Object.assign({}, state, { state: 'done',
        count: { relation: 'exact', matched: 3 }, coverage: { complete: true } }) } },
      'DELETE /api/v2/queries/q-1': { body: { item: Object.assign({}, state, { state: 'cancelled' }) } },
      'POST /api/v2/queries/q-1/continue': { body: { item: state, accepted: true } }
    }).impl,
    csrfToken: 'csrf'
  });
  const created = await provider.search({ file_id: 'f-1', text: 'x' });
  assert.equal(created.query_version, 1);
  const polled = await provider.poll(created.query_id);
  assert.equal(polled.state, 'done');
  assert.equal(polled.query_version, 1);
  const cancelled = await provider.cancel(created.query_id);
  assert.equal(cancelled.state, 'cancelled');
  const resumed = await provider.resume(created.query_id, 'token-1');
  assert.equal(resumed.query_version, 1);
});

test('a replaced query is not published by a late poll', async () => {
  const provider = createProvider({
    fetchImpl: stubFetch({
      'POST /api/v2/queries': (url, init) => {
        const body = JSON.parse(init.body);
        return { body: { item: { query_id: 'q-' + body.text, state: 'running',
          count: { relation: 'at_least', matched: 1 }, coverage: { complete: false } } } };
      },
      'GET /api/v2/queries/q-first': { body: { item: { query_id: 'q-first', state: 'done',
        count: { relation: 'exact', matched: 1 }, coverage: { complete: true } } } }
    }).impl,
    csrfToken: 'csrf'
  });
  const first = await provider.search({ file_id: 'f-1', text: 'first' });
  await provider.search({ file_id: 'f-1', text: 'second' });
  await assert.rejects(() => provider.poll(first.query_id), /查询已失效|已有更新的查询条件/);
});
