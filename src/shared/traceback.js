/*
 * traceback.js — Traceback aggregation for R6 (R6-010).
 *
 * Design constraints that come straight from the ticket:
 *   - a chain is only formed by an explicit "direct cause" / "During handling"
 *     marker; two exceptions printed back-to-back stay separate;
 *   - frames keep file / line / function / source text, and the source text is
 *     flagged as "from log" — it is never treated as the real file content;
 *   - a traceback without a terminal exception is `truncated`, not "no error";
 *   - non-Python stacks (Java/Go/JS) are recognised and marked instead of being
 *     silently half-parsed;
 *   - prefixed output (`| Traceback ...`, `+ Exception Group ...`) parses the
 *     same as unprefixed output.
 *
 * UMD-lite: window.LBTraceback in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBTraceback = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var TRACEBACK = /^(?:\+ )?Traceback \(most recent call last\):$/;
  var FRAME = /^File "(.*)", line (\d+)(?:, in (.+?))?(?: \[as .*?\])?$/;
  var EXCEPTION = /^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Iteration|Warning)?)(?::\s?(.*))?$/;
  var CAUSE = /^The above exception was the direct cause of the following exception:$/;
  var CONTEXT = /^During handling of the above exception, another exception occurred:$/;
  var GROUP = /^(?:\+ )?Exception Group Traceback/;

  /* Peel leading indentation plus a single `|`/`+` gutter, repeatedly. */
  function stripPrefix(line) {
    var text = line;
    var prefixed = false;
    for (var guard = 0; guard < 4; guard++) {
      var match = /^(\s*)([|+])?\s?(.*)$/.exec(text);
      if (!match || !match[2]) break;
      text = match[3];
      prefixed = true;
    }
    return { text: text, prefixed: prefixed, indent: indentOf(text) };
  }

  function indentOf(text) {
    var match = /^(\s*)/.exec(text);
    return match ? match[1].length : 0;
  }

  function looksNonPython(text) {
    return /^\s+at [\w$.]+\(/.test(text) ||
      /^panic: /m.test(text) ||
      /^\s+at .*\(.*:\d+:\d+\)/.test(text) ||
      /^\s+at Object\./.test(text);
  }

  function emptyTrace(firstLine) {
    return {
      index: 0,
      status: {
        complete: false,
        truncated: false,
        prefixed: false,
        exception_group: false,
        non_python: false,
        reason: null
      },
      frames: [],
      exceptions: [],
      markers: [],
      spans: [],
      first_line: firstLine,
      last_line: firstLine
    };
  }

  /**
   * Parse one record's text into one or more traces.
   * @param {string} message header message
   * @param {string[]} body continuation lines
   * @param {{firstLine?:number, provisional?:boolean}} options
   */
  function parse(message, body, options) {
    options = options || {};
    var lines = [message == null ? '' : String(message)].concat(body || []);
    var baseLine = options.firstLine || 1;
    var traces = [];
    var current = null;
    var pendingFrame = null;

    function finish(reason) {
      if (!current) return;
      current.last_line = baseLine + lines.length - 1;
      if (!current.status.complete) {
        current.status.truncated = true;
        current.status.reason = current.status.reason || reason || 'incomplete';
      } else if (reason) {
        current.status.reason = reason;
      }
      traces.push(current);
      current = null;
      pendingFrame = null;
    }

    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i];
      var stripped = stripPrefix(raw);
      var text = stripped.text;
      var indent = indentOf(text);
      var trimmed = text.trim();
      var lineNo = baseLine + i;

      if (TRACEBACK.test(trimmed) || GROUP.test(trimmed)) {
        var isGroup = GROUP.test(trimmed);
        if (current && current.chained && !current.status.complete) {
          // The marker promised another exception: this header continues the same trace.
          current.chained = false;
          current.status.prefixed = current.status.prefixed || stripped.prefixed;
          current.spans.push({ kind: 'traceback', first_line: lineNo, last_line: lineNo });
          if (isGroup) {
            current.status.exception_group = true;
            current.markers.push({ kind: 'exception-group', line: lineNo });
          }
          continue;
        }
        if (current && current.exceptions.length > 0 && !current.status.complete) {
          // A second header without any marker means the previous trace never finished.
          finish('restarted');
        }
        if (current && current.exceptions.length > 0 && current.status.complete) {
          // Adjacent, independent traceback: keep them separate.
          finish('separate-traceback');
        }
        if (!current) {
          current = emptyTrace(lineNo);
          current.index = traces.length;
          current.spans.push({ kind: 'traceback', first_line: lineNo, last_line: lineNo });
        }
        current.status.prefixed = current.status.prefixed || stripped.prefixed;
        if (isGroup) {
          current.status.exception_group = true;
          current.markers.push({ kind: 'exception-group', line: lineNo });
        }
        continue;
      }

      if (!current) {
        if (CAUSE.test(trimmed) || CONTEXT.test(trimmed)) {
          // A marker without a traceback belongs to whatever came before it.
          continue;
        }
        if (FRAME.test(trimmed) || (indent > 0 && looksNonPython(text))) {
          current = emptyTrace(lineNo);
          current.index = traces.length;
        } else if (EXCEPTION.test(trimmed) && /:\s|Error|Exception|Warning/.test(trimmed)) {
          // A bare terminal exception with no traceback is still a trace.
          current = emptyTrace(lineNo);
          current.index = traces.length;
          current.status.reason = 'no-traceback';
        } else {
          continue;
        }
      }

      current.status.prefixed = current.status.prefixed || stripped.prefixed;
      current.last_line = lineNo;

      if (CAUSE.test(trimmed) || CONTEXT.test(trimmed)) {
        current.markers.push({ kind: CAUSE.test(trimmed) ? 'cause' : 'context', line: lineNo });
        current.status.complete = false;
        current.chained = true;
        continue;
      }

      var frame = FRAME.exec(trimmed);
      if (frame) {
        pendingFrame = {
          index: current.frames.length,
          file: frame[1],
          line: Number(frame[2]),
          fn: frame[3] || '<module>',
          source: null,
          source_from_log: false,
          span: { first_line: lineNo, last_line: lineNo }
        };
        current.frames.push(pendingFrame);
        current.spans.push({ kind: 'frame', first_line: lineNo, last_line: lineNo });
        continue;
      }

      if (pendingFrame && indent > 0 && !EXCEPTION.test(trimmed) && !looksNonPython(text)) {
        if (pendingFrame.source === null) {
          pendingFrame.source = trimmed;
          pendingFrame.source_from_log = true;
          pendingFrame.span.last_line = lineNo;
          continue;
        }
      }
      pendingFrame = null;

      if (looksNonPython(text)) {
        current.status.non_python = true;
        current.status.reason = 'non-python';
        continue;
      }

      var exception = EXCEPTION.exec(trimmed);
      if (exception && indent === 0 && !/^File /.test(trimmed)) {
        current.exceptions.push({
          index: current.exceptions.length,
          type: exception[1],
          message: exception[2] === undefined ? '' : exception[2],
          line: lineNo,
          has_message: exception[2] !== undefined
        });
        current.spans.push({ kind: 'exception', first_line: lineNo, last_line: lineNo });
        current.status.complete = true;
        continue;
      }
    }

    if (current) {
      current.status.provisional = options.provisional === true;
      if (current.exceptions.length === 0) {
        current.status.complete = false;
        finish('no-terminal-exception');
      } else {
        current.status.complete = true;
        finish(null);
      }
    }

    traces.forEach(function (trace, index) {
      trace.index = index;
      trace.chain = buildChain(trace);
      trace.summary = summarize(trace);
    });
    return traces;
  }

  /* Links are only created by explicit markers: one marker = one link. */
  function buildChain(trace) {
    var links = [];
    for (var i = 1; i < trace.exceptions.length; i++) {
      var marker = trace.markers.filter(function (item) {
        return item.line > trace.exceptions[i - 1].line && item.line < trace.exceptions[i].line;
      })[0];
      links.push({
        from: i - 1,
        to: i,
        relation: marker ? marker.kind : 'adjacent',
        explained: Boolean(marker)
      });
    }
    return { links: links, size: trace.exceptions.length };
  }

  function summarize(trace) {
    var primary = trace.exceptions[0] || null;
    return {
      frames: trace.frames.length,
      exceptions: trace.exceptions.length,
      primary_type: primary ? primary.type : null,
      terminal_type: trace.exceptions.length ? trace.exceptions[trace.exceptions.length - 1].type : null,
      complete: trace.status.complete,
      truncated: trace.status.truncated,
      non_python: trace.status.non_python,
      exception_group: trace.status.exception_group,
      prefixed: trace.status.prefixed
    };
  }

  /** Aggregate a framer record ({message, body, provisional}). */
  function aggregate(record) {
    if (!record) return { traces: [], has_trace: false };
    var traces = parse(record.message, record.body, {
      firstLine: record.lines ? record.lines.first : 1,
      provisional: record.provisional === true
    });
    return {
      traces: traces,
      has_trace: traces.length > 0,
      incomplete: traces.some(function (trace) { return !trace.status.complete; })
    };
  }

  return {
    parse: parse,
    aggregate: aggregate,
    buildChain: buildChain,
    summarize: summarize,
    stripPrefix: stripPrefix,
    looksNonPython: looksNonPython
  };
});
