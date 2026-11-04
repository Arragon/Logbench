/* R6 test harness: run the real R5 parser text from src/app.js under Node.
   The parser is not rewritten here - the exact function source is extracted and
   evaluated, so golden expectations always describe the shipped implementation. */
'use strict';
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '..', '..', 'src', 'app.js');

function scanFunction(source, name) {
  const declaration = 'function ' + name + '(';
  const arrow = 'const ' + name + '=';
  const start = source.indexOf(declaration);
  if (start >= 0) {
    const body = source.indexOf('{', start);
    if (body < 0) throw new Error('missing body: ' + name);
    return source.slice(start, scanBlock(source, body) + 1);
  }
  const alt = source.indexOf(arrow);
  if (alt < 0) throw new Error('function not found in app.js: ' + name);
  const arrowAt = source.indexOf('=>', alt);
  if (arrowAt < 0) throw new Error('unsupported declaration: ' + name);
  const params = source.slice(alt + arrow.length, arrowAt).trim();
  let cursor = arrowAt + 2;
  while (cursor < source.length && /\s/.test(source[cursor])) cursor++;
  const prefix = '(' + params + ')=>';
  if (source[cursor] === '{') return prefix + source.slice(cursor, scanBlock(source, cursor) + 1);
  let end = cursor;
  while (end < source.length && source[end] !== ';') {
    if (source[end] === '"' || source[end] === "'" || source[end] === '`') { end = skipString(source, end); }
    end++;
  }
  return prefix + source.slice(cursor, end);
}

function scanBlock(source, openIndex) {
  let i = openIndex;
  let depth = 0;
  let prev = '';
  while (i < source.length) {
    const ch = source[i];
    if (ch === '"' || ch === "'" || ch === '`') {
      i = skipString(source, i);
      prev = ch;
      i++;
      continue;
    }
    if (ch === '/' && source[i + 1] === '/') { i = source.indexOf('\n', i); if (i < 0) break; continue; }
    if (ch === '/' && source[i + 1] === '*') { i = source.indexOf('*/', i) + 2; continue; }
    if (ch === '/' && regexAllowed(prev)) { i = skipRegex(source, i); prev = '/'; i++; continue; }
    if (ch === '{') depth++;
    if (ch === '}') { depth--; if (depth === 0) return i; }
    if (!/\s/.test(ch)) prev = ch;
    i++;
  }
  throw new Error('unbalanced braces');
}

function regexAllowed(prev) {
  return prev === '' || '(,=:[!&|?{};+-*%~^<>'.indexOf(prev) >= 0 || prev === 'r';
}

function skipString(source, index) {
  const quote = source[index];
  let i = index + 1;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '\\') { i += 2; continue; }
    if (ch === quote) return i;
    if (quote === '`' && ch === '$' && source[i + 1] === '{') {
      let depth = 1;
      i += 2;
      while (i < source.length && depth > 0) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}') depth--;
        else if (source[i] === '"' || source[i] === "'" || source[i] === '`') i = skipString(source, i);
        i++;
      }
      continue;
    }
    i++;
  }
  throw new Error('unterminated string');
}

function skipRegex(source, index) {
  let i = index + 1;
  let inClass = false;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '\\') { i += 2; continue; }
    if (ch === '[') inClass = true;
    else if (ch === ']') inClass = false;
    else if (ch === '/' && !inClass) return i;
    i++;
  }
  throw new Error('unterminated regex');
}

let cached = null;

function loadParser() {
  if (cached) return cached;
  const source = fs.readFileSync(APP, 'utf8');
  const norm = new Function('return (' + scanFunction(source, 'norm') + ')')();
  const getFields = new Function('return (' + scanFunction(source, 'getFields') + ')')();
  const parseLocalSrc = scanFunction(source, 'parseLocal');
  const parseLocal = new Function('norm', 'getFields', 'return (' + parseLocalSrc + ')')(norm, getFields);
  cached = { norm, getFields, parseLocal, parseLocalSrc };
  return cached;
}

const GOLDEN_DIR = path.join(__dirname, '..', 'fixtures', 'r6', 'golden');

function listFixtures() {
  if (!fs.existsSync(GOLDEN_DIR)) return [];
  return fs.readdirSync(GOLDEN_DIR).filter((name) => name.endsWith('.log')).sort();
}

/* Records are reduced to the fields that carry parsing semantics; display-only
   state added later by the rule engine is deliberately excluded. */
function project(record) {
  return {
    line: record.line,
    lineEnd: record.lineEnd,
    ts: record.ts,
    key: record.key,
    level: record.level,
    logger: record.logger,
    pid: record.pid,
    tid: record.tid,
    src: record.src,
    srcLine: record.srcLine,
    fn: record.fn,
    message: record.message,
    body: record.body,
    kind: record.kind,
    fields: record.fields,
    hasTrace: !!record.hasTrace,
    phaseId: record.phaseId,
  };
}

function parseFixture(name) {
  const { parseLocal } = loadParser();
  const raw = fs.readFileSync(path.join(GOLDEN_DIR, name), 'utf8');
  return parseLocal(raw, name);
}

module.exports = { loadParser, scanFunction, listFixtures, parseFixture, project, GOLDEN_DIR, APP };
