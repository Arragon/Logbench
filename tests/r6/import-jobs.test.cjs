'use strict';
/*
 * R6-020 acceptance. The engine is replaced by an in-memory recorder, because
 * what is under test is the pipeline's discipline: how much it reads at once,
 * how many writes it keeps in flight, what survives a pause or a failure.
 * The real engine and real IndexedDB are covered by qa/r6/provider_local.py and
 * the import harness.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const jobs = require(path.join(ROOT, 'src', 'shared', 'import-jobs.js'));
const framer = require(path.join(ROOT, 'src', 'shared', 'framer.js'));
const time = require(path.join(ROOT, 'src', 'shared', 'time.js'));

function line(i) {
  const level = i % 13 === 0 ? 'ERROR' : 'INFO';
  return `[2026-01-01 00:00:${String(i % 60).padStart(2, '0')}.${String(i % 1000).padStart(3, '0')}] ` +
    `[${level}] [app] [100] [200] [worker.py:${i}] [req-${i}] [user] run() message ${i} energy\n`;
}

function makeFile(name, lines, lastModified) {
  const text = Array.from({ length: lines }, (_, i) => line(i)).join('');
  const bytes = new TextEncoder().encode(text);
  const reads = [];
  return {
    name,
    size: bytes.length,
    lastModified: lastModified || 1,
    bytes,
    reads,
    slice(start, end) {
      reads.push(end - start);
      return { arrayBuffer: async () => bytes.slice(start, end).buffer };
    }
  };
}

function makeClient(options) {
  options = options || {};
  const calls = [];
  const blocks = new Map();
  const published = [];
  let inFlight = 0;
  let peakInFlight = 0;
  let failOn = options.failOn || null;

  return {
    calls,
    blocks,
    published,
    peakInFlight: () => peakInFlight,
    async call(method, params) {
      calls.push({ method, params });
      if (failOn && failOn.method === method && failOn.file === params.file_id) {
        const error = new Error('injected failure');
        error.code = failOn.code || 'storage_error';
        throw error;
      }
      inFlight += 1;
      peakInFlight = Math.max(peakInFlight, inFlight);
      await new Promise(resolve => setTimeout(resolve, options.delay || 1));
      inFlight -= 1;
      if (method === 'stageBlock') {
        const key = params.file_id + '|' + params.generation;
        if (!blocks.has(key)) blocks.set(key, []);
        blocks.get(key).push({ block_no: params.block_no, records: params.records });
        return { file_id: params.file_id, block_no: params.block_no, records: params.records.length };
      }
      if (method === 'publishIndex') {
        published.push(params);
        return { file_id: params.file_id, generation: params.generation };
      }
      return { ok: true };
    },
    recordsOf(fileId) {
      const list = blocks.get(fileId + '|1') || [];
      return list.sort((a, b) => a.block_no - b.block_no).flatMap(block => block.records);
    }
  };
}

function build(files, options) {
  const client = makeClient(options);
  const events = [];
  const queue = jobs.createImportQueue(Object.assign({
    client,
    framer,
    time,
    emit: (name, payload) => events.push({ name, payload }),
    schedule: (fn) => setTimeout(fn, 0)
  }, options || {}));
  return { client, queue, events, files };
}

async function waitFor(predicate, timeout = 8000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return true;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  return false;
}

test('tasks are registered synchronously, before any byte is read', () => {
  const file = makeFile('a.log', 10);
  const { queue } = build([file]);
  const tasks = queue.add([file]);
  assert.equal(tasks.length, 1);
  assert.equal(tasks[0].phase, 'queued', 'visible in the same tick');
  assert.equal(file.reads.length, 0, 'no I/O has started yet');
});

test('a file is never read in one piece and each read is bounded', async () => {
  const file = makeFile('big.log', 30000);
  assert.ok(file.size > 2 * 1024 * 1024, 'fixture must exceed one chunk to prove chunking');
  const { queue } = build([file], { chunkSize: 64 * 1024 });
  const [task] = queue.add([file]);
  await waitFor(() => task.phase === 'indexed', 15000);
  assert.equal(task.phase, 'indexed');
  assert.ok(file.reads.length > 1);
  assert.ok(Math.max(...file.reads) <= 64 * 1024, 'reads must not exceed the chunk size');
  assert.equal(file.reads.reduce((a, b) => a + b, 0), file.size);
});

test('blocks are committed under the in-flight cap and published once', async () => {
  const file = makeFile('cap.log', 3000);
  const { queue, client } = build([file], { chunkSize: 128 * 1024, maxInFlight: 2 });
  const [task] = queue.add([file]);
  await waitFor(() => task.phase === 'indexed', 15000);
  assert.equal(task.phase, 'indexed');
  assert.ok(client.peakInFlight() <= 2, 'in-flight writes peaked at ' + client.peakInFlight());
  assert.equal(client.published.length, 1);
  const watermark = client.published[0].watermark;
  assert.equal(watermark.raw_bytes, file.size);
  assert.equal(watermark.indexed_records, task.records);
  assert.ok(watermark.framed_lines >= task.records);

  const staged = client.blocks.get('local-' + task.hash + '|1');
  assert.ok(staged.every((block, index) => block.block_no === index), 'block numbers are contiguous');
  assert.equal(staged.reduce((sum, block) => sum + block.records.length, 0), task.records);
});

test('records carry exact ts_ns strings and rule fields', async () => {
  const file = makeFile('time.log', 60);
  const { queue, client } = makeClient
    ? build([file], {})
    : null;
  const [task] = queue.add([file]);
  await waitFor(() => task.phase === 'indexed');
  const records = client.recordsOf('local-' + task.hash);
  assert.ok(records.length > 0);
  assert.equal(typeof records[0].ts_ns, 'string', 'ts_ns is a decimal string, not a Number');
  assert.equal(records[0].ts_ns.length, 19);
  assert.equal(typeof records[0].ts_key, 'number');
  assert.equal(records[0].record_id, 'local-' + task.hash + ':0');
});

test('pause stops at a boundary and resume continues without re-reading', async () => {
  const file = makeFile('pause.log', 2500);
  const { queue } = build([file], { chunkSize: 64 * 1024 });
  const [task] = queue.add([file]);
  await waitFor(() => task.bytes_done > 0);
  queue.pause(task.id);
  await waitFor(() => task.phase === 'paused');
  assert.equal(task.phase, 'paused');
  const readAtPause = task.checkpoint.bytes_read;
  const chunksAtPause = file.reads.length;
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(task.checkpoint.bytes_read, readAtPause, 'no progress while paused');
  assert.equal(file.reads.length, chunksAtPause, 'no reads while paused');

  queue.resume(task.id);
  await waitFor(() => task.phase === 'indexed', 15000);
  assert.equal(task.phase, 'indexed');
  const total = file.reads.reduce((a, b) => a + b, 0);
  assert.equal(total, file.size, 'every byte read exactly once across the pause');
});

test('cancel keeps committed blocks and does not publish', async () => {
  const file = makeFile('cancel.log', 2500);
  const { queue, client } = build([file], { chunkSize: 64 * 1024 });
  const [task] = queue.add([file]);
  await waitFor(() => task.blocks_written >= 1);
  queue.cancel(task.id);
  await waitFor(() => task.phase === 'cancelled');
  assert.equal(task.phase, 'cancelled');
  const kept = client.blocks.get('local-' + task.hash + '|1') || [];
  assert.ok(kept.length >= 1, 'cancel is not delete (C8)');
  assert.equal(client.published.length, 0, 'a cancelled file is never published');
});

test('one failing file does not stop the others', async () => {
  const good = makeFile('good.log', 300);
  const bad = makeFile('bad.log', 300);
  const badId = 'local-' + ('bad.log-' + bad.size + '-1');
  const client = makeClient({ failOn: { method: 'appendChunk', file: badId } });
  const queue = jobs.createImportQueue({
    client, framer, time, schedule: (fn) => setTimeout(fn, 0), maxConcurrentFiles: 2
  });
  const tasks = queue.add([good, bad]);
  await waitFor(() => tasks[0].phase === 'indexed' && tasks[1].phase === 'failed', 15000);
  assert.equal(tasks[0].phase, 'indexed');
  assert.equal(tasks[1].phase, 'failed');
  assert.equal(tasks[1].error.code, 'storage_error');
  assert.equal(client.published.length, 1, 'only the good file is published');
  queue.retry(tasks[1].id);
  assert.equal(tasks[1].error, null, 'retry clears the failure');
  assert.notEqual(tasks[1].phase, 'failed');
});

test('progress is byte-weighted and never invents a percentage', async () => {
  const small = makeFile('small.log', 20);
  const big = makeFile('big.log', 800);
  const { queue } = build([small, big]);
  const tasks = queue.add([small, big]);
  const first = queue.snapshot();
  assert.equal(first.files_total, 2);
  assert.equal(first.bytes_total, small.size + big.size);
  assert.equal(first.ratio, 0);
  await waitFor(() => tasks.every(task => task.phase === 'indexed'), 15000);
  const done = queue.snapshot();
  assert.equal(done.ratio, 1);
  assert.equal(done.files_ready, 2);
});

test('concurrent files stay at the configured limit', async () => {
  const files = Array.from({ length: 5 }, (_, i) => makeFile('f' + i + '.log', 120));
  const { queue } = build(files, { maxConcurrentFiles: 2, delay: 3 });
  const tasks = queue.add(files);
  let peak = 0;
  const timer = setInterval(() => {
    const running = tasks.filter(t => ['reading', 'saving', 'framing', 'indexing'].includes(t.phase)).length;
    peak = Math.max(peak, running);
  }, 2);
  await waitFor(() => tasks.every(t => t.phase === 'indexed'), 20000);
  clearInterval(timer);
  assert.ok(peak <= 2, 'peak concurrent files was ' + peak);
});
