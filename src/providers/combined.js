/*
 * combined.js — one query surface over several files and two transports (R6-078).
 *
 * The rule this module exists to protect: **a local file never becomes a server
 * query.** A mixed scope is split into a local part and an authorized server part;
 * each part runs its own query with its own snapshot, and only the ordered result
 * (timestamps, levels, ordinals) is merged here. No local path, name or text is sent
 * to the server to make the merge possible.
 *
 * Merge policy, stated so it cannot drift:
 *
 *   - order is by timestamp, then source rank (local before server), then file
 *     index, then ordinal. The tie-breakers are stable, so two identical reads
 *     produce identical pages and a cursor means the same thing twice.
 *   - a record with no timestamp is not placed by guesswork. In `time` mode it is
 *     excluded and reported in `excluded`; in `search` mode it goes after every
 *     timed record, in file order, and the result says `order_final: false`.
 *   - `count.relation` is `exact` only when every child finished and said so.
 *     One running child makes it `at_least`; one failed child makes it `at_least`
 *     with that file named in `coverage.failed`. It never says `exact: 0` for a
 *     scope that was not fully scanned.
 *   - pages are bounded: a child contributes at most `limit` ordinals per query,
 *     and the merger only holds one row per child at a time plus the current page.
 *
 * UMD-lite: window.LBCombinedProvider in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(
    require('../shared/time.js'));
  else root.LBCombinedProvider = factory(root.LBTime);
})(typeof self !== 'undefined' ? self : this, function (time) {
  'use strict';

  var RANK = { local: 0, server: 1 };
  var DEFAULT_LIMIT = 200;

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /* ---------------------------------------------------------------- ordering */

  /**
   * Deterministic comparator. `ts_key == null` sorts last, never first: a record
   * with no timestamp is not "older than everything", it is unplaceable.
   */
  function compareRows(a, b) {
    var left = a.ts_key == null ? null : a.ts_key;
    var right = b.ts_key == null ? null : b.ts_key;
    if (left !== right) {
      if (left == null) return 1;
      if (right == null) return -1;
      return left < right ? -1 : 1;
    }
    if (a.rank !== b.rank) return a.rank - b.rank;
    if (a.file_index !== b.file_index) return a.file_index - b.file_index;
    return a.ordinal - b.ordinal;
  }

  /**
   * Merge already-ordered row arrays. Exported because the axis behaviour is worth
   * testing without a provider, a Worker or a server.
   *
   * @param {Array<Array<object>>} streams rows per stream, each ordered
   * @param {number} limit      maximum rows to return
   * @param {object} options    { include_untimed: boolean }
   */
  function mergeOrdered(streams, limit, options) {
    options = options || {};
    var includeUntimed = options.include_untimed !== false;
    // Partition before merging. An undated row may sit anywhere in a hand-built
    // stream; leaving it in place would block every later row of that stream.
    var timed = streams.map(function (stream) {
      return stream.filter(function (row) { return row.ts_key != null; })
        .sort(compareRows);
    });
    var undated = [];
    streams.forEach(function (stream) {
      stream.forEach(function (row) { if (row.ts_key == null) undated.push(row); });
    });
    undated.sort(compareRows);
    var cursors = timed.map(function () { return 0; });
    var items = [];
    while (items.length < limit) {
      var best = -1;
      for (var index = 0; index < timed.length; index++) {
        var position = cursors[index];
        if (position >= timed[index].length) continue;
        if (best === -1 || compareRows(timed[index][position], timed[best][cursors[best]]) < 0) {
          best = index;
        }
      }
      if (best === -1) break;
      items.push(timed[best][cursors[best]]);
      cursors[best] += 1;
    }
    if (includeUntimed) {
      items = items.concat(undated.slice(0, Math.max(0, limit - items.length)));
    }
    return {
      items: items,
      cursors: cursors,
      untimed: undated.length,
      /** True when every stream is exhausted: the page is the whole result. */
      complete: cursors.every(function (value, index) { return value >= timed[index].length; })
    };
  }

  /* ------------------------------------------------------------------- scope */

  /**
   * Split an explicit scope into transports. Anything not named stays unnamed:
   * "all files" is resolved by the caller from the two listings, not guessed here.
   */
  function splitScope(scope) {
    scope = scope || {};
    var local = (scope.local || []).map(String).filter(Boolean);
    var server = (scope.server || []).map(String).filter(Boolean);
    var overlap = local.filter(function (id) { return server.indexOf(id) !== -1; });
    if (overlap.length) {
      throw errorOf('ambiguous_scope', '同一个文件不能同时属于本机与服务端范围', { ids: overlap });
    }
    return { local: local, server: server, total: local.length + server.length, mixed: local.length > 0 && server.length > 0 };
  }

  /* ----------------------------------------------------------------- provider */

  /**
   * @param {object} options
   *   local          LBLocalProvider instance (used when a scope has one local file)
   *   server         LBServerProvider instance (same rule)
   *   localFactory   fileId -> provider, for independent per-file queries
   *   serverFactory  fileId -> provider, for independent per-file queries
   *   scope          {local: [fileId], server: [fileId]}
   *   onEvent        optional (name, payload) sink for progress
   */
  function createCombinedProvider(options) {
    options = options || {};
    var scope = splitScope(options.scope);
    var files = [];
    var children = [];
    var version = 0;
    var closed = false;
    var stats = { local_queries: 0, server_queries: 0, rows: 0, bytes: 0 };
    var lastPage = null;
    var lastMode = 'time';

    function now() { return Date.now(); }

    function childFor(source, fileId, index) {
      var factory = source === 'local' ? options.localFactory : options.serverFactory;
      if (factory) return factory(fileId);
      var single = source === 'local' ? options.local : options.server;
      if (!single) {
        throw errorOf('missing_provider', '范围包含' + (source === 'local' ? '本机' : '服务端') +
          '文件，但该来源的 Provider 未提供', { file_id: fileId });
      }
      // A single provider instance can only track one query at a time (both
      // adapters guard `lastQuery`), so it is only valid for a one-file scope.
      if (scope[source].length > 1) {
        throw errorOf('per_file_provider_required',
          '多文件查询需要按文件创建 Provider 实例，否则旧查询会被新查询替换',
          { source: source, file_id: fileId });
      }
      return single;
    }

    function plan() {
      var list = [];
      scope.local.forEach(function (fileId, index) {
        list.push({ source: 'local', file_id: fileId, rank: RANK.local, file_index: index });
      });
      scope.server.forEach(function (fileId, index) {
        list.push({ source: 'server', file_id: fileId, rank: RANK.server,
          file_index: scope.local.length + index });
      });
      return list;
    }

    function assertOpen() {
      if (closed) throw errorOf('provider_closed', '混合范围 Provider 已关闭');
    }

    /**
     * Start one query per file. The same spec is handed to every child; the server
     * only ever sees the server files' spec, because a local child never leaves the
     * page.
     */
    function search(spec) {
      assertOpen();
      spec = spec || {};
      var mode = spec.mode === 'search' ? 'search' : 'time';
      lastMode = mode;
      version += 1;
      var captured = version;
      var planned = plan();
      if (!planned.length) {
        lastMode = mode;
        return Promise.resolve(emptyState(mode));
      }
      var limit = Math.min(DEFAULT_LIMIT * 10, Math.max(1, spec.limit || DEFAULT_LIMIT));
      var setupError = null;
      children = planned.map(function (entry) {
        var provider;
        try {
          provider = childFor(entry.source, entry.file_id, entry.file_index);
        } catch (error) {
          // A missing provider is a wiring mistake, not a file that failed: rejecting
          // the whole search is the only answer that cannot be mistaken for a result.
          setupError = setupError || error;
          return { entry: entry, error: error };
        }
        return { entry: entry, provider: provider, limit: limit, rows: [], cursor: 0,
          loaded: false, state: 'queued', relation: 'unknown', complete: false };
      });
      if (setupError) return Promise.reject(setupError);
      if (scope.mixed) {
        stats.local_queries += scope.local.length;
        stats.server_queries += scope.server.length;
      } else if (scope.local.length) {
        stats.local_queries += scope.local.length;
      } else {
        stats.server_queries += scope.server.length;
      }
      var tasks = children.map(function (child) {
        if (child.error) {
          child.state = 'failed';
          return Promise.resolve(child);
        }
        var childSpec = {};
        for (var key in spec) childSpec[key] = spec[key];
        childSpec.file_id = child.entry.file_id;
        childSpec.limit = limit;
        return Promise.resolve(child.provider.search(childSpec)).then(function (snapshot) {
          if (captured !== version) {
            // A newer scope started while this one was searching: its reply is not
            // this query's answer.
            throw errorOf('stale_query', '已有更新的查询范围', { requested: captured, current: version });
          }
          child.snapshot = snapshot;
          child.query_id = snapshot.query_id;
          child.relation = (snapshot.count && snapshot.count.relation) || 'unknown';
          child.complete = !!(snapshot.coverage && snapshot.coverage.complete);
          child.state = snapshot.state || 'running';
          return loadRows(child);
        }, function (error) {
          child.state = 'failed';
          child.error = error;
          return child;
        });
      });
      return Promise.all(tasks).then(function () {
        if (captured !== version) {
          throw errorOf('stale_query', '已有更新的查询范围', { requested: captured, current: version });
        }
        return snapshotState(mode, captured);
      });
    }

    /**
     * Pull one bounded batch of rows for a child.
     *
     * The two transports answer differently and that is not a detail to hide: the local
     * engine returns hit ordinals (the reader fetches headers for them), while the
     * server query state carries counts only and its rows come from the records page.
     * Both end up as the same row shape here.
     */
    function loadRows(child) {
      if (child.loaded || child.error) return Promise.resolve(child);
      child.loaded = true;
      var page = (child.snapshot && child.snapshot.page) || {};
      var ordinals = page.items || null;
      if (ordinals && ordinals.length) {
        child.ordinals = ordinals;
        return Promise.resolve(child.provider.getRecords({
          file_id: child.entry.file_id,
          generation: child.snapshot.generation,
          ordinals: ordinals
        })).then(function (value) {
          var byOrdinal = {};
          (value.items || []).forEach(function (item) { byOrdinal[item.ordinal] = item; });
          assign(child, ordinals.map(function (ordinal) { return byOrdinal[ordinal]; }));
          return child;
        }, fail(child));
      }
      if (child.provider.queryPage && child.query_id) {
        return Promise.resolve(child.provider.queryPage(child.query_id, null)).then(function (value) {
          var items = value.items || [];
          child.ordinals = items.map(function (item) { return item.ordinal; });
          assign(child, items);
          return child;
        }, fail(child));
      }
      child.rows = [];
      child.ordinals = [];
      return Promise.resolve(child);
    }

    function assign(child, items) {
      child.rows = (items || []).filter(Boolean).map(function (item) {
        return rowOf(child.entry, item, child.snapshot);
      }).filter(Boolean);
      child.rows.sort(function (a, b) { return compareRows(a, b); });
      return child;
    }

    function fail(child) {
      return function (error) {
        child.state = 'failed';
        child.error = error;
        child.rows = [];
        return child;
      };
    }

    function rowOf(entry, item, snapshot) {
      var ns = item.ts_ns != null ? item.ts_ns : null;
      var key = item.ts_key != null ? item.ts_key : (ns != null ? time.keyOf(ns) : time.keyOf(item.ts));
      return {
        source: entry.source,
        rank: entry.rank,
        file_index: entry.file_index,
        file_id: entry.file_id,
        generation: snapshot && snapshot.generation,
        ordinal: item.ordinal,
        record_id: item.record_id || (entry.file_id + ':' + item.ordinal),
        ts: item.ts || null,
        ts_ns: ns,
        ts_key: key == null ? null : key,
        level: item.level || null,
        message: item.message || '',
        lines: item.lines || null,
        has_trace: !!item.has_trace,
        version: item.version
      };
    }

    var STATIC = { done: true, failed: true, cancelled: true, paused: true };

    function settled(child) {
      return !!(child.snapshot && STATIC[child.state]);
    }

    function snapshotState(mode, captured) {
      var failed = [];
      var localRows = [];
      var serverRows = [];
      var excluded = [];
      children.forEach(function (child) {
        if (child.error) {
          failed.push({ file_id: child.entry.file_id, source: child.entry.source,
            code: child.error.code || 'query_failed',
            message: String(child.error.message || child.error) });
        }
        if (!child.rows || !child.rows.length) {
          if (child.state === 'failed') return;
          excluded.push({ file_id: child.entry.file_id, source: child.entry.source,
            reason: settled(child) ? 'no_time_domain' : 'incomplete' });
          return;
        }
        var timed = child.rows.filter(function (row) { return row.ts_key != null; });
        var untimed = child.rows.filter(function (row) { return row.ts_key == null; });
        if (!timed.length && mode === 'time') {
          // An unfinished child has not proven it has no timestamps, so it is
          // reported as incomplete rather than as an undated file.
          excluded.push({ file_id: child.entry.file_id, source: child.entry.source,
            reason: settled(child) ? 'no_time_domain' : 'incomplete' });
          if (!settled(child)) {
            (child.entry.source === 'local' ? localRows : serverRows).push(child.rows);
          }
          return;
        }
        (child.entry.source === 'local' ? localRows : serverRows).push(timed);
        if (mode !== 'time' && untimed.length) {
          (child.entry.source === 'local' ? localRows : serverRows).push(untimed);
        }
      });
      var flat = localRows.concat(serverRows);
      var merged = mergeOrdered(flat, Number.MAX_SAFE_INTEGER, {
        include_untimed: mode !== 'time'
      });
      lastPage = { rows: merged.items, mode: mode, version: captured };
      var coverage = coverageOf(failed);
      return {
        provider: 'combined',
        scope: { local: scope.local.slice(), server: scope.server.slice(), mixed: scope.mixed },
        mode: mode,
        query_version: captured,
        files: filesOf(),
        items: merged.items,
        coverage: coverage,
        count: countOf(failed),
        // The order is only final when every file finished scanning. "We sorted what
        // we have" is not the same claim as "this is the first page of the result".
        order_final: mode === 'time' && failed.length === 0 && coverage.complete,
        excluded: excluded,
        stats: statsOf()
      };
    }

    /**
     * Wait (bounded) for the children to settle, then rebuild the merged state.
     * The server query API is asynchronous by design; rendering its first "queued"
     * answer as an empty result is exactly the mistake this waits to avoid.
     */
    function wait(options) {
      options = options || {};
      var timeout = options.timeout_ms == null ? 8000 : options.timeout_ms;
      var interval = options.interval_ms == null ? 40 : options.interval_ms;
      var started = now();
      var captured = version;
      var mode = lastMode;
      function step() {
        if (captured !== version) {
          return Promise.reject(errorOf('stale_query', '已有更新的查询范围',
            { requested: captured, current: version }));
        }
        var pending = children.filter(function (child) {
          return child.provider && child.query_id && !child.error && !settled(child);
        });
        if (!pending.length || now() - started > timeout) {
          return Promise.resolve(snapshotState(mode, captured));
        }
        return Promise.all(pending.map(function (child) {
          return Promise.resolve(child.provider.poll(child.query_id)).then(function (snapshot) {
            child.snapshot = snapshot;
            child.state = snapshot.state || child.state;
            child.relation = (snapshot.count && snapshot.count.relation) || child.relation;
            child.complete = !!(snapshot.coverage && snapshot.coverage.complete);
            child.loaded = false;
            return loadRows(child);
          }, function () { return child; });
        })).then(function () {
          return new Promise(function (resolve) { setTimeout(resolve, interval); });
        }).then(step);
      }
      return step();
    }

    function emptyState(mode) {
      return {
        provider: 'combined', scope: { local: [], server: [], mixed: false }, mode: mode,
        query_version: version, files: [], items: [], excluded: [],
        coverage: { total_files: 0, completed_files: 0, failed: [], complete: true },
        count: { relation: 'exact', matched: 0 }, order_final: true, stats: statsOf()
      };
    }

    function coverageOf(failed) {
      var completed = children.filter(function (child) {
        return (child.complete || child.state === 'done') && !child.error;
      }).length;
      return {
        total_files: children.length,
        completed_files: completed,
        failed: failed,
        complete: failed.length === 0 && completed === children.length
      };
    }

    function countOf(failed) {
      var matched = 0;
      var exact = failed.length === 0;
      children.forEach(function (child) {
        if (child.error) {
          exact = false;
          return;
        }
        var relation = child.relation;
        var value = child.snapshot && child.snapshot.count ? child.snapshot.count.matched : 0;
        if (relation === 'exact' && child.complete) matched += value || 0;
        else {
          exact = false;
          matched += value || 0;
        }
      });
      return {
        relation: failed.length && !children.some(function (child) { return !child.error; })
          ? 'unknown' : (exact ? 'exact' : 'at_least'),
        matched: matched
      };
    }

    function statsOf() {
      return {
        local_queries: stats.local_queries,
        server_queries: stats.server_queries,
        local_files: scope.local.length,
        server_files: scope.server.length
      };
    }

    function filesOf() {
      return children.map(function (child) {
        return {
          file_id: child.entry.file_id, source: child.entry.source,
          state: child.state, relation: child.relation,
          generation: child.snapshot && child.snapshot.generation,
          matched: child.snapshot && child.snapshot.count ? child.snapshot.count.matched : null
        };
      });
    }

    /** The current merged rows, already ordered, without re-querying anything. */
    function page(limit) {
      assertOpen();
      if (!lastPage) return { items: [], next_cursor: null, order_final: true };
      var size = Math.max(1, limit || DEFAULT_LIMIT);
      return {
        items: lastPage.rows.slice(0, size),
        next_cursor: lastPage.rows.length > size ? size : null,
        order_final: true,
        truncated: lastPage.rows.length > size
      };
    }

    /**
     * A window is the only way this module hands out more than one page: it is a new
     * bounded query around an anchor, not a cursor that grows forever.
     */
    function window_(spec) {
      spec = spec || {};
      var copy = {};
      for (var key in spec) copy[key] = spec[key];
      copy.mode = spec.mode || 'time';
      if (spec.from_ns != null || spec.to_ns != null) {
        copy.time_range = { from_ns: spec.from_ns == null ? '0' : String(spec.from_ns),
          to_ns: spec.to_ns == null ? '9223372036854775807' : String(spec.to_ns) };
      }
      return search(copy);
    }

    function cancel() {
      var tasks = children.map(function (child) {
        if (!child.query_id || !child.provider) return Promise.resolve(null);
        return Promise.resolve(child.provider.cancel(child.query_id)).then(function () {
          child.state = 'cancelled';
          return null;
        }, function () { return null; });
      });
      return Promise.all(tasks).then(function () {
        return { cancelled: children.length, version: version };
      });
    }

    function close() {
      closed = true;
      version += 1;
      return Promise.resolve({ closed: true });
    }

    return {
      scope: function () { return { local: scope.local.slice(), server: scope.server.slice() }; },
      search: search,
      wait: wait,
      window: window_,
      page: page,
      cancel: cancel,
      close: close,
      files: filesOf,
      stats: statsOf,
      version: function () { return version; },
      setScope: function (next) { scope = splitScope(next); version += 1; }
    };
  }

  return {
    RANK: RANK,
    compareRows: compareRows,
    mergeOrdered: mergeOrdered,
    splitScope: splitScope,
    createCombinedProvider: createCombinedProvider
  };
});
