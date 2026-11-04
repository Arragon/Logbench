/*
 * client.js — one reusable event stream per browser tab (R6-075).
 *
 * The server side (`server/events.py`) publishes small references: watermarks,
 * record versions, gap markers, error codes. This module consumes them.
 *
 * Rules it enforces, because each one has a failure mode that looks like "the UI
 * is fine":
 *
 *   * **one stream, routed by id.** Not one connection per open file. A tab keeps
 *     a single stream and the reader filters on `fileId` itself.
 *   * **monotonic, de-duplicated.** A reconnect replays from `Last-Event-ID`; the
 *     same revision arriving twice must not be applied twice, and an older event
 *     arriving late must not overwrite a newer one.
 *   * **a gap is announced.** If id N+2 arrives while N+1 is missing, the event is
 *     applied *and* `resync` is requested — silently skipping the hole would leave
 *     the timeline showing data that no longer matches the server.
 *   * **expired replay is honest.** `resync_required` resets the cursor and asks
 *     the caller for a fresh snapshot while keeping the current rows on screen.
 *   * **polling is a stated mode, not a hidden fallback.** When the stream cannot
 *     be established the client says `轮询`, with the same event application path.
 *   * **closing the page only unsubscribes.** Collection is server-side; this
 *     module never calls a "stop" endpoint on unload.
 *
 * Everything is injected (`streamFactory`, `fetchImpl`, `storage`, timers) so the
 * whole state machine is testable under Node with no browser and no server.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBEventClient = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var KINDS = ['archive_commit', 'index_commit', 'record_revision', 'rotation', 'gap',
    'source_error', 'task_progress'];

  var STATE_LABELS = {
    idle: '未连接',
    connecting: '连接中',
    live: '实时',
    polling: '轮询',
    offline: '已断开',
    closed: '已关闭'
  };

  var DEFAULT_POLL = 4000;
  var MAX_POLL = 30000;
  var MAX_STREAM_FAILURES = 2;

  function noop() {}

  function memoryStorage() {
    var data = {};
    return {
      get: function (key) { return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null; },
      set: function (key, value) {
        if (value == null) delete data[key];
        else data[key] = String(value);
      }
    };
  }

  function withQuery(url, params) {
    var query = [];
    for (var key in params) {
      if (params[key] == null) continue;
      query.push(encodeURIComponent(key) + '=' + encodeURIComponent(params[key]));
    }
    if (!query.length) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + query.join('&');
  }

  /**
   * @param {object} options
   *   url, pollUrl, streamFactory, fetchImpl, storage, onEvent, onState, onResync,
   *   pollInterval, maxPollInterval, maxStreamFailures, clock, timer
   */
  function createClient(options) {
    options = options || {};
    var url = options.url || '/api/v2/events';
    var pollUrl = options.pollUrl || '/api/v2/events/poll';
    var storage = options.storage || memoryStorage();
    var storageKey = options.storageKey || 'lb.r6.events.last';
    // Bound wrappers: `timer.set = setTimeout` would be invoked with `this` set to
    // the timer object, and browsers reject that with "Illegal invocation".
    var timer = options.timer || {
      set: function (fn, ms) { return setTimeout(fn, ms); },
      clear: function (id) { clearTimeout(id); }
    };
    var clock = options.clock || function () { return Date.now(); };
    var pollInterval = options.pollInterval || DEFAULT_POLL;
    var maxPollInterval = options.maxPollInterval || MAX_POLL;
    var maxStreamFailures = options.maxStreamFailures == null
      ? MAX_STREAM_FAILURES : options.maxStreamFailures;

    var state = {
      mode: 'idle',
      lastEventId: readCursor(),
      delivered: 0,
      applied: 0,
      duplicates: 0,
      reordered: 0,
      malformed: 0,
      gaps: 0,
      resyncs: 0,
      streamFailures: 0,
      polls: 0,
      pollErrors: 0,
      reason: null,
      at: null
    };

    var stream = null;
    var timerId = null;
    var pollDelay = pollInterval;
    var stopped = true;
    var seen = {};

    function readCursor() {
      var raw = storage.get(storageKey);
      if (raw == null || raw === '') return null;
      var value = Number(raw);
      return isNaN(value) ? null : value;
    }

    function writeCursor(value) {
      state.lastEventId = value;
      storage.set(storageKey, value == null ? null : value);
    }

    function setMode(mode, reason) {
      state.mode = mode;
      state.reason = reason || null;
      state.at = clock();
      if (options.onState) options.onState(publicState());
      return state.mode;
    }

    function publicState() {
      return {
        mode: state.mode, label: STATE_LABELS[state.mode] || state.mode,
        lastEventId: state.lastEventId, applied: state.applied,
        duplicates: state.duplicates, reordered: state.reordered, gaps: state.gaps,
        malformed: state.malformed,
        resyncs: state.resyncs, streamFailures: state.streamFailures,
        polls: state.polls, pollErrors: state.pollErrors, reason: state.reason
      };
    }

    /* ------------------------------------------------------------- applying */

    function apply(event) {
      if (!event || event.id == null) return false;
      var id = Number(event.id);
      if (isNaN(id)) return false;
      state.delivered += 1;
      if (seen[id]) {
        // The same id twice (reconnect replay, or two sockets during a switch).
        state.duplicates += 1;
        return false;
      }
      if (state.lastEventId != null && id <= state.lastEventId) {
        // Out of order: an older reference is stale by definition.
        state.reordered += 1;
        return false;
      }
      seen[id] = true;
      var gap = state.lastEventId != null && id > state.lastEventId + 1;
      if (gap) state.gaps += 1;
      writeCursor(id);
      state.applied += 1;
      if (options.onEvent) options.onEvent(event);
      if (gap && options.onResync) {
        options.onResync({ reason: 'event_gap', from: state.lastEventId, to: id });
      }
      return true;
    }

    function applyBatch(events) {
      var applied = 0;
      (events || []).forEach(function (event) { if (apply(event)) applied += 1; });
      return applied;
    }

    function resync(payload) {
      state.resyncs += 1;
      // The cursor is no longer trustworthy: the next snapshot defines the truth.
      writeCursor(null);
      if (options.onResync) {
        options.onResync({ reason: (payload && payload.reason) || 'resync_required' });
      }
    }

    /* ---------------------------------------------------------------- stream */

    function parseFrame(kind, raw) {
      var payload = {};
      try {
        payload = raw ? JSON.parse(raw) : {};
      } catch (error) {
        payload = { raw: String(raw).slice(0, 200) };
      }
      return { kind: kind, payload: payload };
    }

    function handleFrame(kind, raw) {
      var frame = parseFrame(kind, raw);
      if (kind === 'resync_required') { resync(frame.payload); return; }
      if (kind === 'revocation') {
        // Authorisation changed: reconnecting on our own would just loop.
        stop('acl_changed');
        return;
      }
      var id = frame.payload && frame.payload.id != null ? frame.payload.id : null;
      if (id == null) {
        // No id means it cannot be ordered or de-duplicated. Hand it over as a
        // notice so the UI can say so, but never let it touch the cursor.
        state.malformed += 1;
        if (options.onEvent) {
          options.onEvent({ id: null, kind: frame.payload.kind || kind,
                            at: frame.payload.at || null, payload: frame.payload,
                            notice: true, malformed: true });
        }
        return;
      }
      apply({
        id: id,
        kind: frame.payload.kind || kind,
        at: frame.payload.at || null,
        payload: frame.payload,
        scope: frame.payload.scope || null
      });
    }

    function openStream() {
      if (stopped) return;
      if (!options.streamFactory) { startPolling('no_stream_support'); return; }
      setMode('connecting');
      // The server reads `last` (or the Last-Event-ID header the browser sends on
      // its own reconnect); the parameter name is part of the wire contract.
      var target = withQuery(url, { last: state.lastEventId });
      try {
        stream = options.streamFactory(target);
      } catch (error) {
        stream = null;
        streamFailed('stream_error');
        return;
      }
      if (!stream) { startPolling('no_stream_support'); return; }
      stream.onopen = function () {
        state.streamFailures = 0;
        pollDelay = pollInterval;
        setMode('live');
      };
      stream.onerror = function () { streamFailed('stream_error'); };
      KINDS.forEach(function (kind) {
        stream.addEventListener(kind, function (message) {
          handleFrame(kind, message && message.data);
        });
      });
      stream.addEventListener('resync_required', function (message) {
        handleFrame('resync_required', message && message.data);
      });
      stream.addEventListener('revocation', function () {
        handleFrame('revocation', null);
      });
    }

    function closeStream() {
      if (!stream) return;
      try {
        stream.onopen = null;
        stream.onerror = null;
        stream.close();
      } catch (error) { /* already gone */ }
      stream = null;
    }

    function streamFailed(reason) {
      state.streamFailures += 1;
      closeStream();
      if (stopped) return;
      if (state.streamFailures >= maxStreamFailures) {
        startPolling(reason);
        return;
      }
      setMode('connecting', reason);
      timerId = timer.set(function () { openStream(); }, 1000 * state.streamFailures);
    }

    /* --------------------------------------------------------------- polling */

    function startPolling(reason) {
      if (stopped) return;
      closeStream();
      setMode('polling', reason || null);
      schedulePoll(0);
    }

    function schedulePoll(delay) {
      if (stopped) return;
      if (timerId) timer.clear(timerId);
      timerId = timer.set(pollOnce, delay);
    }

    function pollOnce() {
      timerId = null;
      if (stopped) return Promise.resolve(publicState());
      if (!options.fetchImpl) return Promise.resolve(publicState());
      var target = withQuery(pollUrl, { last: state.lastEventId });
      state.polls += 1;
      return Promise.resolve(options.fetchImpl(target)).then(function (result) {
        pollDelay = pollInterval;
        if (result && result.resync_required) resync(result);
        applyBatch(result && result.events);
        if (state.mode !== 'polling') setMode('polling');
        schedulePoll(pollInterval);
        return publicState();
      }, function (error) {
        state.pollErrors += 1;
        // Back off, but stay in polling: claiming `live` here would be a lie.
        pollDelay = Math.min(maxPollInterval, Math.round(pollDelay * 1.7));
        setMode('offline', (error && error.message) || 'poll_failed');
        schedulePoll(pollDelay);
        return publicState();
      });
    }

    /* ------------------------------------------------------------------ api */

    function start() {
      if (!stopped) return publicState();
      stopped = false;
      pollDelay = pollInterval;
      openStream();
      return publicState();
    }

    function stop(reason) {
      stopped = true;
      if (timerId) { timer.clear(timerId); timerId = null; }
      closeStream();
      setMode('closed', reason || null);
      return publicState();
    }

    /** Force a poll now; used by the fallback button and by tests. */
    function poll() {
      return pollOnce();
    }

    return {
      start: start,
      stop: stop,
      poll: poll,
      state: publicState,
      apply: apply,
      resync: resync,
      kinds: KINDS.slice(),
      labels: STATE_LABELS
    };
  }

  /* --------------------------------------------------------------- followers */

  /**
   * Follow-the-tail state, deliberately separate from "is collection running".
   *
   * Scrolling up pauses following; it does not pause collection, and it does not
   * stop the events. The count keeps rising in a badge until the user asks for the
   * tail again, which is the only moment the view jumps.
   */
  function createFollower(options) {
    options = options || {};
    var threshold = options.threshold == null ? 24 : options.threshold;
    var following = options.following !== false;
    var pending = 0;
    var manual = false;

    function observe(frame) {
      frame = frame || {};
      var height = frame.scrollHeight || 0;
      var top = frame.scrollTop || 0;
      var view = frame.clientHeight || 0;
      var distance = height - top - view;
      var atBottom = distance <= threshold;
      if (frame.programmatic) return snapshot();
      if (atBottom) {
        following = true;
        manual = false;
        pending = 0;
      } else if (following && distance > threshold) {
        following = false;
        manual = true;
      }
      return snapshot();
    }

    function notify(count) {
      var added = count == null ? 1 : count;
      if (added <= 0) return { action: 'none', pending: pending, following: following };
      if (following) return { action: 'seek', count: added, pending: 0, following: true };
      pending += added;
      return { action: 'badge', count: pending, pending: pending, following: false };
    }

    function requestFollow() {
      following = true;
      manual = false;
      var count = pending;
      pending = 0;
      return { action: 'seek', count: count, pending: 0, following: true };
    }

    function snapshot() {
      return { following: following, pending: pending, manual: manual, threshold: threshold };
    }

    return {
      observe: observe,
      notify: notify,
      requestFollow: requestFollow,
      isFollowing: function () { return following; },
      pending: function () { return pending; },
      state: snapshot
    };
  }

  /**
   * Record identity ledger.
   *
   * A provisional record (a Traceback still being completed) keeps its
   * `record_id` but arrives again with a higher version. The reader must update
   * the row in place; appending would show the same record twice, and ignoring
   * would leave a truncated stack on screen forever.
   */
  function createLedger() {
    var seen = {};
    return {
      admit: function (item) {
        if (!item || item.recordId == null) return 'append';
        var key = String(item.recordId);
        var version = item.version == null ? 0 : Number(item.version);
        var previous = seen[key];
        if (previous == null) {
          seen[key] = version;
          return 'append';
        }
        if (version <= previous) return 'ignore';
        seen[key] = version;
        return 'update';
      },
      versionOf: function (recordId) {
        return seen[String(recordId)];
      },
      size: function () { return Object.keys(seen).length; },
      reset: function () { seen = {}; }
    };
  }

  /** Closing a tab unsubscribes; it must never stop server-side collection. */
  function attachPageHooks(client, win) {
    win = win || (typeof window !== 'undefined' ? window : null);
    if (!win || !win.addEventListener) return noop;
    function bye() { client.stop('page_hidden'); }
    win.addEventListener('pagehide', bye);
    win.addEventListener('beforeunload', bye);
    return function () {
      win.removeEventListener('pagehide', bye);
      win.removeEventListener('beforeunload', bye);
    };
  }

  return {
    KINDS: KINDS,
    STATE_LABELS: STATE_LABELS,
    createClient: createClient,
    createFollower: createFollower,
    createLedger: createLedger,
    attachPageHooks: attachPageHooks,
    withQuery: withQuery
  };
});
