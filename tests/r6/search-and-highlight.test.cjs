'use strict';
/*
 * R6-025 / R6-026 acceptance.
 *
 * Search: the assertions are about wording and versioning — a running scan must
 * not say "0 条", an IME composition must not submit, and a stale reply must not
 * replace a newer one.
 *
 * Highlight: marks are computed only for the rows passed in, the cache is keyed
 * by record version and query signature, and log text is never turned into HTML.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const SC = require(path.join(ROOT, 'src', 'ui', 'search-controller.js'));
const HL = require(path.join(ROOT, 'src', 'ui', 'highlight-controller.js'));
const Q = require(path.join(ROOT, 'src', 'shared', 'query.js'));

function makeClock() {
  let value = 0;
  const queue = [];
  return {
    now: () => value,
    tick: (ms) => { value += ms; },
    schedule(fn, ms) {
      const entry = { fn, at: value + (ms || 0), id: queue.length + 1 };
      queue.push(entry);
      return entry.id;
    },
    clear(id) { const index = queue.findIndex(e => e.id === id); if (index >= 0) queue.splice(index, 1); },
    /** Run everything whose time has come. */
    async flush(step = 100) {
      for (let guard = 0; guard < 200; guard++) {
        this.tick(step);
        const due = queue.filter(e => e.at <= value);
        if (!due.length) {
          if (!queue.length) return;
          this.tick(step);
          continue;
        }
        for (const entry of due) {
          queue.splice(queue.indexOf(entry), 1);
          entry.fn();
        }
        await new Promise(resolve => setImmediate(resolve));
      }
    }
  };
}

function fakeProvider(behaviour) {
  const calls = [];
  let counter = 0;
  return {
    calls,
    async search(spec) {
      calls.push(spec.text);
      counter += 1;
      return behaviour(spec, counter);
    },
    async poll() { return { state: 'done', query_id: 'q', count: { relation: 'exact', matched: 0 }, coverage: { complete: true } }; },
    async cancel(id) { return { state: 'cancelled', query_id: id, count: { relation: 'unknown', matched: null }, coverage: { complete: false, scanned_blocks: 1, total_blocks: 10 } }; },
    async resume() { return { state: 'running', query_id: 'q' }; }
  };
}

test('debounce collapses keystrokes into one request', async () => {
  const clock = makeClock();
  const provider = fakeProvider(() => ({ state: 'done', query_id: 'q1', count: { relation: 'exact', matched: 3 }, coverage: { complete: true, scanned_blocks: 10, total_blocks: 10 } }));
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock) });
  controller.input('e');
  controller.input('en');
  controller.input('ene');
  controller.input('ener');
  controller.input('energy');
  await clock.flush();
  assert.deepEqual(provider.calls, ['energy'], 'only the settled text is searched');
});

test('an IME composition is not submitted until it ends', async () => {
  const clock = makeClock();
  const provider = fakeProvider(() => ({ state: 'done', query_id: 'q', count: { relation: 'exact', matched: 1 }, coverage: { complete: true, scanned_blocks: 1, total_blocks: 1 } }));
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock) });
  controller.compositionStart();
  controller.input('dian');
  await clock.flush();
  assert.deepEqual(provider.calls, [], 'nothing is submitted mid-composition');
  controller.compositionEnd('电量');
  await clock.flush();
  assert.deepEqual(provider.calls, ['电量']);
});

test('Enter submits immediately without waiting for the debounce', async () => {
  const clock = makeClock();
  const provider = fakeProvider(() => ({ state: 'done', query_id: 'q', count: { relation: 'exact', matched: 2 }, coverage: { complete: true, scanned_blocks: 1, total_blocks: 1 } }));
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock), debounceMs: 250 });
  controller.input('energy');
  await controller.submit();
  assert.deepEqual(provider.calls, ['energy']);
});

test('a running scan is described as "at least", never as zero', async () => {
  const clock = makeClock();
  const provider = fakeProvider(() => ({
    state: 'running', query_id: 'q1',
    count: { relation: 'at_least', matched: 1240 },
    coverage: { complete: false, scanned_blocks: 34, total_blocks: 100 }
  }));
  // poll keeps returning "running" so the controller stays in the searching phase
  provider.poll = async () => ({ state: 'running', query_id: 'q1', count: { relation: 'at_least', matched: 1240 }, coverage: { complete: false, scanned_blocks: 40, total_blocks: 100 } });
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock) });
  controller.input('energy');
  await clock.flush(200);
  const text = controller.describe().text;
  assert.ok(/至少/.test(text) || /上一份/.test(text), 'wording was: ' + text);
  assert.ok(!/^0 条/.test(text), 'a running scan must not claim zero');
});

test('only a finished scan is allowed to say "0 条"', async () => {
  const clock = makeClock();
  const provider = fakeProvider(() => ({ state: 'done', query_id: 'q', count: { relation: 'exact', matched: 0 }, coverage: { complete: true, scanned_blocks: 100, total_blocks: 100 } }));
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock) });
  controller.input('zzz');
  await clock.flush();
  assert.equal(controller.describe().text, '0 条');
  assert.equal(controller.describe().relation, 'exact');
});

test('a cancelled scan says so and reports the scanned share', () => {
  const controller = SC.createSearchController({ provider: fakeProvider(() => ({})), baseSpec: { file_id: 'demo', generation: 1 }, schedule: (fn) => setTimeout(fn, 0) });
  const text = controller.describeResult({
    state: 'cancelled', count: { relation: 'unknown', matched: null },
    coverage: { complete: false, scanned_blocks: 34, total_blocks: 100 }
  });
  assert.equal(text.relation, 'unknown');
  assert.ok(/已取消/.test(text.text));
  assert.ok(/34%/.test(text.text));
  assert.ok(!/0 条/.test(text.text));
});

test('a paused scan offers resume and states the lower bound', () => {
  const controller = SC.createSearchController({ provider: fakeProvider(() => ({})), baseSpec: { file_id: 'demo', generation: 1 }, schedule: (fn) => setTimeout(fn, 0) });
  const text = controller.describeResult({
    state: 'paused', count: { relation: 'at_least', matched: 90 },
    coverage: { complete: false, scanned_blocks: 10, total_blocks: 100 }, resume_token: 'q:10:90'
  });
  assert.equal(text.relation, 'at_least');
  assert.equal(text.resumable, true);
  assert.ok(/至少 90 条/.test(text.text), text.text);
});

test('a slow older reply cannot replace a newer one', async () => {
  const clock = makeClock();
  const pending = [];
  const provider = {
    search(spec) {
      return new Promise((resolve) => {
        pending.push({ text: spec.text, resolve });
      });
    },
    async poll() { return { state: 'done', query_id: 'q', count: { relation: 'exact', matched: 1 }, coverage: { complete: true } }; },
    async cancel() { return null; },
    async resume() { return null; }
  };
  const controller = SC.createSearchController({ provider, baseSpec: { file_id: 'demo', generation: 1 }, now: clock.now, schedule: clock.schedule.bind(clock) });
  controller.input('slow');
  await clock.flush();
  controller.input('fast');
  await clock.flush();
  assert.equal(pending.length, 2);
  // Resolve the newer one first, then the older one.
  pending[1].resolve({ state: 'done', query_id: 'q2', count: { relation: 'exact', matched: 2 }, coverage: { complete: true } });
  await new Promise(resolve => setImmediate(resolve));
  pending[0].resolve({ state: 'done', query_id: 'q1', count: { relation: 'exact', matched: 99 }, coverage: { complete: true } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(controller.state().result.count.matched, 2, 'the newer query owns the state');
});

test('highlighting computes marks for the window only, and caches by version', () => {
  const highlighter = HL.createHighlighter({
    computeMarks: (record, ast) => Q.computeMarks(record, ast, { max_marks: 20 }),
    textOf: (record) => [record.message].concat(record.body || []).join('\n')
  });
  const ast = Q.normalizeQuery({ text: 'energy' }).ast;
  const context = { ast, query_signature: 'sig-1', projection_version: 'v1' };
  const window = Array.from({ length: 40 }, (_, i) => ({
    ordinal: i, record_id: 'r' + i, version: 1,
    message: i % 4 === 0 ? 'energy spike ' + i : 'idle ' + i
  }));
  const out = highlighter.forWindow(window, context);
  assert.equal(out.length, 40);
  assert.equal(highlighter.stats().calls, 40, 'one compute per visible row, not per result');
  assert.equal(out[0].marks.length, 1);

  highlighter.forWindow(window, context);
  assert.equal(highlighter.stats().calls, 40, 'the second pass is served from cache');
  assert.ok(highlighter.stats().hits >= 40);

  const revised = window.map(record => ({ ...record, version: 2 }));
  highlighter.forWindow(revised, context);
  assert.equal(highlighter.stats().calls, 80, 'a new record version is not a cache hit');
});

test('log text is split into text segments, never HTML', () => {
  const highlighter = HL.createHighlighter({
    computeMarks: () => [],
    textOf: (record) => record.message
  });
  const hostile = '<img src=x onerror=alert(1)> energy';
  const segments = highlighter.segments(hostile, []);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, hostile, 'the markup stays literal text');
  assert.equal(Object.keys(segments[0]).includes('html'), false);
});

test('overlapping marks merge text but keep both term ids', () => {
  const highlighter = HL.createHighlighter({ computeMarks: () => [], textOf: () => '' });
  const text = 'abcdef';
  const segments = highlighter.segments(text, [
    { start: 1, end: 4, term: 0 },
    { start: 2, end: 5, term: 1 }
  ]);
  assert.equal(segments.map(s => s.text).join(''), text, 'no characters are duplicated or lost');
  const overlap = segments.find(s => s.text === 'cd');
  assert.deepEqual(overlap.terms, [0, 1], 'the shared span carries both term ids');
  const union = new Set(segments.filter(s => s.terms).flatMap(s => s.terms));
  assert.deepEqual([...union].sort(), [0, 1]);
});
