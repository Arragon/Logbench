/*
 * rpc.js — request/response protocol between the page and a resident Worker (R6-019).
 *
 * The failure this exists to prevent is not "a message got lost". It is the one
 * R5 already had: a reply from an abandoned search arriving late and overwriting
 * the current one. So the client never resolves a promise twice, never resolves
 * a promise whose id it has abandoned, and reports an orphan reply as a counter
 * that tests assert on. A late reply is a bug you can see, not a race you lose.
 *
 * Envelope (`WorkerEnvelope` in contracts.md):
 *   request   { v, type:'call',   id, method, params }
 *   reply     { v, type:'reply',  id, ok:true,  value }
 *             { v, type:'reply',  id, ok:false, error:{ code, message, details } }
 *   cancel    { v, type:'cancel', id }
 *   event     { v, type:'event',  topic, payload }        (server -> client, unsolicited)
 *
 * Transfer: raw byte ranges come back as `Uint8Array`. They are transferred, not
 * copied, when the caller passes `transfer` — but copied bytes in a reply are
 * still bounded by `MAX_PAGE_BYTES`, because "it was on the heap anyway" is how
 * a 1 GiB log turns into a 1 GiB message.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBRpc = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var PROTOCOL = 'logbench.rpc/1';
  var MAX_PAGE_RECORDS = 2000;
  var MAX_PAGE_BYTES = 4194304;

  var CLIENT_METHODS = [
    'open', 'close', 'listFiles', 'getMetadata', 'ensureIndex',
    'headers', 'getPage', 'getRecord', 'detail', 'readRaw',
    'getTimeline', 'seek', 'getRecords', 'search', 'poll', 'cancelSearch', 'resume',
    'putFile', 'appendChunk', 'finalizeFile', 'stageIndex', 'stageBlock', 'publishIndex'
  ];

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function isPlainObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  /** Structural validation; returns a list of problems instead of throwing. */
  function validateRequest(message) {
    var problems = [];
    if (!isPlainObject(message)) return ['not an object'];
    if (message.v !== PROTOCOL) problems.push('protocol: ' + message.v);
    if (message.type !== 'call' && message.type !== 'cancel') problems.push('type: ' + message.type);
    if (typeof message.id !== 'string' || !message.id) problems.push('id');
    if (message.type === 'call') {
      if (CLIENT_METHODS.indexOf(message.method) === -1) problems.push('method: ' + message.method);
      if (message.params !== undefined && !isPlainObject(message.params)) problems.push('params');
    }
    return problems;
  }

  function validateReply(message) {
    var problems = [];
    if (!isPlainObject(message)) return ['not an object'];
    if (message.v !== PROTOCOL) problems.push('protocol');
    if (message.type !== 'reply') problems.push('type');
    if (typeof message.id !== 'string' || !message.id) problems.push('id');
    if (message.ok === true) {
      if (!('value' in message)) problems.push('value');
    } else if (message.ok === false) {
      if (!isPlainObject(message.error) || typeof message.error.code !== 'string') problems.push('error');
    } else {
      problems.push('ok');
    }
    return problems;
  }

  /**
   * Client with bounded pending calls.
   * `post(message, transfer)` is the transport; `now()` is injectable so timeout
   * behaviour is testable without sleeping.
   */
  function createRpcClient(options) {
    options = options || {};
    var post = options.post;
    if (typeof post !== 'function') throw errorOf('no_transport', '缺少消息通道');
    var now = options.now || function () { return Date.now(); };
    var defaultTimeout = options.timeoutMs || 30000;
    var stats = { calls: 0, replies: 0, timeouts: 0, cancelled: 0, orphan_replies: 0, errors: 0 };
    var listeners = [];
    var pendingById = Object.create(null);
    var abandonedById = Object.create(null);
    var counter = 0;

    function call(method, params, callOptions) {
      callOptions = callOptions || {};
      var id = 'r' + (++counter).toString(36) + '-' + Math.random().toString(36).slice(2, 8);
      var timeoutMs = callOptions.timeoutMs == null ? defaultTimeout : callOptions.timeoutMs;
      stats.calls += 1;
      var promise = new Promise(function (resolve, reject) {
        var timer = null;
        var settled = false;
        function settle(fn, value) {
          if (settled) return false;
          settled = true;
          if (timer) clearTimeout(timer);
          fn(value);
          return true;
        }
        pendingById[id] = {
          method: method,
          resolve: function (value) { settle(resolve, value); },
          reject: function (error) { settle(reject, error); }
        };
        if (timeoutMs > 0) {
          timer = setTimeout(function () {
            abandonedById[id] = true;
            delete pendingById[id];
            stats.timeouts += 1;
            settle(reject, errorOf('rpc_timeout',
              '本机引擎在 ' + timeoutMs + 'ms 内未响应：' + method, { method: method }));
          }, timeoutMs);
        }
        try {
          post({ v: PROTOCOL, type: 'call', id: id, method: method, params: params || {} },
            callOptions.transfer || []);
        } catch (error) {
          delete pendingById[id];
          stats.errors += 1;
          settle(reject, errorOf('transport_failed', '消息发送失败：' + (error && error.message)));
        }
      });
      promise.rpcId = id;
      return promise;
    }

    function handleMessage(message) {
      if (!isPlainObject(message)) return;
      if (message.type === 'event') {
        listeners.forEach(function (fn) {
          try { fn(message.topic, message.payload); } catch (ignored) { /* listener bugs must not kill the pump */ }
        });
        return;
      }
      if (message.type !== 'reply') return;
      var entry = pendingById[message.id];
      if (!entry) {
        // Late, cancelled or duplicated reply. Never resolved, only counted: this
        // is exactly the reply that used to overwrite a newer result.
        stats.orphan_replies += 1;
        return;
      }
      delete pendingById[message.id];
      stats.replies += 1;
      if (message.ok === true) {
        entry.resolve(message.value);
      } else {
        stats.errors += 1;
        entry.reject(errorOf(message.error.code, message.error.message || '本机引擎返回错误',
          message.error.details));
      }
    }

    function cancel(id) {
      var entry = pendingById[id];
      if (!entry) return false;
      delete pendingById[id];
      abandonedById[id] = true;
      stats.cancelled += 1;
      try { post({ v: PROTOCOL, type: 'cancel', id: id }, []); } catch (ignored) { /* caller still gets a rejection */ }
      entry.reject(errorOf('rpc_cancelled', '请求已取消：' + entry.method));
      return true;
    }

    function closeAll(reason) {
      Object.keys(pendingById).forEach(function (id) {
        var entry = pendingById[id];
        delete pendingById[id];
        entry.reject(errorOf('rpc_closed', reason || '本机引擎已关闭'));
      });
    }

    return {
      call: call,
      cancel: cancel,
      handleMessage: handleMessage,
      closeAll: closeAll,
      onEvent: function (fn) { listeners.push(fn); },
      pendingCount: function () { return Object.keys(pendingById).length; },
      wasAbandoned: function (id) { return abandonedById[id] === true; },
      stats: function () {
        var copy = {};
        for (var key in stats) copy[key] = stats[key];
        copy.pending = Object.keys(pendingById).length;
        return copy;
      }
    };
  }

  /**
   * Server side. Duplicate ids return the cached reply instead of running the
   * handler twice (C9 idempotence for retried actions).
   */
  function createRpcServer(options) {
    options = options || {};
    var handlers = options.handlers || {};
    var post = options.post;
    if (typeof post !== 'function') throw errorOf('no_transport', '缺少消息通道');
    var cache = Object.create(null);
    var inflight = Object.create(null);
    var order = [];
    var cacheLimit = options.cacheLimit || 256;
    var stats = { calls: 0, duplicate_ids: 0, invalid: 0, failures: 0, unknown_methods: 0 };

    function reply(id, payload) {
      var message = { v: PROTOCOL, type: 'reply', id: id };
      if (payload.ok) { message.ok = true; message.value = payload.value; }
      else { message.ok = false; message.error = payload.error; }
      delete inflight[id];
      var keep = cache[id];
      if (!keep || order.length < cacheLimit) {
        cache[id] = message;
        order.push(id);
        while (order.length > cacheLimit) delete cache[order.shift()];
      }
      post(message, payload.transfer || []);
    }

    function handleMessage(message) {
      if (message && message.type === 'cancel') {
        if (options.onCancel) { try { options.onCancel(message.id); } catch (ignored) { /* noop */ } }
        return;
      }
      var problems = validateRequest(message);
      if (problems.length) {
        stats.invalid += 1;
        reply((message && message.id) || 'unknown', {
          ok: false,
          error: { code: 'invalid_request', message: '请求结构不合法', details: { problems: problems } }
        });
        return;
      }
      if (cache[message.id] || inflight[message.id]) {
        // Same id while the first call is still running or already answered:
        // the handler must not run twice (C9). A finished call replays its
        // cached reply; an in-flight one will answer when it completes.
        stats.duplicate_ids += 1;
        if (cache[message.id]) post(cache[message.id], []);
        return;
      }
      inflight[message.id] = true;
      var handler = handlers[message.method];
      if (typeof handler !== 'function') {
        stats.unknown_methods += 1;
        reply(message.id, { ok: false, error: { code: 'unknown_method', message: '未实现的方法：' + message.method } });
        return;
      }
      stats.calls += 1;
      var output;
      try {
        output = handler(message.params || {});
      } catch (error) {
        stats.failures += 1;
        reply(message.id, { ok: false, error: { code: (error && error.code) || 'handler_error', message: String(error && error.message || error) } });
        return;
      }
      Promise.resolve(output).then(function (value) {
        var transfer = [];
        var plain = value;
        if (value && value.__transfer) {
          transfer = value.transfer || [];
          plain = value.value;
        }
        reply(message.id, { ok: true, value: plain, transfer: transfer });
      }, function (error) {
        stats.failures += 1;
        reply(message.id, {
          ok: false,
          error: {
            code: (error && error.code) || 'handler_error',
            message: String(error && error.message || error),
            details: (error && error.details) || undefined
          }
        });
      });
    }

    return {
      handleMessage: handleMessage,
      stats: function () { var copy = {}; for (var key in stats) copy[key] = stats[key]; return copy; },
      clearCache: function () { cache = Object.create(null); order = []; }
    };
  }

  return {
    PROTOCOL: PROTOCOL,
    MAX_PAGE_RECORDS: MAX_PAGE_RECORDS,
    MAX_PAGE_BYTES: MAX_PAGE_BYTES,
    CLIENT_METHODS: CLIENT_METHODS,
    errorOf: errorOf,
    validateRequest: validateRequest,
    validateReply: validateReply,
    createRpcClient: createRpcClient,
    createRpcServer: createRpcServer
  };
});
