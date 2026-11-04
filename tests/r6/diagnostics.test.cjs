/*
 * diagnostics.test.cjs — instrumentation must be measurable and non-leaky.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const diag = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'diagnostics.js'));

function fakeClock() {
  let value = 0;
  const clock = () => value;
  clock.advance = ms => { value += ms; };
  return clock;
}

test('phases are aggregated separately so cloning is not blamed on matching', () => {
  const clock = fakeClock();
  const recorder = diag.createRecorder({ now: clock }).start();
  recorder.span('queue', () => clock.advance(5));
  recorder.span('queue', () => clock.advance(15));
  recorder.span('match', () => clock.advance(40));
  const report = recorder.report();
  assert.equal(report.schema, 'logbench.diagnostics/1');
  assert.deepEqual(report.phases.queue, { calls: 2, total_ms: 20, max_ms: 15, p50_ms: 5, p95_ms: 15, mean_ms: 10 });
  assert.deepEqual(report.phases.match, { calls: 1, total_ms: 40, max_ms: 40, p50_ms: 40, p95_ms: 40, mean_ms: 40 });
});

test('begin/end pairs survive exceptions through span', () => {
  const clock = fakeClock();
  const recorder = diag.createRecorder({ now: clock });
  assert.throws(() => recorder.span('index', () => { clock.advance(7); throw new Error('boom'); }), /boom/);
  assert.equal(recorder.report().phases.index.calls, 1);
});

test('samples are capped so instrumentation cannot become the leak', () => {
  const recorder = diag.createRecorder({ now: () => 0 });
  for (let i = 0; i < 1500; i++) recorder.span('render', () => 0);
  assert.equal(recorder.report().phases.render.calls, 1000);
});

test('counters, bytes and gauges are recorded verbatim', () => {
  const recorder = diag.createRecorder({ now: () => 0 });
  recorder.count('worker_created').count('worker_created').count('records_cloned', 651658);
  recorder.addBytes('postmessage', 518000000);
  recorder.gauge('mounted_rows', 42);
  const report = recorder.report();
  assert.equal(report.counters.worker_created, 2);
  assert.equal(report.counters.records_cloned, 651658);
  assert.equal(report.bytes.postmessage, 518000000);
  assert.equal(report.gauges.mounted_rows, 42);
});

test('report names phases that were never exercised', () => {
  const recorder = diag.createRecorder({ now: () => 0 });
  recorder.span('read', () => 0);
  const report = recorder.report();
  assert.ok(report.phases_never_exercised.includes('storage'));
  assert.ok(!report.phases_never_exercised.includes('read'));
});

test('redaction drops file names and search terms but keeps correlation', () => {
  const secretPath = '/Users/someone/secret-project/burst_energy_2026.log';
  const secretQuery = 'customer-token-abc123';
  const redactedPath = diag.redactPath(secretPath);
  const redactedQuery = diag.redactQuery(secretQuery);
  assert.equal(redactedPath.ext, 'log');
  assert.equal(redactedQuery.length, secretQuery.length);
  assert.equal(redactedQuery.tokens, 1);
  const serialized = JSON.stringify([redactedPath, redactedQuery]);
  assert.ok(!serialized.includes('burst_energy'));
  assert.ok(!serialized.includes('customer-token'));
  assert.match(redactedPath.tag, /^[0-9a-f]{8}$/);
  assert.equal(diag.redactPath(secretPath).tag, redactedPath.tag, 'hash must be stable');
  assert.notEqual(diag.redactPath('/other/a.log').tag, redactedPath.tag);
});

test('a report never carries raw log bodies', () => {
  const line = 'Traceback (most recent call last): ValueError: bad recipe';
  const recorder = diag.createRecorder({ now: () => 0 }).start();
  recorder.span('decode', () => line.length);
  recorder.mark('first_line', diag.redactText(line));
  const text = JSON.stringify(recorder.report());
  assert.ok(!text.includes('ValueError'));
  assert.ok(!text.includes('bad recipe'));
});

test('budget distinguishes exhausted from complete', () => {
  const clock = fakeClock();
  const budget = diag.createBudget(1800, clock);
  clock.advance(1799);
  assert.equal(budget.verdict(), 'complete');
  assert.equal(budget.expired(), false);
  clock.advance(2);
  assert.equal(budget.verdict(), 'exhausted');
  assert.equal(budget.remaining(), 0);
});

test('long tasks are summarised with a max', () => {
  const recorder = diag.createRecorder({ now: () => 0 });
  recorder.longTask(120).longTask(80);
  const stats = recorder.report().long_tasks;
  assert.equal(stats.count, 2);
  assert.equal(stats.max_ms, 120);
  assert.equal(stats.total_ms, 200);
});

test('reset clears state between runs', () => {
  const recorder = diag.createRecorder({ now: () => 0 }).start();
  recorder.count('worker_created').span('read', () => 0);
  recorder.reset();
  const report = recorder.report();
  assert.deepEqual(report.counters, {});
  assert.deepEqual(report.phases, {});
});

test('summary of an empty phase is all zeros instead of NaN', () => {
  assert.deepEqual(diag.summarize([]), { calls: 0, total_ms: 0, max_ms: 0, p50_ms: 0, p95_ms: 0, mean_ms: 0 });
});
