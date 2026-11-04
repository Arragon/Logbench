/*
 * server.js — ReaderProvider over the v2 HTTP API (R6-045).
 *
 * This replaces "download the whole file and parse it in the page" with reads
 * against the server index: headers, detail, raw ranges, timeline, seek and the
 * asynchronous query API. It exposes exactly the same surface as `local.js`, so
 * the view layer keeps treating `kind` as a transport detail.
 *
 * Three things this adapter refuses to do:
 *
 *   - invent data. Every method either has a server answer or rejects with the
 *     server's error code; nothing is approximated locally.
 *   - serve a reply for a dataset it no longer belongs to. `open()` bumps a token
 *     and every reply is checked against it (`dataset_replaced`).
 *   - publish an older query. `search()` bumps a query version and `poll`/`resume`
 *     results from an older version are rejected (`stale_query`).
 *
 * `fetchImpl` is injectable so the adapter can be tested against a stub without a
 * running server; the real HTTP path is covered by the M4 gate.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(
    require('../shared/query.js'));
  else root.LBServerProvider = factory(root.LBQuery);
})(typeof self !== 'undefined' ? self : this, function (query) {
  'use strict';

  var CAPABILITIES = {
    kind: 'server',
    search: true,
    stream_bytes: true,
    resume: true,
    write: true,
    max_page_records: 2000,
    max_page_bytes: 4194304
  };

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function createProvider(options) {
    options = options || {};
    var fetchImpl = options.fetchImpl || (typeof fetch === 'function' ? fetch.bind(null) : null);
    if (!fetchImpl) throw errorOf('no_fetch', '服务端 Provider 需要 fetch');
    var base = (options.base || '').replace(/\/$/, '');
    var csrf = options.csrfToken || '';
    var datasetId = options.datasetId || '';
    var token = 0;
    var queryVersion = 0;
    var lastQuery = null;
    /* The server state object does not carry the client's query version, so it is
     * tracked beside it. Reading `lastQuery.query_version` was always undefined and
     * made `poll`/`cancel`/`resume` answer `stale_query` on their own query. */
    var lastQueryVersion = null;
    var capabilities = null;
    var closed = false;
    var stats = { requests: 0, bytes: 0, errors: 0 };

    function bump() { token += 1; return token; }

    /** `f_xxx:12` must keep its colon: an encoded `%3A` is not a record id. */
    function encodeRecordId(ref) {
      if (ref.record_id) {
        var parts = String(ref.record_id).split(':');
        if (parts.length === 2) {
          return encodeURIComponent(parts[0]) + ':' + encodeURIComponent(parts[1]);
        }
        return encodeURIComponent(ref.record_id);
      }
      return encodeURIComponent(ref.file_id) + ':' + encodeURIComponent(ref.ordinal);
    }

    /** Closed is a client state, not a server answer: fail before the request. */
    function closedError() {
      return closed ? errorOf('provider_closed', '服务端 Provider 已关闭') : null;
    }

    function guard(captured, what) {
      if (closed) return errorOf('provider_closed', '服务端 Provider 已关闭');
      if (captured !== token) {
        return errorOf('dataset_replaced', '数据集已切换，' + what + '的结果已作废', {
          requested: captured, current: token
        });
      }
      return null;
    }

    function call(method, path, body, extra) {
      var init = { method: method, credentials: 'same-origin', headers: {} };
      if (body !== undefined && body !== null) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      if (method !== 'GET' && method !== 'HEAD') init.headers['X-LogBench-CSRF'] = csrf;
      if (extra) for (var key in extra) init.headers[key] = extra[key];
      stats.requests += 1;
      return fetchImpl(base + path, init).then(function (response) {
        return response.text().then(function (text) {
          var value = null;
          if (text) {
            try { value = JSON.parse(text); } catch (error) { value = null; }
          }
          if (!response.ok) {
            stats.errors += 1;
            var detail = (value && value.error) || {};
            throw errorOf(detail.code || ('http_' + response.status),
              detail.message || ('服务端返回 ' + response.status), detail.details);
          }
          stats.bytes += text ? text.length : 0;
          return value == null ? {} : value;
        });
      });
    }

    function open(id) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      datasetId = id || datasetId;
      var captured = bump();
      queryVersion += 1;
      lastQuery = null;
      lastQueryVersion = null;
      return call('GET', '/api/v2/files/' + encodeURIComponent(datasetId)).then(function (value) {
        var stale = guard(captured, 'open');
        if (stale) throw stale;
        capabilities = CAPABILITIES;
        return {
          dataset_id: value.file_id || datasetId, token: captured,
          capabilities: CAPABILITIES,
          index: { coverage: value.coverage, layers: value.layers, generation: value.generation }
        };
      });
    }

    function close() {
      closed = true;
      bump();
      return Promise.resolve({ closed: true });
    }

    function listFiles(params) {
      params = params || {};
      var captured = token;
      var target = '/api/v1/files?path=' + encodeURIComponent(params.path || '')
        + '&q=' + encodeURIComponent(params.q || '')
        + '&cursor=' + (params.cursor == null ? 0 : params.cursor)
        + '&limit=' + (params.limit == null ? 100 : params.limit);
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'listFiles');
        if (stale) throw stale;
        return value;
      });
    }

    function checkLimit(limit) {
      if (limit != null && limit > 2000) {
        return errorOf('page_too_large', '单页最多 2000 条', { requested: limit });
      }
      return null;
    }

    function headers(params) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      params = params || {};
      var oversize = checkLimit(params.limit);
      if (oversize) return Promise.reject(oversize);
      var captured = token;
      var target = '/api/v2/files/' + encodeURIComponent(params.file_id || datasetId) + '/records'
        + '?limit=' + (params.limit == null ? 200 : params.limit);
      if (params.cursor != null) target += '&cursor=' + encodeURIComponent(params.cursor);
      if (params.ordinals && params.ordinals.length) {
        target += '&ordinals=' + params.ordinals.join(',');
      }
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'headers');
        if (stale) throw stale;
        return value;
      });
    }

    function detail(ref) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      if (!ref || !ref.file_id) return Promise.reject(errorOf('bad_ref', '缺少 RecordRef.file_id'));
      var captured = token;
      var recordId = encodeRecordId(ref);
      var target = '/api/v2/records/' + recordId;
      if (ref.version) target += '?version=' + encodeURIComponent(ref.version);
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'detail');
        if (stale) throw stale;
        if (ref.version != null && value.version && value.version.rules
            && value.version.rules !== ref.version) {
          throw errorOf('stale_record', '该记录属于另一个版本，请重新打开',
            { requested: ref.version, current: value.version.rules });
        }
        return value;
      });
    }

    function readRaw(params) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      params = params || {};
      if (params.length > 4194304) {
        return Promise.reject(errorOf('page_too_large', '单次原文读取不得超过 4 MiB'));
      }
      var captured = token;
      var recordId = encodeRecordId(params);
      var start = params.offset || 0;
      var target = '/api/v2/records/' + recordId
        + '/raw?start=' + start + '&end=' + (start + (params.length || 0));
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'readRaw');
        if (stale) throw stale;
        return value;
      });
    }

    function getRecords(params) {
      params = params || {};
      var captured = token;
      return headers({
        file_id: params.file_id, ordinals: params.ordinals || [], limit: 2000
      }).then(function (value) {
        var stale = guard(captured, 'getRecords');
        if (stale) throw stale;
        return value;
      });
    }

    function getTimeline(params) {
      params = params || {};
      var captured = token;
      var target = '/api/v2/files/' + encodeURIComponent(params.file_id || datasetId)
        + '/timeline?page=' + (params.page || 0) + '&pageSize=' + (params.page_size || 50);
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'getTimeline');
        if (stale) throw stale;
        return value;
      });
    }

    function seek(params) {
      params = params || {};
      var captured = token;
      var target = '/api/v2/files/' + encodeURIComponent(params.file_id || datasetId) + '/seek?';
      if (params.line != null) target += 'line=' + params.line;
      else if (params.byte != null) target += 'byte=' + params.byte;
      else return Promise.reject(errorOf('bad_request', 'seek 需要 line 或 byte'));
      return call('GET', target).then(function (value) {
        var stale = guard(captured, 'seek');
        if (stale) throw stale;
        return value;
      });
    }

    function ensureIndex(fileId, generation) {
      var captured = token;
      // Indexing is requested by file id: the server already holds the authorized
      // path, so the client never sends one.
      return call('POST', '/api/v2/files/' + encodeURIComponent(fileId || datasetId) + '/index')
        .then(function (value) {
          var stale = guard(captured, 'ensureIndex');
          if (stale) throw stale;
          return value;
        });
    }

    /** Returns the first snapshot; the server has already queued the scan. */
    function search(spec) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      spec = spec || {};
      var normalized = query.normalizeQuery(spec);
      if (!normalized.ok) return Promise.reject(errorOf(normalized.error.code, normalized.error.message));
      queryVersion += 1;
      var version = queryVersion;
      var captured = token;
      var fileId = spec.file_id || datasetId;
      var body = {
        file_id: fileId,
        generation: spec.generation,
        text: spec.text,
        terms: spec.terms,
        joiner: spec.joiner,
        flags: spec.flags || {},
        levels: spec.levels,
        time_range: spec.time_range,
        projection_version: spec.projection_version,
        match_all: spec.match_all === true,
        limit: spec.limit,
        budget_ms: spec.budget_ms
      };
      return call('POST', '/api/v2/queries', body).then(function (value) {
        var stale = guard(captured, 'search');
        if (stale) throw stale;
        if (version !== queryVersion) {
          throw errorOf('stale_query', '已有更新的查询条件', { requested: version, current: queryVersion });
        }
        lastQuery = value.item;
        lastQueryVersion = version;
        return augment(value.item, version);
      });
    }

    function augment(state, version) {
      var copy = {};
      for (var key in state) copy[key] = state[key];
      copy.query_version = version;
      copy.provider = 'server';
      return copy;
    }

    function poll(queryId) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQueryVersion == null ? queryVersion : lastQueryVersion;
      var captured = token;
      return call('GET', '/api/v2/queries/' + encodeURIComponent(queryId)).then(function (value) {
        var stale = guard(captured, 'poll');
        if (stale) throw stale;
        // A newer search replaced this one: its answer must not be published.
        if (version !== queryVersion) {
          throw errorOf('stale_query', '已有更新的查询条件', { requested: version, current: queryVersion });
        }
        lastQuery = value.item;
        lastQueryVersion = version;
        return augment(value.item, version);
      });
    }

    function cancel(queryId) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQueryVersion == null ? queryVersion : lastQueryVersion;
      return call('DELETE', '/api/v2/queries/' + encodeURIComponent(queryId)).then(function (value) {
        lastQuery = value.item;
        lastQueryVersion = version;
        return augment(value.item, version);
      });
    }

    function resume(queryId, resumeToken) {
      var closedNow = closedError();
      if (closedNow) return Promise.reject(closedNow);
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQueryVersion == null ? queryVersion : lastQueryVersion;
      return call('POST', '/api/v2/queries/' + encodeURIComponent(queryId) + '/continue',
        { resume_token: resumeToken }).then(function (value) {
          lastQuery = value.item;
          lastQueryVersion = version;
          return augment(value.item, version);
        });
    }

    function queryPage(queryId, cursor) {
      var target = '/api/v2/queries/' + encodeURIComponent(queryId) + '/records';
      if (cursor != null) target += '?cursor=' + encodeURIComponent(cursor);
      return call('GET', target);
    }

    function exportQuery(queryId, format) {
      return call('GET', '/api/v2/queries/' + encodeURIComponent(queryId)
        + '/export?format=' + (format || 'ndjson'));
    }

    return {
      open: open,
      close: close,
      listFiles: listFiles,
      headers: headers,
      detail: detail,
      readRaw: readRaw,
      getRecords: getRecords,
      getTimeline: getTimeline,
      seek: seek,
      ensureIndex: ensureIndex,
      search: search,
      poll: poll,
      cancel: cancel,
      resume: resume,
      queryPage: queryPage,
      exportQuery: exportQuery,
      capabilities: function () { return capabilities || CAPABILITIES; },
      dataset: function () { return { dataset_id: datasetId, token: token }; },
      queryVersion: function () { return queryVersion; },
      stats: function () { return { requests: stats.requests, bytes: stats.bytes, errors: stats.errors }; }
    };
  }

  return { CAPABILITIES: CAPABILITIES, createProvider: createProvider };
});
