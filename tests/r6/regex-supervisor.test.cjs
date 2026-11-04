'use strict';
/*
 * R6-024 acceptance at the supervisor level. The Worker is fake here on purpose:
 * what is under test is the policy — when to destroy an executor, what a killed
 * slice is allowed to claim, and how the 2s slice clock differs from the 30s
 * budget clock. `qa/r6/regex_isolation.py` proves the same policy against a real
 * Worker that is really terminated.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const SV = require(path.join(ROOT, 'src', 'shared', 'regex-supervisor.js'));

function makeClock() {
  let value = 0;
  return { now: () => value, tick: (ms) => { value += ms; } };
}

/**
 * A fake executor. `policy` decides what a post does:
 *   ok    — reply immediately with matches for texts containing 'energy'
 *   hang  — never reply (the supervisor must terminate)
 *   error — reply invalid_pattern
 */
function makeFactory(policy) {
  const workers = [];
  function spawn() {
    const worker = {
      policy,
      terminated: false,
      posted: 0,
      terminate() { worker.terminated = true; },
      onMessage(fn) { worker.listener = fn; },
      post(message) {
        worker.posted += 1;
        if (policy === 'hang') return;
        if (policy === 'error') {
          worker.listener({ type: 'result', id: message.id, ok: false,
            error: { code: 'invalid_pattern', message: 'bad pattern' } });
          return;
        }
        const hits = [];
        let count = 0;
        message.texts.forEach((text, index) => {
          if (text.indexOf('energy') !== -1) { count += 1; if (hits.length < 2000) hits.push(message.ordinals[index]); }
        });
        worker.listener({ type: 'result', id: message.id, ok: true, hits, count,
          scanned: message.texts.length, elapsed_ms: 1 });
      }
    };
    workers.push(worker);
    return worker;
  }
  return { spawn, workers };
}

test('a healthy scan completes and counts every hit', async () => {
  const factory = makeFactory('ok');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn, sliceMs: 2000 });
  const texts = Array.from({ length: 4500 }, (_, i) => i % 3 === 0 ? 'energy spike ' + i : 'idle ' + i);
  const result = await supervisor.scan({ pattern: 'energy', texts });
  assert.equal(result.state, 'done');
  assert.equal(result.coverage.complete, true);
  assert.equal(result.scanned_records, 4500);
  assert.equal(result.matched, 1500);
  assert.equal(result.hits.length, 1500);
  assert.equal(factory.workers.length, 3, 'one worker per 2000-record slice');
  assert.ok(factory.workers.every(w => w.terminated === false));
});

test('a slice that never answers is terminated and the job ends partial', async () => {
  const factory = makeFactory('hang');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn, sliceMs: 5, recordsPerSlice: 1000 });
  const texts = Array.from({ length: 3000 }, (_, i) => 'energy ' + i);
  const result = await supervisor.scan({ pattern: '(a+)+$', texts });
  assert.equal(result.state, 'partial');
  assert.equal(result.killed, true);
  assert.equal(result.error.code, 'slice_timeout');
  assert.equal(result.error.unscanned_from, 0);
  assert.equal(result.coverage.complete, false, 'a killed slice is never complete');
  assert.equal(result.coverage.unscanned_records, 3000);
  assert.equal(result.matched, 0);
  assert.ok(factory.workers[0].terminated, 'the executor must actually be terminated');
  assert.equal(supervisor.stats().terminated, 1);
});

test('a killed slice is not reported as an exact zero', async () => {
  const factory = makeFactory('hang');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn, sliceMs: 5, recordsPerSlice: 500 });
  const result = await supervisor.scan({ pattern: '(a+)+$', texts: new Array(500).fill('a'.repeat(50)) });
  assert.notEqual(result.state, 'done');
  assert.notEqual(result.coverage.complete, true);
  // The caller must render "未完成的扫描" rather than "0 条".
  assert.equal(result.state, 'partial');
});

test('the budget clock pauses a long healthy scan without calling it a regex error', async () => {
  const clock = makeClock();
  const factory = makeFactory('ok');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn, sliceMs: 2000, budgetMs: 10, now: clock.now });
  const texts = Array.from({ length: 10000 }, (_, i) => 'energy ' + i);
  // Charge the clock as each slice is dispatched, so the budget is reached
  // after a bounded amount of real work.
  const originalPost = factory.spawn;
  let posted = 0;
  const spawn = () => {
    const worker = originalPost();
    const post = worker.post;
    worker.post = (message) => { posted += 1; clock.tick(4); post(message); };
    return worker;
  };
  const result = await SV.createSupervisor({ spawn, sliceMs: 2000, budgetMs: 10, now: clock.now })
    .scan({ pattern: 'energy', texts });
  assert.equal(result.state, 'paused');
  assert.equal(result.coverage.reason, 'budget');
  assert.equal(result.error, null, 'budget exhaustion is not an error');
  assert.ok(result.resume_token, 'a paused scan can be resumed');
  assert.ok(result.scanned_records > 0 && result.scanned_records < 10000);
  assert.equal(posted, result.slices);
});

test('an invalid pattern fails without pretending the records were scanned', async () => {
  const factory = makeFactory('error');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn });
  const result = await supervisor.scan({ pattern: '(', texts: ['a', 'b'] });
  assert.equal(result.state, 'failed');
  assert.equal(result.error.code, 'invalid_pattern');
  assert.equal(result.coverage.complete, false);
  assert.equal(result.scanned_records, 0);
});

test('pattern identity is stable and does not leak the pattern text', async () => {
  const factory = makeFactory('ok');
  const supervisor = SV.createSupervisor({ spawn: factory.spawn });
  const first = await supervisor.scan({ pattern: 'secret-token-abc', texts: ['x'] });
  const second = await supervisor.scan({ pattern: 'secret-token-abc', texts: ['x'] });
  assert.equal(first.pattern_id, second.pattern_id, 'the same pattern has the same id');
  assert.ok(first.pattern_id.indexOf('secret') === -1, 'the id is a hash, not the pattern');
  const other = await supervisor.scan({ pattern: 'secret-token-abd', texts: ['x'] });
  assert.notEqual(other.pattern_id, first.pattern_id);
});
