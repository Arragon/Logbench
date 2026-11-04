/*
 * search-controller.js — debounce, IME, versioning and honest result wording (R6-025).
 *
 * The wording is the feature. Three sentences that look similar are not:
 *
 *   "至少 1,240 条（已扫描 34%）"   a scan still running; more may exist
 *   "1,240 条"                      the scan finished; this is the total
 *   "0 条"                           only allowed after a finished scan
 *   "已取消（扫描 34%），未完成"     cancelled is not zero either
 *
 * Everything else here exists to keep those sentences true: 250ms debounce, Enter
 * submits immediately, a composition window is never submitted mid-IME, and every
 * request carries a version so a slower older reply cannot replace a newer one.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBSearchController = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = { debounceMs: 250, pollMs: 120 };

  function createSearchController(options) {
    options = options || {};
    var provider = options.provider;
    if (!provider) throw new Error('search controller needs a provider');
    var now = options.now || function () { return Date.now(); };
    var schedule = options.schedule || function (fn, ms) { return setTimeout(fn, ms || 0); };
    var unschedule = options.cancel || function (handle) {
      if (handle && typeof clearTimeout === 'function') clearTimeout(handle);
    };
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];
    var onChange = options.onChange || function () {};

    var version = 0;
    var composing = false;
    var state = {
      phase: 'idle',              // idle | typing | searching | done | paused | cancelled | partial | error
      query: '',
      result: null,
      previous: null,             // last finished result, kept visible while searching
      error: null,
      submitted_at: 0,
      finished_at: 0
    };

    function emit() { onChange(publicState()); }

    var timer = null;
    var timerToken = 0;

    function cancelTimer() {
      if (timer != null) { unschedule(timer); timer = null; }
      timerToken += 1;
    }

    function publicState() {
      return {
        phase: state.phase,
        query: state.query,
        result: state.result,
        previous: state.previous,
        error: state.error,
        version: version,
        composing: composing
      };
    }

    /** Human wording. Kept next to the state so it cannot drift from it. */
    function describe(snapshot) {
      var value = snapshot || publicState();
      var result = value.result;
      if (value.phase === 'idle') return { text: '输入关键词开始搜索', relation: null };
      if (value.phase === 'typing') {
        return { text: result ? '上一份结果：' + describeResult(result).text : '等待输入完成', relation: result ? describeResult(result).relation : null, stale: !!result };
      }
      if (value.phase === 'searching') {
        var shown = value.previous || result;
        return {
          text: shown ? '搜索中，当前显示上一份结果：' + describeResult(shown).text : '搜索中…',
          relation: shown ? describeResult(shown).relation : null,
          stale: !!shown
        };
      }
      if (value.phase === 'error') return { text: '查询失败：' + (value.error && value.error.message || '未知错误'), relation: 'unknown' };
      if (!result) return { text: '没有结果', relation: null };
      return describeResult(result);
    }

    function describeResult(result) {
      var count = result.count || { relation: 'unknown', matched: null };
      var coverage = result.coverage || {};
      var percent = coverage.total_blocks
        ? Math.round((coverage.scanned_blocks / coverage.total_blocks) * 100)
        : (coverage.complete ? 100 : null);

      if (result.state === 'cancelled') {
        return { text: '已取消' + (percent == null ? '' : '（已扫描 ' + percent + '%）') + '，结果不完整',
                 relation: 'unknown', incomplete: true };
      }
      if (result.state === 'paused' || result.state === 'partial') {
        return {
          text: (result.state === 'paused' ? '已暂停' : '扫描未完成') +
            (percent == null ? '' : '（已扫描 ' + percent + '%）') +
            (count.matched == null ? '' : '，至少 ' + count.matched + ' 条'),
          relation: 'at_least',
          incomplete: true,
          resumable: result.state === 'paused'
        };
      }
      if (count.relation === 'exact') {
        return { text: count.matched + ' 条', relation: 'exact' };
      }
      if (count.relation === 'at_least') {
        return { text: '至少 ' + count.matched + ' 条' + (percent == null ? '' : '（已扫描 ' + percent + '%）'),
                 relation: 'at_least', incomplete: true };
      }
      return { text: '结果数未知' + (percent == null ? '' : '（已扫描 ' + percent + '%）'), relation: 'unknown' };
    }

    /** Typing. While an IME composition is open, nothing is submitted. */
    function input(text) {
      state.query = text;
      state.phase = text ? 'typing' : 'idle';
      cancelTimer();
      if (composing) { emit(); return; }
      if (!text) { state.result = null; emit(); return; }
      var token = timerToken;
      timer = schedule(function () { if (token === timerToken) submit(); }, settings.debounceMs);
      emit();
    }

    function compositionStart() { composing = true; cancelTimer(); emit(); }
    function compositionEnd(text) {
      composing = false;
      state.query = text == null ? state.query : text;
      if (state.query) submit();
      else { state.phase = 'idle'; emit(); }
    }

    function submit() {
      cancelTimer();
      var text = state.query;
      if (!text || !text.trim()) { state.phase = 'idle'; emit(); return Promise.resolve(null); }
      version += 1;
      var mine = version;
      state.phase = 'searching';
      state.error = null;
      if (state.result) state.previous = state.result;
      state.submitted_at = now();
      emit();

      // The base spec is read at submit time: the open file can change between
      // keystrokes, and searching the previous file silently would be worse than
      // an error.
      var base = options.getBaseSpec ? options.getBaseSpec() : (options.baseSpec || {});
      if (!base || base.file_id == null) {
        state.phase = 'idle';
        emit();
        return Promise.resolve(null);
      }
      return provider.search(Object.assign({}, base, { text: text }))
        .then(function (result) {
          if (mine !== version) return null;   // a newer keystroke owns the UI
          state.result = result;
          state.finished_at = now();
          state.phase = result.state === 'paused' ? 'paused'
            : result.state === 'cancelled' ? 'cancelled'
            : result.state === 'partial' ? 'partial' : 'done';
          emit();
          if (state.phase === 'searching' || result.state === 'running' || result.state === 'queued') {
            return poll(result.query_id);
          }
          return result;
        })
        .catch(function (error) {
          if (mine !== version) return null;
          state.phase = 'error';
          state.error = { code: error.code || 'search_failed', message: error.message };
          emit();
          return null;
        });
    }

    /** Poll a progressive query until it leaves the running state. */
    function poll(queryId) {
      var mine = version;
      return new Promise(function (resolve) {
        function tick() {
          if (mine !== version) { resolve(null); return; }
          provider.poll(queryId).then(function (result) {
            if (mine !== version) { resolve(null); return; }
            state.result = result;
            if (result.state === 'running' || result.state === 'queued') {
              schedule(tick, settings.pollMs);
              emit();
              return;
            }
            state.phase = result.state === 'done' ? 'done'
              : result.state === 'paused' ? 'paused'
              : result.state === 'partial' ? 'partial'
              : result.state === 'cancelled' ? 'cancelled' : 'done';
            state.finished_at = now();
            emit();
            resolve(result);
          }, function (error) {
            if (mine !== version) { resolve(null); return; }
            state.phase = 'error';
            state.error = { code: error.code, message: error.message };
            emit();
            resolve(null);
          });
        }
        schedule(tick, settings.pollMs);
      });
    }

    function cancel() {
      var result = state.result;
      if (!result || !result.query_id) return Promise.resolve(null);
      return provider.cancel(result.query_id).then(function (cancelled) {
        state.result = cancelled;
        state.phase = 'cancelled';
        emit();
        return cancelled;
      });
    }

    function resume() {
      var result = state.result;
      if (!result || !result.resume_token) return Promise.resolve(null);
      state.phase = 'searching';
      emit();
      return provider.resume(result.query_id, result.resume_token).then(function (next) {
        state.result = next;
        if (next.state === 'running' || next.state === 'queued') return poll(next.query_id);
        state.phase = next.state === 'done' ? 'done' : 'paused';
        emit();
        return next;
      });
    }

    function retry() { return submit(); }

    return {
      input: input,
      submit: submit,
      compositionStart: compositionStart,
      compositionEnd: compositionEnd,
      cancel: cancel,
      resume: resume,
      retry: retry,
      state: publicState,
      describe: describe,
      describeResult: describeResult,
      destroy: cancelTimer
    };
  }

  return { DEFAULTS: DEFAULTS, createSearchController: createSearchController };
});
