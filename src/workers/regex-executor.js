/*
 * regex-executor.js — a disposable Worker that runs one regex slice (R6-024).
 *
 * It deliberately contains no yielding loop. That is the design: a catastrophic
 * pattern cannot be interrupted from inside, so the *supervisor* watches it and
 * terminates the whole Worker after a fixed wall-clock slice. A cooperative
 * yield would only work for patterns that cooperate, which is not the case.
 *
 * It never receives a record's raw text for storage, only the strings it must
 * test, and it never evaluates a string as code. The pattern is compiled here,
 * once, by the same JavaScript engine that will run the real query, so match
 * semantics do not change (RE2 would differ on backreferences and lookbehind).
 */
(function () {
  'use strict';

  var scope = typeof self !== 'undefined' ? self : this;

  scope.onmessage = function (event) {
    var message = event.data || {};
    if (message.type !== 'run') return;
    var started = Date.now();
    var out = {
      type: 'result',
      id: message.id,
      ok: true,
      hits: [],
      count: 0,
      scanned: 0,
      error: null,
      elapsed_ms: 0
    };
    try {
      var regex = new RegExp(message.pattern, message.flags || '');
      var texts = message.texts || [];
      var ordinals = message.ordinals || [];
      // A zero-length match is refused, not silently counted: `a*` matches every
      // position and would otherwise inflate counts to the string length.
      var probe = regex.exec('');
      if (probe && probe[0] === '') {
        out.ok = false;
        out.error = { code: 'empty_match', message: '正则可以匹配空串，拒绝执行' };
        regex.lastIndex = 0;
        scope.postMessage(out);
        return;
      }
      regex.lastIndex = 0;
      for (var i = 0; i < texts.length; i++) {
        regex.lastIndex = 0;
        if (regex.test(texts[i])) {
          out.count += 1;
          if (out.hits.length < (message.maxHits || 2000)) out.hits.push(ordinals[i]);
        }
        out.scanned += 1;
      }
    } catch (error) {
      out.ok = false;
      out.error = { code: 'invalid_pattern', message: String(error && error.message || error) };
    }
    out.elapsed_ms = Date.now() - started;
    scope.postMessage(out);
  };
})();
