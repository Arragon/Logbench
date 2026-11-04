'use strict';
/*
 * R6-048: server acceleration is explicit, resumable and observable.
 *
 * The uploader is tested against a stub API that records calls, so "resume skips
 * what the server already has" and "cancel stops mid-file without losing parts"
 * are assertions about behaviour rather than about a progress bar looking fine.
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const { createUploader, resumeFrom, createServerReader } = require(path.join(ROOT, 'src', 'ui', 'server-mode.js'));

function fakeFile(bytes, name) {
  return {
    name: name || 'demo.log',
    size: bytes,
    slice: (start, end) => ({
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(Math.max(0, Math.min(end, bytes) - start)))
    })
  };
}

function fakeApi(options) {
  options = options || {};
  const calls = { created: 0, parts: [], committed: 0, polled: 0 };
  const sessions = {};
  const api = {
    createUpload: (body) => {
      calls.created += 1;
      const id = 'u-1';
      sessions[id] = { session_id: id, name: body.name, size: body.size,
        chunk_size: body.chunkSize, parts_expected: Math.ceil(body.size / body.chunkSize),
        parts: [] };
      return Promise.resolve(sessions[id]);
    },
    getUpload: (id) => Promise.resolve(sessions[id]),
    putPart: (id, partNo, bytes, digest) => {
      if (options.failOn && options.failOn === partNo) {
        const error = new Error('boom');
        error.code = 'network';
        return Promise.reject(error);
      }
      calls.parts.push({ id, partNo, size: bytes.length, digest });
      if (!sessions[id]) sessions[id] = { session_id: id, parts: [] };
      sessions[id].parts.push({ part_no: partNo, size: bytes.length, digest });
      return Promise.resolve({ idempotent: false, part: partNo });
    },
    commitUpload: () => {
      calls.committed += 1;
      return Promise.resolve({ job_id: 'j-1', file_id: 'f-1', sealed_path: '/tmp/x', state: 'sealed' });
    },
    getIndexJob: () => {
      calls.polled += 1;
      const state = options.jobStates
        ? options.jobStates[Math.min(calls.polled - 1, options.jobStates.length - 1)]
        : 'indexed';
      return Promise.resolve({ id: 'j-1', state: state, progress: { phase: 'framing' } });
    }
  };
  return { api, calls, sessions };
}

test('a full upload reports monotonic progress and seals once', async () => {
  const stub = fakeApi();
  const states = [];
  const uploader = createUploader({ api: stub.api, partBytes: 1024, onState: (s) => states.push(s) });
  await uploader.upload(fakeFile(3000));
  assert.deepStrictEqual(stub.calls.parts.map((p) => p.partNo), [1, 2, 3]);
  assert.strictEqual(stub.calls.committed, 1);
  assert.strictEqual(states[states.length - 1].phase, 'indexing');
  const percents = states.map((s) => s.percent);
  assert.deepStrictEqual(percents, [...percents].sort((a, b) => a - b), 'progress never goes backwards');
  assert.strictEqual(states[states.length - 1].bytesDone, 3000);
});

test('resume uploads only the missing parts', async () => {
  const stub = fakeApi();
  // Pretend parts 1 and 3 already arrived in an earlier session.
  stub.api.getUpload = () => Promise.resolve({
    session_id: 'u-1', name: 'demo.log', size: 3000, chunk_size: 1024, parts_expected: 3,
    parts: [{ part_no: 1 }, { part_no: 3 }]
  });
  const uploader = createUploader({ api: stub.api, partBytes: 1024 });
  await uploader.upload(fakeFile(3000), 'u-1');
  assert.deepStrictEqual(stub.calls.parts.map((p) => p.partNo), [2]);
  assert.strictEqual(stub.calls.committed, 1);
});

test('resumeFrom falls back to the parts list when the server does not say', () => {
  assert.deepStrictEqual(resumeFrom({ parts_expected: 3, parts: [{ part_no: 2 }] }), [1, 3]);
  assert.deepStrictEqual(resumeFrom({ resume: [4] }), [4]);
  assert.deepStrictEqual(resumeFrom(null), []);
});

test('cancelling stops before the next part and keeps the session', async () => {
  const stub = fakeApi();
  let uploader = null;
  const states = [];
  uploader = createUploader({
    api: {
      ...stub.api,
      putPart: (id, partNo, bytes, digest) => {
        const result = stub.api.putPart(id, partNo, bytes, digest);
        return result.then((value) => { uploader.cancel(); return value; });
      }
    },
    partBytes: 1024,
    onState: (s) => states.push(s)
  });
  await uploader.upload(fakeFile(3000));
  assert.strictEqual(stub.calls.parts.length, 1, 'the second part is not started');
  assert.strictEqual(stub.calls.committed, 0, 'a cancelled upload is not sealed');
  assert.strictEqual(states[states.length - 1].phase, 'paused');
});

test('a transport failure surfaces as a failed state with a code', async () => {
  const stub = fakeApi({ failOn: 2 });
  const states = [];
  const uploader = createUploader({ api: stub.api, partBytes: 1024, onState: (s) => states.push(s) });
  await assert.rejects(() => uploader.upload(fakeFile(3000)));
  const last = states[states.length - 1];
  assert.strictEqual(last.phase, 'failed');
  assert.strictEqual(last.error.code, 'network');
  assert.strictEqual(stub.calls.committed, 0);
});

test('index polling reports phases and settles on ready', async () => {
  const stub = fakeApi({ jobStates: ['queued', 'framing', 'indexing', 'indexed'] });
  const states = [];
  const uploader = createUploader({ api: stub.api, partBytes: 4096, onState: (s) => states.push(s) });
  await uploader.upload(fakeFile(1000));
  const settled = await uploader.pollIndex({}, { interval: 0, timeout: 2000 });
  assert.strictEqual(settled.phase, 'ready');
  const phases = states.map((s) => s.phase);
  assert.ok(phases.includes('indexing'));
  assert.strictEqual(phases[phases.length - 1], 'ready');
});

test('a failed index job is reported, not silently left pending', async () => {
  const stub = fakeApi({ jobStates: ['framing', 'failed'] });
  const uploader = createUploader({ api: stub.api, partBytes: 4096 });
  await uploader.upload(fakeFile(1000));
  const state = await uploader.pollIndex({}, { interval: 0, timeout: 2000 });
  assert.strictEqual(state.phase, 'failed');
});

test('the server reader pages without downloading the file', async () => {
  const requested = [];
  const provider = {
    open: () => Promise.resolve({ index: { coverage: { state: 'complete' } } }),
    headers: (params) => {
      requested.push(params);
      return Promise.resolve({ items: [{ ordinal: 1 }], next_cursor: params.cursor == null ? 2 : null });
    },
    detail: (ref) => Promise.resolve({ record: { ordinal: ref.ordinal } }),
    search: (spec) => Promise.resolve({ query_id: 'q-1', state: 'done', spec: spec })
  };
  const reader = createServerReader({ provider: provider, limit: 50 });
  const first = await reader.open('f-1');
  assert.deepStrictEqual(first.rows, [{ ordinal: 1 }]);
  assert.strictEqual(requested[0].limit, 50);
  const second = await reader.next();
  assert.strictEqual(second.done, true);
  assert.strictEqual(requested[1].cursor, 2);
  const detail = await reader.detail(7);
  assert.strictEqual(detail.record.ordinal, 7);
});
