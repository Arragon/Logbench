'use strict';
/*
 * R6-028 / 029 / 030 acceptance.
 *
 * Details: raw text is loaded in bounded chunks and navigation cannot leave the
 * snapshot. Timeline: page flags come from the provider, and a revision does not
 * reset the strip. Workspace: write size follows the change, not the collection.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const D = require(path.join(ROOT, 'src', 'ui', 'details.js'));
const T = require(path.join(ROOT, 'src', 'ui', 'timeline.js'));
const W = require(path.join(ROOT, 'src', 'shared', 'workspace.js'));

function fakeProvider(overrides) {
  return Object.assign({
    async detail(ref) {
      if (ref.ordinal === 99) {
        const error = new Error('该记录已更新');
        error.code = 'stale_record';
        throw error;
      }
      return {
        file_id: ref.file_id, generation: 1, ordinal: ref.ordinal, version: 3,
        header: { level: 'ERROR', ts: '2026-01-01 00:00:00.000', lines: { first: 10, last: 12 } },
        body: ['  File "x.py", line 1', 'ValueError: boom'],
        raw_size: 1000,
        findings: []
      };
    },
    async readRaw(params) {
      return { bytes: new TextEncoder().encode('x'.repeat(params.length)) };
    },
    async getTimeline(params) {
      const size = params.page_size;
      const start = params.page * size;
      const total = 130;
      const nodes = [];
      for (let i = start; i < Math.min(total, start + size); i++) {
        nodes.push({ ordinal: i * 7, label: 'node ' + i, level: 'ERROR', ts_key: 1000 + i });
      }
      return {
        file_id: params.file_id, generation: 1, page: params.page, page_size: size,
        nodes, total, has_prev: params.page > 0,
        has_next: start + size < total,
        count: { relation: 'exact', matched: total }, coverage: { complete: true }
      };
    }
  }, overrides || {});
}

test('raw text is loaded in chunks and reports what is still pending', async () => {
  const details = D.createDetails({ provider: fakeProvider(), rawChunk: 400 });
  const ref = { file_id: 'f', generation: 1, ordinal: 1 };
  const plan = details.rawPlan(1000);
  assert.deepEqual(plan.map(p => p.length), [400, 400, 200], 'the tail chunk is short, not padded');

  const seen = [];
  const result = await details.loadRaw(ref, { total: 1000, onChunk: (chunk) => seen.push(chunk) });
  assert.equal(result.complete, true);
  assert.equal(result.text.length, 1000);
  assert.equal(seen.length, 3);
  assert.equal(seen[0].pending, 600, 'the caller knows what is not loaded yet');
});

test('a cancelled raw load rejects instead of returning a short string', async () => {
  const details = D.createDetails({ provider: fakeProvider(), rawChunk: 100 });
  const controller = { aborted: false };
  const pending = details.loadRaw({ file_id: 'f', generation: 1, ordinal: 1 },
    { total: 500, signal: controller });
  controller.aborted = true;
  await assert.rejects(pending, err => err.code === 'cancelled');
});

test('navigation stays inside the snapshot that opened the drawer', async () => {
  const details = D.createDetails({ provider: fakeProvider() });
  const drawer = await details.open({ file_id: 'f', generation: 1, ordinal: 7 },
    { snapshot: { items: [3, 7, 11] } });
  assert.equal(details.navigate(drawer, 1, null).ordinal, 11);
  assert.equal(details.navigate(drawer, -1, null).ordinal, 3);
  assert.equal(details.navigate(drawer, 5, null), null, 'past the last hit is null, not the next file row');
  assert.equal(details.navigate(drawer, -5, null), null);
});

test('a revised record is reported as stale, not shown as the clicked version', async () => {
  const details = D.createDetails({ provider: fakeProvider() });
  await assert.rejects(
    () => details.open({ file_id: 'f', generation: 1, ordinal: 99 }),
    err => err.code === 'stale_record');
  assert.equal(details.stats().stale, 1);
});

test('a traceback body is returned in full and batched fields are viewport sized', async () => {
  const details = D.createDetails({ provider: fakeProvider() });
  const drawer = await details.open({ file_id: 'f', generation: 1, ordinal: 1 });
  assert.equal(drawer.body.length, 2, 'no silent six-line truncation');
  const fields = Array.from({ length: 95 }, (_, i) => ({ key: 'k' + i, value: String(i) }));
  const batches = details.fieldBatches(fields, 40);
  assert.deepEqual(batches.map(b => b.length), [40, 40, 15]);
});

test('timeline page flags come from the provider, not from a short page', async () => {
  const timeline = T.createTimeline({ provider: fakeProvider() });
  timeline.reset({ file_id: 'f', generation: 1 });
  timeline.pageSize(50);
  let state = await timeline.load(0);
  assert.equal(state.nodes.length, 50);
  assert.equal(state.has_prev, false);
  assert.equal(state.has_next, true);
  state = await timeline.next();
  assert.equal(state.page, 1);
  assert.equal(state.has_prev, true);
  state = await timeline.next();
  assert.equal(state.nodes.length, 30, 'last page is short');
  assert.equal(state.has_next, false, 'a short last page still disables next');

  // A full last page must not enable next either: the flag is authoritative.
  const fullLastPage = T.createTimeline({
    provider: fakeProvider({
      async getTimeline(params) {
        return { file_id: 'f', generation: 1, page: params.page, page_size: params.page_size,
          nodes: Array.from({ length: params.page_size }, (_, i) => ({ ordinal: i })), total: 100,
          has_prev: true, has_next: false, count: { relation: 'exact', matched: 100 }, coverage: { complete: true } };
      }
    })
  });
  fullLastPage.reset({ file_id: 'f', generation: 1 });
  const last = await fullLastPage.load(1);
  assert.equal(last.has_next, false, 'the flag wins over "the page looked full"');
});

test('a node revision is applied in place without resetting the page', async () => {
  const timeline = T.createTimeline({ provider: fakeProvider() });
  timeline.reset({ file_id: 'f', generation: 1 });
  await timeline.load(1);
  const before = timeline.snapshot();
  const result = timeline.applyRevision({ ordinal: before.nodes[2].ordinal, label: 'updated', level: 'WARN' });
  const after = timeline.snapshot();
  assert.equal(result.changed, true);
  assert.equal(after.page, before.page, 'the page does not reset');
  assert.equal(after.nodes[2].label, 'updated');
  assert.equal(after.revision, before.revision + 1);
  assert.equal(timeline.applyRevision({ ordinal: -1 }).changed, false, 'off-page nodes are ignored');
});

test('an unsupported timeline page size is refused', () => {
  const timeline = T.createTimeline({ provider: fakeProvider() });
  assert.throws(() => timeline.pageSize(42), err => err.code === 'invalid_page_size');
  assert.equal(timeline.pageSize(150), 150);
});

function memoryStore() {
  const data = new Map();
  const writes = [];
  return {
    data, writes,
    get(key) { return data.has(key) ? JSON.parse(JSON.stringify(data.get(key))) : null; },
    put(key, value) { writes.push({ key, bytes: JSON.stringify(value).length }); data.set(key, JSON.parse(JSON.stringify(value))); return true; },
    delete(key) { data.delete(key); return true; }
  };
}

test('saving one preference does not grow with the number of bookmarks', async () => {
  const store = memoryStore();
  const workspace = W.createWorkspace({ store });
  await workspace.load();

  // 10,000 bookmarks, each written as its own small record.
  for (let i = 0; i < 10000; i++) {
    await workspace.savePatch({ bookmark: { key: 'b' + i, value: { ordinal: i } } });
  }
  const beforeBytes = store.writes.reduce((sum, write) => sum + write.bytes, 0);
  const countBefore = store.writes.length;

  await workspace.savePatch({ preferences: { theme: 'paper' } });
  const lastWrites = store.writes.slice(countBefore);
  const lastBytes = lastWrites.reduce((sum, write) => sum + write.bytes, 0);
  assert.ok(lastBytes < 400, 'one preference write was ' + lastBytes + ' bytes');
  assert.ok(lastBytes * 100 < beforeBytes, 'cost follows the change, not the collection');
});

test('query hits are refused rather than silently dropped', async () => {
  const workspace = W.createWorkspace({ store: memoryStore() });
  await assert.rejects(
    () => workspace.savePatch({ view: { queryHits: [1, 2, 3] } }),
    err => err.code === 'result_set_not_storable');
  await assert.rejects(
    () => workspace.savePatch({ hits: [1] }),
    err => err.code === 'result_set_not_storable');
});

test('two tabs writing from the same revision produce a conflict, not a silent merge', async () => {
  const store = memoryStore();
  const first = W.createWorkspace({ store });
  const second = W.createWorkspace({ store });
  await first.load();
  await second.load();
  const base = first.revision();
  await first.savePatch({ preferences: { theme: 'ink' } }, { baseRevision: base });
  await assert.rejects(
    () => second.savePatch({ preferences: { theme: 'paper' } }, { baseRevision: base }),
    err => err.code === 'revision_conflict');
});

test('repeating the same view does not write again', async () => {
  const store = memoryStore();
  const workspace = W.createWorkspace({ store });
  await workspace.load();
  const view = { scope: 'logs', selected: 'r1', anchor: { ordinal: 10, offset_y: 4 } };
  await workspace.saveView(view);
  const count = store.writes.length;
  const second = await workspace.saveView(view);
  assert.equal(second.skipped, true);
  assert.equal(store.writes.length, count, 'scrolling does not produce writes');
});
