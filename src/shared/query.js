/*
 * query.js — query AST, matching, marks and projection versions (R6-012).
 *
 * Rules this module enforces:
 *   - `matches()` returns a boolean plus a per-term bitmask. It never builds a
 *     per-record object graph, because that is what made R5 clone 130k records
 *     for every keystroke.
 *   - `computeMarks()` is a separate call: highlighting is only computed for the
 *     rows that are actually rendered.
 *   - An invalid or zero-length regex becomes a structured error, never an
 *     exception thrown into the render loop and never an infinite loop.
 *   - v1 projection is exactly what R5 searched (message + body). v2 adds field
 *     values and metadata, and the version is explicit so saved views keep
 *     meaning.
 *   - `%` and `_` are literal characters unless the user asked for a regex; the
 *     query language must never accidentally inherit SQL LIKE or FTS syntax.
 *
 * UMD-lite: window.LBQuery in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBQuery = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var PROTOCOL = 'logbench.query/1';
  var PROJECTIONS = { v1: ['message', 'body'], v2: ['message', 'body', 'fields', 'logger', 'fn', 'src'] };
  var CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
  var WORD = /[\p{L}\p{N}_]/u;

  function hash(text) {
    var value = 0x811c9dc5;
    var source = String(text == null ? '' : text);
    for (var i = 0; i < source.length; i++) {
      value ^= source.charCodeAt(i);
      value = (value + ((value << 1) + (value << 4) + (value << 7) + (value << 8) + (value << 24))) >>> 0;
    }
    return ('00000000' + value.toString(16)).slice(-8);
  }

  function fail(code, message, extra) {
    var error = { code: code, message: message };
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  function escapeLiteral(text) {
    return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function parseTerm(raw, flags) {
    var term = {
      kind: flags.regex ? 'regex' : 'literal',
      value: String(raw == null ? '' : raw),
      case_sensitive: flags.case_sensitive === true,
      whole_word: flags.whole_word === true,
      negated: false
    };
    if (term.value === '') return { error: fail('empty_term', '空的搜索词不能构成查询') };
    var body = term.kind === 'regex' ? term.value : escapeLiteral(term.value);
    var modifiers = term.case_sensitive ? 'u' : 'iu';
    var source = term.whole_word && !CJK.test(term.value) ? '(?<![\\p{L}\\p{N}_])(?:' + body + ')(?![\\p{L}\\p{N}_])' : '(?:' + body + ')';
    var compiled;
    try {
      compiled = new RegExp(source, modifiers + 'g');
    } catch (error) {
      return { error: fail('invalid_regex', '正则表达式无效：' + error.message, { pattern: term.value }) };
    }
    var emptyProbe;
    try {
      emptyProbe = new RegExp(source, modifiers);
    } catch (error) {
      return { error: fail('invalid_regex', '正则表达式无效：' + error.message, { pattern: term.value }) };
    }
    if (emptyProbe.test('')) {
      return { error: fail('empty_match', '该模式可以匹配空字符串，会破坏分页与高亮', { pattern: term.value }) };
    }
    term.pattern = source;
    term.modifiers = modifiers;
    term.regex = compiled;
    term.ascii_fast_path = term.kind === 'literal' && !term.whole_word && /^[\x20-\x7e]*$/.test(term.value);
    return { term: term };
  }

  /**
   * Build a stable AST + signature from a QuerySpec-shaped object.
   * Equivalent specs produce the same signature regardless of key order.
   */
  function normalizeQuery(spec) {
    spec = spec || {};
    var rawText = spec.text == null ? '' : String(spec.text);
    var flags = {
      case_sensitive: !!(spec.flags && spec.flags.case_sensitive),
      whole_word: !!(spec.flags && spec.flags.whole_word),
      regex: !!(spec.flags && spec.flags.regex)
    };
    var joiner = spec.joiner === 'or' ? 'or' : 'and';
    /*
     * `match_all` is the "everything in this range" query: a time scope or a level
     * filter with no text. It is an explicit flag rather than an empty string,
     * because an empty query must stay an error — otherwise a mistyped search
     * silently returns the whole file and looks like a result.
     */
    if (spec.match_all === true) {
      var allTerm = {
        kind: 'all', value: '', case_sensitive: false, whole_word: false, negated: false,
        match_all: true, ascii_fast_path: false, pattern: '', modifiers: 'u', regex: null
      };
      return {
        ok: true,
        ast: {
          protocol: PROTOCOL, joiner: joiner, terms: [allTerm], match_all: true,
          projection_version: spec.projection_version === 'v2' ? 'v2' : 'v1',
          levels: Array.isArray(spec.levels) ? spec.levels.slice() : null,
          time_range: spec.time_range || null
        },
        signature: hash(PROTOCOL + '|all'),
        error: null
      };
    }
    var parts = spec.terms && spec.terms.length ? spec.terms.map(String) : (rawText === '' ? [] : [rawText]);
    var terms = [];
    for (var i = 0; i < parts.length; i++) {
      var parsed = parseTerm(parts[i], flags);
      if (parsed.error) return { ok: false, error: parsed.error, ast: null, signature: null };
      terms.push(parsed.term);
    }
    if (!terms.length) return { ok: false, error: fail('empty_query', '查询为空'), ast: null, signature: null };
    var canonical = PROTOCOL + '|' + joiner + '|' +
      terms.map(function (term) {
        return [term.kind, term.case_sensitive ? 'Aa' : 'aa', term.whole_word ? 'w' : '-', term.value].join('~');
      }).sort().join('&');
    var ast = {
      protocol: PROTOCOL,
      joiner: joiner,
      terms: terms,
      projection_version: spec.projection_version === 'v2' ? 'v2' : 'v1',
      levels: Array.isArray(spec.levels) ? spec.levels.slice() : null,
      time_range: spec.time_range || null
    };
    return { ok: true, ast: ast, signature: hash(canonical), error: null };
  }

  function haystack(record, projectionVersion) {
    var fields = PROJECTIONS[projectionVersion] || PROJECTIONS.v1;
    var chunks = [];
    for (var i = 0; i < fields.length; i++) {
      var key = fields[i];
      if (key === 'body') {
        if (record.body && record.body.length) chunks.push(record.body.join('\n'));
      } else if (key === 'fields') {
        var list = record.fields || [];
        for (var j = 0; j < list.length; j++) {
          var field = list[j];
          if (Array.isArray(field)) chunks.push(String(field[0]) + '=' + String(field[1]));
          else chunks.push(String(field.key) + '=' + String(field.value));
        }
      } else if (record[key] != null) {
        chunks.push(String(record[key]));
      }
    }
    return chunks.join('\n');
  }

  function testTerm(term, text) {
    if (term.match_all) return true;
    if (term.ascii_fast_path && /^[\x00-\x7f]*$/.test(text)) {
      var needle = term.case_sensitive ? term.value : term.value.toLowerCase();
      var hay = term.case_sensitive ? text : text.toLowerCase();
      return hay.indexOf(needle) !== -1;
    }
    term.regex.lastIndex = 0;
    return term.regex.test(text);
  }

  /**
   * Match one record. Returns {hit, mask, terms} only — no excerpts, no arrays
   * of ranges. Mask bit i is set when term i matched.
   */
  /* ns string / ns number -> ms key, without losing the ordering it is used for. */
  function timeKey(value) {
    if (value == null) return null;
    if (typeof value === 'number') return value > 1e14 ? Math.floor(value / 1e6) : value;
    return Number(BigInt(String(value)) / 1000000n);
  }

  function matches(record, ast, options) {
    options = options || {};
    if (!ast || !ast.terms || !ast.terms.length) return { hit: false, mask: 0, terms: 0, skipped: 'empty' };
    if (ast.levels && ast.levels.length && ast.levels.indexOf(record.level) === -1) {
      return { hit: false, mask: 0, terms: ast.terms.length, skipped: 'level' };
    }
    /*
     * Time range is enforced here, not by the caller, so that candidate pruning
     * (R6-023) and exact matching agree on the same predicate. A record with no
     * parsed timestamp is kept: excluding it would be a guess, not a filter.
     */
    if (ast.time_range) {
      var from = timeKey(ast.time_range.from_ns);
      var to = timeKey(ast.time_range.to_ns);
      var ns = record.ts_key != null ? record.ts_key : timeKey(record.ts_ns);
      if (ns != null && ((from != null && ns < from) || (to != null && ns > to))) {
        return { hit: false, mask: 0, terms: ast.terms.length, skipped: 'time' };
      }
    }
    var text = haystack(record, options.projection_version || ast.projection_version);
    var mask = 0;
    var hits = 0;
    for (var i = 0; i < ast.terms.length; i++) {
      if (testTerm(ast.terms[i], text)) {
        mask |= (1 << i);
        hits += 1;
        if (ast.joiner === 'or') return { hit: true, mask: mask, terms: ast.terms.length, skipped: null };
      } else if (ast.joiner === 'and') {
        return { hit: false, mask: mask, terms: ast.terms.length, skipped: null };
      }
    }
    return { hit: ast.joiner === 'or' ? hits > 0 : hits === ast.terms.length, mask: mask, terms: ast.terms.length, skipped: null };
  }

  /** Highlight ranges for one rendered row. Call this only for visible rows. */
  function computeMarks(record, ast, options) {
    options = options || {};
    var text = haystack(record, options.projection_version || ast.projection_version);
    var ranges = [];
    for (var i = 0; i < ast.terms.length; i++) {
      var term = ast.terms[i];
      // A match-all term has no text to highlight; producing marks for it would
      // mark every character of every visible row.
      if (term.match_all || !term.regex) continue;
      term.regex.lastIndex = 0;
      var match;
      var limit = options.max_marks || 500;
      while ((match = term.regex.exec(text)) !== null) {
        if (match[0].length === 0) break;          // defensive: never loop forever
        if (ranges.length >= limit) break;
        ranges.push({ start: match.index, end: match.index + match[0].length, term: i });
      }
    }
    ranges.sort(function (a, b) { return a.start - b.start || a.end - b.end; });
    return ranges;
  }

  function project(record, version) {
    return {
      version: version === 'v2' ? 'v2' : 'v1',
      text: haystack(record, version === 'v2' ? 'v2' : 'v1'),
      fields: version === 'v2' ? (record.fields || []).slice() : null
    };
  }

  return {
    PROTOCOL: PROTOCOL,
    PROJECTIONS: PROJECTIONS,
    normalizeQuery: normalizeQuery,
    matches: matches,
    computeMarks: computeMarks,
    haystack: haystack,
    project: project,
    timeKey: timeKey,
    signatureOf: function (spec) {
      var result = normalizeQuery(spec);
      return result.ok ? result.signature : null;
    },
    hash: hash
  };
});
