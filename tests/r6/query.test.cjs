/*
 * query.test.cjs — R6-012 acceptance: AST identity, matching semantics, marks,
 * projection versions, and refusal of unsafe patterns.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const q = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'query.js'));

const RECORD = {
  level: 'ERROR',
  message: 'burst energy below floor',
  body: ['Traceback (most recent call last):', '  File "energy.py", line 118', 'ValueError: floor'],
  fields: [['retries', '0'], ['converged', 'false'], ['note', '']],
  logger: 'euv.energy',
  fn: 'burst',
  src: 'energy.py'
};

function ast(spec) {
  const result = q.normalizeQuery(spec);
  assert.equal(result.ok, true, JSON.stringify(result.error));
  return result.ast;
}

test('equivalent specs share a signature; different flags do not', () => {
  const a = q.signatureOf({ text: 'energy', flags: { case_sensitive: false, whole_word: false, regex: false } });
  const b = q.signatureOf({ flags: { regex: false, whole_word: false, case_sensitive: false }, text: 'energy' });
  assert.equal(a, b);
  const c = q.signatureOf({ text: 'energy', flags: { case_sensitive: true, whole_word: false, regex: false } });
  assert.notEqual(a, c);
  const d = q.signatureOf({ text: 'energy', flags: { case_sensitive: false, whole_word: true, regex: false } });
  assert.notEqual(a, d);
});

test('multi-term order does not change identity, joiner does', () => {
  const ab = q.signatureOf({ terms: ['a', 'b'] });
  const ba = q.signatureOf({ terms: ['b', 'a'] });
  assert.equal(ab, ba);
  const or = q.signatureOf({ terms: ['a', 'b'], joiner: 'or' });
  assert.notEqual(ab, or);
});

test('literal search is case-insensitive by default and reversible', () => {
  const loose = ast({ text: 'ENERGY' });
  assert.equal(q.matches(RECORD, loose).hit, true);
  const strict = ast({ text: 'ENERGY', flags: { case_sensitive: true } });
  assert.equal(q.matches(RECORD, strict).hit, false);
});

test('Unicode case folding works for accented letters', () => {
  const record = { message: 'ÜBER uns', body: [], fields: [] };
  assert.equal(q.matches(record, ast({ text: 'über' })).hit, true);
  assert.equal(q.matches(record, ast({ text: 'ÜBER' })).hit, true);
});

test('whole-word respects underscores and word characters', () => {
  const record = { message: 'foo_bar foo baz', body: [], fields: [] };
  const marked = ast({ text: 'foo', flags: { whole_word: true } });
  const marks = q.computeMarks(record, marked);
  assert.equal(marks.length, 1, 'the foo inside foo_bar is not a whole word');
  assert.deepEqual(marks.map(m => m.start), [8]);
});

test('CJK whole-word degrades to substring because CJK has no separators', () => {
  const record = { message: '这是中文日志', body: [], fields: [] };
  const result = q.matches(record, ast({ text: '中文', flags: { whole_word: true } }));
  assert.equal(result.hit, true);
});

test('non-BMP characters produce correct code-unit ranges', () => {
  const record = { message: 'start 😀 end', body: [], fields: [] };
  const marks = q.computeMarks(record, ast({ text: '😀' }));
  assert.equal(marks.length, 1);
  assert.equal(record.message.slice(marks[0].start, marks[0].end), '😀');
});

test('dot does not cross newlines, explicit newline does', () => {
  const record = { message: 'a', body: ['b'], fields: [] };
  assert.equal(q.matches(record, ast({ text: 'a.b' })).hit, false);
  assert.equal(q.matches(record, ast({ text: 'a\nb' })).hit, true);
});

test('AND requires every term; OR requires one, and the mask reports which', () => {
  const and = ast({ terms: ['energy', 'floor'] });
  assert.deepEqual(q.matches(RECORD, and).mask, 3);
  const andMiss = ast({ terms: ['energy', 'missing'] });
  assert.equal(q.matches(RECORD, andMiss).hit, false);
  const or = ast({ terms: ['missing', 'floor'], joiner: 'or' });
  const hit = q.matches(RECORD, or);
  assert.equal(hit.hit, true);
  assert.equal(hit.mask, 2);
});

test('matching returns a flat result, never a per-record object graph', () => {
  const result = q.matches(RECORD, ast({ text: 'energy' }));
  assert.deepEqual(Object.keys(result).sort(), ['hit', 'mask', 'skipped', 'terms']);
});

test('v1 and v2 projections differ on fields, as declared', () => {
  const spec = { terms: ['energy', 'retries'], projection_version: 'v1' };
  const v1 = ast(spec);
  assert.equal(q.matches(RECORD, v1).hit, false, 'v1 does not search field values');
  const v2 = ast({ terms: ['energy', 'retries'], projection_version: 'v2' });
  assert.equal(q.matches(RECORD, v2).hit, true);
  assert.equal(q.project(RECORD, 'v2').version, 'v2');
  assert.equal(q.project(RECORD, 'v1').fields, null);
});

test('LIKE and FTS syntax stays literal', () => {
  const record = { message: 'progress 50% done', body: [], fields: [] };
  assert.equal(q.matches(record, ast({ text: '50%' })).hit, true);
  assert.equal(q.matches(record, ast({ text: '5_%' })).hit, false, '_ is literal, not a LIKE wildcard');
  const percentOnly = { message: 'a%b', body: [], fields: [] };
  assert.equal(q.matches(percentOnly, ast({ text: '%' })).hit, true);
  assert.equal(q.matches(percentOnly, ast({ text: 'a%b' })).hit, true);
  assert.equal(q.matches(percentOnly, ast({ text: 'a.b' })).hit, false, 'dot is literal, not a regex');
});

test('invalid regex becomes a structured error, not an exception', () => {
  const result = q.normalizeQuery({ text: '(', flags: { regex: true } });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'invalid_regex');
  assert.equal(result.error.pattern, '(');
});

test('patterns that can match empty are refused before they loop', () => {
  for (const pattern of ['.*', 'a*', '(?:)', '^']) {
    const result = q.normalizeQuery({ text: pattern, flags: { regex: true } });
    assert.equal(result.ok, false, pattern);
    assert.equal(result.error.code, 'empty_match', pattern);
  }
});

test('empty query is rejected instead of matching everything', () => {
  assert.equal(q.normalizeQuery({ text: '', flags: {} }).error.code, 'empty_query');
  assert.equal(q.normalizeQuery({ text: '   ', flags: {} }).ok, true, 'spaces are a real term');
});

test('level filter short-circuits without touching the text', () => {
  const result = q.matches(RECORD, ast({ text: 'energy', levels: ['INFO'] }));
  assert.equal(result.hit, false);
  assert.equal(result.skipped, 'level');
});

test('marks are opt-in, bounded and sorted', () => {
  const record = { message: 'x x x x x', body: [], fields: [] };
  const marks = q.computeMarks(record, ast({ text: 'x' }), { max_marks: 3 });
  assert.equal(marks.length, 3);
  assert.deepEqual(marks.map(m => m.start), [0, 2, 4]);
});

test('regex mode searches what the user wrote, not the literal', () => {
  const record = { message: 'retry 1, retry 2, retry 10', body: [], fields: [] };
  const literal = ast({ text: 'retry \\d+' });
  assert.equal(q.matches(record, literal).hit, false);
  const regex = ast({ text: 'retry \\d+', flags: { regex: true } });
  assert.equal(q.computeMarks(record, regex).length, 3);
});

test('time_range filters matching, and never guesses for records without ts_ns', () => {
  const inside = { message: 'energy ok', body: [], fields: [], ts_ns: 1000 };
  const before = { message: 'energy ok', body: [], fields: [], ts_ns: 10 };
  const unknown = { message: 'energy ok', body: [], fields: [] };
  const ast = q.normalizeQuery({ text: 'energy', time_range: { from_ns: 500, to_ns: 2000 } }).ast;
  assert.equal(q.matches(inside, ast).hit, true);
  assert.equal(q.matches(before, ast).skipped, 'time');
  assert.equal(q.matches(unknown, ast).hit, true, 'unknown timestamp is kept, not assumed outside');
});

test('match_all is an explicit "everything in range" query, not an empty search', () => {
  const normalized = q.normalizeQuery({ match_all: true, levels: ['ERROR'],
    time_range: { from_ns: 500, to_ns: 2000 } });
  assert.equal(normalized.ok, true);
  assert.equal(normalized.ast.match_all, true);
  assert.equal(normalized.ast.terms.length, 1);
  // An empty query is still an error: typing nothing must not return the whole file.
  assert.equal(q.normalizeQuery({ text: '' }).ok, false);
  assert.equal(q.normalizeQuery({ text: '' }).error.code, 'empty_query');
});

test('match_all hits records with no text and respects level and time filters', () => {
  const ast = q.normalizeQuery({ match_all: true, levels: ['ERROR'],
    time_range: { from_ns: 500, to_ns: 2000 } }).ast;
  const empty = { message: '', body: [], fields: [], level: 'ERROR', ts_ns: 1000 };
  const info = { message: 'ok', body: [], fields: [], level: 'INFO', ts_ns: 1000 };
  const late = { message: 'ok', body: [], fields: [], level: 'ERROR', ts_ns: 9000 };
  assert.equal(q.matches(empty, ast).hit, true, 'an empty message is still a record in range');
  assert.equal(q.matches(info, ast).skipped, 'level');
  assert.equal(q.matches(late, ast).skipped, 'time');
});

test('match_all produces no highlight marks and a stable signature', () => {
  const one = q.normalizeQuery({ match_all: true });
  const two = q.normalizeQuery({ match_all: true, levels: ['ERROR'] });
  assert.equal(one.signature, two.signature, 'level is a filter, not part of the term signature');
  const marks = q.computeMarks({ message: 'anything', body: [], fields: [] }, one.ast);
  assert.deepEqual(marks, [], 'marking every character would be unusable');
});
