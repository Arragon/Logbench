/*
 * contracts.js — R6 public contract validator and v1->v2 projection.
 *
 * Single implementation shared by the browser bundle and the Node tests
 * (UMD-lite: window.LBContracts in the page, module.exports under Node).
 *
 * The shape of every document lives in contracts/r6/contracts.schema.json.
 * Nothing here may re-declare a shape; this file only evaluates the schema
 * plus the semantic invariants C1-C12 documented in docs/r6/contracts.md.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBContracts = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var PREFIX = '#/$defs/';
  var KINDS = { text: 1, kv: 1, stack: 1, mask: 1, unparsed: 1 };
  var TS_RE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})?$/;
  var DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

  /* C13: a timestamp must name a real instant, not merely the right digit count. */
  function validTimestamp(text) {
    if (typeof text !== 'string') return false;
    var m = TS_RE.exec(text);
    if (!m) return false;
    var year = +m[1], month = +m[2], day = +m[3];
    var hour = +m[4], minute = +m[5], second = +m[6];
    if (month < 1 || month > 12) return false;
    var leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    var limit = DAYS[month] + (month === 2 && leap ? 1 : 0);
    if (day < 1 || day > limit) return false;
    if (hour > 23 || minute > 59 || second > 60) return false;
    var offset = m[8];
    if (offset && offset !== 'Z') {
      if (+offset.slice(1, 3) > 23 || +offset.slice(4, 6) > 59) return false;
    }
    return true;
  }


  function typeOf(value) {
    if (value === null) return 'null';
    if (Array.isArray(value)) return 'array';
    var t = typeof value;
    if (t === 'number') return Number.isInteger(value) ? 'integer' : 'number';
    return t;
  }

  function matchesType(value, expected) {
    var actual = typeOf(value);
    if (expected === 'number') return actual === 'number' || actual === 'integer';
    if (expected === 'integer') return actual === 'integer';
    return actual === expected;
  }

  /* Compare two decimal strings without converting to Number: offsets may exceed 2^53. */
  function compareDecimal(a, b) {
    var x = String(a), y = String(b);
    var nx = x.charAt(0) === '-', ny = y.charAt(0) === '-';
    if (nx !== ny) return nx ? -1 : 1;
    var dx = nx ? x.slice(1) : x, dy = ny ? y.slice(1) : y;
    var sign = nx ? -1 : 1;
    if (dx.length !== dy.length) return dx.length < dy.length ? -sign : sign;
    if (dx === dy) return 0;
    return dx < dy ? -sign : sign;
  }

  function resolveRef(schema, node) {
    if (!node || typeof node !== 'object' || !node.$ref) return node;
    var ref = node.$ref;
    if (ref.indexOf(PREFIX) !== 0) throw new Error('unsupported $ref: ' + ref);
    var name = ref.slice(PREFIX.length);
    var target = schema.$defs && schema.$defs[name];
    if (!target) throw new Error('missing $defs entry: ' + name);
    return target;
  }

  function validateNode(doc, node, schema, path, errors) {
    node = resolveRef(schema, node);
    if (!node || typeof node !== 'object') return;

    if (Object.prototype.hasOwnProperty.call(node, 'const') && doc !== node.const) {
      errors.push(path + ': expected const ' + JSON.stringify(node.const));
    }
    if (node.enum && node.enum.indexOf(doc) === -1) {
      errors.push(path + ': value ' + JSON.stringify(doc) + ' not in enum');
    }
    if (node.type) {
      var types = Array.isArray(node.type) ? node.type : [node.type];
      var ok = false;
      for (var i = 0; i < types.length; i++) if (matchesType(doc, types[i])) { ok = true; break; }
      if (!ok) {
        errors.push(path + ': expected ' + types.join('|') + ', got ' + typeOf(doc));
        return;
      }
    }
    if (typeof doc === 'string') {
      if (node.pattern && !new RegExp(node.pattern).test(doc)) errors.push(path + ': does not match ' + node.pattern);
      if (typeof node.minLength === 'number' && doc.length < node.minLength) errors.push(path + ': shorter than ' + node.minLength);
      if (typeof node.maxLength === 'number' && doc.length > node.maxLength) errors.push(path + ': longer than ' + node.maxLength);
    }
    if (typeof doc === 'number') {
      if (typeof node.minimum === 'number' && doc < node.minimum) errors.push(path + ': below minimum ' + node.minimum);
      if (typeof node.maximum === 'number' && doc > node.maximum) errors.push(path + ': above maximum ' + node.maximum);
    }
    if (Array.isArray(doc)) {
      if (typeof node.minItems === 'number' && doc.length < node.minItems) errors.push(path + ': fewer than ' + node.minItems + ' items');
      if (typeof node.maxItems === 'number' && doc.length > node.maxItems) errors.push(path + ': more than ' + node.maxItems + ' items');
      if (node.items) for (var k = 0; k < doc.length; k++) validateNode(doc[k], node.items, schema, path + '[' + k + ']', errors);
    }
    if (typeOf(doc) === 'object') {
      var required = node.required || [];
      for (var r = 0; r < required.length; r++) {
        if (!Object.prototype.hasOwnProperty.call(doc, required[r])) errors.push(path + ': missing required ' + required[r]);
      }
      var props = node.properties || {};
      for (var key in doc) {
        if (!Object.prototype.hasOwnProperty.call(doc, key)) continue;
        if (Object.prototype.hasOwnProperty.call(props, key)) {
          validateNode(doc[key], props[key], schema, path + '.' + key, errors);
        } else if (node.additionalProperties === false) {
          errors.push(path + ': unexpected property ' + key);
        } else if (node.additionalProperties && typeof node.additionalProperties === 'object') {
          validateNode(doc[key], node.additionalProperties, schema, path + '.' + key, errors);
        }
      }
    }
    if (Array.isArray(node.oneOf)) {
      var passed = 0;
      var hint = '';
      for (var o = 0; o < node.oneOf.length; o++) {
        var bucket = [];
        validateNode(doc, node.oneOf[o], schema, path, bucket);
        if (bucket.length === 0) passed++;
        else if (!hint) hint = bucket[0];
      }
      if (passed !== 1) errors.push(path + ': matched ' + passed + ' of ' + node.oneOf.length + ' oneOf branches (' + hint + ')');
    }
    if (Array.isArray(node.anyOf)) {
      var anyPassed = false;
      for (var a = 0; a < node.anyOf.length; a++) {
        var b2 = [];
        validateNode(doc, node.anyOf[a], schema, path, b2);
        if (b2.length === 0) { anyPassed = true; break; }
      }
      if (!anyPassed) errors.push(path + ': matched no anyOf branch');
    }
    if (Array.isArray(node.allOf)) {
      for (var l = 0; l < node.allOf.length; l++) validateNode(doc, node.allOf[l], schema, path, errors);
    }
  }

  function semantics(defName, doc, errors, path) {
    var at = path || '$';
    if (!doc || typeof doc !== 'object') return;
    switch (defName) {
      case 'RecordRef': {
        if (doc.bytes && typeof doc.bytes === 'object') {
          if (compareDecimal(doc.bytes.start, doc.bytes.end) > 0) errors.push(at + '.bytes: start must be <= end (C1)');
        }
        if (doc.lines && doc.lines.last < doc.lines.first) errors.push(at + '.lines: last must be >= first (C2)');
        break;
      }
      case 'RecordHeader': {
        if (doc.ref) semantics('RecordRef', doc.ref, errors, at + '.ref');
        if (doc.ref && doc.ref.bytes === null && doc.provisional === false) {
          errors.push(at + ': bytes=null requires provisional=true (C10)');
        }
        if (doc.ts === null && doc.ts_ns !== undefined) errors.push(at + ': ts_ns requires ts (C11)');
        if (doc.ts !== null && doc.ts !== undefined && !validTimestamp(doc.ts)) errors.push(at + ': ts is not a real instant (C13)');
        var fields = Array.isArray(doc.fields) ? doc.fields : [];
        for (var i = 0; i < fields.length; i++) semantics('FieldValue', fields[i], errors, at + '.fields[' + i + ']');
        break;
      }
      case 'FieldValue': {
        var vt = doc.value_type, v = doc.value;
        var good = false;
        if (vt === 'integer') good = typeof v === 'number' && Number.isInteger(v);
        else if (vt === 'number') good = typeof v === 'number';
        else if (vt === 'boolean') good = typeof v === 'boolean';
        else if (vt === 'string') good = typeof v === 'string';
        else if (vt === 'null') good = v === null;
        if (!good) errors.push(at + ': value_type=' + vt + ' does not match carried value (C7)');
        break;
      }
      case 'Finding': {
        if (doc.ref) semantics('RecordRef', doc.ref, errors, at + '.ref');
        break;
      }
      case 'QuerySpec': {
        if (doc.time_range) {
          if (compareDecimal(doc.time_range.from_ns, doc.time_range.to_ns) > 0) {
            errors.push(at + '.time_range: from_ns must be <= to_ns (C12)');
          }
        }
        break;
      }
      case 'QueryState': {
        if (doc.count) semantics('CountRelation', doc.count, errors, at + '.count');
        if (doc.coverage) semantics('Coverage', doc.coverage, errors, at + '.coverage');
        if (doc.state === 'paused') {
          if (typeof doc.resume_token !== 'string' || doc.resume_token.length === 0) {
            errors.push(at + ': state=paused requires a resume_token (C5)');
          }
        }
        if (doc.state === 'partial' && doc.coverage && doc.coverage.complete === true) {
          errors.push(at + ': state=partial requires coverage.complete=false (C5)');
        }
        if (doc.state === 'done' && doc.count && doc.count.relation === 'unknown') {
          errors.push(at + ': state=done cannot carry an unknown count (C5)');
        }
        break;
      }
      case 'CountRelation': {
        if (doc.relation === 'unknown' && doc.matched !== null) errors.push(at + ': relation=unknown requires matched=null (C3)');
        if (doc.relation !== 'unknown' && doc.matched === null) errors.push(at + ': relation=' + doc.relation + ' requires an integer matched (C3)');
        if (typeof doc.matched === 'number' && typeof doc.returned === 'number' && doc.returned > doc.matched) {
          errors.push(at + ': returned exceeds matched (C3)');
        }
        break;
      }
      case 'Coverage': {
        if (compareDecimal(doc.scanned_bytes, doc.total_bytes) > 0) errors.push(at + ': scanned_bytes exceeds total_bytes (C4)');
        var complete = compareDecimal(doc.scanned_bytes, doc.total_bytes) >= 0;
        if (doc.complete !== complete) errors.push(at + ': complete must equal (scanned_bytes >= total_bytes) (C4)');
        break;
      }
      case 'TaskProgress': {
        if (compareDecimal(doc.bytes_done, doc.bytes_total) > 0) errors.push(at + ': bytes_done exceeds bytes_total (C6)');
        if (doc.phase === 'uploaded' && doc.indexed !== false) errors.push(at + ': phase=uploaded cannot imply indexed (C6)');
        if (doc.phase === 'indexed' && doc.indexed !== true) errors.push(at + ': phase=indexed requires indexed=true (C6)');
        break;
      }
      default:
        break;
    }
  }

  function validate(doc, defName, schema) {
    if (!schema || !schema.$defs) throw new Error('schema bundle with $defs required');
    if (!schema.$defs[defName]) throw new Error('unknown definition: ' + defName);
    var errors = [];
    validateNode(doc, { $ref: PREFIX + defName }, schema, '$', errors);
    semantics(defName, doc, errors, '$');
    return errors;
  }

  /* R5 v1 record -> v2 RecordHeader. Throws nothing; returns {ok, value|error}. */
  function projectV1(record) {
    if (!record || typeof record !== 'object') return { ok: false, error: 'record is not an object' };
    if (typeof record.id !== 'string' || record.id === '') return { ok: false, error: 'record.id is required' };
    if (typeof record.fileId !== 'string' || record.fileId === '') return { ok: false, error: 'record.fileId is required' };
    if (!KINDS[record.kind]) return { ok: false, error: 'unknown kind: ' + record.kind };
    var line = record.line, lineEnd = record.lineEnd == null ? record.line : record.lineEnd;
    if (!(line >= 1) || !(lineEnd >= 1)) return { ok: false, error: 'line numbers are 1-based' };
    if (lineEnd < line) return { ok: false, error: 'lineEnd precedes line' };
    if (typeof record.level !== 'string' || record.level === '') return { ok: false, error: 'record.level is required' };
    if (!Array.isArray(record.fields)) return { ok: false, error: 'record.fields must be an array of [key, value] pairs' };
    var fields = [];
    for (var i = 0; i < record.fields.length; i++) {
      var pair = record.fields[i];
      if (!Array.isArray(pair) || pair.length !== 2) return { ok: false, error: 'fields[' + i + '] is not a [key, value] pair' };
      fields.push({ key: String(pair[0]), value: String(pair[1]), value_type: 'string' });
    }
    var ordinal = typeof record.ordinal === 'number' ? record.ordinal : 0;
    return {
      ok: true,
      value: {
        ref: {
          dataset_id: record.fileId,
          record_id: record.id,
          version: 1,
          ordinal: ordinal,
          bytes: null,
          lines: { first: line, last: lineEnd }
        },
        ts: record.ts ? record.ts : null,
        level: record.level,
        logger: typeof record.logger === 'string' ? record.logger : '',
        kind: record.kind,
        has_trace: record.hasTrace === true,
        message: typeof record.message === 'string' ? record.message : '',
        fields: fields,
        provisional: true
      }
    };
  }

  function canTransition(machine, kind, from, to) {
    var group = machine && machine[kind];
    if (!group) return false;
    var edges = group.transitions[from];
    if (!edges) return false;
    return edges.indexOf(to) !== -1;
  }

  return {
    validate: validate,
    projectV1: projectV1,
    canTransition: canTransition,
    compareDecimal: compareDecimal,
    validTimestamp: validTimestamp,
    typeOf: typeOf
  };
});
