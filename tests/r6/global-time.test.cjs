/*
 * global-time.test.cjs — R6-079 selection and range model.
 *
 * The dialog itself needs a browser; what can be wrong without one is the model:
 * "a folder includes its descendants", "cancelling one file inside it excludes only
 * that file", "a saved view re-discovers descendants", and "one start later than the
 * end is refused". Those are asserted here.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const gt = require(path.join(__dirname, '..', '..', 'src', 'ui', 'global-time.js'));

const SCOPE = {
  local: [
    { file_id: 'l-a', name: 'a.log', path: 'a.log' },
    { file_id: 'l-b', name: 'b.log', path: 'dir/b.log' }
  ],
  shared: [
    { fileId: 's-1', displayName: 'one.log', relPath: 'sw/one.log', rootLabel: 'main' },
    { fileId: 's-2', displayName: 'two.log', relPath: 'sw/two.log', rootLabel: 'main' },
    { fileId: 's-3', displayName: 'three.log', relPath: 'diag/three.log', rootLabel: 'main' }
  ],
  private: [
    { fileId: 'p-1', displayName: 'mine.log', relPath: 'mine.log', rootLabel: 'private' }
  ]
};

function ids(selection, roots) {
  return gt.resolveSelection(roots, selection).files.map((file) => file.file_id).sort();
}

function node(roots, label) {
  const stack = roots.slice();
  while (stack.length) {
    const current = stack.shift();
    if (current.label === label) return current;
    if (current.children) stack.push(...current.children);
  }
  return null;
}

test('the tree keeps three sources apart and nests directories', () => {
  const roots = gt.buildTree(SCOPE);
  assert.deepEqual(roots.map((root) => root.id),
    ['root:local', 'root:shared', 'root:private']);
  const shared = roots[1];
  assert.deepEqual(shared.children.map((child) => child.type), ['dir', 'dir']);
  const dir = node(roots, 'sw');
  assert.equal(gt.filesUnder(dir).length, 2);
  // A directory id is stable across rebuilds: that is what a saved view stores.
  assert.deepEqual(gt.buildTree(SCOPE)[1].children.map((child) => child.id),
    shared.children.map((child) => child.id));
});

test('selecting a folder selects every descendant', () => {
  const roots = gt.buildTree(SCOPE);
  const shared = roots[1];
  const selection = gt.toggle(roots, gt.emptySelection(), shared);
  assert.deepEqual(ids(selection, roots),
    ['p-1', 's-1', 's-2', 's-3'].filter((id) => id.indexOf('s-') === 0).sort());
  assert.equal(gt.stateOf(roots, selection, shared), 'on');
});

test('cancelling one file inside a selected folder excludes only that file', () => {
  const roots = gt.buildTree(SCOPE);
  const shared = roots[1];
  let selection = gt.toggle(roots, gt.emptySelection(), shared);
  const file = node(roots, 'two.log');
  selection = gt.toggle(roots, selection, file);
  assert.deepEqual(ids(selection, roots), ['s-1', 's-3']);
  assert.equal(gt.stateOf(roots, selection, shared), 'mixed');
  // Both the root and the directory are partially selected; the page shows the
  // tri-state on each level so a collapsed folder is not mistaken for "all".
  const partial = gt.resolveSelection(roots, selection).partial;
  assert.ok(partial.includes(shared.id), JSON.stringify(partial));
});

test('selecting a single file inside an unselected folder selects just that file', () => {
  const roots = gt.buildTree(SCOPE);
  const file = node(roots, 'three.log');
  const selection = gt.toggle(roots, gt.emptySelection(), file);
  assert.deepEqual(ids(selection, roots), ['s-3']);
  assert.equal(gt.stateOf(roots, selection, node(roots, 'diag')), 'on');
  assert.equal(gt.stateOf(roots, selection, roots[1]), 'mixed');
});

test('the local and server roots can be mixed in one selection', () => {
  const roots = gt.buildTree(SCOPE);
  let selection = gt.toggle(roots, gt.emptySelection(), roots[0]);
  selection = gt.toggle(roots, selection, node(roots, 'mine.log'));
  assert.deepEqual(ids(selection, roots), ['l-a', 'l-b', 'p-1']);
});

test('toggling a folder twice returns to an empty selection', () => {
  const roots = gt.buildTree(SCOPE);
  const shared = roots[1];
  let selection = gt.toggle(roots, gt.emptySelection(), shared);
  selection = gt.toggle(roots, selection, shared);
  assert.deepEqual(ids(selection, roots), []);
});

/* --------------------------------------------------------------------- ranges */

test('a range is exact to the nanosecond and closed at both ends', () => {
  const result = gt.makeRange(
    { date: '2026-03-01', clock: '08:00:00', fraction: '123456' },
    { date: '2026-03-01', clock: '09:30:15', fraction: '' });
  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(result.range.from_ns, '1772352000123456000');
  assert.equal(result.range.closed, true);
  assert.equal(result.range.timezone, 'UTC');
  assert.ok(BigInt(result.range.from_ns) < BigInt(result.range.to_ns));
});

test('a missing or malformed time is refused with a code the UI can show', () => {
  const cases = [
    [{ date: '', clock: '00:00' }, {}, 'missing_time'],
    [{ date: '2026-13-01', clock: '00:00' }, {}, 'invalid_date'],
    [{ date: '2026-03-01', clock: '24:00' }, {}, 'invalid_time'],
    [{ date: '2026-03-01', clock: '08:00' }, { fraction: '1234567890' }, 'invalid_fraction']
  ];
  for (const [from, extra, code] of cases) {
    const result = gt.makeRange(Object.assign({ fraction: '' }, from),
      { date: '2026-03-02', clock: '00:00', fraction: extra.fraction || '' });
    assert.equal(result.ok, false, code);
    assert.equal(result.error.code, code, code);
  }
});

test('a start later than the end is refused instead of silently swapped', () => {
  const result = gt.makeRange({ date: '2026-03-02', clock: '00:00', fraction: '' },
    { date: '2026-03-01', clock: '00:00', fraction: '' });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'range_inverted');
});

/* ------------------------------------------------------------------ saved views */

test('a saved view re-discovers files added to the folder later', () => {
  const roots = gt.buildTree(SCOPE);
  const selection = gt.toggle(roots, gt.emptySelection(), node(roots, 'sw'));
  const view = gt.viewFrom({ selection, range: { from_text: 'a', to_text: 'b' },
    search: { text: 'iter', flags: {} } }, '我的范围');
  const before = gt.applyView(roots, view);
  assert.deepEqual(before.files.map((file) => file.file_id).sort(), ['s-1', 's-2']);
  const grown = SCOPE.shared.concat([
    { fileId: 's-4', displayName: 'four.log', relPath: 'sw/four.log', rootLabel: 'main' }
  ]);
  const after = gt.applyView(gt.buildTree(Object.assign({}, SCOPE, { shared: grown })), view);
  assert.deepEqual(after.files.map((file) => file.file_id).sort(), ['s-1', 's-2', 's-4']);
  assert.deepEqual(after.missing, []);
  assert.equal(view.search.text, 'iter');
});

test('a saved view reports a vanished file instead of substituting one', () => {
  const roots = gt.buildTree(SCOPE);
  let selection = gt.toggle(roots, gt.emptySelection(), node(roots, 'sw'));
  selection = gt.toggle(roots, selection, node(roots, 'diag'));
  const view = gt.viewFrom({ selection, range: null, search: null }, '混合');
  const smaller = SCOPE.shared.filter((file) => file.fileId !== 's-3');
  const result = gt.applyView(gt.buildTree(Object.assign({}, SCOPE, { shared: smaller })), view);
  assert.equal(result.files.length, 2);
  assert.equal(result.missing.length, 1);
  assert.equal(result.missing[0].id, node(roots, 'diag').id);
  assert.equal(result.missing[0].reason, 'folder_missing');
});

test('an excluded file that no longer exists is reported, not dropped silently', () => {
  const roots = gt.buildTree(SCOPE);
  let selection = gt.toggle(roots, gt.emptySelection(), node(roots, 'sw'));
  selection = gt.toggle(roots, selection, node(roots, 'one.log'));
  const view = gt.viewFrom({ selection, range: null, search: null }, 'x');
  const next = gt.buildTree(Object.assign({}, SCOPE, {
    shared: SCOPE.shared.filter((file) => file.fileId !== 's-1')
  }));
  const result = gt.applyView(next, view);
  assert.deepEqual(result.files.map((file) => file.file_id), ['s-2']);
  assert.ok(result.missing.some((item) => item.reason === 'file_missing'));
});
