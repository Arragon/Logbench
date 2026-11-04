/*
 * http.js — one JSON client for the R6 page.
 *
 * The page had no HTTP layer of its own: `providers/server.js` speaks only to the
 * reader API, and the v1 workbench has its own adapter. The library and storage
 * views need the same three things everywhere — a CSRF header on writes, one
 * error shape, and a cancellation token that stops an old workspace's reply from
 * overwriting a new one.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBHttp = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function httpError(code, message, details, status) {
    var error = new Error(message);
    error.code = code;
    error.details = details || {};
    error.status = status || 0;
    return error;
  }

  function createClient(options) {
    options = options || {};
    var fetchImpl = options.fetchImpl || (typeof fetch === 'function' ? fetch.bind(null) : null);
    if (!fetchImpl) throw httpError('no_fetch', '当前环境不支持 fetch');
    var base = (options.base || '').replace(/\/$/, '');
    var csrfToken = options.csrfToken || '';
    var epoch = 0;
    var closed = false;

    function setCsrf(token) { csrfToken = token || ''; }

    function send(method, path, body, opts) {
      opts = opts || {};
      if (closed) return Promise.reject(httpError('client_closed', '页面已卸载'));
      var startEpoch = epoch;
      var init = { method: method, credentials: 'same-origin', headers: {} };
      if (body !== undefined && body !== null) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      if (method !== 'GET' && method !== 'HEAD') init.headers['X-LogBench-CSRF'] = csrfToken;
      if (opts.headers) for (var key in opts.headers) init.headers[key] = opts.headers[key];
      return fetchImpl(base + path, init).then(function (response) {
        return response.text().then(function (text) {
          var value = null;
          if (text) {
            try { value = JSON.parse(text); } catch (error) { value = null; }
          }
          if (startEpoch !== epoch && !opts.allowStale) {
            // A workspace switch happened while this request was in flight. The
            // answer belongs to the previous scope and must not be rendered.
            throw httpError('stale_workspace', '工作区已切换，旧结果已丢弃');
          }
          if (!response.ok) {
            var detail = (value && value.error) || {};
            throw httpError(detail.code || ('http_' + response.status),
              detail.message || ('请求失败：' + response.status),
              detail.details, response.status);
          }
          return value == null ? {} : value;
        });
      });
    }

    return {
      get: function (path, opts) { return send('GET', path, null, opts); },
      post: function (path, body, opts) { return send('POST', path, body === undefined ? {} : body, opts); },
      put: function (path, body, opts) { return send('PUT', path, body === undefined ? {} : body, opts); },
      patch: function (path, body, opts) { return send('PATCH', path, body === undefined ? {} : body, opts); },
      del: function (path, body, opts) { return send('DELETE', path, body === undefined ? null : body, opts); },
      setCsrf: setCsrf,
      /** Bump the epoch: every in-flight reply from the previous scope is dropped. */
      reset: function () { epoch += 1; },
      close: function () { closed = true; epoch += 1; },
      epoch: function () { return epoch; }
    };
  }

  /** `?a=1&b=2` with encoding and without empty keys. */
  function query(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === undefined || value === null || value === '') return;
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  return { createClient: createClient, query: query, httpError: httpError };
});
