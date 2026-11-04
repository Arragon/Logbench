/*
 * compare-axis.test.cjs — R6-080 alignment oracle.
 *
 * Time comparison fails in four specific ways, and each has a test here:
 *   - a timestamp present in two files becomes two rows, not one (no silent merge);
 *   - a timestamp missing from one file is an empty slot, not the previous value;
 *   - several records sharing one timestamp keep all of them (duplicates survive);
 *   - an undated file is not placed on the axis at all.
 *
 * The window request is also asserted, because "same anchor" has to mean the same
 * absolute range for every pane — not the same scroll percentage.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const compare = require(path.join(__dirname, '..', '..', 'src', 'ui', 'compare.js'));

function rec(ordinal, tsMs, extra) {
  return Object.assign({
    ordinal,
    ts_key: tsMs,
    ts_ns: tsMs == null ? null : String(BigInt(tsMs) * 1000000n),
    ts: tsMs == null ? '' : new Date(tsMs).toISOString(),
    level: 'INFO',
    message: 'm' + ordinal
  }, extra || {});
}

const T0 = Date.UTC(2026, 2, 1, 8, 0, 0);
const MIN = 60 * 1000;

/* -------------------------------------------------------------------- layout */

test('2 and 3 files default to columns, 4 files default to a 2x2 grid', () => {
  assert.equal(compare.defaultLayout(2), 'columns');
  assert.equal(compare.defaultLayout(3), 'columns');
  assert.equal(compare.defaultLayout(4), 'grid');
  assert.equal(compare.defaultLayout(4, 'row'), 'row');
  assert.deepEqual(compare.layoutStyle('grid', 4), { columns: 2, rows: 2 });
  assert.deepEqual(compare.layoutStyle('columns', 3), { columns: 3, rows: 1 });
});

test('the file limits are enforced instead of silently trimmed', () => {
  assert.throws(() => compare.validateFiles([{ file_id: 'a' }]), /至少需要 2 个文件/);
  assert.throws(() => compare.validateFiles([1, 2, 3, 4, 5].map((n) => ({ file_id: 'f' + n }))),
    /最多 4 个文件/);
  assert.throws(() => compare.validateFiles([{ file_id: 'a' }, { file_id: 'a' }]),
    /不能对比两次/);
});

/* -------------------------------------------------------------------- matrix */

test('a shared timestamp becomes one slot with one row per file', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0), rec(1, T0 + MIN)] },
    { file_id: 'b', rows: [rec(0, T0), rec(1, T0 + MIN)] }
  ]);
  assert.equal(matrix.slots.length, 2);
  assert.deepEqual(matrix.slots.map((slot) => slot.ts_key), [T0, T0 + MIN]);
  assert.deepEqual(matrix.slots[0].cells.map((cell) => cell.length), [1, 1]);
  assert.equal(matrix.slots[0].lines, 1);
});

test('a missing timestamp is an empty slot and never the previous value', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0), rec(1, T0 + MIN), rec(2, T0 + 2 * MIN)] },
    { file_id: 'b', rows: [rec(0, T0), rec(1, T0 + 2 * MIN)] }
  ]);
  assert.equal(matrix.slots.length, 3);
  assert.deepEqual(matrix.slots.map((slot) => slot.cells[1].length), [1, 0, 1]);
  assert.equal(matrix.slots[1].cells[1][0], undefined,
    'the middle slot must not be filled with the previous row');
});

test('duplicate timestamps inside one file are all kept and pad the others', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0), rec(1, T0), rec(2, T0)] },
    { file_id: 'b', rows: [rec(0, T0)] }
  ]);
  assert.equal(matrix.slots.length, 1);
  assert.equal(matrix.slots[0].cells[0].length, 3);
  assert.equal(matrix.slots[0].cells[1].length, 1);
  assert.equal(matrix.slots[0].lines, 3, 'the slot is three lines tall in every pane');
  assert.deepEqual(matrix.slots[0].cells[0].map((row) => row.ordinal), [0, 1, 2]);
});

test('rows arriving out of order are sorted into the axis', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(2, T0 + 2 * MIN), rec(0, T0), rec(1, T0 + MIN)] }
  ]);
  assert.deepEqual(matrix.slots.map((slot) => slot.ts_key), [T0, T0 + MIN, T0 + 2 * MIN]);
});

test('two files in different time domains share the axis without inventing rows', () => {
  const later = Date.UTC(2026, 3, 15, 12, 0, 0);
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0)] },
    { file_id: 'b', rows: [rec(0, later)] }
  ]);
  assert.deepEqual(matrix.slots.map((slot) => slot.ts_key), [T0, later]);
  assert.deepEqual(matrix.slots.map((slot) => slot.cells.map((cell) => cell.length)),
    [[1, 0], [0, 1]]);
});

test('an undated file is reported, not merged onto the axis', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0)] },
    { file_id: 'b', rows: [rec(0, null)] }
  ]);
  assert.equal(matrix.slots.length, 1);
  assert.equal(matrix.untimed_count, 1);
  assert.equal(matrix.untimed[1].length, 1);
  assert.equal(matrix.untimed[1][0].row.ordinal, 0);
});

test('the slot count is bounded and says when it truncated', () => {
  const rows = [];
  for (let index = 0; index < 50; index++) rows.push(rec(index, T0 + index * 1000));
  const matrix = compare.buildMatrix([{ file_id: 'a', rows }], { max_slots: 10 });
  assert.equal(matrix.slots.length, 10);
  assert.equal(matrix.truncated, true);
  assert.equal(matrix.first_ts_key, T0);
});

test('an empty window produces no slots and no invented header', () => {
  const matrix = compare.buildMatrix([{ file_id: 'a', rows: [] }, { file_id: 'b', rows: [] }]);
  assert.equal(matrix.slots.length, 0);
  assert.equal(matrix.first_ts_key, null);
  assert.equal(matrix.columns.length, 2);
});

/* -------------------------------------------------------------------- windows */

test('every pane asks for the same absolute window from one anchor', () => {
  const files = [{ file_id: 'a', source: 'local' }, { file_id: 'b', source: 'server' }];
  const specs = compare.windowsFor(files, T0, 5 * MIN * 1000000, 400);
  assert.equal(specs.length, 2);
  const expectedFrom = (BigInt(T0) * 1000000n).toString();
  const expectedTo = (BigInt(T0) * 1000000n + BigInt(5 * MIN * 1000000)).toString();
  for (const spec of specs) {
    assert.equal(spec.from_ns, expectedFrom);
    assert.equal(spec.to_ns, expectedTo);
    assert.equal(spec.limit, 400);
  }
  assert.equal(specs[0].file_id, 'a');
  assert.equal(specs[1].file_id, 'b');
});

test('an anchor-free window is an explicit null, not a zero', () => {
  const specs = compare.windowsFor([{ file_id: 'a' }], null);
  assert.equal(specs[0].from_ns, null);
  assert.equal(specs[0].to_ns, null);
});

test('the anchor is the first timestamp at or after the requested slot', () => {
  const matrix = compare.buildMatrix([
    { file_id: 'a', rows: [rec(0, T0), rec(1, T0 + MIN)] },
    { file_id: 'b', rows: [rec(0, T0 + MIN)] }
  ]);
  assert.equal(compare.anchorOf(matrix, 0), T0);
  assert.equal(compare.anchorOf(matrix, 1), T0 + MIN);
  assert.equal(compare.anchorOf(matrix, 9), null);
});
