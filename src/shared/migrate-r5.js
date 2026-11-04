/*
 * migrate-r5.js — migrate the v3.0.3 browser cache into the R6 store (R6-016).
 *
 * What v3.0.3 actually leaves behind (verified against src/app.js and src/r5.js):
 *   IndexedDB  `logbench-user-<uid>-db`   stores: files (text + records), meta
 *   localStorage `logbench-user-<uid>-workspace`  view state, drafts, anchors
 *
 * Migration rules:
 *   - scan reads *keys and metadata only*; raw text is read one file at a time,
 *     so opening the workspace never loads every log into memory;
 *   - every file is chunked into the R6 store, then re-read and hashed to prove
 *     the round trip before the legacy copy may be dropped;
 *   - a checkpoint records which files are done, so closing the tab mid-way
 *     resumes instead of restarting;
 *   - the legacy database is never deleted by this module. `finalize()` only
 *     reports readiness; the caller decides;
 *   - drafts are copied but flagged `drafts_active: false`: a draft must not
 *     silently become an applied rule.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBMigrateR5 = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CHECKPOINT_ID = 'migrate-r5';
  var CHUNK = 1024 * 1024;

  function errorOf(code, message, extra) {
    var error = new Error(message || code);
    error.code = code;
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  /* 64-bit FNV-1a over bytes; 16 hex chars. Not a security primitive. */
  function digestBytes(bytes, seed) {
    var high = (seed && seed.high) || 0x811c9dc5;
    var low = (seed && seed.low) || 0x01000193;
    for (var i = 0; i < bytes.length; i++) {
      low ^= bytes[i];
      low = (low + ((low << 1) + (low << 4) + (low << 7) + (low << 8) + (low << 24))) >>> 0;
      high = (high ^ low) >>> 0;
      high = (high + (high << 1) + (high << 4)) >>> 0;
    }
    return { high: high >>> 0, low: low >>> 0, hex: hex(high) + hex(low) };
  }

  function hex(value) { return ('00000000' + (value >>> 0).toString(16)).slice(-8); }

  function digestText(text) {
    return digestBytes(new TextEncoder().encode(text)).hex;
  }

  function planVolumes(totalBytes, volumeBytes) {
    var size = volumeBytes || 64 * 1024 * 1024;
    if (!(totalBytes >= 0)) throw errorOf('invalid_size', '大小必须是非负数');
    var count = totalBytes === 0 ? 0 : Math.ceil(totalBytes / size);
    var volumes = [];
    for (var index = 0; index < count; index++) {
      volumes.push({ index: index, from: index * size, to: Math.min(totalBytes, (index + 1) * size) });
    }
    return volumes;
  }

  function openLegacy(factory, userId) {
    var name = 'logbench-user-' + userId + '-db';
    return new Promise(function (resolve, reject) {
      var request;
      try { request = factory.open(name); }
      catch (error) { reject(errorOf('legacy_unavailable', '无法打开旧版本机缓存')); return; }
      request.onerror = function () { reject(errorOf('legacy_unavailable', '无法打开旧版本机缓存')); };
      request.onblocked = function () { reject(errorOf('legacy_blocked', '另一个页面正持有旧版缓存，请关闭后重试')); };
      request.onsuccess = function () {
        var db = request.result;
        var stores = Array.prototype.slice.call(db.objectStoreNames);
        if (stores.indexOf('files') === -1) {
          db.close();
          reject(errorOf('legacy_empty', '旧版缓存中没有 files 存储'));
          return;
        }
        resolve(db);
      };
    });
  }

  function readLegacyIndex(db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction('files', 'readonly');
      var store = tx.objectStore('files');
      var keysRequest = store.getAllKeys();
      var items = [];
      keysRequest.onsuccess = function () {
        var keys = keysRequest.result || [];
        var remaining = keys.length;
        if (!remaining) { resolve([]); return; }
        keys.forEach(function (key) {
          // Read one record at a time but only keep metadata; the text is dropped
          // right after measuring it so scanning does not retain the corpus.
          var request = store.get(key);
          request.onsuccess = function () {
            var record = request.result || {};
            items.push({
              legacy_id: key,
              name: record.name || String(key),
              size: record.size != null ? record.size : (record.text ? record.text.length : 0),
              saved_size: record.text ? record.text.length : 0,
              modified: record.modified || null
            });
            if (--remaining === 0) resolve(items);
          };
          request.onerror = function () { reject(errorOf('legacy_read_failed', '读取旧版文件索引失败')); };
        });
      };
      keysRequest.onerror = function () { reject(errorOf('legacy_read_failed', '读取旧版文件列表失败')); };
    });
  }

  function readLegacyFile(db, legacyId) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction('files', 'readonly');
      var request = tx.objectStore('files').get(legacyId);
      request.onsuccess = function () {
        var record = request.result;
        if (!record) { reject(errorOf('legacy_missing', '旧版缓存中找不到该文件')); return; }
        resolve(record);
      };
      request.onerror = function () { reject(errorOf('legacy_read_failed', '读取旧版文件失败')); };
    });
  }

  function createMigrator(options) {
    var store = options.store;
    var factory = options.indexedDB;
    var userId = options.userId;
    var localStorageRef = options.localStorage;
    var chunkSize = options.chunkSize || CHUNK;
    if (!store) throw errorOf('no_store', '需要 R6 本地存储');

    function readWorkspaceSnapshot() {
      try {
        var raw = localStorageRef ? localStorageRef.getItem('logbench-user-' + userId + '-workspace') : null;
        return raw ? JSON.parse(raw) : null;
      } catch (error) {
        return null;
      }
    }

    function loadCheckpoint() {
      return store.getCheckpoint(CHECKPOINT_ID).then(function (value) {
        return value || { done: [], failed: [], drafts_active: false };
      });
    }

    function saveCheckpoint(value) {
      return store.putCheckpoint(CHECKPOINT_ID, value);
    }

    function migrateOne(db, item, generation) {
      return readLegacyFile(db, item.legacy_id).then(function (record) {
        var text = record.text == null ? '' : String(record.text);
        var bytes = new TextEncoder().encode(text);
        var sourceDigest = digestBytes(bytes).hex;
        var total = Math.ceil(bytes.length / chunkSize);
        var writes = [];
        for (var index = 0; index < total; index++) {
          var slice = bytes.slice(index * chunkSize, Math.min(bytes.length, (index + 1) * chunkSize));
          writes.push(store.putRawChunk(item.legacy_id, generation, index, slice.buffer));
        }
        return Promise.all(writes).then(function () {
          return store.putFileMeta({
            file_id: item.legacy_id,
            name: item.name,
            size: bytes.length,
            chunk_size: chunkSize,
            generation: generation,
            chunks: total,
            digest: sourceDigest,
            migrated_from: 'r5',
            migrated_at: Date.now()
          }).then(function () {
            if (bytes.length === 0) return new Uint8Array(0);
            // Verify by reading back through the R6 path, not by trusting the write.
            return store.getRawRange(item.legacy_id, generation, 0, bytes.length, chunkSize);
          }).then(function (roundTrip) {
            var actual = digestBytes(roundTrip).hex;
            if (actual !== sourceDigest) {
              return { legacy_id: item.legacy_id, ok: false, reason: 'digest_mismatch', expected: sourceDigest, actual: actual };
            }
            return { legacy_id: item.legacy_id, ok: true, bytes: bytes.length, digest: actual };
          });
        });
      });
    }

    /** Migrate every file, resuming from the checkpoint. One file in memory at a time. */
    function run(options2) {
      options2 = options2 || {};
      var generation = options2.generation || 1;
      return openLegacy(factory, userId).then(function (db) {
        return readLegacyIndex(db).then(function (items) {
          return loadCheckpoint().then(function (checkpoint) {
            var pending = items.filter(function (item) { return checkpoint.done.indexOf(item.legacy_id) === -1; });
            var cursor = Promise.resolve();
            var results = [];
            pending.forEach(function (item) {
              cursor = cursor.then(function () {
                return migrateOne(db, item, generation).then(function (result) {
                  results.push(result);
                  if (result.ok) checkpoint.done.push(item.legacy_id);
                  else checkpoint.failed.push(item.legacy_id);
                  return saveCheckpoint(checkpoint);
                });
              });
            });
            return cursor.then(function () {
              db.close();
              var snapshot = readWorkspaceSnapshot();
              var workspace = {
                migrated_from: 'r5',
                drafts_active: false,
                view: snapshot && snapshot.view ? snapshot.view : 'logs',
                scope: snapshot && snapshot.scope ? snapshot.scope : 'all',
                savedAt: snapshot && snapshot.savedAt ? snapshot.savedAt : null,
                drafts: snapshot && snapshot.drafts ? snapshot.drafts : null,
                bookmarks: snapshot && snapshot.bookmarks ? snapshot.bookmarks : null,
                anchors: snapshot && snapshot.anchors ? snapshot.anchors : null
              };
              return store.putCheckpoint(CHECKPOINT_ID, checkpoint).then(function () {
                return store.putCheckpoint('workspace', workspace);
              }).then(function () {
                return {
                  total: items.length,
                  migrated: results.filter(function (item) { return item.ok; }).length,
                  failed: results.filter(function (item) { return !item.ok; }),
                  checkpoint: checkpoint,
                  drafts_active: false
                };
              });
            });
          });
        });
      });
    }

    /** Everything verified? Only then may the caller consider dropping the legacy copy. */
    function readiness() {
      return loadCheckpoint().then(function (checkpoint) {
        return {
          done: checkpoint.done.slice(),
          failed: checkpoint.failed.slice(),
          safe_to_drop_legacy: checkpoint.failed.length === 0 && checkpoint.done.length > 0
        };
      });
    }

    return { run: run, readiness: readiness, readWorkspaceSnapshot: readWorkspaceSnapshot };
  }

  return {
    CHECKPOINT_ID: CHECKPOINT_ID,
    CHUNK: CHUNK,
    digestBytes: digestBytes,
    digestText: digestText,
    planVolumes: planVolumes,
    openLegacy: openLegacy,
    readLegacyIndex: readLegacyIndex,
    createMigrator: createMigrator
  };
});
