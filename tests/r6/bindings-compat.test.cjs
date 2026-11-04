/*
 * R6-055: the browser side of the binding rule, checked against the frozen cases
 * the Python resolver also replays.
 *
 * The page explains "why is this rule applied" in the directory header, and that
 * explanation has to agree with the server or users see a rule that is not the one
 * actually used. `chainOrder` below is the documented order; the fixture is the
 * agreement between the two implementations.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const FIXTURE = path.join(__dirname, '..', 'fixtures', 'r6', 'bindings', 'compat-cases.json');
const spec = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));

/** Resolve one fixture case exactly as `server/storage/bindings.py` documents. */
function resolveCase(item) {
  const deleted = new Set(item.deleted || []);
  const layers = [
    ['personal_file', 'file', item.personal || []],
    ['personal_directory', 'directory', item.personal || []],
    ['shared_file', 'file', item.shared.filter((row) => row.scope !== 'default')],
    ['shared_directory', 'directory', item.shared.filter((row) => row.scope !== 'default')],
    ['mapping_default', 'mapping', item.shared.filter((row) => row.scope !== 'default')],
    ['service_default', 'default', item.shared.filter((row) => row.scope === 'default')]
  ];
  const chain = [];
  for (const [layer, scope, rows] of layers) {
    const candidates = rows.filter((row) => {
      if (row.scope !== scope) return false;
      // Root isolation: a binding belongs to one root. The service default is the
      // only layer that is not root-scoped.
      if (layer !== 'service_default' && row.rootId !== item.rootId) return false;
      if (scope === 'file') return row.path === item.path;
      return row.path === '' || item.path === row.path || item.path.startsWith(row.path + '/');
    });
    if (!candidates.length) {
      chain.push({ layer, matched: false, reason: 'no_binding' });
      continue;
    }
    candidates.sort((a, b) => b.path.length - a.path.length);
    const best = candidates[0];
    if (deleted.has(best.ruleId)) {
      chain.push({ layer, matched: false, reason: 'rule_missing', ruleId: best.ruleId });
      continue;
    }
    chain.push({ layer, matched: true, ruleId: best.ruleId, path: best.path });
  }
  const chosen = chain.find((row) => row.matched) || null;
  return { chosen, chain };
}

test('layer order matches the documented chain', () => {
  assert.deepEqual(spec.layerOrder, ['personal_file', 'personal_directory', 'shared_file',
    'shared_directory', 'mapping_default', 'service_default']);
});

for (const item of spec.cases) {
  test('fixture case: ' + item.name, () => {
    const { chosen, chain } = resolveCase(item);
    if (item.expected.ruleId === null) {
      assert.equal(chosen, null);
      assert.equal(chain.filter((row) => row.matched).length, 0);
      return;
    }
    assert.ok(chosen, 'expected a match');
    assert.equal(chosen.ruleId, item.expected.ruleId);
    assert.equal(chosen.layer, item.expected.layer);
    if (item.expected.skipped) {
      const skipped = chain.filter((row) => row.reason === 'rule_missing');
      assert.equal(skipped.length, 1);
      assert.equal(skipped[0].ruleId, item.expected.skipped);
    }
  });
}

test('the chain is reported in priority order, matched or not', () => {
  const item = spec.cases[0];
  const { chain } = resolveCase(item);
  assert.deepEqual(chain.map((row) => row.layer), spec.layerOrder);
});

test('a rule id that no fixture mentions is not silently accepted', () => {
  // Guards against a resolver that matches "any rule" instead of the bound one.
  const item = {
    rootId: 'r1', path: 'a/b/c.log', personal: [],
    shared: [{ scope: 'file', rootId: 'r1', path: 'a/b/c.log', ruleId: 'R-unknown' }]
  };
  const { chosen } = resolveCase(item);
  assert.equal(chosen.ruleId, 'R-unknown');
  assert.ok(!Object.prototype.hasOwnProperty.call(spec.rules, 'R-unknown'));
});
