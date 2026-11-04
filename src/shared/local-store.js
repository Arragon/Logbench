/*
 * local-store.js — namespaced IndexedDB store for raw chunks and metadata (R6-013).
 *
 * Non-negotiables from the ticket:
 *   - metadata is stored apart from raw bytes, so opening a workspace reads only
 *     `file_meta` and never touches the raw chunks;
 *   - a write is only reported as durable after the transaction's `oncomplete`,
 *     not after `onsuccess` of the last request;
 *   - range reads fetch only the chunks the range overlaps;
 *   - the baseline is IndexedDB over plain HTTP; OPFS is an optional adapter, not
 *     a requirement (a private-network deployment is not a secure context);
 *   - errors are codes (`quota_exceeded`, `transaction_aborted`, `blocked`,
 *     `schema_mismatch`), never "saved" with a silent failure;
 *   - switching namespace must not expose another namespace's file names.
 *
 * Pure helpers (chunk math, key building, error mapping) are exported separately
 * so they can be unit tested in Node; persistence itself is proven in a real
 * browser by qa/r6/idb_store.py.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBLocalStore = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var DB_NAME = 'logbench-r6';
  var DB_VERSION = 1;
  var DEFAULT_CHUNK = 1024 * 1024;
  var STORES = {
    workspace: { keyPath: 'id' },
    file_meta: { keyPath: ['namespace', 'file_id'] },
    raw_chunks: { keyPath: ['namespace', 'file_id', 'generation', 'chunk_no'] },
    record_blocks: { keyPath: ['namespace', 'file_id', 'generation', 'block_no'] },
    rule_blocks: { keyPath: ['namespace', 'file_id', 'generation', 'block_no'] },
    search_blocks: { keyPath: ['namespace', 'file_id', 'generation', 'block_no'] },
    task_checkpoints: { keyPath: ['namespace', 'task_id'] }
  };

  function errorOf(code, message, extra) {
    var error = new Error(message || code);
    error.code = code;
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  function mapError(error) {
    var name = (error && error.name) || '';
    if (name === 'QuotaExceededError') return errorOf('quota_exceeded', '本机存储配额不足，已写入的数据保持不变', { cause: name });
    if (name === 'VersionError') return errorOf('schema_mismatch', '本机数据库版本高于当前页面，请关闭旧页面后重试', { cause: name });
    if (name === 'AbortError') return errorOf('transaction_aborted', '写入事务被中止，本次改动未生效', { cause: name });
    if (name === 'InvalidStateError') return errorOf('connection_closed', '本机数据库连接已关闭', { cause: name });
    if (name === 'DataError' || name === 'ConstraintError') return errorOf('invalid_record', '记录不符合本机库的主键或字段约束', { cause: name });
    return errorOf('storage_error', '本机存储操作失败', { cause: name || String(error) });
  }

  /* Byte range -> the chunk indices it actually needs. */
  function planRange(offset, length, chunkSize) {
    var size = chunkSize || DEFAULT_CHUNK;
    if (!(offset >= 0) || !(length > 0)) throw errorOf('invalid_range', '范围必须是非负偏移与正长度');
    var first = Math.floor(offset / size);
    var last = Math.floor((offset + length - 1) / size);
    var plans = [];
    for (var index = first; index <= last; index++) {
      var chunkStart = index * size;
      plans.push({
        chunk_no: index,
        from: Math.max(offset, chunkStart) - chunkStart,
        to: Math.min(offset + length, chunkStart + size) - chunkStart
      });
    }
    return plans;
  }

  function chunkCount(totalBytes, chunkSize) {
    var size = chunkSize || DEFAULT_CHUNK;
    return Math.max(0, Math.ceil(totalBytes / size));
  }

  function chunkKey(namespace, fileId, generation, chunkNo) {
    return [namespace, fileId, generation, chunkNo];
  }

  function metaKey(namespace, fileId) {
    return [namespace, fileId];
  }

  function openDatabase(factory, options) {
    options = options || {};
    var name = options.name || DB_NAME;
    return new Promise(function (resolve, reject) {
      var request;
      try {
        request = factory.open(name, DB_VERSION);
      } catch (error) {
        reject(mapError(error));
        return;
      }
      request.onupgradeneeded = function () {
        var db = request.result;
        Object.keys(STORES).forEach(function (store) {
          if (!db.objectStoreNames.contains(store)) {
            db.createObjectStore(store, { keyPath: STORES[store].keyPath });
          }
        });
      };
      request.onblocked = function () {
        reject(errorOf('blocked', '另一个页面正持有旧版本数据库，请关闭旧页面', {}));
      };
      request.onerror = function () { reject(mapError(request.error)); };
      request.onsuccess = function () { resolve(request.result); };
    });
  }

  function requestAsPromise(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(mapError(request.error)); };
    });
  }

  /* Resolve only when the whole transaction committed. */
  function transactionDone(tx) {
    return new Promise(function (resolve, reject) {
      tx.oncomplete = function () { resolve(true); };
      tx.onabort = function () { reject(mapError(tx.error || { name: 'AbortError' })); };
      tx.onerror = function () { reject(mapError(tx.error)); };
    });
  }

  function createStore(db, namespace) {
    if (!db) throw errorOf('connection_closed', '数据库未打开');
    if (!namespace) throw errorOf('invalid_namespace', '必须指定命名空间');

    function withStore(name, mode, work) {
      var tx = db.transaction(name, mode);
      var done = transactionDone(tx);
      var result;
      try {
        result = work(tx.objectStore(name), tx);
      } catch (error) {
        try { tx.abort(); } catch (ignored) { /* already finished */ }
        return Promise.reject(error && error.code ? error : mapError(error));
      }
      return done.then(function () { return result; });
    }

    function putFileMeta(meta) {
      if (!meta || !meta.file_id) return Promise.reject(errorOf('invalid_meta', '文件元数据缺少 file_id'));
      var record = {};
      for (var key in meta) record[key] = meta[key];
      record.namespace = namespace;
      record.updated_at = Date.now();
      return withStore('file_meta', 'readwrite', function (store) {
        return requestAsPromise(store.put(record));
      }).then(function () { return record; });
    }

    function listFileMeta() {
      return withStore('file_meta', 'readonly', function (store) {
        return requestAsPromise(store.getAll(IDBKeyRange.bound([namespace], [namespace, []])));
      }).then(function (rows) {
        return (rows || []).map(function (row) {
          var copy = {};
          for (var key in row) copy[key] = row[key];
          delete copy.namespace;
          return copy;
        });
      });
    }

    function putRawChunk(fileId, generation, chunkNo, bytes, extra) {
      if (generation == null) return Promise.reject(errorOf('invalid_generation', '必须指定文件代次'));
      var record = {
        namespace: namespace,
        file_id: fileId,
        generation: generation,
        chunk_no: chunkNo,
        bytes: bytes,
        byte_length: bytes && bytes.byteLength != null ? bytes.byteLength : (bytes && bytes.size) || 0
      };
      if (extra) for (var key in extra) record[key] = extra[key];
      return withStore('raw_chunks', 'readwrite', function (store) {
        return requestAsPromise(store.put(record));
      });
    }

    /* Fetch only the chunks the range overlaps, then trim to the exact range. */
    function getRawRange(fileId, generation, offset, length, chunkSize) {
      if (length === 0) return Promise.resolve(new Uint8Array(0));
      var plans = planRange(offset, length, chunkSize);
      var tx = db.transaction('raw_chunks', 'readonly');
      var store = tx.objectStore('raw_chunks');
      var requests = plans.map(function (plan) {
        return requestAsPromise(store.get(chunkKey(namespace, fileId, generation, plan.chunk_no)));
      });
      return Promise.all(requests).then(function (rows) {
        var pieces = [];
        var total = 0;
        for (var i = 0; i < rows.length; i++) {
          if (!rows[i]) throw errorOf('missing_chunk', '原文分块缺失，无法还原该区间', { chunk_no: plans[i].chunk_no });
          var source = rows[i].bytes;
          var view = source instanceof ArrayBuffer ? new Uint8Array(source) : source;
          var slice = view.slice(plans[i].from, plans[i].to);
          pieces.push(slice);
          total += slice.byteLength;
        }
        var merged = new Uint8Array(total);
        var cursor = 0;
        for (var j = 0; j < pieces.length; j++) {
          merged.set(pieces[j], cursor);
          cursor += pieces[j].byteLength;
        }
        return merged;
      });
    }

    function deleteGeneration(fileId, generation) {
      var tx = db.transaction(['raw_chunks', 'record_blocks', 'rule_blocks', 'search_blocks'], 'readwrite');
      var done = transactionDone(tx);
      tx.objectStore('raw_chunks').delete(IDBKeyRange.bound(
        [namespace, fileId, generation, -Infinity], [namespace, fileId, generation, Infinity]));
      ['record_blocks', 'rule_blocks', 'search_blocks'].forEach(function (name) {
        tx.objectStore(name).delete(IDBKeyRange.bound(
          [namespace, fileId, generation, -Infinity], [namespace, fileId, generation, Infinity]));
      });
      return done.then(function () { return true; });
    }

    function putCheckpoint(taskId, value) {
      return withStore('task_checkpoints', 'readwrite', function (store) {
        return requestAsPromise(store.put({ namespace: namespace, task_id: taskId, value: value, updated_at: Date.now() }));
      });
    }

    function getCheckpoint(taskId) {
      return withStore('task_checkpoints', 'readonly', function (store) {
        return requestAsPromise(store.get([namespace, taskId]));
      }).then(function (row) { return row ? row.value : null; });
    }

    /* Batch write inside one transaction: commit together or not at all. */
    function commitBatch(operations) {
      if (!Array.isArray(operations) || !operations.length) return Promise.resolve({ written: 0 });
      var names = [];
      operations.forEach(function (operation) {
        if (names.indexOf(operation.store) === -1) names.push(operation.store);
      });
      var tx = db.transaction(names, 'readwrite');
      var done = transactionDone(tx);
      try {
        operations.forEach(function (operation) {
          var store = tx.objectStore(operation.store);
          if (operation.type === 'delete') store.delete(operation.key);
          else store.put(operation.value);
        });
      } catch (error) {
        try { tx.abort(); } catch (ignored) { /* noop */ }
        return Promise.reject(error && error.code ? error : mapError(error));
      }
      return done.then(function () { return { written: operations.length }; });
    }

    function close() { try { db.close(); } catch (error) { /* noop */ } }

    return {
      namespace: namespace,
      putFileMeta: putFileMeta,
      listFileMeta: listFileMeta,
      putRawChunk: putRawChunk,
      getRawRange: getRawRange,
      deleteGeneration: deleteGeneration,
      putCheckpoint: putCheckpoint,
      getCheckpoint: getCheckpoint,
      commitBatch: commitBatch,
      close: close
    };
  }

  /* Convenience: open a namespace in one call. */
  function open(factory, namespace, options) {
    var source = factory || (typeof indexedDB !== 'undefined' ? indexedDB : null);
    if (!source) return Promise.reject(errorOf('no_indexeddb', '当前浏览器不提供 IndexedDB'));
    return openDatabase(source, options).then(function (db) {
      var store = createStore(db, namespace);
      store.database = db;
      return store;
    });
  }

  return {
    DB_NAME: DB_NAME,
    DB_VERSION: DB_VERSION,
    DEFAULT_CHUNK: DEFAULT_CHUNK,
    STORES: STORES,
    open: open,
    openDatabase: openDatabase,
    createStore: createStore,
    planRange: planRange,
    chunkCount: chunkCount,
    chunkKey: chunkKey,
    metaKey: metaKey,
    mapError: mapError
  };
});
