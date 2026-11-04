/* R6-005 golden corpus contract.
   Pins R5 parser behaviour on cross-block, Unicode, Traceback and value-shape cases.
   Expectations were recorded from the shipped parser, not hand-written. */
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const harness = require('./parser-harness.cjs');

const GOLDEN = harness.GOLDEN_DIR;
const EXPECTED = JSON.parse(fs.readFileSync(path.join(GOLDEN, 'expected.json'), 'utf8'));
const MANIFEST = JSON.parse(fs.readFileSync(path.join(GOLDEN, 'manifest.json'), 'utf8'));

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function recordsOf(name) {
  return harness.parseFixture(name).map(harness.project);
}

function messages(records) {
  return records.flatMap((record) => [record.message].concat(record.body));
}

test('manifest matches the committed fixture bytes', () => {
  assert.strictEqual(MANIFEST.schema, 'logbench.r6.fixture-manifest/1');
  assert.ok(MANIFEST.files.length >= 4);
  for (const entry of MANIFEST.files) {
    const raw = fs.readFileSync(path.join(GOLDEN, entry.name));
    assert.strictEqual(raw.length, entry.bytes, entry.name + ' size drifted');
    assert.strictEqual(sha256(raw), entry.sha256, entry.name + ' hash drifted');
  }
});

test('expectations were recorded from the current parser source', () => {
  assert.strictEqual(EXPECTED.app_sha256, sha256(fs.readFileSync(harness.APP)),
    'src/app.js changed without re-recording golden expectations');
  for (const [name, fixture] of Object.entries(EXPECTED.fixtures)) {
    const raw = fs.readFileSync(path.join(GOLDEN, name));
    assert.strictEqual(sha256(raw), fixture.sha256, name + ' expectation out of sync');
  }
});

test('parser output equals recorded expectations', () => {
  for (const [name, fixture] of Object.entries(EXPECTED.fixtures)) {
    assert.deepStrictEqual(recordsOf(name), fixture.records, name);
  }
});

test('value shapes survive verbatim (0 / false / null / empty / 19-digit)', () => {
  const records = recordsOf('edge_cases.log');
  const warning = records.find((record) => record.level === 'WARNING');
  assert.ok(warning.message.includes('retry: 0'));
  assert.ok(warning.message.includes('enabled: false'));
  assert.ok(warning.message.includes('payload: null'));
  assert.ok(/note:\s?$/.test(warning.message), 'empty value must not become null');

  const bigint = records.find((record) => record.message.startsWith('burstStartTime'));
  assert.ok(bigint.message.includes('1234567890123456789'), '19-digit integer lost precision');
  assert.ok(bigint.message.includes('1234567890173456789'));
  assert.strictEqual(typeof bigint.fields[0][1], 'string');
  assert.ok(bigint.fields[0][1].includes('1234567890123456789'));
});

test('physical order is preserved even when timestamps descend or repeat', () => {
  const records = recordsOf('edge_cases.log');
  const keys = records.map((record) => record.key);
  assert.strictEqual(keys.length, 10);
  assert.ok(keys[7] > keys[8], 'descending timestamps must stay in physical order');
  assert.strictEqual(keys[8], keys[9], 'duplicate timestamps must both survive');
  assert.ok(records[8].message.startsWith('iteration: 9 energy'));
  assert.ok(records[9].message.startsWith('iteration: 9 err_code'));
});

test('traceback bodies stay attached and complete', () => {
  const records = recordsOf('traceback_chain.log');
  const chained = records[0];
  assert.strictEqual(chained.hasTrace, true);
  assert.strictEqual(chained.kind, 'stack');
  assert.ok(chained.body.includes('ValueError: bad recipe'));
  assert.ok(chained.body.includes('The above exception was the direct cause of the following exception:'));
  assert.ok(chained.body.filter((line) => line.includes('Traceback (most recent call last):')).length >= 2);

  const truncated = records.find((record) => record.message.startsWith('truncated traceback'));
  assert.strictEqual(truncated.hasTrace, true);
  assert.ok(truncated.body[truncated.body.length - 1].includes('no terminal line'),
    'truncated stack must keep its last physical line instead of inventing a terminator');

  const grouped = records.find((record) => record.message.startsWith('ExceptionGroup raised'));
  assert.ok(grouped.body.some((line) => line.includes('Exception Group Traceback')));
  assert.ok(grouped.body.some((line) => line.includes('ExceptionGroup: grouped failures')));
  assert.ok(grouped.body.some((line) => line.includes('ValueError: first')));
  assert.ok(grouped.body.some((line) => line.includes('KeyError: second')));
});

test('CRLF and BOM do not leak into field values', () => {
  const records = recordsOf('unicode_crlf_bom.log');
  assert.strictEqual(records.length, 3);
  assert.strictEqual(records[0].level, 'EVENT', 'BOM must not break the first header');
  assert.ok(records[0].ts && records[0].ts.startsWith('2020-01-01'));
  for (const text of messages(records)) {
    assert.ok(!text.includes('\r'), 'carriage return leaked into a value');
    assert.ok(!text.includes('\ufeff'), 'BOM leaked into a value');
  }
  assert.ok(records[0].message.includes('参数：轴1＝100.0'));
  assert.ok(records[1].message.includes('（６５℃）'), 'full-width punctuation must survive');
});

test('continuation body survives a missing trailing newline', () => {
  const records = recordsOf('no_trailing_newline.log');
  assert.strictEqual(records.length, 2);
  assert.strictEqual(records[0].kind, 'kv');
  assert.deepStrictEqual(records[0].body.slice(0, 2), ['burstEuvEnergy3sigma: 0.21338501572608948',
    'burstDropletCount: 420']);
  assert.strictEqual(records[1].message, 'best_index_mask: 0');
});

test('known parser defects stay documented, not silently normalised', () => {
  assert.ok(EXPECTED.known_defects.length >= 2);
  const ids = EXPECTED.known_defects.map((item) => item.id);
  assert.ok(ids.includes('orphan-absorbed'));
  assert.ok(ids.includes('prefixed-traceback-split'));

  const absorbed = recordsOf('edge_cases.log')[6];
  assert.deepStrictEqual(absorbed.body, ['', 'orphan line without any bracket header'],
    'orphan absorption is the current contract; changing it needs a deliberate update');

  const stamped = recordsOf('traceback_chain.log');
  const first = stamped.find((record) => record.message.startsWith('| Traceback'));
  assert.ok(first && first.hasTrace, 'first stamped stack line must be flagged');
  assert.ok(!first.body.length, 'current R5 behaviour: stamped stack lines are not merged');
});
