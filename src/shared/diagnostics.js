/*
 * diagnostics.js — opt-in, local-only timing instrumentation for the R6 reader.
 *
 * Rules that the tests enforce:
 *   1. A report never contains log text, credentials, full search terms or file
 *      names. Anything free-form goes through redact*() first.
 *   2. Timing is reported per phase, so a slow search is never blamed on the
 *      regex engine when the cost was cloning 650k records.
 *   3. The recorder is inert unless started; it keeps at most 1000 samples per
 *      phase so instrumentation cannot become the memory problem.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBDiagnostics = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var SCHEMA = 'logbench.diagnostics/1';
  var MAX_SAMPLES = 1000;
  var PHASES = [
    'read', 'decode', 'framing', 'rules', 'postmessage',
    'index', 'match', 'highlight', 'render', 'storage', 'queue'
  ];

  function nowMs() {
    if (typeof performance !== 'undefined' && typeof performance.now === 'function') return performance.now();
    return Date.now();
  }

  /* Non-cryptographic 32-bit FNV-1a: stable correlation tag, not a secret. */
  function hash(text) {
    var value = 0x811c9dc5;
    var source = String(text == null ? '' : text);
    for (var i = 0; i < source.length; i++) {
      value ^= source.charCodeAt(i);
      value = (value + ((value << 1) + (value << 4) + (value << 7) + (value << 8) + (value << 24))) >>> 0;
    }
    return ('00000000' + value.toString(16)).slice(-8);
  }

  function redactText(text) {
    var source = String(text == null ? '' : text);
    return { length: source.length, tag: hash(source) };
  }

  function redactPath(path) {
    var source = String(path == null ? '' : path);
    var slash = Math.max(source.lastIndexOf('/'), source.lastIndexOf('\\'));
    var name = source.slice(slash + 1);
    var dot = name.lastIndexOf('.');
    return {
      ext: dot > 0 ? name.slice(dot + 1).toLowerCase() : '',
      tag: hash(source),
      length: source.length
    };
  }

  function redactQuery(text) {
    var source = String(text == null ? '' : text);
    return {
      length: source.length,
      tokens: source.trim() === '' ? 0 : source.trim().split(/\s+/).length,
      tag: hash(source)
    };
  }

  function percentile(sorted, ratio) {
    if (!sorted.length) return 0;
    var index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(ratio * sorted.length) - 1));
    return sorted[index];
  }

  function summarize(samples) {
    if (!samples.length) return { calls: 0, total_ms: 0, max_ms: 0, p50_ms: 0, p95_ms: 0, mean_ms: 0 };
    var sorted = samples.slice().sort(function (a, b) { return a - b; });
    var total = 0;
    for (var i = 0; i < samples.length; i++) total += samples[i];
    return {
      calls: samples.length,
      total_ms: Math.round(total * 10) / 10,
      max_ms: Math.round(sorted[sorted.length - 1] * 10) / 10,
      p50_ms: Math.round(percentile(sorted, 0.5) * 10) / 10,
      p95_ms: Math.round(percentile(sorted, 0.95) * 10) / 10,
      mean_ms: Math.round((total / samples.length) * 10) / 10
    };
  }

  function createRecorder(options) {
    options = options || {};
    var clock = options.now || nowMs;
    var enabled = options.enabled !== false;
    var samples = {};
    var counters = {};
    var bytes = {};
    var marks = {};
    var gauges = {};
    var open = {};
    var longTasks = { count: 0, max_ms: 0, total_ms: 0 };
    var redactions = [];
    var startedAt = null;

    function bucket(name) {
      if (!samples[name]) samples[name] = [];
      return samples[name];
    }

    function push(name, duration) {
      var list = bucket(name);
      if (list.length < MAX_SAMPLES) list.push(duration);
    }

    return {
      get enabled() { return enabled; },
      now: clock,
      start: function () { startedAt = clock(); return this; },
      begin: function (phase) { if (enabled) open[phase] = clock(); return this; },
      end: function (phase) {
        if (!enabled || open[phase] === undefined) return 0;
        var duration = clock() - open[phase];
        delete open[phase];
        push(phase, duration);
        return duration;
      },
      span: function (phase, fn) {
        if (!enabled) return fn();
        var started = clock();
        try {
          return fn();
        } finally {
          push(phase, clock() - started);
        }
      },
      count: function (key, amount) {
        if (!enabled) return this;
        counters[key] = (counters[key] || 0) + (amount === undefined ? 1 : amount);
        return this;
      },
      addBytes: function (key, amount) {
        if (!enabled) return this;
        bytes[key] = (bytes[key] || 0) + (amount || 0);
        return this;
      },
      mark: function (key, value) {
        if (!enabled) return this;
        marks[key] = value;
        return this;
      },
      gauge: function (key, value) {
        if (!enabled) return this;
        gauges[key] = value;
        return this;
      },
      longTask: function (duration) {
        if (!enabled) return this;
        longTasks.count += 1;
        longTasks.max_ms = Math.max(longTasks.max_ms, duration);
        longTasks.total_ms += duration;
        return this;
      },
      redact: function (kind, value) {
        if (enabled) redactions.push(kind);
        if (kind === 'path') return redactPath(value);
        if (kind === 'query') return redactQuery(value);
        return redactText(value);
      },
      report: function () {
        var phases = {};
        for (var name in samples) phases[name] = summarize(samples[name]);
        var missing = [];
        for (var i = 0; i < PHASES.length; i++) if (!samples[PHASES[i]]) missing.push(PHASES[i]);
        return {
          schema: SCHEMA,
          enabled: enabled,
          started_at_ms: startedAt,
          duration_ms: startedAt === null ? 0 : Math.round((clock() - startedAt) * 10) / 10,
          phases: phases,
          phases_never_exercised: missing,
          counters: counters,
          bytes: bytes,
          marks: marks,
          gauges: gauges,
          long_tasks: {
            count: longTasks.count,
            max_ms: Math.round(longTasks.max_ms * 10) / 10,
            total_ms: Math.round(longTasks.total_ms * 10) / 10
          },
          redacted_fields: redactions.length,
          note: '本报告不含日志原文、凭据、完整搜索词或文件名；只含长度与关联标签。'
        };
      },
      reset: function () {
        samples = {}; counters = {}; bytes = {}; marks = {}; gauges = {};
        open = {}; longTasks = { count: 0, max_ms: 0, total_ms: 0 }; redactions = []; startedAt = null;
        return this;
      }
    };
  }

  /* Budget accounting: the 1800ms style deadline must report where it went. */
  function createBudget(limitMs, clock) {
    clock = clock || nowMs;
    var started = clock();
    return {
      limit_ms: limitMs,
      elapsed: function () { return clock() - started; },
      remaining: function () { return Math.max(0, limitMs - (clock() - started)); },
      expired: function () { return (clock() - started) >= limitMs; },
      /* 'exhausted' must be reported as partial, never as zero matches. */
      verdict: function () { return (clock() - started) >= limitMs ? 'exhausted' : 'complete'; }
    };
  }

  return {
    SCHEMA: SCHEMA,
    PHASES: PHASES.slice(),
    createRecorder: createRecorder,
    createBudget: createBudget,
    summarize: summarize,
    redactText: redactText,
    redactPath: redactPath,
    redactQuery: redactQuery,
    hash: hash
  };
});
