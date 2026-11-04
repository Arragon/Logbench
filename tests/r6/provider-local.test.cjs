'use strict';
/*
 * R6-019 acceptance, minus IndexedDB itself.
 *
 * The client, the server and the engine all run for real here; only the
 * transport is in-memory and only the store is fake. That combination is what
 * lets these tests assert things a browser run cannot easily show:
 *
 *   - a late reply is counted as an orphan and never resolves anything;
 *   - a page over the limit is refused, not truncated;
 *   - a search from before a dataset switch cannot publish;
 *   - ten files cause one engine, not ten.
 *
 * Real IndexedDB behaviour (chunks, transactions, refresh) is proven separately
 * in qa/r6/provider_local.py against a real Worker.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const rpc = require(path.join(ROOT, 'src', 'shared', 'rpc.js'));
const core = require(path.join(ROOT, 'src', 'shared', 'engine-core.js'));
const query = require(path.join(ROOT, 'src', 'shared', 'query.js'));
const candidates = require(path.join(ROOT, 'src', 'shared', 'candidates.js'));
const providerFactory = require(path.join(ROOT, 'src', 'providers', 'local.js'));

/* A store/indexer pair that behaves like the real ones for the calls the engine makes. */
function makeStore(files) {
  const meta = new Map(files.map(file => [file.file_id, file.meta]));
  const raw = new Map();
  const blocks = new Map();
  const published = new Map();
  let rawReads = 0;

  const store = {
    namespace: 'ns-test',
    putFileMeta(value) { meta.set(value.file_id, value); return Promise.resolve(true); },
    listFileMeta() { return Promise.resolve([...meta.values()]); },
    putRawChunk(fileId, generation, chunkNo, bytes) {
      raw.set(fileId + '|' + generation + '|' + chunkNo, bytes); return Promise.resolve(true);
    },
    getRawRange(fileId, generation, offset, length) {
      rawReads += 1;
      const parts = [...raw.entries()]
        .filter(([key]) => key.startsWith(fileId + '|' + generation + '|'))
        .sort((a, b) => Number(a[0].split('|')[2]) - Number(b[0].split('|')[2]))
        .map(([, value]) => Buffer.from(value));
      const all = Buffer.concat(parts);
      return Promise.resolve(new Uint8Array(all.subarray(offset, offset + length)));
    },
    stats: () => ({ rawReads })
  };

  const indexer = {
    readPublished(fileId) {
      const value = published.get(fileId);
      return Promise.resolve(value || null);
    },
    getBlocks(fileId, generation, layer) {
      return Promise.resolve(blocks.get(fileId + '|' + generation + '|' + layer) || []);
    },
    publish(fileId, generation, records, watermark) {
      published.set(fileId, { file_id: fileId, generation, watermark: watermark || { raw_bytes: 1, framed_lines: records.length } });
      blocks.set(fileId + '|' + generation + '|record_blocks',
        [{ block_no: 0, records }]);
    }
  };

  return { store, indexer, meta, raw, blocks, published, stats: store.stats };
}

function makeRecords(count, prefix) {
  const records = [];
  for (let i = 0; i < count; i++) {
    records.push({
      ordinal: i,
      record_id: prefix + ':' + i,
      version: 1,
      level: i % 17 === 0 ? 'ERROR' : 'INFO',
      ts: '2026-01-01 00:00:' + String(i % 60).padStart(2, '0') + '.000',
      ts_ns: 1767225600000000000 + i * 1000000,
      lines: { first: i + 1, last: i + 1 },
      message: prefix + ' record ' + i + (i % 5 === 0 ? ' energy spike' : ''),
      body: [],
      has_trace: false
    });
  }
  return records;
}

/** Two in-process endpoints wired to each other, with a knob to hold replies. */
/** Let queued setImmediate replies drain before asserting on counters. */
function settle(turns = 6) {
  let chain = Promise.resolve();
  for (let i = 0; i < turns; i++) chain = chain.then(() => new Promise(resolve => setImmediate(resolve)));
  return chain;
}

function makeLink(options) {
  options = options || {};
  const held = [];
  let client = null;
  let server = null;
  const sent = [];

  const toServer = (message) => {
    sent.push({ direction: 'to-server', message, bytes: JSON.stringify(message).length });
    if (options.hold) { held.push(message); return; }
    setImmediate(() => server.handleMessage(message));
  };
  const toClient = (message) => {
    sent.push({ direction: 'to-client', message, bytes: JSON.stringify(message).length });
    setImmediate(() => client.handleMessage(message));
  };

  server = rpc.createRpcServer({ post: toClient, handlers: options.handlers || {} });
  client = rpc.createRpcClient({ post: toServer, timeoutMs: options.timeoutMs || 5000 });
  return {
    client,
    server,
    sent,
    flush() { const copy = held.splice(0); copy.forEach(m => server.handleMessage(m)); return settle(); },
    bytes: (direction) => sent.filter(s => s.direction === direction).reduce((sum, s) => sum + s.bytes, 0)
  };
}

function buildEngine(files) {
  const fake = makeStore(files);
  const engine = core.createEngine({
    store: fake.store,
    indexer: fake.indexer,
    normalizeQuery: query.normalizeQuery,
    matches: query.matches,
    candidates,
    buildBlockIndex: (fileId, generation) => {
      const blocks = fake.blocks.get(fileId + '|' + generation + '|record_blocks') || [];
      const records = blocks.flatMap(block => block.records);
      return candidates.buildBlockIndex(records, { records_per_block: 128 });
    }
  });
  return { fake, engine };
}

function handlersFor(engine) {
  const handlers = {};
  rpc.CLIENT_METHODS.forEach(name => {
    handlers[name] = params => {
      if (name === 'poll' || name === 'cancelSearch' || name === 'resume') return { ok: true };
      return engine.handle(name, params || {});
    };
  });
  return handlers;
}

test('ten files are served by one engine and a page never exceeds the limits', async () => {
  const files = [];
  for (let i = 0; i < 10; i++) {
    const id = 'f' + i;
    files.push({ file_id: id, meta: { file_id: id, name: id + '.log', size: 10, generation: 1, chunks: 1 } });
  }
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('f0', 1, makeRecords(5000, 'f0'));

  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client, datasetId: 'ns-test' });
  await provider.open('ns-test');

  const listed = await provider.listFiles();
  assert.equal(listed.files.length, 10);
  assert.equal(fake.stats().rawReads, 0, 'listing files must not read raw chunks');

  const page = await provider.headers({ file_id: 'f0', generation: 1, limit: 200 });
  assert.equal(page.items.length, 200);
  assert.equal(page.next_cursor, 200);
  assert.equal(page.count.relation, 'exact');

  await assert.rejects(
    () => provider.headers({ file_id: 'f0', generation: 1, limit: 2001 }),
    err => err.code === 'page_too_large');
  await assert.rejects(
    () => link.client.call('headers', { file_id: 'f0', generation: 1, limit: 5000 }),
    err => err.code === 'page_too_large');
  await assert.rejects(
    () => provider.readRaw({ file_id: 'f0', generation: 1, offset: 0, length: 5 * 1024 * 1024 }),
    err => err.code === 'page_too_large');
});

test('a stale generation is refused instead of serving another generation', async () => {
  const files = [{ file_id: 'g', meta: { file_id: 'g', name: 'g.log', size: 1, generation: 2, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('g', 2, makeRecords(10, 'g'));
  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client });
  await provider.open('ns');

  await assert.rejects(
    () => provider.headers({ file_id: 'g', generation: 1, limit: 10 }),
    err => err.code === 'stale_generation' && err.details.published === 2);
});

test('a reply arriving after a dataset switch cannot publish', async () => {
  const files = [{ file_id: 'a', meta: { file_id: 'a', name: 'a.log', size: 1, generation: 1, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('a', 1, makeRecords(50, 'a'));
  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client });
  await provider.open('first');

  // Attach the rejection handler before switching, otherwise Node reports an
  // unhandled rejection in the window between the switch and the assertion.
  const inFlight = provider.headers({ file_id: 'a', generation: 1, limit: 10 }).catch(error => error);
  await provider.open('second');
  const failure = await inFlight;
  assert.equal(failure.code, 'dataset_replaced');
});

test('an earlier search cannot overwrite a later one', async () => {
  const files = [{ file_id: 's', meta: { file_id: 's', name: 's.log', size: 1, generation: 1, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('s', 1, makeRecords(200, 's'));
  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client });
  await provider.open('ns');

  const versionBefore = provider.queryVersion();
  const first = provider.search({ file_id: 's', generation: 1, text: 'energy' }).catch(error => error);
  const second = provider.search({ file_id: 's', generation: 1, text: 'record 7' });
  const stale = await first;
  assert.equal(stale.code, 'stale_query');
  const result = await second;
  assert.equal(result.query_version, versionBefore + 2);
  assert.ok(result.count.matched > 0);
});

test('cancelled and timed-out calls leave no pending promise and count orphans', async () => {
  const files = [{ file_id: 'c', meta: { file_id: 'c', name: 'c.log', size: 1, generation: 1, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('c', 1, makeRecords(20, 'c'));
  const link = makeLink({ handlers: handlersFor(engine), hold: true });

  const pending = link.client.call('listFiles', {});
  assert.equal(link.client.pendingCount(), 1);
  link.client.cancel(pending.rpcId);
  await assert.rejects(pending, err => err.code === 'rpc_cancelled');
  assert.equal(link.client.pendingCount(), 0);

  await link.flush();  // the server finally answers the abandoned id
  assert.equal(link.client.stats().orphan_replies, 1);
  assert.equal(link.client.pendingCount(), 0);
});

test('a timeout rejects with a recoverable code and a later reply is ignored', async () => {
  const link = makeLink({ handlers: {}, hold: true, timeoutMs: 10 });
  const pending = link.client.call('listFiles', {});
  await assert.rejects(pending, err => err.code === 'rpc_timeout');
  assert.equal(link.client.stats().timeouts, 1);
  await link.flush();
  assert.equal(link.client.stats().orphan_replies, 1);
});

test('duplicate request ids run the handler once', async () => {
  let runs = 0;
  const link = makeLink({ handlers: { listFiles: () => { runs += 1; return { files: [] }; } } });
  const message = { v: rpc.PROTOCOL, type: 'call', id: 'same-id', method: 'listFiles', params: {} };
  link.server.handleMessage(message);
  link.server.handleMessage(message);
  await settle();
  assert.equal(runs, 1);
  assert.equal(link.server.stats().duplicate_ids, 1);
});

test('invalid requests are answered with a structured error, not silence', async () => {
  const link = makeLink({ handlers: {} });
  const replies = [];
  link.client.onEvent(() => {});
  const original = link.client.handleMessage.bind(link.client);
  link.client.handleMessage = (message) => { replies.push(message); original(message); };
  link.server.handleMessage({ v: 'nope', type: 'call', id: 'x', method: 'listFiles' });
  await settle();
  assert.equal(replies.length, 1);
  assert.equal(replies[0].ok, false);
  assert.equal(replies[0].error.code, 'invalid_request');
  assert.ok(replies[0].error.details.problems.length > 0);
});

test('page cache is bounded by bytes, not by luck', () => {
  const cache = core.createPageCache({ maxBytes: 100, maxEntries: 10 });
  for (let i = 0; i < 50; i++) cache.put('k' + i, { i }, 30);
  assert.ok(cache.stats().bytes <= 100 + 30, 'one entry may exceed the budget, two may not');
  assert.ok(cache.stats().evictions > 0);
  cache.get('k49');
  assert.equal(cache.stats().hits, 1);
});

test('search pages hits and reports candidate coverage', async () => {
  const files = [{ file_id: 'p', meta: { file_id: 'p', name: 'p.log', size: 1, generation: 1, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('p', 1, makeRecords(600, 'p'));
  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client });
  await provider.open('ns');

  const result = await provider.search({ file_id: 'p', generation: 1, text: 'energy', limit: 5 });
  assert.equal(result.state, 'done');
  assert.ok(result.page.items.length <= 5);
  assert.ok(result.count.matched >= 100, 'every fifth record contains the term');
  assert.equal(result.coverage.total_blocks, 5);
  assert.ok(result.coverage.pruned_blocks >= 0);
  assert.equal(result.order_final, true);
});

test('raw reads go through the generation and honour the byte cap', async () => {
  const files = [{ file_id: 'r', meta: { file_id: 'r', name: 'r.log', size: 8, generation: 1, chunks: 1 } }];
  const { fake, engine } = buildEngine(files);
  fake.indexer.publish('r', 1, makeRecords(4, 'r'));
  fake.raw.set('r|1|0', new Uint8Array([104, 105]));
  const link = makeLink({ handlers: handlersFor(engine) });
  const provider = providerFactory.createProvider({ client: link.client });
  await provider.open('ns');

  const value = await provider.readRaw({ file_id: 'r', generation: 1, offset: 0, length: 2 });
  assert.equal(Buffer.from(value.bytes).toString(), 'hi');
  assert.equal(fake.stats().rawReads, 1);

  await assert.rejects(
    () => provider.readRaw({ file_id: 'r', generation: 1, offset: 0, length: 4194305 }),
    err => err.code === 'page_too_large');
});
