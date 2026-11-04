/*
 * local.js — ReaderProvider over the resident local engine (R6-019).
 *
 * The view layer must not know whether rows came from IndexedDB or SQLite, so
 * this adapter exposes the same six methods as the future server provider:
 * `headers`, `detail`, `search`, `poll`, `cancel`, `resume`. Storage routing
 * (listFiles / ensureIndex / readRaw / timeline / seek) is additive.
 *
 * Two guards live here rather than in the Worker, because they are about *the
 * caller's* state, not the data:
 *
 *   dataset token   `open()` bumps it. A reply that arrives after a dataset
 *                   switch is rejected with `dataset_replaced` instead of
 *                   rendering the previous file's rows under the new name.
 *   query version   every `search()` bumps it. `poll()`/`resume()` from an older
 *                   version cannot publish a result; the UI simply sees the
 *                   newer one.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(
    require('../shared/rpc.js'), require('../shared/query.js'));
  else root.LBLocalProvider = factory(root.LBRpc, root.LBQuery);
})(typeof self !== 'undefined' ? self : this, function (rpc, query) {
  'use strict';

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  var CAPABILITIES = {
    kind: 'local',
    search: true,
    stream_bytes: true,
    resume: true,
    write: false,
    max_page_records: 2000,
    max_page_bytes: 4194304
  };

  /**
   * @param {object} options
   *   client      — rpc client (call/cancel/closeAll)
   *   datasetId   — namespace / dataset name
   *   onEvent     — optional event sink for import progress
   */
  function createProvider(options) {
    options = options || {};
    var client = options.client;
    if (!client) throw errorOf('no_client', '本机 Provider 需要 RPC 客户端');
    var datasetId = options.datasetId || 'local';
    var token = 0;
    var queryVersion = 0;
    var lastQuery = null;
    var closed = false;
    var capabilities = null;

    function bump() { token += 1; return token; }

    function guard(captured, what) {
      if (closed) return errorOf('provider_closed', '本机 Provider 已关闭');
      if (captured !== token) {
        return errorOf('dataset_replaced', '数据集已切换，' + what + '的结果已作废', {
          requested: captured, current: token
        });
      }
      return null;
    }

    function open(id) {
      datasetId = id || datasetId;
      var captured = bump();
      queryVersion += 1;
      lastQuery = null;
      return client.call('open', { dataset_id: datasetId }).then(function (value) {
        var stale = guard(captured, 'open');
        if (stale) throw stale;
        capabilities = CAPABILITIES;
        return { dataset_id: value.dataset_id, token: captured, capabilities: CAPABILITIES };
      });
    }

    function close() {
      closed = true;
      bump();
      client.closeAll('dataset closed');
      return Promise.resolve({ closed: true });
    }

    function listFiles() {
      var captured = token;
      return client.call('listFiles', {}).then(function (value) {
        var stale = guard(captured, 'listFiles');
        if (stale) throw stale;
        return value;
      });
    }

    function getMetadata(fileId) {
      var captured = token;
      return client.call('getMetadata', { file_id: fileId }).then(function (value) {
        var stale = guard(captured, 'getMetadata');
        if (stale) throw stale;
        return value;
      });
    }

    function ensureIndex(fileId, generation) {
      var captured = token;
      return client.call('ensureIndex', { file_id: fileId, generation: generation }).then(function (value) {
        var stale = guard(captured, 'ensureIndex');
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
      params = params || {};
      var oversize = checkLimit(params.limit);
      if (oversize) return Promise.reject(oversize);
      var captured = token;
      return client.call('headers', {
        file_id: params.file_id,
        generation: params.generation,
        offset: params.cursor == null ? 0 : params.cursor,
        limit: params.limit == null ? 200 : params.limit
      }).then(function (value) {
        var stale = guard(captured, 'headers');
        if (stale) throw stale;
        return value;
      });
    }

    function detail(ref) {
      if (!ref || !ref.file_id) return Promise.reject(errorOf('bad_ref', '缺少 RecordRef.file_id'));
      var captured = token;
      return client.call('detail', {
        file_id: ref.file_id, generation: ref.generation, ordinal: ref.ordinal
      }).then(function (value) {
        var stale = guard(captured, 'detail');
        if (stale) throw stale;
        if (ref.version != null && value.version != null && value.version < ref.version) {
          throw errorOf('stale_record', '该记录已更新，请重新打开', {
            requested: ref.version, current: value.version
          });
        }
        return value;
      });
    }

    function readRaw(params) {
      params = params || {};
      if (params.length > 4194304) {
        return Promise.reject(errorOf('page_too_large', '单次原文读取不得超过 4 MiB'));
      }
      var captured = token;
      return client.call('readRaw', {
        file_id: params.file_id, generation: params.generation,
        offset: params.offset, length: params.length
      }).then(function (value) {
        var stale = guard(captured, 'readRaw');
        if (stale) throw stale;
        return value;
      });
    }

    function getRecords(params) {
      params = params || {};
      var captured = token;
      return client.call('getRecords', {
        file_id: params.file_id, generation: params.generation, ordinals: params.ordinals || []
      }).then(function (value) {
        var stale = guard(captured, 'getRecords');
        if (stale) throw stale;
        return value;
      });
    }

    function getTimeline(params) {
      params = params || {};
      var captured = token;
      return client.call('getTimeline', {
        file_id: params.file_id, generation: params.generation,
        page: params.page || 0, page_size: params.page_size || 50
      }).then(function (value) {
        var stale = guard(captured, 'getTimeline');
        if (stale) throw stale;
        return value;
      });
    }

    function seek(params) {
      params = params || {};
      var captured = token;
      return client.call('seek', {
        file_id: params.file_id, generation: params.generation,
        ordinal: params.ordinal, ts_ns: params.ts_ns
      }).then(function (value) {
        var stale = guard(captured, 'seek');
        if (stale) throw stale;
        return value;
      });
    }

    /**
     * `search` returns the first snapshot synchronously (a promise here, because
     * the RPC is asynchronous). Results carry the version that produced them.
     *
     * A caller that does not know the generation (a cross-file scope, for example)
     * gets the published one resolved here: the Worker keys its block index by
     * `file_id|generation`, and passing `null` reaches IndexedDB as an invalid key
     * range instead of a useful error.
     */
    function search(spec) {
      var normalized = query.normalizeQuery(spec || {});
      if (!normalized.ok) return Promise.reject(errorOf(normalized.error.code, normalized.error.message));
      if ((spec && spec.generation == null) && (spec && spec.file_id)) {
        var preflight = token;
        return getMetadata(spec.file_id).then(function (meta) {
          if (preflight !== token) throw errorOf('dataset_replaced', '数据集已切换，search 的结果已作废');
          var next = {};
          for (var key in spec) next[key] = spec[key];
          next.generation = meta && meta.generation != null ? meta.generation : next.generation;
          return searchWithVersion(next, normalized);
        });
      }
      return searchWithVersion(spec, normalized);
    }

    function searchWithVersion(spec, normalized) {
      queryVersion += 1;
      var version = queryVersion;
      var captured = token;
      var request = {
        file_id: (spec && spec.file_id) || null,
        generation: (spec && spec.generation) || null,
        limit: (spec && spec.limit) || 200,
        spec: {
          text: spec.text, terms: spec.terms, joiner: spec.joiner, flags: spec.flags,
          levels: spec.levels, time_range: spec.time_range,
          projection_version: spec.projection_version, match_all: spec.match_all === true
        },
        query_version: version,
        signature: normalized.signature
      };
      return client.call('search', request).then(function (value) {
        var stale = guard(captured, 'search');
        if (stale) throw stale;
        if (version !== queryVersion) {
          throw errorOf('stale_query', '已有更新的查询条件', { requested: version, current: queryVersion });
        }
        lastQuery = value;
        return augment(value, version);
      });
    }

    function augment(state, version) {
      var copy = {};
      for (var key in state) copy[key] = state[key];
      copy.query_version = version;
      copy.provider = 'local';
      return copy;
    }

    function poll(queryId) {
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQuery.query_version;
      var captured = token;
      return client.call('poll', { query_id: queryId }).then(function (value) {
        var stale = guard(captured, 'poll');
        if (stale) throw stale;
        if (version !== queryVersion) {
          throw errorOf('stale_query', '已有更新的查询条件', { requested: version, current: queryVersion });
        }
        lastQuery = value;
        return augment(value, version);
      });
    }

    function cancel(queryId) {
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQuery.query_version;
      return client.call('cancelSearch', { query_id: queryId }).then(function (value) {
        lastQuery = value;
        return augment(value, version);
      });
    }

    function resume(queryId, resumeToken) {
      if (!lastQuery || lastQuery.query_id !== queryId) {
        return Promise.reject(errorOf('unknown_query', '查询已失效：' + queryId));
      }
      var version = lastQuery.query_version;
      return client.call('resume', { query_id: queryId, resume_token: resumeToken })
        .then(function (value) {
          lastQuery = value;
          return augment(value, version);
        });
    }

    return {
      open: open,
      close: close,
      listFiles: listFiles,
      getMetadata: getMetadata,
      ensureIndex: ensureIndex,
      headers: headers,
      detail: detail,
      readRaw: readRaw,
      getRecords: getRecords,
      getTimeline: getTimeline,
      seek: seek,
      search: search,
      poll: poll,
      cancel: cancel,
      resume: resume,
      capabilities: function () { return capabilities || CAPABILITIES; },
      dataset: function () { return { dataset_id: datasetId, token: token }; },
      queryVersion: function () { return queryVersion; }
    };
  }

  return {
    CAPABILITIES: CAPABILITIES,
    createProvider: createProvider
  };
});
