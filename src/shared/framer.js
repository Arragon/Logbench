/*
 * framer.js — streaming, byte-exact record framer (R6-009).
 *
 * Why this exists: R5 calls `file.text()` and splits the whole file, so a 1 GiB
 * log dies on the main thread and there is no way to resume. This framer takes
 * Uint8Array chunks, keeps only the current record in memory, and reports exact
 * half-open byte ranges plus 1-based inclusive physical line numbers.
 *
 * Faithfulness rule: the framing decisions (header match, traceback merge,
 * continuation append, unparsed fallback) are the same predicates R5 uses, so
 * golden fixtures produce the same logical records. `tests/r6/framer.test.cjs`
 * compares against the real `parseLocal` extracted from src/app.js.
 *
 * Everything is UMD-lite: window.LBFramer in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBFramer = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* Same header grammar as src/app.js:90 so the two framers agree by construction. */
  var HEADER = /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[(.+):(\d+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(\S+)\(\)(?:[ \t]+<[^>]+>)?[ \t]?(.*)$/;
  var STAMPED = /^\[\d{4}-/;

  function isTrace(line) { return /Traceback \(most recent call last\):/.test(line); }
  function isChain(line) { return /^(?:The above exception|During handling of the above exception)/.test(line.trim()); }
  function isTerminal(line) {
    return /^[A-Za-z_][\w.]*:/.test(line) ||
      /^(?:[A-Za-z_][\w.]*(?:Error|Exception|Interrupt)|SystemExit|GeneratorExit|StopIteration)\s*$/.test(line);
  }
  function isContinuation(line) {
    return isTerminal(line) || !line.trim() || /^\s/.test(line) ||
      /^(?:File ["']|Traceback \(|The above exception|During handling|[A-Za-z_][\w.]*:\s|[\^~]+$|[+|\-]{2,})/.test(line);
  }

  function blankRecord(datasetId, ordinal, line, byteStart) {
    return {
      dataset_id: datasetId,
      record_id: datasetId + '-' + ordinal,
      version: 1,
      ordinal: ordinal,
      bytes: { start: byteStart, end: byteStart },
      lines: { first: line, last: line },
      ts: null,
      level: 'UNPARSED',
      logger: '\u2014',
      pid: '',
      tid: '',
      src: '\u2014',
      src_line: 0,
      fn: 'unparsed',
      message: '',
      body: [],
      body_lines: [],
      raw: [],
      has_trace: false,
      trace_complete: false,
      trace_chain_pending: false,
      published_provisional: false,
      provisional: false
    };
  }

  function createFramer(options) {
    options = options || {};
    var datasetId = options.datasetId || 'dataset';
    var header = options.headerPattern || HEADER;

    var pending = [];              // bytes of the current, unterminated line
    // A resumed import continues where the committed blocks stopped: ordinal,
    // absolute byte offset and physical line number all come from the last
    // checkpoint so `bytes.start` and `lines.first` stay file-absolute.
    var position = options.startPosition || 0;   // absolute byte offset of the next byte
    var lineNo = options.startLine || 0;         // last completed physical line
    var lineStart = position;                    // absolute byte offset where `pending` starts
    var bomChecked = position > 0;
    var ordinal = options.startOrdinal || 0;
    var current = null;            // open record
    var provisionalVersions = {};  // ordinal -> version already published as provisional
    var drained = [];              // completed records not yet returned
    var stats = { bytes: 0, lines: 0, records: 0, provisional: 0, unparsed: 0 };

    function startRecord(line, startByte) {
      var match = line.match(header);
      if (!match) {
        var record = blankRecord(datasetId, ordinal++, lineNo, startByte);
        record.message = line;
        record.raw = [line];
        record.body = [];
        record.body_lines = [];
        record.bytes.end = position;
        stats.records += 1;
        stats.unparsed += 1;
        return record;
      }
      var fresh = blankRecord(datasetId, ordinal++, lineNo, startByte);
      fresh.ts = match[1];
      fresh.level = match[2].toUpperCase();
      fresh.logger = match[3];
      fresh.pid = match[4];
      fresh.tid = match[5];
      fresh.src = match[6];
      fresh.src_line = Number(match[7]);
      fresh.fn = match[10];
      fresh.message = match[11];
      fresh.raw = [line];
      fresh.body = [];
      fresh.body_lines = [];
      fresh.has_trace = isTrace(match[11]);
      fresh.provisional = false;
      stats.records += 1;
      return fresh;
    }

    function updateTrace(record, text) {
      if (isChain(text)) { record.trace_chain_pending = true; return; }
      if (isTrace(text)) { record.trace_complete = false; record.trace_chain_pending = false; record.has_trace = true; return; }
      if (isTerminal(text)) record.trace_complete = true;
    }

    /* Merging per-line stamped stack output: only when adjacent and identity-consistent. */
    function shouldMerge(record, match) {
      if (!record || !record.has_trace) return false;
      if (record.pid !== match[4] || record.tid !== match[5] || record.logger !== match[3]) return false;
      var text = match[11];
      if (isChain(text) || isTrace(text)) return !record.trace_complete || record.trace_chain_pending || isChain(text);
      if (record.trace_complete) return !text.trim();
      return isContinuation(text);
    }

    function finish(record) {
      if (!record) return null;
      var version = provisionalVersions[record.ordinal] ? provisionalVersions[record.ordinal] + 1 : 1;
      var snapshot = {
        dataset_id: record.dataset_id,
        record_id: record.record_id,
        version: version,
        ordinal: record.ordinal,
        bytes: { start: String(record.bytes.start), end: String(record.bytes.end) },
        lines: { first: record.lines.first, last: record.lines.last },
        ts: record.ts,
        level: record.level,
        logger: record.logger,
        pid: record.pid,
        tid: record.tid,
        src: record.src,
        src_line: record.src_line,
        fn: record.fn,
        message: record.message,
        body: record.body.slice(),
        body_lines: record.body_lines.slice(),
        raw: record.raw.slice(),
        has_trace: record.has_trace,
        trace_complete: record.trace_complete,
        provisional: false
      };
      delete provisionalVersions[record.ordinal];
      return snapshot;
    }

    function consumeLine(text, startByte, endByte) {
      if (!bomChecked) {
        bomChecked = true;
        if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
      }
      lineNo += 1;
      var match = text.match(header);

      if (!match && isTrace(text) && current && current.trace_complete && !current.trace_chain_pending) {
        var done = finish(current);
        if (done) drained.push(done);
        current = startRecord(text, startByte);
        current.bytes.end = endByte;
        return;
      }

      if (match) {
        if (shouldMerge(current, match)) {
          current.body.push(match[11]);
          current.body_lines.push(lineNo);
          current.raw.push(text);
          current.lines.last = lineNo;
          current.bytes.end = endByte;
          updateTrace(current, match[11]);
          return;
        }
        var previous = finish(current);
        if (previous) drained.push(previous);
        current = startRecord(text, startByte);
        current.bytes.end = endByte;
        return;
      }

      var keep = current && !STAMPED.test(text) && !(current.level === 'UNPARSED' && !isContinuation(text));
      if (keep) {
        current.body.push(text);
        current.body_lines.push(lineNo);
        current.raw.push(text);
        current.lines.last = lineNo;
        current.bytes.end = endByte;
        if (current.has_trace || isTrace(text)) updateTrace(current, text);
        return;
      }

      var flushed = finish(current);
      if (flushed) drained.push(flushed);
      current = startRecord(text, startByte);
      current.bytes.end = endByte;
      if (!isTrace(text)) {
        var lone = finish(current);
        if (lone) drained.push(lone);
        current = null;
      }
    }

    function push(chunk) {
      if (!chunk || chunk.length === 0) return drain();
      if (!(chunk instanceof Uint8Array)) chunk = new Uint8Array(chunk);
      if (!pending.length) lineStart = position;
      var start = 0;
      for (var i = 0; i < chunk.length; i++) {
        if (chunk[i] !== 0x0a) continue;
        var slice;
        if (pending.length) {
          var merged = new Uint8Array(pending.length + (i - start));
          merged.set(pending, 0);
          merged.set(chunk.subarray(start, i), pending.length);
          slice = merged;
          pending = [];
        } else {
          slice = chunk.subarray(start, i);
        }
        var endByte = position + (i - start) + 1;
        var text = decodeLine(slice);
        consumeLine(text, lineStart, endByte);
        position = endByte;
        lineStart = position;
        start = i + 1;
      }
      if (start < chunk.length) {
        var tail = chunk.subarray(start);
        if (pending.length) {
          var grown = new Uint8Array(pending.length + tail.length);
          grown.set(pending, 0);
          grown.set(tail, pending.length);
          pending = grown;
        } else {
          pending = Array.prototype.slice.call(tail);
        }
      }
      position += chunk.length - start;
      stats.bytes = position;
      return drain();
    }

    function decodeLine(bytes) {
      if (!bytes.length) return '';
      if (bytes[bytes.length - 1] === 0x0d) bytes = bytes.subarray(0, bytes.length - 1);
      var decoder = new TextDecoder('utf-8');
      return decoder.decode(bytes);
    }

    function drain() {
      var out = drained;
      drained = [];
      return out;
    }

    /*
     * flush({eof:false}) publishes an already-started record as provisional so a
     * live tail can render something, while keeping it open for the same
     * record_id. An unterminated trailing line is NOT published: without a
     * newline there is no line boundary to attach it to, so it stays pending.
     */
    function flush(options) {
      options = options || {};
      var eof = options.eof !== false;
      if (!eof) {
        var out = drain();
        if (current && !current.published_provisional) {
          current.published_provisional = true;
          provisionalVersions[current.ordinal] = 1;
          stats.provisional += 1;
          out.push({
            dataset_id: current.dataset_id,
            record_id: current.record_id,
            version: 1,
            ordinal: current.ordinal,
            bytes: { start: String(current.bytes.start), end: String(current.bytes.end) },
            lines: { first: current.lines.first, last: current.lines.last },
            ts: current.ts,
            level: current.level,
            logger: current.logger,
            pid: current.pid,
            tid: current.tid,
            src: current.src,
            src_line: current.src_line,
            fn: current.fn,
            message: current.message,
            body: current.body.slice(),
            body_lines: current.body_lines.slice(),
            raw: current.raw.slice(),
            has_trace: current.has_trace,
            trace_complete: false,
            provisional: true
          });
        }
        return out;
      }
      if (pending.length) {
        var text = decodeLine(new Uint8Array(pending));
        pending = [];
        consumeLine(text, lineStart, position);
        lineStart = position;
      }
      var done = drain();
      var last = finish(current);
      if (last) done.push(last);
      current = null;
      stats.lines = lineNo;
      return done;
    }

    function checkpoint() {
      return {
        schema: 'logbench.framer-checkpoint/1',
        dataset_id: datasetId,
        position: position,
        line_no: lineNo,
        line_start: lineStart,
        pending: pending.slice(),
        ordinal: ordinal,
        provisional_versions: JSON.parse(JSON.stringify(provisionalVersions)),
        stats: JSON.parse(JSON.stringify(stats)),
        current: current ? JSON.parse(JSON.stringify(current)) : null
      };
    }

    function restore(state) {
      if (!state || state.schema !== 'logbench.framer-checkpoint/1') throw new Error('bad checkpoint');
      position = state.position;
      lineNo = state.line_no;
      lineStart = state.line_start;
      pending = state.pending.slice();
      ordinal = state.ordinal;
      provisionalVersions = JSON.parse(JSON.stringify(state.provisional_versions));
      stats = JSON.parse(JSON.stringify(state.stats));
      current = state.current ? JSON.parse(JSON.stringify(state.current)) : null;
      bomChecked = true;
      drained = [];
      return api;
    }

    function reset() {
      pending = []; position = 0; lineNo = 0; lineStart = 0;
      bomChecked = false; ordinal = 0; current = null;
      provisionalVersions = {}; drained = [];
      stats = { bytes: 0, lines: 0, records: 0, provisional: 0, unparsed: 0 };
      return api;
    }

    var api = {
      datasetId: datasetId,
      push: push,
      flush: flush,
      checkpoint: checkpoint,
      restore: restore,
      reset: reset,
      stats: function () { return JSON.parse(JSON.stringify(stats)); },
      isOpen: function () { return current !== null; }
    };
    return api;
  }

  /* Convenience for whole-file use in tests and tools. */
  function frameAll(bytes, datasetId, chunkSize) {
    var framer = createFramer({ datasetId: datasetId });
    var size = chunkSize || 65536;
    var all = [];
    for (var offset = 0; offset < bytes.length; offset += size) {
      all = all.concat(framer.push(bytes.subarray(offset, Math.min(bytes.length, offset + size))));
    }
    return all.concat(framer.flush({ eof: true }));
  }

  return {
    createFramer: createFramer,
    frameAll: frameAll,
    HEADER: HEADER,
    isTrace: isTrace,
    isChain: isChain,
    isTerminal: isTerminal,
    isContinuation: isContinuation
  };
});
