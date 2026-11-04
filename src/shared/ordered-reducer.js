/*
 * ordered-reducer.js — commit batches in source order (R6-039).
 *
 * Parallel workers finish in whatever order they finish. What must not happen:
 *
 *   * a checkpoint that skips ahead of a batch that has not been committed, so a
 *     crash leaves a hole nobody re-reads;
 *   * a retried or duplicated batch committed twice;
 *   * a batch from an older fencing token committed after a newer one took over;
 *   * an out-of-order buffer that grows without bound because one batch is slow.
 *
 * The reducer keeps `nextSeq`, commits only contiguous batches, and reports
 * `pressure` once the buffer is half full so the producer can slow down *before*
 * the buffer is full rather than after.
 *
 * UMD-lite: window.LBOrderedReducer in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBOrderedReducer = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function createOrderedReducer(options) {
    options = options || {};
    var maxBuffer = options.maxBuffer || 16;
    var token = options.token == null ? null : options.token;
    var nextSeq = options.startSeq || 0;
    var buffer = new Map();
    var stats = { committed: 0, buffered: 0, duplicates: 0, stale: 0, old_token: 0,
      pressure: 0, overflow: 0, max_buffered: 0 };
    var lastBoundary = null;

    function accept(batch) {
      if (!batch || typeof batch.seq !== 'number') {
        throw new Error('batch needs a numeric seq');
      }
      if (token !== null && batch.token != null && batch.token < token) {
        stats.old_token += 1;
        return { commit: [], dropped: 'old_token' };
      }
      if (batch.seq < nextSeq) {
        stats.stale += 1;
        return { commit: [], dropped: 'stale' };
      }
      if (buffer.has(batch.seq)) {
        stats.duplicates += 1;
        return { commit: [], dropped: 'duplicate' };
      }
      var commit = [];
      if (batch.seq === nextSeq) {
        commit.push(batch);
        nextSeq += 1;
        lastBoundary = batch.boundary || lastBoundary;
        // Drain everything that became contiguous.
        while (buffer.has(nextSeq)) {
          var ready = buffer.get(nextSeq);
          buffer.delete(nextSeq);
          commit.push(ready);
          lastBoundary = ready.boundary || lastBoundary;
          nextSeq += 1;
        }
      } else {
        buffer.set(batch.seq, batch);
        if (buffer.size > stats.max_buffered) stats.max_buffered = buffer.size;
        if (buffer.size > maxBuffer) {
          stats.overflow += 1;
          throw new Error('ordered buffer overflow at seq ' + batch.seq);
        }
      }
      stats.committed += commit.length;
      stats.buffered = buffer.size;
      if (buffer.size * 2 >= maxBuffer) stats.pressure += 1;
      return { commit: commit, dropped: null, pressure: buffer.size * 2 >= maxBuffer };
    }

    function adoptToken(value) {
      // A takeover with a higher fencing token invalidates earlier tokens; it does
      // not retroactively commit what they buffered.
      if (token === null || value > token) {
        token = value;
        return true;
      }
      return false;
    }

    return {
      accept: accept,
      adoptToken: adoptToken,
      token: function () { return token; },
      nextSeq: function () { return nextSeq; },
      pending: function () { return buffer.size; },
      boundary: function () { return lastBoundary; },
      checkpoint: function () { return { seq: nextSeq - 1, boundary: lastBoundary }; },
      pressure: function () { return buffer.size * 2 >= maxBuffer; },
      stats: function () { return JSON.parse(JSON.stringify(stats)); }
    };
  }

  return { createOrderedReducer: createOrderedReducer };
});
