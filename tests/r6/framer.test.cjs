/*
 * framer.test.cjs — R6-009 acceptance.
 *
 * The claim under test is not "it parses logs" but "it frames byte-for-byte the
 * same logical records as R5, under any chunk boundary, and can resume".
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const framer = require(path.join(ROOT, 'src', 'shared', 'framer.js'));
const harness = require(path.join(__dirname, 'parser-harness.cjs'));

const SHAPE = ['ts', 'level', 'logger', 'pid', 'tid', 'src', 'src_line', 'fn', 'message', 'has_trace'];

function comparable(record) {
  return {
    lines: [record.lines.first, record.lines.last],
    ts: record.ts === '' ? null : record.ts,
    level: record.level,
    logger: record.logger,
    pid: record.pid,
    tid: record.tid,
    src: record.src,
    src_line: record.src_line,
    fn: record.fn,
    message: record.message,
    has_trace: record.has_trace,
    body: record.body
  };
}

function r5Comparable(record) {
  return {
    lines: [record.line, record.lineEnd],
    ts: record.ts ? record.ts : null,
    level: record.level,
    logger: record.logger,
    pid: record.pid,
    tid: record.tid,
    src: record.src,
    src_line: record.srcLine,
    fn: record.fn,
    message: record.message,
    has_trace: record.hasTrace === true,
    body: record.body || []
  };
}

function readFixture(name) {
  return fs.readFileSync(path.join(harness.GOLDEN_DIR, name));
}

function fixtures() {
  return harness.listFixtures();
}

test('same logical records as the real R5 parseLocal', () => {
  const { parseLocal } = harness.loadParser();
  const mismatches = [];
  for (const name of fixtures()) {
    const bytes = readFixture(name);
    const expected = parseLocal(bytes.toString('utf8'), name).map(r5Comparable);
    const actual = framer.frameAll(bytes, name, 65536).map(comparable);
    if (expected.length !== actual.length) {
      mismatches.push(`${name}: count ${expected.length} vs ${actual.length}`);
      continue;
    }
    for (let i = 0; i < expected.length; i++) {
      for (const key of Object.keys(expected[i])) {
        if (JSON.stringify(expected[i][key]) !== JSON.stringify(actual[i][key])) {
          mismatches.push(`${name}#${i}.${key}: ${JSON.stringify(expected[i][key]).slice(0, 80)} vs ${JSON.stringify(actual[i][key]).slice(0, 80)}`);
          break;
        }
      }
    }
  }
  assert.deepEqual(mismatches, []);
});

test('identical output for every chunk boundary', () => {
  const name = 'edge_cases.log';
  const bytes = readFixture(name);
  const reference = framer.frameAll(bytes, name, bytes.length);
  for (const size of [1, 2, 3, 5, 7, 13, 64, 257, 1024, 4096, 65536]) {
    const chunked = framer.frameAll(bytes, name, size);
    assert.deepEqual(chunked.map(comparable), reference.map(comparable), `chunk size ${size}`);
  }
});

test('checkpoint at any offset resumes to the same result', () => {
  const name = 'traceback_chain.log';
  const bytes = readFixture(name);
  const reference = framer.frameAll(bytes, name, 64).map(comparable);
  for (const cut of [1, 7, 30, 61, 120, 200]) {
    if (cut >= bytes.length) continue;
    const first = framer.createFramer({ datasetId: name });
    const collected = first.push(bytes.subarray(0, cut));
    const saved = first.checkpoint();
    assert.equal(saved.schema, 'logbench.framer-checkpoint/1');
    const resumed = framer.createFramer({ datasetId: name });
    resumed.restore(JSON.parse(JSON.stringify(saved)));
    const rest = resumed.push(bytes.subarray(cut));
    const all = collected.concat(rest, resumed.flush({ eof: true }));
    assert.deepEqual(all.map(comparable), reference, `cut at ${cut}`);
  }
});

test('every byte range slices back to the record raw lines', () => {
  for (const name of fixtures()) {
    const bytes = readFixture(name);
    for (const record of framer.frameAll(bytes, name, 97)) {
      const start = Number(record.bytes.start);
      const end = Number(record.bytes.end);
      assert.ok(end >= start, 'range ordered');
      const slice = bytes.subarray(start, end).toString('utf8').replace(/\r?\n$/, '').replace(/^\ufeff/, '');
      const rebuilt = record.raw.join('\n');
      assert.equal(slice.replace(/\r\n/g, '\n'), rebuilt, `${name}#${record.ordinal} slice mismatch`);
    }
  }
});

test('CRLF and BOM produce the same records as LF', () => {
  const lf = Buffer.from('[2020-01-01 00:00:00.001] [INFO] [a] [1] [2] [x.py:3] [m] [n] fn() hello\nnext line\n', 'utf8');
  const crlf = Buffer.from('\ufeff' + lf.toString('utf8').replace(/\n/g, '\r\n'), 'utf8');
  const a = framer.frameAll(lf, 'a', 3).map(comparable);
  const b = framer.frameAll(crlf, 'a', 4).map(comparable);
  assert.deepEqual(b, a);
  assert.equal(a.length, 1);
  assert.equal(a[0].message, 'hello');
  assert.deepEqual(a[0].body, ['next line']);
  assert.deepEqual(a[0].lines, [1, 2]);
});

test('a multi-byte character split across chunks survives', () => {
  const text = '[2020-01-01 00:00:00.001] [INFO] [a] [1] [2] [x.py:3] [m] [n] fn() 中文日志正常\n';
  const bytes = Buffer.from(text, 'utf8');
  const charIndex = bytes.indexOf(Buffer.from('中', 'utf8'));
  for (const cut of [charIndex, charIndex + 1, charIndex + 2]) {
    const f = framer.createFramer({ datasetId: 'u' });
    const out = f.push(bytes.subarray(0, cut)).concat(f.push(bytes.subarray(cut)));
    const all = out.concat(f.flush({ eof: true }));
    assert.equal(all.length, 1, `cut ${cut}`);
    assert.equal(all[0].message, '中文日志正常');
    assert.equal(all[0].level, 'INFO');
  }
});

test('empty input and a single unterminated line', () => {
  assert.deepEqual(framer.frameAll(Buffer.from('', 'utf8'), 'e', 8), []);
  const one = framer.frameAll(Buffer.from('stray', 'utf8'), 'e', 8);
  assert.equal(one.length, 1);
  assert.equal(one[0].level, 'UNPARSED');
  assert.equal(one[0].message, 'stray');
});

test('appending file: provisional snapshot first, final record second', () => {
  const head = '[2020-01-01 00:00:00.001] [ERROR] [a] [1] [2] [x.py:3] [m] [n] fn() boom\n';
  const tail = 'Traceback (most recent call last):\n  File "x.py", line 3\nValueError: bad\n';
  const f = framer.createFramer({ datasetId: 'g' });
  const first = f.push(Buffer.from(head, 'utf8'));
  assert.equal(first.length, 0, 'nothing final yet');
  const provisional = f.flush({ eof: false });
  assert.equal(provisional.length, 1);
  assert.equal(provisional[0].provisional, true);
  assert.equal(provisional[0].version, 1);
  assert.equal(provisional[0].record_id, 'g-0');
  const rest = f.push(Buffer.from(tail, 'utf8'));
  const done = rest.concat(f.flush({ eof: true }));
  assert.equal(done.length, 1);
  assert.equal(done[0].record_id, provisional[0].record_id, 'same identity when the tail arrives');
  assert.equal(done[0].version, 2, 'version increments after a provisional publish');
  assert.equal(done[0].provisional, false);
  assert.equal(done[0].has_trace, true);
  assert.deepEqual({ first: done[0].lines.first, last: done[0].lines.last }, { first: 1, last: 4 });
  assert.equal(done[0].body.length, 3, 'traceback tail is folded into the record');
});

test('EOF flush and append flush stay distinguishable', () => {
  const f = framer.createFramer({ datasetId: 'z' });
  f.push(Buffer.from('[2020-01-01 00:00:00.001] [INFO] [a] [1] [2] [x.py:3] [m] [n] fn() x\n', 'utf8'));
  const provisional = f.flush({ eof: false });
  assert.equal(provisional.length, 1, 'provisional published');
  assert.equal(provisional[0].provisional, true);
  const fin = f.flush({ eof: true });
  assert.equal(fin.length, 1, 'final record published on EOF');
  assert.equal(fin[0].version, 2);
  assert.equal(fin[0].provisional, false);
});

test('an unterminated trailing line is not published as a record', () => {
  const f = framer.createFramer({ datasetId: 'p' });
  assert.equal(f.push(Buffer.from('partial-line-without-newline', 'utf8')).length, 0);
  assert.equal(f.flush({ eof: false }).length, 0, 'no line boundary yet');
  const done = f.flush({ eof: true });
  assert.equal(done.length, 1, 'EOF completes the line');
  assert.equal(done[0].message, 'partial-line-without-newline');
});

test('reset returns the framer to a clean state', () => {
  const f = framer.createFramer({ datasetId: 'r' });
  f.push(Buffer.from('stray\n', 'utf8'));
  f.reset();
  assert.equal(f.stats().records, 0);
  assert.equal(f.isOpen(), false);
});
