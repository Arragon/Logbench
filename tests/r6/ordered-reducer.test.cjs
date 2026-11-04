'use strict';
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const { createOrderedReducer } = require(path.join(ROOT, 'src', 'shared', 'ordered-reducer.js'));

function batch(seq, extra) {
  return Object.assign({ seq: seq, boundary: { bytes: seq * 10 }, records: [{ ordinal: seq }] }, extra || {});
}

test('commits contiguous batches immediately', () => {
  const reducer = createOrderedReducer({ maxBuffer: 8 });
  assert.deepStrictEqual(reducer.accept(batch(0)).commit.map(b => b.seq), [0]);
  assert.deepStrictEqual(reducer.accept(batch(1)).commit.map(b => b.seq), [1]);
  assert.strictEqual(reducer.nextSeq(), 2);
  assert.strictEqual(reducer.checkpoint().seq, 1);
  assert.strictEqual(reducer.checkpoint().boundary.bytes, 10);
});

test('an out-of-order batch waits, then commits as a contiguous run', () => {
  const reducer = createOrderedReducer({ maxBuffer: 8 });
  reducer.accept(batch(0));
  const late = reducer.accept(batch(3));
  assert.deepStrictEqual(late.commit, []);
  assert.strictEqual(reducer.pending(), 1);
  reducer.accept(batch(1));
  const settled = reducer.accept(batch(2));
  assert.deepStrictEqual(settled.commit.map(b => b.seq), [2, 3]);
  assert.strictEqual(reducer.checkpoint().seq, 3);
});

test('the checkpoint never jumps a gap', () => {
  const reducer = createOrderedReducer({ maxBuffer: 8 });
  reducer.accept(batch(0));
  for (const seq of [2, 3, 4]) reducer.accept(batch(seq));
  // 1 is missing: 2..4 are buffered and the checkpoint stays at 0.
  assert.strictEqual(reducer.checkpoint().seq, 0);
  const filled = reducer.accept(batch(1));
  assert.deepStrictEqual(filled.commit.map(b => b.seq), [1, 2, 3, 4]);
  assert.strictEqual(reducer.checkpoint().seq, 4);
});

test('a duplicated batch is dropped, not committed twice', () => {
  const reducer = createOrderedReducer({ maxBuffer: 8 });
  reducer.accept(batch(0));
  const duplicate = reducer.accept(batch(0));
  assert.deepStrictEqual(duplicate.commit, []);
  assert.strictEqual(duplicate.dropped, 'stale');
  assert.strictEqual(reducer.stats().stale, 1);

  reducer.accept(batch(2));
  const again = reducer.accept(batch(2));
  assert.strictEqual(again.dropped, 'duplicate');
  assert.strictEqual(reducer.stats().duplicates, 1);
});

test('a batch from an older fencing token is refused', () => {
  const reducer = createOrderedReducer({ maxBuffer: 8, token: 5 });
  const old = reducer.accept(batch(0, { token: 4 }));
  assert.deepStrictEqual(old.commit, []);
  assert.strictEqual(old.dropped, 'old_token');
  assert.ok(reducer.adoptToken(6));
  assert.ok(!reducer.adoptToken(3));
  assert.strictEqual(reducer.token(), 6);
});

test('the buffer is bounded and pressure is reported before it overflows', () => {
  const reducer = createOrderedReducer({ maxBuffer: 4 });
  reducer.accept(batch(0));
  assert.strictEqual(reducer.pressure(), false);
  reducer.accept(batch(5));
  reducer.accept(batch(6));
  assert.strictEqual(reducer.pressure(), true, 'half full means pressure');
  assert.throws(() => {
    for (let seq = 7; seq < 40; seq += 1) reducer.accept(batch(seq));
  }, /overflow/);
});

test('a slow first batch keeps every later batch buffered but uncommitted', () => {
  const reducer = createOrderedReducer({ maxBuffer: 32 });
  const trace = [];
  for (let seq = 1; seq < 10; seq += 1) trace.push(...reducer.accept(batch(seq)).commit.map(b => b.seq));
  assert.deepStrictEqual(trace, [], 'nothing commits before seq 0');
  trace.push(...reducer.accept(batch(0)).commit.map(b => b.seq));
  assert.deepStrictEqual(trace, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.strictEqual(reducer.stats().max_buffered, 9);
});

test('replaying a whole schedule from scratch is idempotent', () => {
  const seqs = [0, 2, 1, 3, 5, 4, 6];
  const first = createOrderedReducer({ maxBuffer: 8 });
  const second = createOrderedReducer({ maxBuffer: 8 });
  const run = (reducer, order) => {
    const committed = [];
    for (const seq of order) committed.push(...reducer.accept(batch(seq)).commit.map(b => b.seq));
    return committed;
  };
  const a = run(first, seqs);
  const b = run(second, seqs.concat(seqs));
  assert.deepStrictEqual(a, [0, 1, 2, 3, 4, 5, 6]);
  assert.deepStrictEqual(b, a, 'a duplicate delivery changes nothing');
});
