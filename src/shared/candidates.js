/*
 * candidates.js — block-level candidate pruning with a no-false-negative contract (R6-023).
 *
 * The premise: a block index may only *exclude* blocks that provably cannot
 * contain a match. It must never stand in for exact matching. Every query still
 * runs `query.matches()` on the surviving blocks, so a wrong prune shows up as a
 * missing hit, which is exactly what `candidate-differential.test.cjs` forbids.
 *
 * Soundness rules, in the order they bite:
 *
 *   1. A regex term is never pruned by grams. `a.*b` can match text whose every
 *      trigram is absent from any fixed list we could store.
 *   2. A literal term of one character is never pruned: the gram index cannot
 *      represent one-character evidence.
 *   3. Case-insensitive Latin terms are pruned against a *folded* gram set,
 *      never against raw text. ASCII lowercase is a homomorphism on ASCII
 *      substrings, so `lower(query) ⊆ lower(text)` follows from `query ⊆ text`.
 *      The converse also holds for ASCII-only queries even when the text has
 *      non-ASCII characters, since folding leaves ASCII code units intact.
 *   4. Case-sensitive terms and caseless terms (CJK, digits, punctuation) are
 *      pruned against the raw gram set. A caseless term has `toLowerCase()` and
 *      `toUpperCase()` both equal to itself, so folding is the identity there
 *      and the raw set is the folded set.
 *   5. Anything else — a cased non-ASCII term under case-insensitive search
 *      (Greek final sigma, Turkish dotted I, `ß`) — falls back to scanning.
 *      `"ΟΣ".toLowerCase()` is `"ος"`, so a folded gram set would miss a real
 *      `Σ` and produce a false negative. We decline to prune instead.
 *
 * Block boundaries: grams are built over `blockText + nextBlockPrefix(n-1)`, so
 * a word that straddles two blocks is still represented in the block where it
 * starts. Without the overlap, `...ener|gy...` would prune both halves.
 *
 * Metadata pruning (level, time) is separate and is only applied when the block
 * can prove it: `ts_complete === false` disables time pruning for that block.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBCandidates = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULT_OPTIONS = {
    records_per_block: 512,
    gram_size: 3,
    // Guards the memory of a single block's gram set.
    max_grams_per_block: 200000
  };

  var FNV_OFFSET = 0x811c9dc5;
  var FNV_PRIME = 0x01000193;

  function hash32(text) {
    var h = FNV_OFFSET;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h >>> 0;
  }

  function asciiOnly(text) {
    for (var i = 0; i < text.length; i++) {
      if (text.charCodeAt(i) > 0x7f) return false;
    }
    return true;
  }

  function caseless(text) {
    return text === text.toLowerCase() && text === text.toUpperCase();
  }

  /** Sorted, de-duplicated gram hashes for `text`. */
  function gramHashes(text, size) {
    var n = size || DEFAULT_OPTIONS.gram_size;
    var out = [];
    if (!text || text.length < n) return out;
    var seen = Object.create(null);
    for (var i = 0; i + n <= text.length; i++) {
      var h = hash32(text.substr(i, n));
      if (seen[h] === 1) continue;
      seen[h] = 1;
      out.push(h);
    }
    out.sort(function (a, b) { return a - b; });
    return out;
  }

  function contains(sorted, value) {
    var lo = 0;
    var hi = sorted.length - 1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (sorted[mid] === value) return true;
      if (sorted[mid] < value) lo = mid + 1; else hi = mid - 1;
    }
    return false;
  }

  /* Same ms-key rule as query.js: ns strings are exact on the wire, ms in memory. */
  function timeKey(value) {
    if (value == null) return null;
    if (typeof value === 'number') return value > 1e14 ? Math.floor(value / 1e6) : value;
    return Number(BigInt(String(value)) / 1000000n);
  }

  /* Projection text for one record. v1 = message + body, the same text R5 searched. */
  function projectionText(record) {
    var parts = [];
    if (record.message != null) parts.push(String(record.message));
    if (record.body && record.body.length) parts.push(record.body.join('\n'));
    return parts.join('\n');
  }

  /**
   * Build one block index entry per `records_per_block` records.
   * Text used for grams is the projection, not the raw line: searching `raw`
   * would include timestamps and field noise the matcher never reads.
   */
  function buildBlockIndex(records, options) {
    var opts = {};
    for (var key in DEFAULT_OPTIONS) opts[key] = DEFAULT_OPTIONS[key];
    for (var given in (options || {})) opts[given] = options[given];

    var blocks = [];
    var total = records.length;
    for (var start = 0; start < total; start += opts.records_per_block) {
      var end = Math.min(total, start + opts.records_per_block);
      var texts = [];
      var levels = Object.create(null);
      var tsFirst = null;
      var tsLast = null;
      var tsComplete = true;
      for (var i = start; i < end; i++) {
        var record = records[i];
        texts.push(projectionText(record));
        if (record.level) levels[record.level] = (levels[record.level] || 0) + 1;
        var ns = record.ts_key != null ? record.ts_key : timeKey(record.ts_ns);
        if (ns == null) tsComplete = false;
        else {
          if (tsFirst == null || ns < tsFirst) tsFirst = ns;
          if (tsLast == null || ns > tsLast) tsLast = ns;
        }
      }
      // Overlap the next block's prefix so a token split across the boundary is
      // still present in the block where it begins.
      var tail = '';
      if (end < total) {
        var nextRecord = records[end];
        tail = projectionText(nextRecord).slice(0, Math.max(0, opts.gram_size - 1));
      }
      var joined = texts.join('\n');
      var folded = joined.toLowerCase() + tail.toLowerCase();
      blocks.push({
        block_no: blocks.length,
        first_ordinal: start,
        last_ordinal: end - 1,
        count: end - start,
        levels: levels,
        ts_first_key: tsFirst,
        ts_last_key: tsLast,
        ts_complete: tsComplete,
        grams: gramHashes(joined + tail, opts.gram_size).slice(0, opts.max_grams_per_block),
        grams_folded: gramHashes(folded, opts.gram_size).slice(0, opts.max_grams_per_block),
        grams_truncated: false
      });
      var last = blocks[blocks.length - 1];
      last.grams_truncated = last.grams.length >= opts.max_grams_per_block ||
        last.grams_folded.length >= opts.max_grams_per_block;
      // A truncated gram set can still be used: absence from a subset is weaker
      // than absence from the whole, but truncation is a prefix of a sorted set,
      // so absence is no longer meaningful. Mark it and refuse to prune.
    }
    return {
      blocks: blocks,
      stats: {
        blocks: blocks.length,
        records: total,
        gram_size: opts.gram_size,
        records_per_block: opts.records_per_block
      }
    };
  }

  /**
   * Which gram set may be used for a term, or null when pruning is unsafe.
   * Returning null is a deliberate, tested answer, not a silent fallback.
   */
  function gramStrategy(term, options) {
    var text = term.value;
    var size = options.gram_size || 3;
    if (term.kind !== 'literal') return { set: null, reason: 'regex' };
    // The index stores exactly one gram size. A term shorter than that size has
    // no representable evidence, so it scans rather than guessing.
    if (text.length < size) return { set: null, reason: 'shorter_than_gram' };
    if (term.case_sensitive) return { set: 'raw', size: size, probe: text };
    if (asciiOnly(text)) return { set: 'folded', size: size, probe: text.toLowerCase() };
    if (caseless(text)) return { set: 'raw', size: size, probe: text };
    return { set: null, reason: 'folding_unsafe' };
  }

  function blockHasAll(block, set, probe, size) {
    var grams = block[set === 'folded' ? 'grams_folded' : 'grams'];
    if (!grams || !grams.length) return true;      // nothing to prune on
    if (block.grams_truncated) return true;        // a prefix cannot prove absence
    var needed = gramHashes(probe, size);
    for (var i = 0; i < needed.length; i++) {
      if (!contains(grams, needed[i])) return false;
    }
    return true;
  }

  /**
   * Candidate blocks for an AST.
   * `excluded` is the union of every provable exclusion; a block survives if any
   * term could still match inside it.
   */
  function plan(ast, index, options) {
    var opts = {};
    for (var key in DEFAULT_OPTIONS) opts[key] = DEFAULT_OPTIONS[key];
    for (var given in (options || {})) opts[given] = options[given];

    var blocks = (index && index.blocks) || [];
    if (!blocks.length) {
      return {
        candidates: [],
        coverage: { scanned_blocks: 0, pruned_blocks: 0, unindexed_blocks: 0, total_blocks: 0 },
        reasons: { full_scan: 1 },
        selective: false
      };
    }

    var strategies = (ast.terms || []).map(function (term) { return gramStrategy(term, opts); });
    var fallback = strategies.some(function (s) { return s.set === null; });
    var reasons = Object.create(null);
    strategies.forEach(function (s) { if (s.set === null) reasons[s.reason] = (reasons[s.reason] || 0) + 1; });

    var levels = ast.levels && ast.levels.length ? ast.levels : null;
    var range = ast.time_range || null;
    var candidates = [];
    var prunedByGram = 0;
    var prunedByMeta = 0;

    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i];
      var keep = true;

      // Joiners make gram evidence per-term; a block is dropped only when every
      // term is provably absent, regardless of AND/OR.
      if (!fallback) {
        var anyPossible = strategies.some(function (strategy) {
          return blockHasAll(block, strategy.set, strategy.probe, strategy.size);
        });
        if (!anyPossible) {
          keep = false;
          prunedByGram += 1;
        }
      }

      if (keep && levels) {
        var hitLevel = levels.some(function (level) { return (block.levels[level] || 0) > 0; });
        if (!hitLevel) {
          keep = false;
          prunedByMeta += 1;
        }
      }
      if (keep && range && block.ts_complete) {
        var fromKey = timeKey(range.from_ns);
        var toKey = timeKey(range.to_ns);
        var before = false;
        var after = false;
        if (fromKey != null && block.ts_last_key != null && block.ts_last_key < fromKey) before = true;
        if (toKey != null && block.ts_first_key != null && block.ts_first_key > toKey) after = true;
        if (before || after) {
          keep = false;
          prunedByMeta += 1;
        }
      }

      if (keep) candidates.push(block.block_no);
    }

    return {
      candidates: candidates,
      coverage: {
        scanned_blocks: candidates.length,
        pruned_blocks: blocks.length - candidates.length,
        pruned_by_gram: prunedByGram,
        pruned_by_metadata: prunedByMeta,
        unindexed_blocks: 0,
        total_blocks: blocks.length
      },
      reasons: reasons,
      selective: candidates.length < blocks.length
    };
  }

  /** Records covered by the candidate blocks, preserving original order. */
  function collect(records, index, candidateBlocks) {
    var out = [];
    var allowed = Object.create(null);
    candidateBlocks.forEach(function (n) { allowed[n] = 1; });
    for (var i = 0; i < index.blocks.length; i++) {
      if (allowed[i] !== 1) continue;
      var block = index.blocks[i];
      for (var j = block.first_ordinal; j <= block.last_ordinal; j++) out.push(records[j]);
    }
    return out;
  }

  return {
    DEFAULT_OPTIONS: DEFAULT_OPTIONS,
    hash32: hash32,
    asciiOnly: asciiOnly,
    caseless: caseless,
    gramHashes: gramHashes,
    projectionText: projectionText,
    timeKey: timeKey,
    buildBlockIndex: buildBlockIndex,
    gramStrategy: gramStrategy,
    plan: plan,
    collect: collect
  };
});
