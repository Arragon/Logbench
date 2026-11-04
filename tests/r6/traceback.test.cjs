/*
 * traceback.test.cjs — R6-010 acceptance: chains, boundaries, states.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const tb = require(path.join(ROOT, 'src', 'shared', 'traceback.js'));
const framer = require(path.join(ROOT, 'src', 'shared', 'framer.js'));
const harness = require(path.join(__dirname, 'parser-harness.cjs'));

const PLAIN = [
  'Traceback (most recent call last):',
  '  File "/opt/app/energy.py", line 118, in burst',
  '    raise ValueError("floor")',
  'ValueError: floor'
];

test('one traceback: frame, source from log, terminal exception', () => {
  const traces = tb.parse('job failed', PLAIN);
  assert.equal(traces.length, 1);
  const trace = traces[0];
  assert.equal(trace.frames.length, 1);
  assert.deepEqual(
    { file: trace.frames[0].file, line: trace.frames[0].line, fn: trace.frames[0].fn },
    { file: '/opt/app/energy.py', line: 118, fn: 'burst' }
  );
  assert.equal(trace.frames[0].source, 'raise ValueError("floor")');
  assert.equal(trace.frames[0].source_from_log, true, 'source text comes from the log, not the filesystem');
  assert.equal(trace.exceptions.length, 1);
  assert.equal(trace.exceptions[0].type, 'ValueError');
  assert.equal(trace.exceptions[0].message, 'floor');
  assert.equal(trace.status.complete, true);
  assert.equal(trace.status.truncated, false);
  assert.equal(trace.summary.terminal_type, 'ValueError');
});

test('explicit direct cause links two exceptions into one trace', () => {
  const lines = [
    'Traceback (most recent call last):',
    '  File "a.py", line 3, in outer',
    '    inner()',
    'ValueError: bad',
    '',
    'The above exception was the direct cause of the following exception:',
    '',
    'Traceback (most recent call last):',
    '  File "b.py", line 9, in inner',
    '    raise RuntimeError("wrapper")',
    'RuntimeError: wrapper'
  ];
  const traces = tb.parse('failed', lines);
  assert.equal(traces.length, 1, 'a marker keeps the chain in one trace');
  const trace = traces[0];
  assert.equal(trace.exceptions.length, 2);
  assert.deepEqual(trace.chain.links, [{ from: 0, to: 1, relation: 'cause', explained: true }]);
  assert.equal(trace.summary.primary_type, 'ValueError');
  assert.equal(trace.summary.terminal_type, 'RuntimeError');
  assert.equal(trace.status.complete, true);
});

test('"During handling" is a context link, not a cause', () => {
  const lines = [
    'Traceback (most recent call last):',
    '  File "a.py", line 1, in f',
    'ValueError: first',
    'During handling of the above exception, another exception occurred:',
    'Traceback (most recent call last):',
    '  File "b.py", line 2, in g',
    'KeyError: second'
  ];
  const trace = tb.parse('x', lines)[0];
  assert.equal(trace.exceptions.length, 2);
  assert.equal(trace.chain.links[0].relation, 'context');
});

test('adjacent tracebacks with no marker stay separate', () => {
  const lines = PLAIN.concat(['', 'Traceback (most recent call last):', '  File "c.py", line 1, in h', 'KeyError: other']);
  const traces = tb.parse('two failures', lines);
  assert.equal(traces.length, 2);
  assert.equal(traces[0].exceptions[0].type, 'ValueError');
  assert.equal(traces[0].status.reason, 'separate-traceback', 'the closed trace records why it ended');
  assert.equal(traces[1].exceptions[0].type, 'KeyError');
});

test('a traceback without a terminal exception is truncated, not clean', () => {
  const traces = tb.parse('cut off', ['Traceback (most recent call last):', '  File "a.py", line 5, in f']);
  assert.equal(traces.length, 1);
  assert.equal(traces[0].status.complete, false);
  assert.equal(traces[0].status.truncated, true);
  assert.equal(traces[0].status.reason, 'no-terminal-exception');
});

test('prefixed per-line stamped output parses like plain output', () => {
  const prefixed = ['| Traceback (most recent call last):', '|   File "a.py", line 3, in f', '| ValueError: bad'];
  const trace = tb.parse('p', prefixed)[0];
  assert.equal(trace.status.prefixed, true);
  assert.equal(trace.frames.length, 1);
  assert.equal(trace.frames[0].file, 'a.py');
  assert.equal(trace.exceptions[0].type, 'ValueError');
  assert.equal(trace.status.complete, true);
});

test('ExceptionGroup is flagged instead of being flattened', () => {
  const lines = [
    '  + Exception Group Traceback (most recent call last):',
    '  |   File "a.py", line 10, in main',
    '  | ValueError: one'
  ];
  const trace = tb.parse('group', lines)[0];
  assert.equal(trace.status.exception_group, true);
  assert.equal(trace.frames.length, 1, 'frames inside the group tree are still read');
  assert.equal(trace.exceptions[0].type, 'ValueError');
});

test('non-Python stacks are marked, not half-parsed', () => {
  const lines = ['java.lang.IllegalStateException: boom', '\tat com.acme.Svc.run(Svc.java:42)'];
  const traces = tb.parse('jvm', lines);
  assert.equal(traces.length, 1);
  assert.equal(traces[0].status.non_python, true);
  assert.equal(traces[0].status.reason, 'non-python');
  assert.equal(traces[0].frames.length, 0);
});

test('a bare exception without any traceback is still recorded', () => {
  const trace = tb.parse('ValueError: only a message', [])[0];
  assert.equal(trace.exceptions.length, 1);
  assert.equal(trace.status.reason, 'no-traceback');
  assert.equal(trace.status.complete, true);
});

test('frames keep exact line spans relative to the record', () => {
  const traces = tb.parse('m', PLAIN, { firstLine: 30 });
  const trace = traces[0];
  assert.deepEqual(trace.spans.map(s => [s.kind, s.first_line]), [
    ['traceback', 31], ['frame', 32], ['exception', 34]
  ]);
  assert.deepEqual(trace.frames[0].span, { first_line: 32, last_line: 33 });
});

test('aggregate() on a framer record keeps the record boundary', () => {
  const bytes = fs.readFileSync(path.join(harness.GOLDEN_DIR, 'traceback_chain.log'));
  const records = framer.frameAll(bytes, 'tb', 64);
  const results = records.map(record => tb.aggregate(record));
  const withTraces = results.filter(item => item.has_trace);
  assert.ok(withTraces.length >= 3, 'golden fixture contains several tracebacks');
  const truncated = records.filter(record => /truncated traceback follows/.test(record.message));
  assert.equal(truncated.length, 1);
  const truncatedAgg = tb.aggregate(truncated[0]);
  assert.equal(truncatedAgg.traces[0].status.complete, false, 'truncated traceback stays incomplete');
  for (const item of withTraces) {
    for (const trace of item.traces) {
      assert.ok(trace.summary.frames >= 0);
      assert.ok(trace.spans.length >= 1);
    }
  }
});

test('provisional record marks the trace incomplete', () => {
  const record = { message: 'running', body: PLAIN.slice(0, 3), lines: { first: 1, last: 3 }, provisional: true };
  const result = tb.aggregate(record);
  assert.equal(result.incomplete, true);
  assert.equal(result.traces[0].status.reason, 'no-terminal-exception');
  assert.equal(result.traces[0].status.provisional, true);
});
