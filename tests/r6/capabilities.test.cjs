/*
 * capabilities.test.cjs — browser capability detection without a browser.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const caps = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'capabilities.js'));

function FakeBlob() {}
FakeBlob.prototype.slice = function slice() { return null; };

function fakeEnv(overrides) {
  const env = {
    isSecureContext: true,
    indexedDB: { open() {} },
    Worker: function Worker() {},
    Blob: FakeBlob,
    navigator: { storage: { getDirectory() {}, estimate() {} } }
  };
  Object.assign(env, overrides || {});
  return env;
}

test('secure context with all APIs reports everything enabled', () => {
  const result = caps.detect(fakeEnv());
  assert.equal(result.secure_context, true);
  for (const key of ['indexeddb', 'opfs', 'worker', 'blob_slice', 'storage_estimate']) {
    assert.equal(result[key].state, 'enabled', key);
    assert.equal(result[key].reason, null, key);
  }
  assert.deepEqual(caps.blockers(result), []);
});

test('OPFS missing is not a fatal error', () => {
  const result = caps.detect(fakeEnv({ navigator: {} }));
  assert.equal(result.opfs.state, 'unavailable');
  assert.match(result.opfs.reason, /OPFS/);
  assert.deepEqual(caps.blockers(result), [], 'OPFS must never block reading');
});

test('private-network HTTP degrades OPFS with an explicit reason', () => {
  const result = caps.detect(fakeEnv({ isSecureContext: false }));
  assert.equal(result.opfs.state, 'unavailable');
  assert.match(result.opfs.reason, /非安全上下文/);
  assert.equal(result.indexeddb.state, 'enabled', 'IndexedDB still works over plain HTTP');
});

test('missing IndexedDB blocks import but keeps the reason readable', () => {
  const result = caps.detect(fakeEnv({ indexedDB: undefined }));
  assert.equal(result.indexeddb.state, 'disabled');
  assert.match(result.indexeddb.reason, /IndexedDB/);
  assert.deepEqual(caps.blockers(result), ['indexeddb']);
});

test('missing Blob.slice blocks streaming reads', () => {
  const result = caps.detect(fakeEnv({ Blob: function NoSlice() {} }));
  assert.equal(result.blob_slice.state, 'disabled');
  assert.deepEqual(caps.blockers(result), ['blob_slice']);
});

test('missing Worker degrades search speed without blocking', () => {
  const result = caps.detect(fakeEnv({ Worker: undefined }));
  assert.equal(result.worker.state, 'disabled');
  assert.match(result.worker.reason, /主线程/);
  assert.deepEqual(caps.blockers(result), []);
});

test('detect tolerates an empty environment', () => {
  const result = caps.detect({});
  assert.equal(result.secure_context, false);
  assert.equal(result.indexeddb.state, 'disabled');
  assert.equal(result.opfs.state, 'unavailable');
});
