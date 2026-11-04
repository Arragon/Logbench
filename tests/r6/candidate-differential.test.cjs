'use strict';
/*
 * R6-023 acceptance: candidate pruning may only ever *exclude blocks that cannot
 * match*. The single invariant asserted throughout is therefore a set equality:
 *
 *     hits(full scan)  ===  hits(candidate scan)
 *
 * A false negative fails the suite immediately; extra scanned blocks never do.
 * The suite also pins the cases where pruning is deliberately disabled, because
 * "we scan here" is a decision that has to keep being true.
 */

const test = require('node:test');
const assert = require('node:assert');

const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const C = require(path.join(ROOT, 'src', 'shared', 'candidates.js'));
const Q = require(path.join(ROOT, 'src', 'shared', 'query.js'));

/* Deterministic PRNG so a failure is reproducible from the seed alone. */
function rng(seed) {
  let state = seed >>> 0;
  return () => {
    state ^= state << 13; state >>>= 0;
    state ^= state >> 17;
    state ^= state << 5; state >>>= 0;
    return state / 0x100000000;
  };
}

const WORDS = ['energy', 'floor', 'meter', 'alarm', 'gateway', 'timeout', '重试', '电量', 'Gateway',
  'ENERGY', 'αβγ', 'ΟΣ', 'İSTANBUL', 'straße', '100%', 'a_b', 'x*y', 'q?z', '设备', '采集'];

function makeRecords(count, seed) {
  const random = rng(seed);
  const records = [];
  for (let i = 0; i < count; i++) {
    const picks = [];
    const n = 1 + Math.floor(random() * 3);
    for (let j = 0; j < n; j++) picks.push(WORDS[Math.floor(random() * WORDS.length)]);
    const level = ['INFO', 'WARN', 'ERROR', 'DEBUG'][Math.floor(random() * 4)];
    records.push({
      ordinal: i,
      level,
      ts: '2026-01-01 00:00:%02d.000'.replace('%02d', String(i % 60).padStart(2, '0')),
      ts_ns: 1767225600000000000 + i * 1000000,
      message: picks.join(' ') + ' #' + i,
      body: random() > 0.7 ? ['  detail: ' + picks[0] + ' repeated'] : []
    });
  }
  return records;
}

function scanAll(records, spec) {
  const parsed = Q.normalizeQuery(spec);
  assert.ok(parsed.ok, 'query must normalize: ' + spec.text);
  const hits = [];
  for (const record of records) {
    if (Q.matches(record, parsed.ast).hit) hits.push(record.ordinal);
  }
  return { hits, ast: parsed.ast };
}

function scanPruned(records, spec, index, options) {
  const parsed = Q.normalizeQuery(spec);
  assert.ok(parsed.ok);
  const planned = C.plan(parsed.ast, index, options);
  const hits = [];
  for (const blockNo of planned.candidates) {
    const block = index.blocks[blockNo];
    for (let i = block.first_ordinal; i <= block.last_ordinal; i++) {
      if (Q.matches(records[i], parsed.ast).hit) hits.push(records[i].ordinal);
    }
  }
  return { hits, planned };
}

const QUERIES = [
  { label: 'ascii lower', spec: { text: 'energy' } },
  { label: 'ascii upper (case-insensitive)', spec: { text: 'ENERGY' } },
  { label: 'ascii case-sensitive hit', spec: { text: 'ENERGY', flags: { case_sensitive: true } } },
  { label: 'ascii case-sensitive miss', spec: { text: 'energy', flags: { case_sensitive: true } } },
  { label: 'two-char CJK', spec: { text: '电量' } },
  { label: 'one-char CJK', spec: { text: '电' } },
  { label: 'greek final sigma', spec: { text: 'Σ' } },
  { label: 'turkish dotted I', spec: { text: 'istanbul' } },
  { label: 'eszett', spec: { text: 'straße' } },
  { label: 'literal percent', spec: { text: '100%' } },
  { label: 'literal underscore', spec: { text: 'a_b' } },
  { label: 'literal star', spec: { text: 'x*y' } },
  { label: 'literal question', spec: { text: 'q?z' } },
  { label: 'regex alternation', spec: { text: 'energy|alarm', flags: { regex: true } } },
  { label: 'regex cross-line', spec: { text: '#1[\\s\\S]*#3', flags: { regex: true } } },
  { label: 'whole word', spec: { text: 'gate', flags: { whole_word: true } } },
  { label: 'no hits', spec: { text: 'zzz_absent_token' } },
  { label: 'level filter', spec: { text: 'energy', levels: ['ERROR'] } },
  { label: 'time range', spec: { text: 'energy', time_range: { from_ns: 1767225600000000000 + 10 * 1000000, to_ns: 1767225600000000000 + 40 * 1000000 } } }
];

test('candidate scan equals full scan for every query shape', () => {
  const records = makeRecords(900, 7);
  for (const perBlock of [1, 2, 7, 64, 512, 2000]) {
    const index = C.buildBlockIndex(records, { records_per_block: perBlock });
    for (const item of QUERIES) {
      const full = scanAll(records, item.spec);
      const pruned = scanPruned(records, item.spec, index);
      assert.deepStrictEqual(pruned.hits, full.hits,
        `${item.label} @ ${perBlock}/block: pruning changed the hit set`);
    }
  }
});

test('token straddling a block boundary stays findable', () => {
  // "gateway" is split so that "gate" ends block 0 and "way" starts block 1.
  const records = [
    { ordinal: 0, level: 'INFO', ts_ns: 1, message: 'value gate' },
    { ordinal: 1, level: 'INFO', ts_ns: 2, message: 'way opened' },
    { ordinal: 2, level: 'INFO', ts_ns: 3, message: 'unrelated line' }
  ];
  const index = C.buildBlockIndex(records, { records_per_block: 1 });
  // The concatenation used for grams is "value gate\n" + "way opened..." — the
  // newline is part of the projection, so prove boundary overlap with a record
  // whose own text continues into the next block without a separator.
  const tight = [
    { ordinal: 0, level: 'INFO', ts_ns: 1, message: 'xx ener' },
    { ordinal: 1, level: 'INFO', ts_ns: 2, message: 'gy yy' }
  ];
  const tightIndex = C.buildBlockIndex(tight, { records_per_block: 1 });
  // "ener" + "gy" spans the boundary; "rgy" only exists if block 0 carries the
  // next block's prefix.
  assert.ok(tightIndex.blocks[0].grams.includes(C.hash32('rgy')),
    'block 0 must include the gram formed with the next block prefix');
});

test('pruning is refused when folding would be lossy', () => {
  const size = 3;
  const regex = { kind: 'regex', value: 'a.*b', case_sensitive: false, whole_word: false };
  assert.equal(C.gramStrategy(regex, { gram_size: size }).set, null);

  const short = { kind: 'literal', value: '电', case_sensitive: false, whole_word: false };
  assert.equal(C.gramStrategy(short, { gram_size: size }).reason, 'shorter_than_gram');

  const sigma = { kind: 'literal', value: 'ΣΟΦΟΣ', case_sensitive: false, whole_word: false };
  assert.equal(C.gramStrategy(sigma, { gram_size: size }).reason, 'folding_unsafe');

  const shortCjk = { kind: 'literal', value: '电量', case_sensitive: false, whole_word: false };
  assert.equal(C.gramStrategy(shortCjk, { gram_size: size }).reason, 'shorter_than_gram');

  const cjk = { kind: 'literal', value: '电量采集', case_sensitive: false, whole_word: false };
  assert.equal(C.gramStrategy(cjk, { gram_size: size }).set, 'raw');

  const ascii = { kind: 'literal', value: 'ENERGY', case_sensitive: false, whole_word: false };
  const strategy = C.gramStrategy(ascii, { gram_size: size });
  assert.equal(strategy.set, 'folded');
  assert.equal(strategy.probe, 'energy');
});

test('a block whose gram set was truncated is never pruned', () => {
  const records = makeRecords(40, 11);
  const index = C.buildBlockIndex(records, { records_per_block: 40, max_grams_per_block: 10 });
  assert.equal(index.blocks[0].grams_truncated, true);
  const parsed = Q.normalizeQuery({ text: 'energy' });
  const planned = C.plan(parsed.ast, index);
  assert.deepStrictEqual(planned.candidates, [0], 'truncated grams must not exclude the block');
});

test('metadata pruning needs a complete timestamp set', () => {
  const complete = [
    { ordinal: 0, level: 'INFO', ts_ns: 10, message: 'a' },
    { ordinal: 1, level: 'INFO', ts_ns: 20, message: 'b' }
  ];
  const index = C.buildBlockIndex(complete, { records_per_block: 2 });
  const parsed = Q.normalizeQuery({ text: 'a', time_range: { from_ns: 100, to_ns: 200 } });
  const planned = C.plan(parsed.ast, index);
  assert.deepStrictEqual(planned.candidates, [], 'entirely-before range prunes the block');
  assert.equal(planned.coverage.pruned_by_metadata, 1);

  const partial = [
    { ordinal: 0, level: 'INFO', ts_ns: 10, message: 'a' },
    { ordinal: 1, level: 'INFO', ts: null, ts_ns: null, message: 'b' }
  ];
  const partialIndex = C.buildBlockIndex(partial, { records_per_block: 2 });
  const plannedPartial = C.plan(parsed.ast, partialIndex);
  assert.deepStrictEqual(plannedPartial.candidates, [0],
    'a block with an unparsed timestamp cannot be pruned by time');
});

test('coverage distinguishes gram pruning from metadata pruning', () => {
  const records = makeRecords(400, 3);
  const index = C.buildBlockIndex(records, { records_per_block: 20 });
  const parsed = Q.normalizeQuery({ text: 'zzz_absent_token' });
  const planned = C.plan(parsed.ast, index);
  assert.equal(planned.coverage.scanned_blocks, 0);
  assert.ok(planned.coverage.pruned_by_gram > 0);
  assert.equal(planned.coverage.pruned_by_metadata, 0);
  assert.equal(planned.selective, true);
});

test('collect preserves ordinal order and never duplicates', () => {
  const records = makeRecords(50, 5);
  const index = C.buildBlockIndex(records, { records_per_block: 7 });
  const collected = C.collect(records, index, [0, 2, 4]);
  const ordinals = collected.map(r => r.ordinal);
  assert.deepStrictEqual(ordinals, ordinals.slice().sort((a, b) => a - b));
  assert.equal(new Set(ordinals).size, ordinals.length);
  assert.deepStrictEqual(ordinals, [0, 1, 2, 3, 4, 5, 6, 14, 15, 16, 17, 18, 19, 20, 28, 29, 30, 31, 32, 33, 34]);
});
