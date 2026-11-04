/*
 * contracts.test.cjs — R6 public contract conformance.
 *
 * Reads the same fixtures as tests/test_r6_contracts.py and exercises the
 * browser implementation in src/shared/contracts.js, so a divergence between
 * the two sides fails the build instead of being discovered in production.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const contracts = require(path.join(ROOT, 'src', 'shared', 'contracts.js'));
const schema = JSON.parse(fs.readFileSync(path.join(ROOT, 'contracts', 'r6', 'contracts.schema.json'), 'utf8'));
const machine = JSON.parse(fs.readFileSync(path.join(ROOT, 'contracts', 'r6', 'state-machine.json'), 'utf8'));
const cases = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'r6', 'contracts', 'cases.json'), 'utf8')).cases;
const projection = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'r6', 'contracts', 'projection.json'), 'utf8')).cases;

test('schema bundle is the only shape owner', () => {
  assert.equal(schema.contract_version, 'logbench.r6/1');
  const defs = Object.keys(schema.$defs);
  for (const required of ['RecordRef', 'RecordHeader', 'Finding', 'TaskProgress', 'QuerySpec', 'QueryState', 'ReaderProvider', 'WorkerEnvelope']) {
    assert.ok(defs.includes(required), 'missing $defs.' + required);
  }
});

test('every fixture case gets the verdict the fixture declares', () => {
  const mismatches = [];
  for (const item of cases) {
    const errors = contracts.validate(item.doc, item.def, schema);
    if (errors.length === 0 !== item.valid) mismatches.push(item.name + ' -> ' + JSON.stringify(errors.slice(0, 2)));
  }
  assert.deepEqual(mismatches, []);
});

test('valid cases produce no error text at all', () => {
  for (const item of cases.filter(c => c.valid)) {
    assert.deepEqual(contracts.validate(item.doc, item.def, schema), [], item.name);
  }
});

test('invalid cases fail for the documented reason, not accidentally', () => {
  const expected = {
    'record-header-missing-provisional': /missing required provisional/,
    'record-header-unsafe-integer-offset': /expected string/,
    'record-header-null-bytes-but-final': /C10/,
    'record-header-impossible-timestamp': /C13/,
    'record-header-value-type-lies': /C7/,
    'record-header-reversed-bytes': /C1/,
    'finding-lowercase-severity': /not in enum/,
    'query-spec-limit-too-large': /above maximum/,
    'query-state-partial-but-complete': /C5/,
    'query-state-paused-without-token': /C5/,
    'query-state-unknown-with-number': /C3/,
    'query-state-returned-exceeds-matched': /C3/,
    'task-uploaded-but-claims-indexed': /C6/,
    'task-bytes-over-total': /C6/,
    'provider-unknown-kind': /not in enum/,
    'envelope-old-protocol': /const/
  };
  for (const [name, pattern] of Object.entries(expected)) {
    const item = cases.find(c => c.name === name);
    assert.ok(item, 'fixture missing: ' + name);
    const errors = contracts.validate(item.doc, item.def, schema);
    assert.ok(errors.length > 0, name + ' should be invalid');
    assert.ok(errors.some(e => pattern.test(e)), name + ' failed for an unexpected reason: ' + errors.join(' | '));
  }
});

test('decimal offsets compare without float precision loss', () => {
  assert.equal(contracts.compareDecimal('12345678901234567890', '12345678901234567891'), -1);
  assert.equal(contracts.compareDecimal('9007199254740993', '9007199254740992'), 1);
  assert.equal(contracts.compareDecimal('0', '0'), 0);
  assert.equal(contracts.compareDecimal('-1', '0'), -1);
});

test('v1 projection mirrors the declared expectation', () => {
  for (const item of projection) {
    const result = contracts.projectV1(item.input);
    assert.equal(result.ok, item.valid, item.name + ': ' + (result.error || ''));
    if (result.ok) assert.deepEqual(result.value, item.expect, item.name);
  }
});

test('projected headers satisfy the v2 contract', () => {
  for (const item of projection.filter(c => c.valid)) {
    const value = contracts.projectV1(item.input).value;
    assert.deepEqual(contracts.validate(value, 'RecordHeader', schema), [], item.name);
  }
});

test('query state machine only accepts declared edges', () => {
  assert.equal(contracts.canTransition(machine, 'query', 'queued', 'running'), true);
  assert.equal(contracts.canTransition(machine, 'query', 'done', 'running'), false);
  assert.equal(contracts.canTransition(machine, 'query', 'partial', 'running'), true);
  assert.equal(contracts.canTransition(machine, 'query', 'paused', 'done'), false);
  assert.equal(contracts.canTransition(machine, 'query', 'running', 'totally-done'), false);
});

test('task state machine forbids upload-completion shortcuts', () => {
  assert.equal(contracts.canTransition(machine, 'task', 'uploading', 'uploaded'), true);
  assert.equal(contracts.canTransition(machine, 'task', 'uploading', 'indexed'), false);
  assert.equal(contracts.canTransition(machine, 'task', 'uploaded', 'indexing'), true);
  assert.equal(contracts.canTransition(machine, 'task', 'cancelled', 'indexing'), false);
});

test('every declared state appears in the transition table exactly once', () => {
  for (const kind of ['query', 'task']) {
    const group = machine[kind];
    assert.deepEqual(Object.keys(group.transitions).sort(), group.states.slice().sort(), kind);
    for (const state of group.terminal) assert.equal(group.transitions[state].length, 0, kind + '.' + state);
  }
});
