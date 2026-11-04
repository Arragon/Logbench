'use strict';
/*
 * R6-020 time parsing. The point of these cases is precision, not formatting:
 * a 19-digit nanosecond value must survive as a string, and two timestamps
 * 400 ns apart must not compare equal.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const T = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'time.js'));

test('nanoseconds stay exact as a decimal string', () => {
  const parsed = T.parseTs('2026-01-01 00:00:01.123456789');
  assert.equal(parsed.valid, true);
  assert.equal(typeof parsed.ns, 'string');
  assert.equal(parsed.ns, '1767225601123456789');
  // The Number path is demonstrably lossy, which is why it is not used.
  assert.notEqual(String(Number(parsed.ns)), parsed.ns);
});

test('two timestamps 400ns apart are ordered by BigInt, not by Number', () => {
  const a = T.parseTs('2026-01-01 00:00:01.123456100');
  const b = T.parseTs('2026-01-01 00:00:01.123456500');
  assert.equal(T.compareNs(a.ns, b.ns), -1);
  // The ms key deliberately cannot see a 400 ns difference; compareNs can.
  assert.equal(T.keyOf(a.ns), T.keyOf(b.ns), 'ms key is a coarse ordering key, and says so');
  assert.equal(T.compareNs(a.ns, b.ns), -1, 'exact comparison uses BigInt');
  assert.notEqual(String(Number(a.ns)), a.ns, 'Number cannot round-trip 19 digits');
});

test('an offset is applied and the same instant normalises equal', () => {
  const utc = T.parseTs('2026-01-01T00:00:00Z');
  const plus8 = T.parseTs('2026-01-01T08:00:00+08:00');
  assert.equal(T.compareNs(utc.ns, plus8.ns), 0);
  assert.equal(utc.normalised, plus8.normalised);
});

test('a timestamp without a zone is read as UTC', () => {
  assert.equal(T.parseTs('2026-01-01 00:00:00').ns, T.parseTs('2026-01-01T00:00:00Z').ns);
});

test('invalid calendar values are rejected with a reason', () => {
  assert.equal(T.parseTs('2026-13-01 00:00:00').reason, 'month');
  assert.equal(T.parseTs('2026-02-30 00:00:00').reason, 'day');
  assert.equal(T.parseTs('2024-02-29 00:00:00').valid, true, '2024 is a leap year');
  assert.equal(T.parseTs('2026-02-29 00:00:00').reason, 'day');
  assert.equal(T.parseTs('2026-01-01 24:00:00').reason, 'hour');
  assert.equal(T.parseTs('2026-01-01 00:60:00').reason, 'minute');
  assert.equal(T.parseTs('2026-01-01 00:00:61').reason, 'second');
  assert.equal(T.parseTs('not a timestamp').reason, 'shape');
  assert.equal(T.parseTs('').reason, 'empty');
});

test('keys are milliseconds and preserve ordering across the ns/ms boundary', () => {
  const parsed = T.parseTs('2026-01-01 00:00:01.999999999');
  assert.equal(parsed.key, Date.UTC(2026, 0, 1, 0, 0, 1, 999));
  assert.equal(T.keyOf(parsed.ns), parsed.key);
  assert.equal(T.keyOf(parsed.key), parsed.key);
  assert.equal(T.rangeKey(null), null);
});
