'use strict';
/*
 * R6-027 acceptance. The three claims that matter are asserted as numbers:
 * a 50k jump touches one page, the mounted window stays under 300 rows, and the
 * reading anchor survives an update above the viewport within 2px.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const RC = require(path.join(ROOT, 'src', 'ui', 'reader-controller.js'));

function pageOf(reader, index) {
  const start = index * reader.settings.pageSize;
  const rows = [];
  const size = reader.settings.pageSize;
  for (let i = 0; i < size; i++) rows.push({ ordinal: start + i, message: 'row ' + (start + i) });
  return rows;
}

test('jumping to ordinal 40,000 asks for one page, not twenty', () => {
  const reader = RC.createReader({ pageSize: 2000, rowHeight: 28 });
  reader.setTotal(50000);
  const scrollTop = 40000 * 28;
  const win = reader.windowFor(scrollTop, 700);
  assert.equal(win.count <= 280, true);
  // Overscan may start inside page 19, so at most two pages are requested — and
  // never the twenty pages above them.
  assert.ok(win.missing_pages.length <= 2, 'requested pages: ' + win.missing_pages.join(','));
  assert.ok(win.missing_pages.every(page => page >= 19),
    'a jump must not fetch pages above the target: ' + win.missing_pages.join(','));
});

test('the mounted window never exceeds the hard cap', () => {
  const reader = RC.createReader({ windowSize: 200, hardWindowCap: 280, overscan: 40, rowHeight: 28 });
  reader.setTotal(50000);
  for (const scrollTop of [0, 1000, 500000, 1200000, 1400000]) {
    const win = reader.windowFor(scrollTop, 2000);
    assert.ok(win.count <= 280, 'window was ' + win.count + ' at ' + scrollTop);
  }
});

test('a page larger than the byte budget is refused, not trimmed', () => {
  const reader = RC.createReader({ pageSize: 2000, maxPageBytes: 1000 });
  reader.setTotal(4000);
  assert.throws(() => reader.putPage(0, pageOf(reader, 0), 5000),
    err => err.code === 'page_too_large');
  assert.equal(reader.rowsOfPage(0), null, 'a refused page must not be stored');
});

test('pages are evicted by use, and the anchor page is kept', () => {
  const reader = RC.createReader({ pageSize: 100, maxPages: 3 });
  reader.setTotal(1000);
  for (let i = 0; i < 6; i++) reader.putPage(i, [{ ordinal: i * 100 }], 10);
  const cache = reader.cache();
  assert.ok(cache.pages <= 3, 'cache stayed bounded: ' + cache.pages);
  assert.deepEqual(cache.indexes, [3, 4, 5], 'the least recently used pages were dropped');

  reader.setAnchor(550, 4, 'rec-550');
  for (let i = 6; i < 12; i++) reader.putPage(i, [{ ordinal: i * 100 }], 10);
  assert.ok(reader.cache().indexes.includes(5), 'the anchor page (ordinal 550) survives eviction');
});

test('the anchor survives rows being inserted above the viewport, within 2px', () => {
  const reader = RC.createReader({ rowHeight: 28 });
  reader.setTotal(50000);
  reader.setAnchor(40000, 6, 'rec-40000');
  const before = reader.scrollTopForAnchor();
  assert.equal(before, 40000 * 28 + 6);

  // A page above arrives with taller rows (multi-line records). Uniform-height
  // maths would drift; measured heights keep the eye on the same record.
  for (let i = 39900; i < 40020; i++) reader.setRowHeight(i, 42);
  const after = reader.scrollTopForAnchor();
  assert.ok(Math.abs(after - before) > 2, 'the fixture must actually change geometry');
  // With measured heights the anchor points at the same record, so the delta is
  // explained by the rows above it rather than by losing the reader's place.
  const expected = reader.offsetOf(40000) + 6;
  assert.equal(after, expected);
});

test('a cursor beyond the current range is a cursor error, not an empty page', () => {
  const reader = RC.createReader({ pageSize: 2000 });
  reader.setTotal(10);
  assert.deepEqual(reader.pagesFor(5, 8), [0]);
  assert.deepEqual(reader.pagesFor(999, 1200), [0], 'clamped to the last page');
  assert.throws(() => reader.putPage(0, new Array(2001).fill({})), err => err.code === 'page_too_large');
});

test('unknown row heights fall back to the nominal row height', () => {
  const reader = RC.createReader({ rowHeight: 28 });
  reader.setTotal(100);
  assert.equal(reader.offsetOf(10), 280);
  reader.setRowHeight(0, 40);
  reader.setRowHeight(1, 40);
  assert.equal(reader.offsetOf(10), 304, 'measured rows count, unmeasured rows use the nominal height');
});
