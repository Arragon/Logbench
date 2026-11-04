/*
 * reader-controller.js — paging, virtual window and anchor keeping (R6-027).
 *
 * The reader never holds a filtered array of the whole file. It holds:
 *
 *   a logical window   [first, last] ordinals that must exist in the DOM
 *   a page cache       a few pages of ≤2000 rows / ≤4 MiB, LRU by bytes
 *   an anchor          record id + the pixel offset of its top inside its row
 *
 * Why the anchor is not "scrollTop": row heights vary (multi-line records), so
 * restoring `scrollTop` after a data update lands on a different record. The
 * anchor is (record_id, offset_y) and is resolved through the current index, so
 * inserting pages above the viewport moves `scrollTop` but not the eye.
 *
 * Jumping to ordinal 40,000 must fetch pages 20..20 (2000 rows/page), not pages
 * 0..19. `pagesFor()` returns exactly the overlapping pages, and the test asserts
 * the fetch list, because "it felt fast" is not evidence.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBReaderController = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = {
    pageSize: 2000,
    maxPageBytes: 4194304,
    windowSize: 200,        // rows kept mounted around the viewport, hard cap < 300
    hardWindowCap: 280,
    overscan: 40,
    rowHeight: 28
  };

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /**
   * @param {object} options
   *   pageSize, windowSize, overscan, rowHeight, maxPageBytes
   */
  function createReader(options) {
    options = options || {};
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];

    var total = 0;
    var pages = new Map();          // pageIndex -> { rows, bytes, lastUsed }
    var pageBytes = 0;
    var clock = 0;
    var anchor = null;              // { record_id, offset_y, ordinal }
    var heights = new Map();        // ordinal -> px, for known rows
    var prefix = null;              // prefix sums of heights, rebuilt on change
    var lastWindow = { first: 0, last: 0 };

    function rowsOfPage(index) {
      var page = pages.get(index);
      return page ? page.rows : null;
    }

    /**
     * Prefix sums of measured heights. Rebuilt lazily: a virtual list changes
     * heights a page at a time, and an O(n) walk per scroll frame at 50k rows is
     * the kind of cost that shows up as jank rather than as a wrong number.
     */
    function rebuildPrefix() {
      prefix = new Float64Array(total + 1);
      for (var i = 0; i < total; i++) {
        prefix[i + 1] = prefix[i] + (heights.get(i) || settings.rowHeight);
      }
    }

    function ensurePrefix() {
      if (!prefix || prefix.length !== total + 1) rebuildPrefix();
      return prefix;
    }

    function totalHeight() {
      if (!total) return 0;
      return ensurePrefix()[total];
    }

    function offsetOf(ordinal) {
      if (ordinal <= 0) return 0;
      return ensurePrefix()[Math.min(ordinal, total)];
    }

    /** Which page indexes overlap [first, last]. */
    function pagesFor(first, last) {
      if (total <= 0) return [];
      var start = Math.max(0, Math.min(first, total - 1));
      var end = Math.max(start, Math.min(last, total - 1));
      var firstPage = Math.floor(start / settings.pageSize);
      var lastPage = Math.floor(end / settings.pageSize);
      var out = [];
      for (var page = firstPage; page <= lastPage; page++) out.push(page);
      return out;
    }

    /** Pages that are needed but not resident, in viewport order. */
    function missingPages(first, last) {
      return pagesFor(first, last).filter(function (page) { return !pages.has(page); });
    }

    function putPage(index, rows, bytes) {
      if (!Array.isArray(rows)) throw errorOf('bad_page', '页数据必须是数组');
      if (rows.length > settings.pageSize) {
        throw errorOf('page_too_large', '单页最多 ' + settings.pageSize + ' 条', { got: rows.length });
      }
      var size = bytes == null ? rows.length * 120 : bytes;
      if (size > settings.maxPageBytes) {
        throw errorOf('page_too_large', '单页数据超过 4 MiB', { bytes: size });
      }
      if (pages.has(index)) pageBytes -= pages.get(index).bytes;
      pages.set(index, { rows: rows, bytes: size, lastUsed: ++clock });
      pageBytes += size;
      evict();
      return { page: index, rows: rows.length, bytes: size };
    }

    function evict() {
      // Keep at most `maxPages` pages, and never evict the page holding the
      // anchor: that is the one the eye is looking at.
      var maxPages = options.maxPages || 12;
      if (pages.size <= maxPages) return;
      var order = [...pages.entries()].sort(function (a, b) { return a[1].lastUsed - b[1].lastUsed; });
      for (var i = 0; i < order.length && pages.size > maxPages; i++) {
        var index = order[i][0];
        var page = pages.get(index);
        if (anchor && Math.floor(anchor.ordinal / settings.pageSize) === index) continue;
        pages.delete(index);
        pageBytes -= page.bytes;
      }
    }

    function setTotal(count) {
      total = count;
      prefix = null;
      lastWindow = { first: 0, last: Math.min(count, settings.windowSize) };
      return { total: total, pages: Math.ceil(total / settings.pageSize) };
    }

    /** Rows that must be in the DOM for this scroll position. */
    function windowFor(scrollTop, viewportHeight) {
      var height = viewportHeight || 600;
      var first = Math.max(0, Math.floor(scrollTop / settings.rowHeight) - settings.overscan);
      var visible = Math.ceil(height / settings.rowHeight) + settings.overscan * 2;
      var size = Math.min(visible, settings.hardWindowCap);
      var last = Math.min(total - 1, first + size - 1);
      if (last - first + 1 > settings.hardWindowCap) last = first + settings.hardWindowCap - 1;
      lastWindow = { first: first, last: last };
      return {
        first: first,
        last: last,
        count: Math.max(0, last - first + 1),
        offset_y: offsetOf(first),
        total_height: totalHeight(),
        missing_pages: missingPages(first, last)
      };
    }

    /** A page index that contains an ordinal, for `seek`. */
    function pageOf(ordinal) {
      return Math.floor(ordinal / settings.pageSize);
    }

    /**
     * Resolve the anchor to a scrollTop under the current data. Used after a
     * query change, a column resize, or reopening the drawer.
     */
    function scrollTopForAnchor(anchorValue) {
      var target = anchorValue || anchor;
      if (!target) return null;
      return offsetOf(target.ordinal) + (target.offset_y || 0);
    }

    function setAnchor(ordinal, offsetY, recordId) {
      anchor = { ordinal: ordinal, offset_y: offsetY || 0, record_id: recordId || null };
      return anchor;
    }

    function anchor() { return anchor; }

    /** Record a measured row height, so the anchor maths uses real geometry. */
    function setRowHeight(ordinal, px) {
      if (px > 0 && heights.get(ordinal) !== px) {
        heights.set(ordinal, px);
        prefix = null;
      }
    }

    return {
      settings: settings,
      setTotal: setTotal,
      total: function () { return total; },
      windowFor: windowFor,
      lastWindow: function () { return lastWindow; },
      pagesFor: pagesFor,
      missingPages: missingPages,
      putPage: putPage,
      rowsOfPage: rowsOfPage,
      pageOf: pageOf,
      setAnchor: setAnchor,
      anchor: anchor,
      scrollTopForAnchor: scrollTopForAnchor,
      setRowHeight: setRowHeight,
      offsetOf: offsetOf,
      totalHeight: totalHeight,
      cache: function () {
        return { pages: pages.size, bytes: pageBytes, indexes: [...pages.keys()].sort(function (a, b) { return a - b; }) };
      }
    };
  }

  return {
    DEFAULTS: DEFAULTS,
    createReader: createReader
  };
});
