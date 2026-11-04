/*
 * details.js — detail drawer data access with lazy raw and snapshot-bound
 * navigation (R6-028).
 *
 * The drawer is an overlay, not a layout change: opening it must not resize the
 * reading area or the timeline. This module therefore never touches layout — it
 * only answers "what is in this record" and "where does raw text come from".
 *
 * Three refusals that matter:
 *
 *   navigation stays inside the snapshot that opened the drawer. Pressing "next"
 *   past the last hit of a search must not silently jump into unrelated rows.
 *   a modified record (version bumped) is reported as `stale_record`, not shown
 *   as if it were the version the user clicked.
 *   a large raw range is loaded in chunks and reports what is still pending,
 *   instead of truncating and looking complete.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBDetails = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULT_RAW_CHUNK = 512 * 1024;
  var DEFAULT_FIELD_BATCH = 40;

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /**
   * @param {object} options
   *   provider   ReaderProvider
   *   rawChunk   bytes per raw request (default 512 KiB, well under the 4 MiB cap)
   */
  function createDetails(options) {
    options = options || {};
    var provider = options.provider;
    if (!provider) throw errorOf('no_provider', '详情需要 Provider');
    var rawChunk = options.rawChunk || DEFAULT_RAW_CHUNK;
    var stats = { opened: 0, raw_requests: 0, raw_bytes: 0, stale: 0, revoked: 0 };

    /**
     * @param {object} ref     { file_id, generation, ordinal, version }
     * @param {object} context { snapshot: { items: ordinals[] } | null }
     */
    function open(ref, context) {
      if (!ref || ref.file_id == null || ref.ordinal == null) {
        return Promise.reject(errorOf('bad_ref', '缺少 RecordRef'));
      }
      stats.opened += 1;
      return provider.detail(ref).then(function (detail) {
        var findings = detail.findings || [];
        return {
          ref: {
            file_id: detail.file_id, generation: detail.generation,
            ordinal: detail.ordinal, version: detail.version
          },
          header: detail.header,
          body: detail.body || [],
          // A traceback is part of the body; nothing here limits its length.
          body_lines: detail.body ? detail.body.length : 0,
          raw_loaded: 0,
          raw_total: detail.raw_size == null ? null : detail.raw_size,
          findings: findings,
          snapshot: context && context.snapshot ? context.snapshot : null
        };
      }).catch(function (error) {
        if (error && (error.code === 'stale_record' || error.code === 'version_conflict')) stats.stale += 1;
        if (error && (error.code === 'forbidden' || error.code === 'unauthorized')) stats.revoked += 1;
        throw error;
      });
    }

    /** Ranges to fetch for a full raw read, in order. */
    function rawPlan(total) {
      if (total == null || total <= 0) return [];
      var plans = [];
      for (var offset = 0; offset < total; offset += rawChunk) {
        plans.push({ offset: offset, length: Math.min(rawChunk, total - offset) });
      }
      return plans;
    }

    /**
     * Load raw text for a record incrementally. `onChunk` receives decoded text
     * as it arrives, so a 4 MiB record can render progressively.
     */
    function loadRaw(ref, options2) {
      options2 = options2 || {};
      var decoder = new TextDecoder('utf-8');
      var loaded = 0;
      var total = options2.total;
      var plans = rawPlan(total);
      var parts = [];

      function step(index) {
        if (index >= plans.length) return Promise.resolve({ text: parts.join(''), bytes: loaded, complete: true });
        if (options2.signal && options2.signal.aborted) {
          return Promise.reject(errorOf('cancelled', '原文加载已取消'));
        }
        var plan = plans[index];
        stats.raw_requests += 1;
        return provider.readRaw({
          file_id: ref.file_id, generation: ref.generation,
          offset: plan.offset, length: plan.length
        }).then(function (chunk) {
          var text = decoder.decode(chunk.bytes, { stream: true });
          parts.push(text);
          loaded += chunk.bytes.length;
          stats.raw_bytes += chunk.bytes.length;
          if (options2.onChunk) {
            options2.onChunk({ text: text, loaded: loaded, total: total, pending: total - loaded });
          }
          return step(index + 1);
        });
      }

      return step(0).then(function (result) {
        var tail = decoder.decode();
        if (tail) parts.push(tail);
        return { text: parts.join(''), bytes: loaded, complete: true };
      });
    }

    /** Split an 18-field record into viewport-sized batches. */
    function fieldBatches(fields, size) {
      var batch = size || DEFAULT_FIELD_BATCH;
      var list = fields || [];
      var out = [];
      for (var i = 0; i < list.length; i += batch) out.push(list.slice(i, i + batch));
      return out;
    }

    /**
     * Next/previous record inside the snapshot that opened the drawer.
     * Returns null at the boundary instead of wrapping into the file.
     */
    function navigate(drawer, delta, context) {
      if (!drawer || !drawer.ref) return null;
      var snapshot = context && context.snapshot ? context.snapshot : drawer.snapshot;
      if (!snapshot || !snapshot.items || !snapshot.items.length) return null;
      var index = snapshot.items.indexOf(drawer.ref.ordinal);
      if (index === -1) return null;
      var next = index + delta;
      if (next < 0 || next >= snapshot.items.length) return null;
      return { ordinal: snapshot.items[next], index: next, total: snapshot.items.length };
    }

    return {
      open: open,
      rawPlan: rawPlan,
      loadRaw: loadRaw,
      fieldBatches: fieldBatches,
      navigate: navigate,
      stats: function () { var copy = {}; for (var key in stats) copy[key] = stats[key]; return copy; }
    };
  }

  return {
    DEFAULT_RAW_CHUNK: DEFAULT_RAW_CHUNK,
    createDetails: createDetails
  };
});
