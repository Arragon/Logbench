/*
 * rules-signature.test.cjs — R6-011 acceptance.
 *
 * Two independent claims:
 *   1. the layered engine decides exactly what v3.0.3 decided (differential
 *      against the frozen copy at tests/fixtures/r6/legacy-engine.js);
 *   2. display-only edits do not change the semantic signature, so a colour
 *      change cannot invalidate a derived index.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const rules = require(path.join(ROOT, 'src', 'shared', 'rules.js'));
const framer = require(path.join(ROOT, 'src', 'shared', 'framer.js'));
const legacy = require(path.join(ROOT, 'tests', 'fixtures', 'r6', 'legacy-engine.js'));
const harness = require(path.join(__dirname, 'parser-harness.cjs'));

const PROFILE = JSON.parse(fs.readFileSync(path.join(ROOT, 'rules', 'cpqpsgx-default.json'), 'utf8'));

function fixtures() {
  return harness.listFixtures();
}

/* R5 records -> the shape both engines expect. */
function recordsFor(name) {
  const bytes = fs.readFileSync(path.join(harness.GOLDEN_DIR, name));
  return framer.frameAll(bytes, name, 4096).map(record => ({
    id: record.record_id,
    fileId: name,
    ordinal: record.ordinal,
    line: record.lines.first,
    lineEnd: record.lines.last,
    ts: record.ts || '',
    level: record.level,
    logger: record.logger,
    pid: record.pid,
    tid: record.tid,
    src: record.src,
    srcLine: record.src_line,
    fn: record.fn,
    message: record.message,
    body: record.body,
    bodyLineNos: record.body_lines,
    raw: record.raw,
    hasTrace: record.has_trace,
    kind: 'text',
    phaseId: 'import'
  }));
}

function normalize(record) {
  return {
    fields: record.fields,
    fieldMeta: (record.fieldMeta || []).map(meta => [meta.key, meta.value, meta.type, meta.source, meta.line, meta.index]),
    issue: record.issue,
    issueSeverity: record.issueSeverity,
    issueIds: (record.issueMatches || []).map(item => item.id),
    issueValues: (record.issueMatches || []).map(item => item.value),
    nodeIds: (record.nodeMatches || []).map(item => item.id || item.name),
    kind: record.kind,
    event: record.event ? [record.event.type, record.event.label] : null
  };
}

test('layered engine matches the frozen v3.0.3 engine on every golden record', () => {
  const mismatches = [];
  for (const name of fixtures()) {
    const input = recordsFor(name);
    const before = legacy.applyProfile(JSON.parse(JSON.stringify(input)), JSON.parse(JSON.stringify(PROFILE)));
    // Feed the legacy traceback object through so the diff isolates the layers
    // this module owns (extraction / anomalies / nodes / kind).
    const withTrace = input.map((record, index) => Object.assign({}, record, { traceback: before[index].traceback }));
    const after = rules.applyProfile(withTrace, JSON.parse(JSON.stringify(PROFILE)));
    assert.equal(after.length, before.length, name);
    for (let i = 0; i < before.length; i++) {
      const left = normalize(before[i]);
      const right = normalize(after[i]);
      for (const key of Object.keys(left)) {
        if (JSON.stringify(left[key]) !== JSON.stringify(right[key])) {
          mismatches.push(`${name}#${i}.${key}: ${JSON.stringify(left[key]).slice(0, 120)} vs ${JSON.stringify(right[key]).slice(0, 120)}`);
          break;
        }
      }
    }
  }
  assert.deepEqual(mismatches, []);
});

test('display-only edits keep the structure signature', () => {
  const base = rules.ruleSnapshot(PROFILE);
  const cosmetic = JSON.parse(JSON.stringify(PROFILE));
  cosmetic.name = '换了名字的规则';
  cosmetic.extractors[0].name = '键值（改名）';
  cosmetic.nodes[0].name = '起点（改名）';
  cosmetic.nodes[0].color = '#00ff00';
  cosmetic.nodes[0].shape = 'hexagon';
  const after = rules.ruleSnapshot(cosmetic);
  assert.equal(after.signatures.structure, base.signatures.structure, 'structure signature ignores names and colours');
  assert.equal(after.signatures.extract, base.signatures.extract, 'extract signature ignores extractor name');
});

test('semantic edits change the layer that owns them, and only that layer', () => {
  const base = rules.signatures(PROFILE);

  const extractChange = JSON.parse(JSON.stringify(PROFILE));
  extractChange.extractors[0].pattern = '(?<key>[A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(?<value>\\S+)';
  const extractSigs = rules.signatures(extractChange);
  assert.notEqual(extractSigs.extract, base.extract, 'extractor pattern is semantic');
  assert.equal(extractSigs.structure, base.structure, 'extractor change must not invalidate anomalies');

  const anomalyChange = JSON.parse(JSON.stringify(PROFILE));
  anomalyChange.anomalies[0].value = '^(FATAL)$';
  const anomalySigs = rules.signatures(anomalyChange);
  assert.notEqual(anomalySigs.structure, base.structure);
  assert.equal(anomalySigs.extract, base.extract, 'anomaly change must not re-extract fields');

  const nodeChange = JSON.parse(JSON.stringify(PROFILE));
  nodeChange.nodes[0].conditions[0].value = 'started only';
  assert.notEqual(rules.signatures(nodeChange).structure, base.structure, 'node conditions are structure');

  const patternOnly = { nodes: [{ name: 'a', type: 'start', pattern: 'alpha' }] };
  const patternOther = { nodes: [{ name: 'a', type: 'start', pattern: 'beta' }] };
  assert.notEqual(rules.signatures(patternOnly).structure, rules.signatures(patternOther).structure,
    'a node that only has a pattern still contributes that pattern to the signature');
});

test('a saved snapshot cannot be rewritten by later draft edits', () => {
  const draft = JSON.parse(JSON.stringify(PROFILE));
  const snapshot = rules.ruleSnapshot(draft);
  const before = snapshot.signatures.structure;
  draft.anomalies[0].value = '^(NEVER)$';
  assert.equal(snapshot.signatures.structure, before, 'snapshot is a copy, not a view');
  assert.equal(rules.signatures(draft).structure === before, false, 'the live draft did change');
  assert.deepEqual(snapshot.profile.extractors[0].pattern, PROFILE.extractors[0].pattern);
});

test('19-digit integers compare exactly, not through Number', () => {
  assert.equal(rules.compareNumber('9007199254740993', '9007199254740992', 'gt'), true);
  assert.equal(rules.compareNumber('9007199254740993', '9007199254740993', 'gte'), true);
  assert.equal(rules.compareNumber('9007199254740993', '9007199254740994', 'lt'), true);
  assert.equal(Number('9007199254740993') === Number('9007199254740992'), true, 'the float path really is lossy');
});

test('0, 0x0 and a missing field stay distinct', () => {
  const record = { level: 'INFO', message: 'x', body: [], fieldMeta: [{ key: 'err_code', value: '0' }], line: 1 };
  const zero = { field: 'fields.err_code', op: 'nonzero', value: '' };
  assert.equal(rules.testCondition(record, zero).matched, false, '0 is not nonzero');
  const hexZero = { level: 'INFO', message: 'x', body: [], fieldMeta: [{ key: 'err_code', value: '0x0' }], line: 1 };
  assert.equal(rules.testCondition(hexZero, zero).matched, false, '0x0 is not nonzero');
  const missing = { level: 'INFO', message: 'x', body: [], fieldMeta: [], line: 1 };
  assert.equal(rules.testCondition(missing, zero).matched, false, 'a missing field is not a failure');
  assert.equal(rules.testCondition(missing, { field: 'fields.err_code', op: 'missing', value: '' }).matched, true);
  assert.equal(rules.testCondition(missing, { field: 'fields.err_code', op: 'exists', value: '' }).matched, false);
});

test('duplicate keys keep their order and their evidence line', () => {
  const record = {
    level: 'WARNING', message: 'a=1 b=2 a=3', body: ['c=4'], bodyLineNos: [7], raw: [], line: 6
  };
  const extractor = {
    rx: /(?<key>[A-Za-z_]\w*)=(?<value>\d+)/g,
    name: 'kv', id: 'kv', source: 'both', key: '', group: 'value', type: 'auto', unit: ''
  };
  const extracted = rules.extractFields(record, [extractor]);
  assert.deepEqual(extracted.fields.map(pair => pair.join('=')), ['a=1', 'b=2', 'a=3', 'c=4']);
  assert.deepEqual(extracted.fieldMeta.map(meta => meta.line), [6, 6, 6, 7]);
  assert.deepEqual(extracted.fieldMeta.map(meta => meta.key), ['a', 'b', 'a', 'c'], 'duplicate keys keep order');
  assert.equal(extracted.fieldMeta[3].source, 'body');
});

test('invalid regex and unknown ops fail loudly at prepare/validate time', () => {
  const broken = { extractors: [{ name: 'x', pattern: '(', source: 'both' }] };
  assert.throws(() => rules.validate(broken), /正则表达式无效/);
  const unknownOp = { anomalies: [{ name: 'y', field: 'level', op: 'similar', value: 'x' }] };
  assert.throws(() => rules.validate(unknownOp), /未知条件/);
  const longPattern = { extractors: [{ name: 'z', pattern: 'a'.repeat(2001), source: 'both' }] };
  assert.throws(() => rules.validate(longPattern), /2000/);
});
