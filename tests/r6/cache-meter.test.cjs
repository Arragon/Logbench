'use strict';
/*
 * The header cache meter.
 *
 * Two things here are contracts rather than cosmetics:
 *
 * 1. every number the operator reads comes from bytes(); the meter and the quota
 *    dialog must agree on it, and "0 B" must never be a lie dressed as a number;
 * 2. the floor the frontend enforces before it sends a quota (16 MiB) has to be
 *    the same floor the API enforces, or a save the UI accepts comes back 400.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const meter = require(path.join(ROOT, 'src', 'ui', 'cache-meter.js'));

test('the module keeps the shape r6-app creates', () => {
  assert.equal(typeof meter.create, 'function');
  assert.equal(typeof meter.bytes, 'function');
});

test('bytes never renders a negative or unreadable number', () => {
  assert.equal(meter.bytes(0), '0 B');
  assert.equal(meter.bytes(-1), '0 B');
  assert.equal(meter.bytes(null), '0 B');
  assert.equal(meter.bytes(undefined), '0 B');
  assert.equal(meter.bytes('not a number'), '0 B');
  assert.equal(meter.bytes(Infinity), '0 B');
});

test('bytes switches unit at each 1024 boundary', () => {
  assert.equal(meter.bytes(1), '1 B');
  assert.equal(meter.bytes(1023), '1023 B');
  assert.equal(meter.bytes(1024), '1 KB');
  assert.equal(meter.bytes(1024 * 1024 - 1024), '1023 KB');
  assert.equal(meter.bytes(1024 * 1024), '1.0 MB');
  assert.equal(meter.bytes(1024 * 1024 * 1024 - 1024 * 1024), '1023.0 MB');
  assert.equal(meter.bytes(1024 * 1024 * 1024), '1.00 GB');
});

test('bytes renders the two numbers the header shows', () => {
  // 6 MiB of derived layers against the 1 GiB default quota: the exact pair the
  // meter puts in the header and the browser gate asserts on.
  assert.equal(meter.bytes(6 * 1024 * 1024), '6.0 MB');
  assert.equal(meter.bytes(1024 * 1024 * 1024), '1.00 GB');
  assert.equal(meter.bytes(500 * 1024 * 1024), '500.0 MB');
});

test('the quota floor the UI accepts is the floor the API enforces', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'server', 'indexing', 'cache_api.py'), 'utf8');
  const declared = /MIN_QUOTA_BYTES\s*=\s*16\s*\*\s*1024\s*\*\*\s*2/.exec(source);
  assert.ok(declared, 'cache_api.py must declare MIN_QUOTA_BYTES as 16 MiB');
  const meterSource = fs.readFileSync(
    path.join(ROOT, 'src', 'ui', 'cache-meter.js'), 'utf8');
  assert.match(meterSource, /value\s*<\s*16\s*\*\s*1048576/,
    'cache-meter.js must refuse anything under 16 MiB before it posts');
});

test('the meter reads the totals-only endpoint, not the per-file one', () => {
  const source = fs.readFileSync(
    path.join(ROOT, 'src', 'ui', 'cache-meter.js'), 'utf8');
  assert.match(source, /api\.get\('\/api\/v2\/cache\/usage'\)/);
  assert.match(source, /api\.put\('\/api\/v2\/cache\/quota'/);
  // The header must stay a meter: no delete/clear call belongs in this module.
  assert.doesNotMatch(source, /api\.(del|post)\('\/api\/v2\/cache/);
});
