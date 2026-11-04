'use strict';
/*
 * R6-022 acceptance. A fake clock makes "the job ran longer than the 1.8s budget"
 * reproducible without sleeping, and a fake block source makes "the scan reached
 * block 37 of 100" an assertion instead of a guess.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const QJ = require(path.join(ROOT, 'src', 'shared', 'query-jobs.js'));

function makeClock() {
  let value = 0;
  return { now: () => value, tick: (ms) => { value += ms; } };
}

/** 100 blocks of 50 records; every 7th record matches. */
function makeSource(options) {
  options = options || {};
  const blocks = 100;
  const perBlock = 50;
  const matchEvery = options.matchEvery || 7;
  const zero = options.zero || false;
  const fetched = [];
  return {
    blocks,
    perBlock,
    fetched,
    totalRecords: blocks * perBlock,
    expected: zero ? 0 : blocks * Math.ceil(perBlock / matchEvery),
    fetch(from, count) {
      fetched.push({ from, count });
      const out = [];
      for (let i = 0; i < count && from + i < blocks; i++) {
        const blockNo = from + i;
        const records = [];
        for (let r = 0; r < perBlock; r++) {
          const ordinal = blockNo * perBlock + r;
          records.push({ ordinal, hit: !zero && r % matchEvery === 0 });
        }
        out.push({ block_no: blockNo, records });
      }
      return Promise.resolve(out);
    },
    match: (record) => record.hit
  };
}

function startJob(service, source, options) {
  options = options || {};
  return service.start({
    fileId: 'demo',
    generation: 1,
    indexVersion: 1,
    signature: options.signature || 'sig-1',
    queryVersion: options.queryVersion || 1,
    blocks: source.blocks,
    records: source.totalRecords,
    pageLimit: options.pageLimit || 20,
    fetch: source.fetch,
    match: source.match,
    cacheKey: options.cacheKey
  });
}

test('a running job never reports a zero result', async () => {
  const clock = makeClock();
  const service = QJ.createQueryService({ now: clock.now, schedule: (fn) => setImmediate(fn), blocksPerSlice: 1, sliceMs: 1000 });
  const source = makeSource();
  const first = startJob(service, source);
  assert.equal(first.state, 'queued');
  // The very first visible snapshot must not claim "0 条".
  assert.equal(first.count.relation, 'at_least');
  assert.equal(first.coverage.complete, false);
  assert.equal(first.order_final, false);
});

test('a long scan reports progress, pages hits, and only then says exact', async () => {
  const clock = makeClock();
  const service = QJ.createQueryService({
    now: clock.now,
    schedule: (fn) => setImmediate(fn),
    blocksPerSlice: 1,
    sliceMs: 1,
    budgetMs: 1e9
  });
  const source = makeSource();
  const state = startJob(service, source, { pageLimit: 20 });
  await new Promise(resolve => setImmediate(resolve));
  const running = service.state(state.query_id);
  assert.equal(running.state, 'running');
  assert.ok(running.coverage.scanned_blocks >= 1);
  assert.ok(running.count.matched > 0, 'progress is visible while scanning');

  const deadline = Date.now() + 5000;
  let final = running;
  while (Date.now() < deadline) {
    final = service.state(state.query_id);
    if (final.state === 'done') break;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(final.state, 'done');
  assert.equal(final.count.relation, 'exact');
  assert.equal(final.count.matched, source.expected);
  assert.equal(final.coverage.complete, true);
  assert.equal(final.coverage.scanned_blocks, 100);
  assert.equal(final.page.items.length, 20, 'only one page of ordinals is carried');
  assert.ok(final.page.next_cursor === 20);
});

test('budget exhaustion pauses with a resume token instead of failing', async () => {
  const clock = makeClock();
  const service = QJ.createQueryService({
    now: clock.now,
    schedule: (fn) => setImmediate(fn),
    blocksPerSlice: 1,
    sliceMs: 1,
    budgetMs: 15
  });
  const source = makeSource();
  // Matching costs time in the real engine; the fake clock charges 0.3ms per
  // match so the 15ms budget is exhausted after a bounded amount of work.
  const match = source.match;
  source.match = (record) => { clock.tick(0.3); return match(record); };
  const state = startJob(service, source);
  // Every match costs time; after 15ms of active work the job must pause.
  const deadline = Date.now() + 3000;
  let paused = null;
  while (Date.now() < deadline) {
    clock.tick(2);
    const now = service.state(state.query_id);
    if (now.state === 'paused') { paused = now; break; }
    if (now.state === 'done') break;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.ok(paused, 'job should pause on budget, not run forever');
  assert.equal(paused.count.relation, 'at_least');
  assert.equal(paused.coverage.complete, false);
  assert.ok(paused.resume_token);
  assert.equal(paused.coverage.reason, 'budget');
  assert.ok(paused.coverage.scanned_blocks < 100);

  const resumed = service.resume(state.query_id, paused.resume_token);
  assert.equal(resumed.state, 'running');
  assert.equal(resumed.coverage.scanned_blocks, paused.coverage.scanned_blocks,
    'resume continues the same scan rather than restarting');
});

test('a wrong resume token is refused', () => {
  const service = QJ.createQueryService({ schedule: (fn) => setImmediate(fn) });
  const source = makeSource();
  const state = startJob(service, source);
  assert.throws(() => service.resume(state.query_id, 'not-the-token'), err => err.code === 'cursor_expired');
});

test('cancel keeps the scanned range and reports an unknown count', async () => {
  const clock = makeClock();
  const service = QJ.createQueryService({
    now: clock.now, schedule: (fn) => setImmediate(fn), blocksPerSlice: 1, sliceMs: 1, budgetMs: 1e9
  });
  const source = makeSource();
  const state = startJob(service, source);
  await new Promise(resolve => setImmediate(resolve));
  const cancelled = service.cancel(state.query_id);
  assert.equal(cancelled.state, 'cancelled');
  assert.equal(cancelled.count.relation, 'unknown');
  assert.equal(cancelled.count.matched, null, 'a cancelled scan has no total');
  assert.equal(cancelled.coverage.complete, false);
  assert.ok(cancelled.coverage.scanned_blocks >= 1, 'the scanned range is stated');
});

test('a zero-hit search still scans everything before claiming zero', async () => {
  const clock = makeClock();
  const service = QJ.createQueryService({
    now: clock.now, schedule: (fn) => setImmediate(fn), blocksPerSlice: 5, sliceMs: 1, budgetMs: 1e9
  });
  const source = makeSource({ zero: true });
  const state = startJob(service, source);
  const deadline = Date.now() + 5000;
  let final = state;
  while (Date.now() < deadline) {
    final = service.state(state.query_id);
    if (final.state === 'done') break;
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.equal(final.state, 'done');
  assert.equal(final.count.matched, 0);
  assert.equal(final.coverage.scanned_blocks, 100, 'zero is only claimed after the full scan');
  assert.equal(final.coverage.complete, true);
});

test('a repeated search is answered from cache, and a new generation invalidates it', async () => {
  const service = QJ.createQueryService({ schedule: (fn) => setImmediate(fn), blocksPerSlice: 10, sliceMs: 1 });
  const source = makeSource();
  const state = startJob(service, source, { cacheKey: 'demo|1|sig-1' });
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline && service.state(state.query_id).state !== 'done') {
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  const cached = startJob(service, source, { cacheKey: 'demo|1|sig-1' });
  assert.equal(cached.cached, true, 'the same query over the same generation is reused');
  assert.equal(cached.count.matched, source.expected);

  const changed = startJob(service, source, { cacheKey: 'demo|2|sig-1' });
  assert.notEqual(changed.cached, true, 'a different generation must not reuse the snapshot');
});

test('unknown query ids are rejected, not silently answered', () => {
  const service = QJ.createQueryService({ schedule: (fn) => setImmediate(fn) });
  assert.throws(() => service.state('nope'), err => err.code === 'unknown_query');
  assert.throws(() => service.cancel('nope'), err => err.code === 'unknown_query');
});
