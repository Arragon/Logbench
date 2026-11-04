/*
 * highlight-controller.js — marks only for rows that are on screen (R6-026).
 *
 * R5 highlighted by walking the whole result set after every keystroke. With a
 * 50k page that is 50k `computeMarks()` calls for at most ~40 visible rows. This
 * controller takes the visible rows and nothing else, and caches per
 * (record, version, query signature, projection), because the same row is
 * re-rendered dozens of times while scrolling.
 *
 * Two safety rules are enforced here rather than trusted to the caller:
 *
 *   segments   the renderer gets `[{text, term?}]`, never an HTML string, so a
 *              log line containing `<img onerror=...>` is text, not markup.
 *   versions   a record revision or a query change invalidates the cache entry;
 *              old marks cannot be painted onto a new record with the same id.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBHighlightController = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = { maxEntries: 400, maxMarksPerRow: 200 };

  /**
   * @param {object} options
   *   computeMarks(record, ast) -> [{start, end, term}]
   *   textOf(record, projectionVersion) -> string
   */
  function createHighlighter(options) {
    options = options || {};
    var computeMarks = options.computeMarks;
    var textOf = options.textOf;
    if (typeof computeMarks !== 'function') throw new Error('highlighter needs computeMarks');
    if (typeof textOf !== 'function') throw new Error('highlighter needs textOf');
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];

    var cache = new Map();
    var stats = { calls: 0, rows: 0, hits: 0, misses: 0, evictions: 0, rejected: 0 };

    function keyOf(record, context) {
      return [record.record_id || record.ordinal, record.version || 1,
              context.query_signature || '-', context.projection_version || 'v1'].join('|');
    }

    function put(key, value) {
      if (cache.size >= settings.maxEntries) {
        var oldest = cache.keys().next().value;
        cache.delete(oldest);
        stats.evictions += 1;
      }
      cache.set(key, value);
    }

    /**
     * @param {object} record  visible record
     * @param {object} context { ast, query_signature, projection_version }
     */
    function marksFor(record, context) {
      stats.rows += 1;
      if (!context || !context.ast) return { marks: [], text: '' };
      var key = keyOf(record, context);
      var hit = cache.get(key);
      if (hit) {
        stats.hits += 1;
        cache.delete(key);
        cache.set(key, hit);
        return hit;
      }
      stats.misses += 1;
      stats.calls += 1;
      var text = textOf(record, context.projection_version || 'v1');
      var marks = [];
      try {
        marks = computeMarks(record, context.ast, { max_marks: settings.maxMarksPerRow }) || [];
      } catch (error) {
        stats.rejected += 1;
        marks = [];
      }
      var value = { marks: marks, text: text };
      put(key, value);
      return value;
    }

    /** Only the rows passed in are computed; this is the whole point. */
    function forWindow(records, context) {
      var out = [];
      for (var i = 0; i < records.length; i++) {
        var entry = marksFor(records[i], context);
        out.push({ record_id: records[i].record_id || records[i].ordinal, marks: entry.marks, text: entry.text });
      }
      return out;
    }

    /**
     * Split text into segments by marks. Overlapping marks are merged for the
     * text but keep their term ids, so two rules highlighting the same span show
     * two labels without duplicating the characters.
     */
    function segments(text, marks) {
      if (!marks || !marks.length) return [{ text: text }];
      // Sweep over boundaries rather than walking marks: overlapping rules must
      // not duplicate or drop characters, and two rules on the same span must
      // both be reported.
      var points = [0, text.length];
      marks.forEach(function (mark) {
        points.push(Math.max(0, Math.min(text.length, mark.start)));
        points.push(Math.max(0, Math.min(text.length, mark.end)));
      });
      points = points.filter(function (value, index, all) { return all.indexOf(value) === index; })
        .sort(function (a, b) { return a - b; });
      var out = [];
      for (var i = 0; i < points.length - 1; i++) {
        var from = points[i];
        var to = points[i + 1];
        if (to <= from) continue;
        var terms = [];
        marks.forEach(function (mark) {
          if (mark.start <= from && mark.end >= to && terms.indexOf(mark.term) === -1) terms.push(mark.term);
        });
        out.push(terms.length ? { text: text.slice(from, to), terms: terms } : { text: text.slice(from, to) });
      }
      return out.filter(function (segment) { return segment.text !== ''; });
    }

    function clear() { cache.clear(); }

    return {
      marksFor: marksFor,
      forWindow: forWindow,
      segments: segments,
      clear: clear,
      stats: function () {
        var copy = {};
        for (var key in stats) copy[key] = stats[key];
        copy.entries = cache.size;
        copy.cache_hit_rate = stats.rows ? stats.hits / stats.rows : 0;
        return copy;
      }
    };
  }

  return { DEFAULTS: DEFAULTS, createHighlighter: createHighlighter };
});
