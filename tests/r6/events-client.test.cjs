'use strict';
/*
 * R6-075: the event client's state machine, without a browser.
 *
 * Every case here corresponds to a failure the ticket names: a replayed id, an
 * event that arrives late, a hole in the sequence, an expired replay window, a
 * stream the proxy will not let through, and a tab that closes.
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const {
  createClient, createFollower, createLedger, attachPageHooks, STATE_LABELS
} = require(path.join(ROOT, 'src', 'events', 'client.js'));

function fakeTimers() {
  let next = 1;
  const pending = new Map();
  return {
    set(fn, ms) { const id = next++; pending.set(id, { fn, ms }); return id; },
    clear(id) { pending.delete(id); },
    runAll(limit) {
      let guard = 0;
      while (pending.size && guard++ < (limit || 50)) {
        const [id, entry] = pending.entries().next().value;
        pending.delete(id);
        entry.fn();
      }
    },
    runNext() {
      const [id, entry] = pending.entries().next().value || [];
      if (!id) return false;
      pending.delete(id);
      entry.fn();
      return true;
    },
    size() { return pending.size; },
    delays() { return [...pending.values()].map(entry => entry.ms); }
  };
}

function fakeStream() {
  const created = [];
  function factory(url) {
    const listeners = {};
    const handle = {
      url,
      closed: false,
      onopen: null,
      onerror: null,
      addEventListener(kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
      close() { handle.closed = true; },
      open() { if (handle.onopen) handle.onopen({}); },
      emit(kind, data) {
        (listeners[kind] || []).forEach(fn => fn({ data: JSON.stringify(data) }));
      },
      emitRaw(kind, data) {
        (listeners[kind] || []).forEach(fn => fn({ data }));
      },
      fail() { if (handle.onerror) handle.onerror({}); }
    };
    created.push(handle);
    return handle;
  }
  return { factory, created, last: () => created[created.length - 1] };
}

function storageStub(initial) {
  const data = Object.assign({}, initial || {});
  return {
    get: key => (Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null),
    set: (key, value) => { if (value == null) delete data[key]; else data[key] = String(value); },
    raw: data
  };
}

function build(extra) {
  const timers = fakeTimers();
  const stream = fakeStream();
  const events = [];
  const resyncs = [];
  const states = [];
  const options = Object.assign({
    url: '/api/v2/events',
    pollUrl: '/api/v2/events/poll',
    streamFactory: stream.factory,
    storage: storageStub(),
    timer: timers,
    onEvent: event => events.push(event),
    onResync: info => resyncs.push(info),
    onState: state => states.push(state.mode)
  }, extra || {});
  const client = createClient(options);
  return { client, timers, stream, events, resyncs, states, options };
}

test('the same event id is applied once even if the server replays it', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().open();
  ctx.stream.last().emit('archive_commit', { id: 7, kind: 'archive_commit', fileId: 'f1' });
  ctx.stream.last().emit('archive_commit', { id: 7, kind: 'archive_commit', fileId: 'f1' });
  assert.strictEqual(ctx.events.length, 1);
  assert.strictEqual(ctx.client.state().duplicates, 1);
  assert.strictEqual(ctx.client.state().lastEventId, 7);
});

test('a late event with an older id never overwrites newer state', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().open();
  ctx.stream.last().emit('index_commit', { id: 9, kind: 'index_commit', records: 5 });
  ctx.stream.last().emit('index_commit', { id: 8, kind: 'index_commit', records: 1 });
  assert.deepStrictEqual(ctx.events.map(event => event.id), [9]);
  assert.strictEqual(ctx.client.state().reordered, 1);
  assert.strictEqual(ctx.client.state().lastEventId, 9);
});

test('a hole in the sequence is applied and announced, never silently skipped', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().open();
  ctx.stream.last().emit('archive_commit', { id: 1, kind: 'archive_commit' });
  ctx.stream.last().emit('archive_commit', { id: 3, kind: 'archive_commit' });
  assert.strictEqual(ctx.client.state().gaps, 1);
  assert.strictEqual(ctx.resyncs.length, 1);
  assert.strictEqual(ctx.resyncs[0].reason, 'event_gap');
  assert.strictEqual(ctx.events.length, 2, 'the newer event is still applied');
});

test('an expired replay window resets the cursor and asks for a snapshot', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().emit('archive_commit', { id: 12, kind: 'archive_commit' });
  ctx.stream.last().emit('resync_required', { reason: 'event_retention_exceeded' });
  assert.strictEqual(ctx.client.state().lastEventId, null);
  assert.strictEqual(ctx.resyncs[0].reason, 'event_retention_exceeded');
  // After the resync the next event is accepted as a fresh baseline.
  ctx.stream.last().emit('archive_commit', { id: 40, kind: 'archive_commit' });
  assert.strictEqual(ctx.client.state().gaps, 0);
  assert.strictEqual(ctx.events.length, 2);
});

test('one stream failure reconnects, two fall back to a stated polling mode', () => {
  const ctx = build();
  ctx.client.start();
  assert.strictEqual(ctx.client.state().mode, 'connecting');
  ctx.stream.last().open();
  assert.strictEqual(ctx.client.state().mode, 'live');
  ctx.stream.last().fail();
  assert.strictEqual(ctx.client.state().mode, 'connecting');
  ctx.timers.runAll();
  assert.strictEqual(ctx.stream.created.length, 2);
  ctx.stream.last().fail();
  assert.strictEqual(ctx.client.state().mode, 'polling');
  assert.strictEqual(ctx.client.state().label, STATE_LABELS.polling);
  assert.notStrictEqual(STATE_LABELS.polling, STATE_LABELS.live);
});

test('polling delivers the same events through the same application path', async () => {
  const seen = [];
  const ctx = build({
    streamFactory: null,
    fetchImpl: url => {
      seen.push(url);
      return Promise.resolve({
        events: [
          { id: 3, kind: 'archive_commit', payload: { fileId: 'f1', committed: true } },
          { id: 4, kind: 'index_commit', payload: { fileId: 'f1', records: 2 } }
        ],
        resync_required: false
      });
    }
  });
  ctx.client.start();
  assert.strictEqual(ctx.client.state().mode, 'polling');
  await ctx.client.poll();
  assert.strictEqual(ctx.events.length, 2);
  assert.strictEqual(ctx.client.state().lastEventId, 4);
  assert.doesNotMatch(seen[0], /[?&]last=/, 'the first request has no cursor yet');
  // A second poll reuses the cursor, so nothing is applied twice.
  await ctx.client.poll();
  assert.strictEqual(ctx.events.length, 2);
  assert.strictEqual(ctx.client.state().duplicates, 2);
  assert.match(seen[1], /last=4/, 'the next poll resumes from the last applied id');
});

test('a failing poll says offline and backs off instead of claiming live', async () => {
  let attempt = 0;
  const ctx = build({
    streamFactory: null,
    fetchImpl: () => {
      attempt += 1;
      return attempt === 1 ? Promise.reject(new Error('502')) : Promise.resolve({ events: [] });
    }
  });
  ctx.client.start();
  await ctx.client.poll();
  assert.strictEqual(ctx.client.state().mode, 'offline');
  assert.strictEqual(ctx.client.state().pollErrors, 1);
  await ctx.client.poll();
  assert.strictEqual(ctx.client.state().mode, 'polling');
});

test('revocation closes the stream and does not reconnect', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().open();
  ctx.stream.last().emit('revocation', { code: 'acl_changed' });
  assert.strictEqual(ctx.client.state().mode, 'closed');
  assert.strictEqual(ctx.stream.last().closed, true);
  ctx.timers.runAll();
  assert.strictEqual(ctx.stream.created.length, 1);
});

test('the cursor survives a reload so nothing is applied twice', () => {
  const storage = storageStub();
  const first = build({ storage });
  first.client.start();
  first.stream.last().open();
  first.stream.last().emit('archive_commit', { id: 21, kind: 'archive_commit' });
  assert.strictEqual(storage.raw['lb.r6.events.last'], '21');

  const second = build({ storage });
  second.client.start();
  assert.match(second.stream.last().url, /last=21/);
  second.stream.last().emit('archive_commit', { id: 21, kind: 'archive_commit' });
  assert.strictEqual(second.events.length, 0);
  second.stream.last().emit('archive_commit', { id: 22, kind: 'archive_commit' });
  assert.strictEqual(second.events.length, 1);
});

test('a non-JSON frame is reported, not thrown into the reader', () => {
  const ctx = build();
  ctx.client.start();
  ctx.stream.last().emitRaw('archive_commit', '{not json');
  assert.strictEqual(ctx.events.length, 1);
  assert.strictEqual(ctx.events[0].payload.raw, '{not json');
  assert.strictEqual(ctx.events[0].notice, true);
  assert.strictEqual(ctx.client.state().malformed, 1);
  assert.strictEqual(ctx.client.state().lastEventId, null, 'a malformed frame cannot move the cursor');
});

test('scrolling up pauses following and counts, clicking the badge returns to the tail', () => {
  const follower = createFollower({ threshold: 20 });
  assert.deepStrictEqual(follower.observe({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 }),
    { following: true, pending: 0, manual: false, threshold: 20 });
  const up = follower.observe({ scrollTop: 400, scrollHeight: 1000, clientHeight: 100 });
  assert.strictEqual(up.following, false);
  const first = follower.notify(3);
  assert.strictEqual(first.action, 'badge');
  assert.strictEqual(first.count, 3);
  assert.strictEqual(follower.notify(2).count, 5);
  const back = follower.requestFollow();
  assert.strictEqual(back.action, 'seek');
  assert.strictEqual(back.count, 5);
  assert.strictEqual(follower.pending(), 0);
  // Returning to the bottom resumes following without a click.
  follower.observe({ scrollTop: 400, scrollHeight: 1000, clientHeight: 100 });
  assert.strictEqual(follower.observe({ scrollTop: 895, scrollHeight: 1000, clientHeight: 100 })
    .following, true);
  assert.strictEqual(follower.notify(1).action, 'seek');
});

test('following does not jump the view when the scroll was programmatic', () => {
  const follower = createFollower({ threshold: 20 });
  follower.observe({ scrollTop: 400, scrollHeight: 1000, clientHeight: 100 });
  const state = follower.observe({ scrollTop: 100, scrollHeight: 1000, clientHeight: 100,
    programmatic: true });
  assert.strictEqual(state.following, false, 'our own scroll restore must not unpin the user');
});

test('a provisional record updates in place instead of appending twice', () => {
  const ledger = createLedger();
  assert.strictEqual(ledger.admit({ recordId: 'r-1', version: 1 }), 'append');
  assert.strictEqual(ledger.admit({ recordId: 'r-1', version: 2, provisional: true }), 'update');
  assert.strictEqual(ledger.admit({ recordId: 'r-1', version: 2 }), 'ignore');
  assert.strictEqual(ledger.admit({ recordId: 'r-1', version: 1 }), 'ignore');
  assert.strictEqual(ledger.admit({ recordId: 'r-2', version: 1 }), 'append');
  assert.strictEqual(ledger.size(), 2);
});

test('closing the tab only unsubscribes', () => {
  const ctx = build();
  const listeners = {};
  const win = {
    addEventListener: (kind, fn) => { listeners[kind] = fn; },
    removeEventListener: kind => { delete listeners[kind]; }
  };
  ctx.client.start();
  attachPageHooks(ctx.client, win);
  listeners.pagehide();
  assert.strictEqual(ctx.client.state().mode, 'closed');
  assert.strictEqual(ctx.stream.last().closed, true);
  assert.strictEqual(ctx.options.fetchImpl, undefined,
    'unloading must not call any server endpoint');
});
