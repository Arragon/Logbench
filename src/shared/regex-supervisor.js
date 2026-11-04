/*
 * regex-supervisor.js — terminates a regex Worker that stops yielding (R6-024).
 *
 * Two clocks, deliberately separate:
 *
 *   sliceMs   2s per slice, enforced from the outside by `terminate()`. Hitting
 *             this means the pattern is pathological for this input; the slice's
 *             records are reported as **not scanned**, and the job ends `partial`.
 *   budgetMs  30s of total active work. Hitting this is normal for a large but
 *             healthy search: the job `paused` with a resume token, and resuming
 *             continues the same scan. It must never be reported as a regex error.
 *
 * The supervisor never reuses a terminated Worker: a Worker killed mid-`test()`
 * may hold a half-built backtracking stack. It spawns a fresh one for the next
 * slice and lets the old one die.
 *
 * It also refuses to report a killed slice as an exact zero. `state: 'partial'`
 * carries `coverage.complete = false`, the pattern id, and the record range that
 * was not scanned.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBRegexSupervisor = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = {
    sliceMs: 2000,
    budgetMs: 30000,
    recordsPerSlice: 2000,
    maxHits: 2000
  };

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function hash(text) {
    var value = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      value ^= text.charCodeAt(i);
      value = (value + ((value << 1) + (value << 4) + (value << 7) + (value << 8) + (value << 24))) >>> 0;
    }
    return value.toString(16);
  }

  /**
   * @param {object} options
   *   spawn()  -> { post(message), terminate(), onMessage(fn) }  (a real Worker wrapper)
   *   now()    injectable clock
   */
  function createSupervisor(options) {
    options = options || {};
    var spawn = options.spawn;
    if (typeof spawn !== 'function') throw errorOf('no_spawn', '正则监督器需要一个 Worker 工厂');
    var now = options.now || function () { return Date.now(); };
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];

    var stats = { spawned: 0, terminated: 0, slices: 0, budget_pauses: 0, rejected: 0 };

    /**
     * @param {object} request
     *   pattern, flags, texts[], ordinals[], token (pattern identity for logs)
     */
    function runSlice(request) {
      return new Promise(function (resolve) {
        var worker = spawn();
        stats.spawned += 1;
        var settled = false;
        var timer = null;

        function finish(result) {
          if (settled) return;
          settled = true;
          if (timer) clearTimeout(timer);
          resolve(result);
        }

        worker.onMessage(function (message) {
          if (settled || !message || message.id !== request.id) return;
          if (message.ok) {
            finish({ ok: true, hits: message.hits || [], count: message.count || 0,
                     scanned: message.scanned || 0, elapsed_ms: message.elapsed_ms || 0 });
          } else {
            stats.rejected += 1;
            finish({ ok: false, error: message.error, scanned: 0 });
          }
        });

        timer = setTimeout(function () {
          // The Worker is not asked to stop; it is destroyed. `terminate()` is the
          // only mechanism that works against a pattern stuck in backtracking.
          stats.terminated += 1;
          try { worker.terminate(); } catch (ignored) { /* already gone */ }
          finish({ ok: false, error: { code: 'slice_timeout', message: '单片超过保护时限，已终止执行' },
                   scanned: 0, terminated: true });
        }, settings.sliceMs);

        try {
          worker.post({
            type: 'run', id: request.id, pattern: request.pattern, flags: request.flags || '',
            texts: request.texts, ordinals: request.ordinals, maxHits: request.maxHits || settings.maxHits
          });
        } catch (error) {
          try { worker.terminate(); } catch (ignored) { /* noop */ }
          finish({ ok: false, error: { code: 'spawn_failed', message: String(error && error.message) }, scanned: 0 });
        }
      });
    }

    /**
     * Scan all texts, one slice at a time. A slice that times out ends the job as
     * `partial`: continuing past an unscanned slice would let the caller report a
     * total that excludes records nobody looked at.
     */
    function scan(request) {
      var texts = request.texts || [];
      var ordinals = request.ordinals || texts.map(function (_, index) { return index; });
      var patternId = request.pattern_id || ('re-' + hash(String(request.pattern) + '|' + (request.flags || '')));
      var started = now();
      var state = {
        pattern_id: patternId,
        matched: 0,
        hits: [],
        scanned_records: 0,
        total_records: texts.length,
        state: 'running',
        coverage: { complete: false, reason: 'in_progress' },
        slices: 0,
        killed: false,
        error: null,
        resume_token: null
      };

      function step(offset) {
        if (offset >= texts.length) {
          state.state = 'done';
          state.coverage = { complete: true, reason: null };
          return Promise.resolve(state);
        }
        if (now() - started >= settings.budgetMs) {
          stats.budget_pauses += 1;
          state.state = 'paused';
          state.coverage = { complete: false, reason: 'budget' };
          state.resume_token = [patternId, offset, state.matched].join(':');
          return Promise.resolve(state);
        }
        var sliceTexts = texts.slice(offset, offset + settings.recordsPerSlice);
        var sliceOrdinals = ordinals.slice(offset, offset + settings.recordsPerSlice);
        var id = patternId + ':' + offset;
        stats.slices += 1;
        return runSlice({ id: id, pattern: request.pattern, flags: request.flags,
                          texts: sliceTexts, ordinals: sliceOrdinals, maxHits: settings.maxHits })
          .then(function (result) {
            if (!result.ok) {
              if (result.terminated || result.error.code === 'slice_timeout') {
                state.killed = true;
                state.state = 'partial';
                state.error = { code: 'slice_timeout', message: result.error.message,
                                pattern_id: patternId, unscanned_from: offset };
                state.coverage = {
                  complete: false,
                  reason: 'terminated',
                  scanned_records: state.scanned_records,
                  unscanned_records: texts.length - offset
                };
                return state;
              }
              state.state = 'failed';
              state.error = { code: result.error.code, message: result.error.message, pattern_id: patternId };
              state.coverage = { complete: false, reason: 'invalid_pattern', scanned_records: state.scanned_records };
              return state;
            }
            state.scanned_records += result.scanned;
            state.matched += result.count;
            for (var i = 0; i < result.hits.length && state.hits.length < settings.maxHits; i++) {
              state.hits.push(result.hits[i]);
            }
            state.slices += 1;
            return step(offset + sliceTexts.length);
          });
      }

      return step(0).then(function (final) {
        final.elapsed_ms = now() - started;
        return final;
      });
    }

    return {
      scan: scan,
      runSlice: runSlice,
      settings: function () { var copy = {}; for (var key in settings) copy[key] = settings[key]; return copy; },
      stats: function () { var copy = {}; for (var key in stats) copy[key] = stats[key]; return copy; }
    };
  }

  return {
    DEFAULTS: DEFAULTS,
    hash: hash,
    createSupervisor: createSupervisor
  };
});
