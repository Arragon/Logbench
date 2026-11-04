/*
 * local-store.test.cjs — chunk maths, key building and error mapping.
 *
 * Persistence itself is NOT tested here: a fake IndexedDB would only test the
 * fake. The authoritative proof is the real-browser run in qa/r6/idb_store.py.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const store = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'local-store.js'));

test('range planning covers exactly the chunks the read touches', () => {
  const plans = store.planRange(0, 2500, 1024);
  assert.deepEqual(plans, [
    { chunk_no: 0, from: 0, to: 1024 },
    { chunk_no: 1, from: 0, to: 1024 },
    { chunk_no: 2, from: 0, to: 452 }
  ]);
});

test('a range inside one chunk reads one chunk', () => {
  assert.deepEqual(store.planRange(1500, 100, 1024), [{ chunk_no: 1, from: 476, to: 576 }]);
});

test('a range ending exactly on a boundary does not touch the next chunk', () => {
  const plans = store.planRange(1024, 1024, 1024);
  assert.equal(plans.length, 1);
  assert.deepEqual(plans[0], { chunk_no: 1, from: 0, to: 1024 });
});

test('chunk count rounds up and an empty file needs no chunks', () => {
  assert.equal(store.chunkCount(0, 1024), 0);
  assert.equal(store.chunkCount(1, 1024), 1);
  assert.equal(store.chunkCount(1024, 1024), 1);
  assert.equal(store.chunkCount(1025, 1024), 2);
});

test('invalid ranges are refused instead of silently clamped', () => {
  assert.throws(() => store.planRange(-1, 10, 1024), /范围/);
  assert.throws(() => store.planRange(0, 0, 1024), /范围/);
});

test('compound keys stay ordered as namespace, file, generation, chunk', () => {
  assert.deepEqual(store.chunkKey('ns', 'f1', 2, 7), ['ns', 'f1', 2, 7]);
  assert.deepEqual(store.metaKey('ns', 'f1'), ['ns', 'f1']);
});

test('storage errors map to codes a UI can branch on', () => {
  assert.equal(store.mapError({ name: 'QuotaExceededError' }).code, 'quota_exceeded');
  assert.equal(store.mapError({ name: 'VersionError' }).code, 'schema_mismatch');
  assert.equal(store.mapError({ name: 'AbortError' }).code, 'transaction_aborted');
  assert.equal(store.mapError({ name: 'DataError' }).code, 'invalid_record');
  assert.equal(store.mapError({ name: 'Weird' }).code, 'storage_error');
  const quota = store.mapError({ name: 'QuotaExceededError' });
  assert.match(quota.message, /配额不足/);
});

test('object stores are declared once so a schema change is a version bump', () => {
  assert.equal(store.DB_VERSION, 1);
  assert.deepEqual(Object.keys(store.STORES).sort(), [
    'file_meta', 'raw_chunks', 'record_blocks', 'rule_blocks', 'search_blocks', 'task_checkpoints', 'workspace'
  ]);
  assert.deepEqual(store.STORES.raw_chunks.keyPath, ['namespace', 'file_id', 'generation', 'chunk_no']);
});
