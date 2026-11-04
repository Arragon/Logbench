/*
 * index-store.js — staged publishing of derived index layers (R6-014).
 *
 * Model:
 *   raw chunks      immutable per (file, generation)
 *   record_blocks   framed headers + byte spans          (regenerable)
 *   rule_blocks     extraction/anomaly results + evidence (regenerable)
 *   search_blocks   projected text for matching           (regenerable)
 *   workspace       one pointer: which generation is published, with which
 *                   signatures and watermarks
 *
 * Publishing flips the pointer in a single transaction. Until that commit lands
 * the previous generation stays published, so a half-written index is never
 * visible. Cache pruning only touches the three regenerable layers — never raw
 * bytes and never the workspace pointer.
 *
 * Watermarks must satisfy: indexed_records <= framed_lines and raw_bytes <= size.
 * A block set that violates this is refused at validate(), not published and
 * discovered later.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBIndexStore = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var LAYERS = ['record_blocks', 'rule_blocks', 'search_blocks'];

  function errorOf(code, message, extra) {
    var error = new Error(message || code);
    error.code = code;
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  /* Pure: block continuity and watermark sanity. */
  function validatePlan(plan) {
    var problems = [];
    if (!plan || typeof plan !== 'object') return { ok: false, problems: ['plan missing'] };
    if (!plan.file_id) problems.push('file_id missing');
    if (!(plan.generation >= 1)) problems.push('generation must be >= 1');
    LAYERS.forEach(function (layer) {
      var blocks = plan[layer];
      if (blocks == null) return;
      if (!Array.isArray(blocks)) { problems.push(layer + ' is not an array'); return; }
      blocks.forEach(function (block, index) {
        if (block.block_no !== index) problems.push(layer + ' block_no ' + block.block_no + ' out of order');
        if (!Array.isArray(block.records)) problems.push(layer + '[' + index + '] has no records array');
      });
    });
    var watermark = plan.watermark || {};
    if (!(watermark.raw_bytes >= 0)) problems.push('raw_bytes missing');
    if (!(watermark.framed_lines >= 0)) problems.push('framed_lines missing');
    if (!(watermark.indexed_records >= 0)) problems.push('indexed_records missing');
    if (watermark.indexed_records > watermark.framed_lines) problems.push('indexed_records exceeds framed_lines');
    if (plan.size != null && watermark.raw_bytes > plan.size) problems.push('raw_bytes exceeds file size');
    if (!plan.signatures || !plan.signatures.structure) problems.push('structure signature missing');
    return { ok: problems.length === 0, problems: problems };
  }

  function watermarkOf(blocks, rawBytes, framedLines) {
    var records = 0;
    (blocks || []).forEach(function (block) { records += (block.records || []).length; });
    return { raw_bytes: rawBytes, framed_lines: framedLines, indexed_records: records };
  }

  /* Pure: which generations may be pruned, and only in regenerable layers. */
  function prunableGenerations(workspace, keep) {
    if (!workspace || !Array.isArray(workspace.generations)) return [];
    var keepSet = [workspace.published_generation].concat(keep || []);
    return workspace.generations.filter(function (generation) {
      return keepSet.indexOf(generation) === -1;
    });
  }

  /* Pure: dropping a superseded record revision from a block. */
  function applyRevision(block, recordId, version) {
    var records = (block.records || []).filter(function (record) {
      return record.record_id !== recordId;
    });
    if (version != null) records.push({ record_id: recordId, version: version, replaced: true });
    return records;
  }

  function createIndexer(store) {
    if (!store) throw errorOf('no_store', '需要先打开本地存储');

    function workspaceId(fileId) { return 'file:' + fileId; }

    function readWorkspace(fileId) {
      return store.getCheckpoint(workspaceId(fileId)).then(function (value) { return value || null; });
    }

    function writeWorkspace(fileId, value) {
      return store.putCheckpoint(workspaceId(fileId), value);
    }

    /**
     * Write all layers of a generation in one transaction. Nothing is published
     * yet: readPublished() still returns the previous generation.
     */
    function stage(plan) {
      var check = validatePlan(plan);
      if (!check.ok) return Promise.reject(errorOf('invalid_plan', '索引计划不完整：' + check.problems.join('；'), { problems: check.problems }));
      var operations = [];
      LAYERS.forEach(function (layer) {
        (plan[layer] || []).forEach(function (block) {
          operations.push({
            store: layer,
            value: {
              namespace: store.namespace,
              file_id: plan.file_id,
              generation: plan.generation,
              block_no: block.block_no,
              records: block.records,
              signature: plan.signatures
            }
          });
        });
      });
      return store.commitBatch(operations).then(function () {
        return { file_id: plan.file_id, generation: plan.generation, blocks: operations.length };
      });
    }

    /** Flip the pointer. One transaction, so readers see old or new, never a mix. */
    function publish(plan) {
      var check = validatePlan(plan);
      if (!check.ok) return Promise.reject(errorOf('invalid_plan', '索引计划不完整：' + check.problems.join('；'), { problems: check.problems }));
      return readWorkspace(plan.file_id).then(function (existing) {
        var generations = (existing && existing.generations) || [];
        if (generations.indexOf(plan.generation) === -1) generations.push(plan.generation);
        var next = {
          file_id: plan.file_id,
          published_generation: plan.generation,
          previous_generation: existing ? existing.published_generation : null,
          signatures: plan.signatures,
          watermark: plan.watermark,
          // Block count lets a reader page through the index without loading it
          // all to find out how big it is.
          blocks: plan.blocks == null ? null : plan.blocks,
          published_at: Date.now(),
          generations: generations
        };
        return writeWorkspace(plan.file_id, next).then(function () { return next; });
      });
    }

    function readPublished(fileId) {
      return readWorkspace(fileId).then(function (value) {
        if (!value || value.published_generation == null) return null;
        return {
          file_id: fileId,
          generation: value.published_generation,
          signatures: value.signatures,
          watermark: value.watermark,
          blocks: value.blocks == null ? null : value.blocks,
          published_at: value.published_at
        };
      });
    }

    /** Cache pruning: regenerable layers only, raw chunks and workspace untouched. */
    function prune(fileId, keep) {
      return readWorkspace(fileId).then(function (workspace) {
        if (!workspace) return { pruned: [] };
        var doomed = prunableGenerations(workspace, keep);
        if (!doomed.length) return { pruned: [] };
        // Deleting a single key would only drop block 0, so delete whole ranges.
        return deleteLayerGenerations(fileId, doomed).then(function () {
          var survivor = workspace.generations.filter(function (generation) {
            return doomed.indexOf(generation) === -1;
          });
          var updated = {};
          for (var key in workspace) updated[key] = workspace[key];
          updated.generations = survivor;
          return writeWorkspace(fileId, updated).then(function () { return { pruned: doomed }; });
        });
      });
    }

    function deleteLayerGenerations(fileId, generations) {
      var db = store.database;
      if (!db || typeof IDBKeyRange === 'undefined') return Promise.resolve(false);
      var tx = db.transaction(LAYERS, 'readwrite');
      var done = new Promise(function (resolve, reject) {
        tx.oncomplete = function () { resolve(true); };
        tx.onabort = function () { reject(errorOf('transaction_aborted', '清理索引失败')); };
        tx.onerror = function () { reject(errorOf('storage_error', '清理索引失败')); };
      });
      generations.forEach(function (generation) {
        LAYERS.forEach(function (layer) {
          tx.objectStore(layer).delete(IDBKeyRange.bound(
            [store.namespace, fileId, generation, 0],
            [store.namespace, fileId, generation, Number.MAX_SAFE_INTEGER]));
        });
      });
      return done;
    }

    /** Replace one record's derived content after its version increments. */
    function replaceRecord(fileId, generation, layer, blockNo, recordId, version) {
      var db = store.database;
      if (!db) return Promise.reject(errorOf('no_store', '需要原生 IndexedDB 才能替换块'));
      var tx = db.transaction(layer, 'readwrite');
      var done = new Promise(function (resolve, reject) {
        tx.oncomplete = function () { resolve(true); };
        tx.onabort = function () { reject(errorOf('transaction_aborted', '替换派生块失败')); };
        tx.onerror = function () { reject(errorOf('storage_error', '替换派生块失败')); };
      });
      var request = tx.objectStore(layer).get([store.namespace, fileId, generation, blockNo]);
      request.onsuccess = function () {
        var block = request.result;
        if (!block) return;
        block.records = applyRevision(block, recordId, version);
        tx.objectStore(layer).put(block);
      };
      return done;
    }

    /**
     * Write a single block, so an importer can stream instead of holding every
     * record in memory before the first commit.
     */
    function stageBlock(params) {
      var layer = params.layer || 'record_blocks';
      if (LAYERS.indexOf(layer) === -1) {
        return Promise.reject(errorOf('bad_layer', '未知索引层：' + layer));
      }
      if (!(params.block_no >= 0)) {
        return Promise.reject(errorOf('invalid_block', 'block_no 必须从 0 开始'));
      }
      return store.commitBatch([{
        store: layer,
        value: {
          namespace: store.namespace,
          file_id: params.file_id,
          generation: params.generation,
          block_no: params.block_no,
          records: params.records || [],
          signature: params.signatures || null
        }
      }]).then(function () {
        return { file_id: params.file_id, generation: params.generation, block_no: params.block_no,
                 records: (params.records || []).length };
      });
    }

    /** Publish a generation whose blocks were written one at a time. */
    function publishIndex(params) {
      return publish({
        file_id: params.file_id,
        generation: params.generation,
        size: params.size,
        signatures: params.signatures,
        watermark: params.watermark,
        blocks: params.blocks
      });
    }

    /** Read one layer's blocks in order. Used by the resident engine for paging. */
    function getBlocks(fileId, generation, layer, fromBlock, toBlock) {
      var db = store.database;
      if (!db) return Promise.reject(errorOf('no_store', '需要原生 IndexedDB 才能读取索引块'));
      if (LAYERS.indexOf(layer) === -1) {
        return Promise.reject(errorOf('bad_layer', '未知索引层：' + layer));
      }
      var tx = db.transaction(layer, 'readonly');
      var objectStore = tx.objectStore(layer);
      // `Infinity` is not a valid IndexedDB key; MAX_SAFE_INTEGER is, and block
      // numbers never reach it.
      var maxBlock = Number.MAX_SAFE_INTEGER;
      var range = IDBKeyRange.bound(
        [store.namespace, fileId, generation, fromBlock == null ? 0 : fromBlock],
        [store.namespace, fileId, generation, toBlock == null ? maxBlock : toBlock]);
      var request = objectStore.getAll(range);
      return new Promise(function (resolve, reject) {
        request.onsuccess = function () { resolve(request.result || []); };
        request.onerror = function () { reject(errorOf('storage_error', '读取索引块失败')); };
      });
    }

    return {
      stage: stage,
      stageBlock: stageBlock,
      publish: publish,
      publishIndex: publishIndex,
      readPublished: readPublished,
      prune: prune,
      replaceRecord: replaceRecord,
      readWorkspace: readWorkspace,
      getBlocks: getBlocks
    };
  }

  return {
    LAYERS: LAYERS,
    validatePlan: validatePlan,
    watermarkOf: watermarkOf,
    prunableGenerations: prunableGenerations,
    applyRevision: applyRevision,
    createIndexer: createIndexer
  };
});
