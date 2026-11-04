/*
 * local-engine.js — the Worker entry that owns local data (R6-019).
 *
 * This file is deliberately thin: it opens the store, wires the shared modules
 * into `engine-core.js`, and pumps RPC messages. Everything with a decision in
 * it lives in a testable module.
 *
 * It is served assembled by `server/frontend.py` (`/assets/workers/local-engine.js`),
 * concatenating the shared scripts ahead of this file, so there is still no
 * build step and no bundler.
 */
(function () {
  'use strict';

  var scope = typeof self !== 'undefined' ? self : this;
  if (!scope.LBRpc || !scope.LBEngineCore || !scope.LBLocalStore || !scope.LBIndexStore) {
    throw new Error('local-engine: shared modules missing from the worker bundle');
  }

  var engine = null;
  var store = null;
  var indexer = null;
  var ready = null;
  var blockIndexCache = Object.create(null);
  var stats = { started: 0, opened: 0, worker_boot: Date.now() };

  function send(message, transfer) {
    scope.postMessage(message, transfer || []);
  }

  function buildEngine(namespace) {
    return scope.LBLocalStore.open(scope.indexedDB, namespace).then(function (opened) {
      store = opened;
      indexer = scope.LBIndexStore.createIndexer(store);
      engine = scope.LBEngineCore.createEngine({
        store: store,
        indexer: indexer,
        datasetId: namespace,
        normalizeQuery: scope.LBQuery.normalizeQuery,
        matches: scope.LBQuery.matches,
        candidates: scope.LBCandidates,
        queryJobs: scope.LBQueryJobs,
        buildBlockIndex: function (fileId, generation) {
          var key = fileId + '|' + generation;
          if (blockIndexCache[key]) return blockIndexCache[key];
          return null;
        }
      });
      return engine;
    });
  }

  /* The block index is built from records on first search and cached per generation. */
  function ensureBlockIndex(fileId, generation) {
    var key = fileId + '|' + generation;
    if (blockIndexCache[key]) return Promise.resolve(blockIndexCache[key]);
    return indexer.getBlocks(fileId, generation, 'record_blocks', 0, null).then(function (blocks) {
      var records = [];
      blocks.forEach(function (block) {
        (block.records || []).forEach(function (record) { records.push(record); });
      });
      var index = scope.LBCandidates.buildBlockIndex(records, { records_per_block: 512 });
      blockIndexCache[key] = index;
      return index;
    });
  }

  function withEngine(method, params) {
    if (method === 'open') {
      var namespace = (params && params.dataset_id) || 'local';
      stats.opened += 1;
      ready = buildEngine(namespace).then(function () {
        return engine.handle('open', params);
      });
      return ready;
    }
    if (!ready) {
      return Promise.reject(scope.LBRpc.errorOf('not_open', '本机引擎尚未打开数据集'));
    }
    return ready.then(function () {
      if (method === 'search') {
        return ensureBlockIndex(params.file_id, params.generation).then(function () {
          return engine.handle(method, params);
        });
      }
      return engine.handle(method, params);
    });
  }

  var server = scope.LBRpc.createRpcServer({
    post: send,
    handlers: {
      open: function (params) { return withEngine('open', params); },
      close: function (params) {
        blockIndexCache = Object.create(null);
        if (engine) engine.clearCache();
        if (store) store.close();
        engine = null; store = null; indexer = null; ready = null;
        return { closed: true };
      },
      listFiles: function (params) { return withEngine('listFiles', params); },
      getMetadata: function (params) { return withEngine('getMetadata', params); },
      ensureIndex: function (params) { return withEngine('ensureIndex', params); },
      headers: function (params) { return withEngine('headers', params); },
      getPage: function (params) { return withEngine('getPage', params); },
      getRecord: function (params) { return withEngine('getRecord', params); },
      getRecords: function (params) { return withEngine('getRecords', params); },
      detail: function (params) { return withEngine('detail', params); },
      readRaw: function (params) { return withEngine('readRaw', params); },
      getTimeline: function (params) { return withEngine('getTimeline', params); },
      seek: function (params) { return withEngine('seek', params); },
      search: function (params) { return withEngine('search', params); },
      poll: function (params) { return withEngine('poll', params); },
      cancelSearch: function (params) { return withEngine('cancelSearch', params); },
      resume: function (params) { return withEngine('resume', params); },
      stageIndex: function (params) {
        return withEngine('stageIndex', params).then(function (value) {
          // A new generation invalidates the cached gram index for that file.
          Object.keys(blockIndexCache).forEach(function (key) {
            if (key.indexOf(params.file_id + '|') === 0) delete blockIndexCache[key];
          });
          return value;
        });
      },
      stageBlock: function (params) {
        return withEngine('stageBlock', params).then(function (value) {
          Object.keys(blockIndexCache).forEach(function (key) {
            if (key.indexOf(params.file_id + '|') === 0) delete blockIndexCache[key];
          });
          return value;
        });
      },
      publishIndex: function (params) { return withEngine('publishIndex', params); },
      putFile: function (params) { return withEngine('putFile', params); },
      appendChunk: function (params) { return withEngine('appendChunk', params); },
      finalizeFile: function (params) { return withEngine('finalizeFile', params); }
    },
    onCancel: function () { /* per-query cancellation lands with R6-022 */ }
  });

  scope.onmessage = function (event) {
    stats.started += 1;
    server.handleMessage(event.data);
  };

  /* Worker-level counters are reported on demand; they never carry log content. */
  scope.__engineStats = function () {
    var copy = {};
    for (var key in stats) copy[key] = stats[key];
    copy.blocks_indexed = Object.keys(blockIndexCache).length;
    copy.engine = engine ? engine.stats() : null;
    return copy;
  };
})();
