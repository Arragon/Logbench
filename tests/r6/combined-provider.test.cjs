/*
 * combined-provider.test.cjs — R6-078 merge semantics without a browser or server.
 *
 * The merger is the part that can silently lie (a wrong tie-breaker duplicates or
 * drops rows across pages, a wrong relation claims an exact zero for a scope that
 * was still scanning), so it is tested directly as well as through the provider.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const combined = require(path.join(__dirname, '..', '..', 'src', 'providers', 'combined.js'));

function row(overrides) {
  return Object.assign({ source: 'local', rank: 0, file_index: 0, file_id: 'f1',
    ordinal: 0, ts_key: 0, ts_ns: '0', ts: '', level: 'INFO', message: '' }, overrides);
}

function header(fileId, ordinal, tsKey, extra) {
  return Object.assign({
    file_id: fileId, ordinal, ts_key: tsKey, ts_ns: String(tsKey * 1000000),
    record_id: fileId + ':' + ordinal, level: 'INFO', message: 'm' + ordinal
  }, extra || {});
}

/** A provider that behaves like the real adapters for the fields combined.js reads. */
function fakeProvider(options) {
  const calls = { search: [], getRecords: [], cancel: [] };
  const state = {
    fileId: options.fileId,
    items: options.items,
    state: options.state || 'done',
    relation: options.relation || 'exact',
    complete: options.complete !== false,
    delay: options.delay || 0,
    fail: options.fail || null,
    version: 0
  };
  return {
    calls,
    search(spec) {
      state.version += 1;
      const version = state.version;
      calls.search.push(spec);
      const snapshot = () => ({
        provider: options.kind || 'local',
        query_id: 'q-' + state.fileId + '-' + version,
        query_version: version,
        generation: 1,
        state: state.state,
        count: { relation: state.relation, matched: state.items.length },
        coverage: { complete: state.complete },
        page: { items: state.items.map((item) => item.ordinal), next_cursor: null },
        // Remember which snapshot produced these rows; the test asserts the latest
        // snapshot is the one that was rendered.
        token: version
      });
      if (state.fail) return Promise.reject(state.fail);
      if (!state.delay) return Promise.resolve(snapshot());
      return new Promise((resolve) => setTimeout(() => resolve(snapshot()), state.delay));
    },
    getRecords(params) {
      calls.getRecords.push(params);
      const wanted = new Set(params.ordinals);
      return Promise.resolve({
        items: state.items.filter((item) => wanted.has(item.ordinal))
          .map((item) => header(state.fileId, item.ordinal, item.ts_key, item))
      });
    },
    cancel(queryId) {
      calls.cancel.push(queryId);
      return Promise.resolve({ state: 'cancelled' });
    }
  };
}

function itemsFor(fileId, keys) {
  return keys.map((tsKey, index) => ({ file_id: fileId, ordinal: index, ts_key: tsKey,
    ts_ns: String(tsKey * 1000000), level: 'INFO', message: fileId + '#' + index }));
}

/* --------------------------------------------------------------------- merge */

test('merge orders by time, then source, then file, then ordinal', () => {
  const streams = [
    [row({ ordinal: 0, ts_key: 100 }), row({ ordinal: 1, ts_key: 300 })],
    [row({ source: 'server', rank: 1, file_index: 1, ordinal: 0, ts_key: 100 }),
      row({ source: 'server', rank: 1, file_index: 1, ordinal: 1, ts_key: 200 })]
  ];
  const merged = combined.mergeOrdered(streams, 10);
  assert.deepEqual(merged.items.map((item) => (item.rank + ':' + item.ordinal)),
    ['0:0', '1:0', '1:1', '0:1']);
  assert.equal(merged.complete, true);
});

test('merge keeps duplicate timestamps instead of deduplicating them', () => {
  const merged = combined.mergeOrdered([
    [row({ ordinal: 0, ts_key: 5 }), row({ ordinal: 1, ts_key: 5 })],
    [row({ ordinal: 0, ts_key: 5, file_index: 1, file_id: 'f2' })]
  ], 10);
  assert.equal(merged.items.length, 3, 'three records share one timestamp and all are kept');
  assert.deepEqual(merged.items.map((item) => item.file_id + '#' + item.ordinal),
    ['f1#0', 'f1#1', 'f2#0']);
});

test('merge respects the limit and reports completeness', () => {
  const merged = combined.mergeOrdered([[
    row({ ordinal: 0, ts_key: 1 }), row({ ordinal: 1, ts_key: 2 }), row({ ordinal: 2, ts_key: 3 })
  ]], 2);
  assert.equal(merged.items.length, 2);
  assert.equal(merged.complete, false);
  assert.deepEqual(merged.cursors, [2]);
});

test('undated records are placed last, never first', () => {
  const streams = [[row({ ordinal: 0, ts_key: null, ts_ns: null }),
    row({ ordinal: 1, ts_key: 50 })]];
  const merged = combined.mergeOrdered(streams, 10);
  assert.deepEqual(merged.items.map((item) => item.ordinal), [1, 0]);
  assert.equal(merged.untimed, 1);
});

/* --------------------------------------------------------------------- scope */

test('a file cannot be in both transports at once', () => {
  assert.throws(() => combined.splitScope({ local: ['f1'], server: ['f1'] }),
    /同一个文件不能同时属于/);
});

test('a pure local scope never constructs or calls a server provider', async () => {
  let serverTouched = 0;
  const provider = combined.createCombinedProvider({
    scope: { local: ['f1'], server: [] },
    localFactory: () => fakeProvider({ fileId: 'f1', items: itemsFor('f1', [10, 20]) }),
    serverFactory: () => { serverTouched += 1; return fakeProvider({ fileId: 'x', items: [] }); }
  });
  const state = await provider.search({ text: 'x' });
  assert.equal(serverTouched, 0);
  assert.equal(state.stats.server_queries, 0);
  assert.equal(state.stats.local_queries, 1);
  assert.deepEqual(state.items.map((item) => item.ordinal), [0, 1]);
  assert.equal(state.count.relation, 'exact');
  assert.equal(state.order_final, true);
});

test('a mixed scope merges both transports and keeps its own counters', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['l1'], server: ['s1'] },
    localFactory: () => fakeProvider({ fileId: 'l1', items: itemsFor('l1', [10, 30]) }),
    serverFactory: () => fakeProvider({ fileId: 's1', items: itemsFor('s1', [20, 40]), kind: 'server' })
  });
  const state = await provider.search({ text: 'x', limit: 100 });
  assert.deepEqual(state.items.map((item) => item.file_id + '#' + item.ordinal),
    ['l1#0', 's1#0', 'l1#1', 's1#1']);
  assert.equal(state.stats.local_queries, 1);
  assert.equal(state.stats.server_queries, 1);
});

test('a multi-file scope without a per-file factory is refused, not silently serialised', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['a', 'b'], server: [] },
    local: fakeProvider({ fileId: 'a', items: [] })
  });
  await assert.rejects(() => provider.search({ text: 'x' }), /多文件查询需要按文件创建/);
});

/* ------------------------------------------------------------------ coverage */

test('one running child makes the total at_least, not exact', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: ['b'] },
    localFactory: () => fakeProvider({ fileId: 'a', items: itemsFor('a', [1]),
      state: 'done', relation: 'exact' }),
    serverFactory: () => fakeProvider({ fileId: 'b', items: itemsFor('b', [2]),
      state: 'running', relation: 'at_least', complete: false })
  });
  const state = await provider.search({ text: 'x' });
  assert.equal(state.count.relation, 'at_least');
  assert.equal(state.coverage.complete, false);
  assert.equal(state.coverage.completed_files, 1);
  assert.equal(state.order_final, false);
  assert.equal(state.items.length, 2, 'the finished file still contributes its rows');
});

test('a failed child is named and does not become a zero result', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: ['b'] },
    localFactory: () => fakeProvider({ fileId: 'a', items: itemsFor('a', [1]) }),
    serverFactory: () => fakeProvider({ fileId: 'b', items: [],
      fail: Object.assign(new Error('文件不存在。'), { code: 'not_found' }) })
  });
  const state = await provider.search({ text: 'x' });
  assert.equal(state.coverage.failed.length, 1);
  assert.equal(state.coverage.failed[0].file_id, 'b');
  assert.equal(state.coverage.failed[0].code, 'not_found');
  assert.equal(state.count.relation, 'at_least');
  assert.deepEqual(state.items.map((item) => item.file_id), ['a']);
});

test('a file without timestamps is excluded from a time range with a reason', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: [] },
    localFactory: () => fakeProvider({ fileId: 'a', items: [
      { file_id: 'a', ordinal: 0, ts_key: null, ts_ns: null, level: 'INFO', message: 'no ts' }
    ] })
  });
  const state = await provider.search({ text: 'x', mode: 'time' });
  assert.equal(state.items.length, 0);
  assert.equal(state.excluded.length, 1);
  assert.equal(state.excluded[0].reason, 'no_time_domain');
});

test('the same undated file is kept last in search mode', async () => {
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: [] },
    localFactory: () => fakeProvider({ fileId: 'a', items: [
      { file_id: 'a', ordinal: 0, ts_key: null, ts_ns: null, level: 'INFO', message: 'no ts' },
      { file_id: 'a', ordinal: 1, ts_key: 7, ts_ns: '7000000', level: 'INFO', message: 'timed' }
    ] })
  });
  const state = await provider.search({ text: 'x', mode: 'search' });
  assert.deepEqual(state.items.map((item) => item.ordinal), [1, 0]);
  assert.equal(state.order_final, false);
});

/* ------------------------------------------------------------------- escapes */

test('a slow reply from a replaced scope is rejected', async () => {
  let release;
  const slow = {
    search() {
      return new Promise((resolve) => { release = () => resolve({
        query_id: 'slow', generation: 1, state: 'done',
        count: { relation: 'exact', matched: 0 }, coverage: { complete: true },
        page: { items: [] }
      }); });
    },
    getRecords() { return Promise.resolve({ items: [] }); },
    cancel() { return Promise.resolve({}); }
  };
  const fast = fakeProvider({ fileId: 'b', items: itemsFor('b', [1]) });
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: [] },
    localFactory: () => slow
  });
  const first = provider.search({ text: 'x' });
  provider.setScope({ local: ['b'], server: [] });
  const provider2 = combined.createCombinedProvider({
    scope: { local: ['b'], server: [] },
    localFactory: () => fast
  });
  await provider2.search({ text: 'x' });
  release();
  await assert.rejects(() => first, /已有更新的查询范围/ || /stale/);
});

test('cancel reaches every child that has a query', async () => {
  const local = fakeProvider({ fileId: 'a', items: itemsFor('a', [1]) });
  const server = fakeProvider({ fileId: 'b', items: itemsFor('b', [2]), kind: 'server' });
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: ['b'] },
    localFactory: () => local,
    serverFactory: () => server
  });
  await provider.search({ text: 'x' });
  const result = await provider.cancel();
  assert.equal(result.cancelled, 2);
  assert.equal(local.calls.cancel.length, 1);
  assert.equal(server.calls.cancel.length, 1);
});

test('a window query sends a time range and never a cursor', async () => {
  const local = fakeProvider({ fileId: 'a', items: itemsFor('a', [1]) });
  const provider = combined.createCombinedProvider({
    scope: { local: ['a'], server: [] },
    localFactory: () => local
  });
  await provider.window({ text: 'x', from_ns: '1000', to_ns: '2000' });
  const spec = local.calls.search[0];
  assert.deepEqual(spec.time_range, { from_ns: '1000', to_ns: '2000' });
  assert.equal(spec.file_id, 'a');
  assert.equal(spec.cursor, undefined);
});
