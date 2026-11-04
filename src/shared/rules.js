/*
 * rules.js — layered rule engine (R6-011).
 *
 * The v3.0.3 engine (src/engine.js) is one 75-line factory that mixes field
 * extraction, anomaly evaluation, node matching and display metadata. That is
 * why a colour change can look like a semantic change and prevent cache reuse.
 *
 * This module keeps the same decisions but splits them into layers with explicit
 * signatures:
 *
 *   structure  — anomaly conditions + node conditions (what can make a record
 *                an issue or an event)
 *   extract    — field extractors (what lands in `fields`/`fieldMeta`)
 *   search     — which projection a saved view searched
 *
 * `semanticSignature()` hashes only those layers, so renames, colours, shapes and
 * column widths do not invalidate a derived index. `ruleSnapshot()` freezes the
 * rules that produced a result, so editing a draft later cannot rewrite evidence
 * that was already computed.
 *
 * Compatibility: src/engine.js still serves the v3.0.3 UI unchanged. The
 * differential test in tests/r6/rules-signature.test.cjs proves this module
 * produces the same fields, issues, nodes and kind on the golden fixtures.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBRules = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var OPS = ['equals', 'notEquals', 'contains', 'notContains', 'startsWith', 'endsWith',
    'regex', 'nonzero', 'gt', 'gte', 'lt', 'lte', 'exists', 'missing'];
  var FIELD_NAMES = ['level', 'logger', 'message', 'body', 'raw', 'fn', 'src', 'pid', 'tid', 'phase', 'traceback.type'];
  var SIGNATURE_PROTOCOL = 'logbench.rules/1';

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function hash(text) {
    var value = 0x811c9dc5;
    var source = String(text == null ? '' : text);
    for (var i = 0; i < source.length; i++) {
      value ^= source.charCodeAt(i);
      value = (value + ((value << 1) + (value << 4) + (value << 7) + (value << 8) + (value << 24))) >>> 0;
    }
    return ('00000000' + value.toString(16)).slice(-8);
  }

  /* ---------- shared primitives (kept byte-compatible with v3.0.3) ---------- */

  function compile(pattern, name, flags) {
    if (typeof pattern !== 'string' || pattern.length > 2000) throw Error(name + '：表达式应为 1–2000 字符');
    try { return new RegExp(pattern, flags || 'i'); }
    catch (error) { throw Error(name + '：正则表达式无效（' + error.message + '）'); }
  }

  function clean(value) {
    return String(value == null ? '' : value).trim()
      .replace(/[;,，]$/, '')
      .replace(/(0x[\da-f]+)\.$/i, '$1');
  }

  function infer(value, type) {
    if (type && type !== 'auto') return type;
    if (/^(true|false)$/i.test(value)) return 'boolean';
    if (/^[-+]?0x[\da-f]+$/i.test(value)) return 'hex';
    if (/^[-+]?\d+(\.\d+)?(e[-+]?\d+)?$/i.test(value)) return 'number';
    return 'string';
  }

  /* Exact integer compare: 19-digit values must not pass through Number. */
  function compareNumber(value, expected, op) {
    value = clean(value); expected = clean(expected);
    var left, right;
    if (/^[-+]?\d+$/.test(value) && /^[-+]?\d+$/.test(expected)) {
      left = BigInt(value); right = BigInt(expected);
    } else {
      left = Number(value); right = Number(expected);
      if (!Number.isFinite(left) || !Number.isFinite(right)) return false;
    }
    return op === 'gt' ? left > right : op === 'gte' ? left >= right : op === 'lt' ? left < right : left <= right;
  }

  function matchValue(value, condition, regex) {
    value = clean(value);
    var expected = String(condition.value == null ? '' : condition.value).trim();
    switch (condition.op) {
      case 'equals': return value.toLowerCase() === expected.toLowerCase();
      case 'contains': return value.toLowerCase().indexOf(expected.toLowerCase()) !== -1;
      case 'regex': return regex.test(value);
      case 'nonzero':
        try {
          if (/^(?:0x[\da-f]+|[-+]?\d+)$/i.test(value)) return BigInt(value) !== 0n;
          return /^[-+]?\d+\.\d+$/.test(value) && Number(value) !== 0;
        } catch (error) { return false; }
      case 'exists': return value !== '';
      case 'gt': case 'gte': case 'lt': case 'lte': return compareNumber(value, expected, condition.op);
      default: return false;
    }
  }

  function conditionLabel(condition) {
    var names = {
      equals: '等于', notEquals: '不等于', contains: '包含', notContains: '不包含',
      startsWith: '开头为', endsWith: '结尾为', regex: '匹配正则', nonzero: '非零',
      gt: '>', gte: '≥', lt: '<', lte: '≤', exists: '存在', missing: '不存在'
    };
    var tail = ['nonzero', 'exists', 'missing'].indexOf(condition.op) !== -1 ? '' : ' ' + String(condition.value == null ? '' : condition.value);
    return condition.field + ' ' + (names[condition.op] || condition.op) + tail;
  }

  function conditionValues(record, field) {
    if (field === 'traceback.type') {
      return record.traceback && record.traceback.sections
        ? record.traceback.sections.filter(function (item) { return item.exception; })
          .map(function (item) { return { value: item.exception.type, line: item.exception.sourceLine, source: 'traceback' }; })
        : [];
    }
    if (field.indexOf('fields.') === 0) {
      return (record.fieldMeta || []).filter(function (item) { return item.key === field.slice(7); })
        .map(function (item) {
          return { value: item.value, line: item.line, source: item.source, extractor: item.rule, extractorId: item.ruleId };
        });
    }
    var value = field === 'body' ? (record.body || []).join('\n')
      : field === 'raw' ? (record.raw || []).join('\n')
        : record[field];
    if (value === undefined || value === null || value === '') return [];
    return [{
      value: String(value),
      line: field === 'body' ? ((record.bodyLineNos && record.bodyLineNos[0]) || record.line + 1) : record.line,
      source: field
    }];
  }

  function testCondition(record, condition) {
    var values = conditionValues(record, condition.field);
    var expected = String(condition.value == null ? '' : condition.value);
    var fold = function (value) {
      return condition.caseSensitive ? String(value) : String(value).toLocaleLowerCase();
    };
    if (condition.op === 'missing') {
      return {
        matched: !values.length,
        evidence: !values.length ? [{ field: condition.field, value: '（字段不存在）', line: record.line, source: condition.field }] : []
      };
    }
    var regex = condition.op === 'regex' ? compile(expected, '条件', condition.caseSensitive ? '' : 'i') : null;
    var hits = values.filter(function (item) {
      var text = clean(item.value);
      switch (condition.op) {
        case 'equals': return fold(text) === fold(expected);
        case 'notEquals': return fold(text) !== fold(expected);
        case 'contains': return fold(text).indexOf(fold(expected)) !== -1;
        case 'notContains': return fold(text).indexOf(fold(expected)) === -1;
        case 'startsWith': return fold(text).indexOf(fold(expected)) === 0;
        case 'endsWith': return fold(text).lastIndexOf(fold(expected)) === fold(text).length - fold(expected).length;
        case 'exists': return true;
        default: return matchValue(text, condition, regex);
      }
    });
    return {
      matched: hits.length > 0,
      evidence: hits.map(function (item) {
        var copy = {};
        for (var key in item) copy[key] = item[key];
        copy.field = condition.field;
        copy.condition = conditionLabel(condition);
        return copy;
      })
    };
  }

  function normalizeNode(node, index) {
    index = index || 0;
    var copy = {};
    for (var key in node) copy[key] = node[key];
    copy.id = node.id || 'node-' + index;
    copy.name = node.name || '未命名节点';
    copy.type = node.type || 'custom';
    copy.combine = node.combine === 'any' ? 'any' : 'all';
    copy.conditions = node.conditions ? clone(node.conditions) : [{ field: 'raw', op: 'regex', value: node.pattern || '', caseSensitive: false }];
    copy.shape = node.shape || ({ start: 'circle', end: 'square', error: 'diamond', warning: 'triangle', custom: 'star' }[node.type] || 'star');
    copy.color = node.color || ({ start: '#047857', end: '#475569', error: '#b91c1c', warning: '#a16207', custom: '#7e22ce' }[node.type] || '#7e22ce');
    return copy;
  }

  function validate(profile) {
    var extractors = (profile && profile.extractors) || [];
    for (var i = 0; i < extractors.length; i++) {
      var item = extractors[i];
      if (item.enabled === false) continue;
      if (!item.name || !String(item.name).trim()) throw Error('提取规则需要名称');
      compile(item.pattern, item.name, 'gim');
      if (['message', 'body', 'both', 'raw'].indexOf(item.source) === -1) throw Error(item.name + '：请选择提取来源');
    }
    var anomalies = (profile && profile.anomalies) || [];
    for (var j = 0; j < anomalies.length; j++) {
      var rule = anomalies[j];
      if (rule.enabled === false) continue;
      if (OPS.indexOf(rule.op) === -1) throw Error((rule.name || '异常规则') + '：未知条件');
    }
    return true;
  }

  /* ---------------------------- layer: extraction --------------------------- */

  function extractFields(record, extractors) {
    var fields = [];
    var meta = [];
    for (var i = 0; i < extractors.length; i++) {
      var extractor = extractors[i];
      var inputs = extractor.source === 'raw'
        ? [{ text: (record.raw || []).join('\n'), source: 'raw', line: record.line }]
        : [].concat(
          extractor.source !== 'body' ? [{ text: record.message, source: 'message', line: record.line }] : [],
          extractor.source !== 'message'
            ? (record.body || []).map(function (text, index) {
              return { text: text, source: 'body', line: (record.bodyLineNos && record.bodyLineNos[index]) || record.line + index + 1 };
            })
            : []
        );
      for (var k = 0; k < inputs.length; k++) {
        var input = inputs[k];
        extractor.rx.lastIndex = 0;
        var found;
        var count = 0;
        while ((found = extractor.rx.exec(input.text)) !== null) {
          if (++count > 512) break;
          var key = (extractor.key && String(extractor.key).trim()) || (found.groups && found.groups.key) || found[1];
          var groupName = extractor.group || 'value';
          var rawValue = (found.groups && found.groups[groupName] !== undefined)
            ? found.groups[groupName]
            : found[Number(groupName) || 2];
          if (key && rawValue !== undefined) {
            var value = clean(rawValue);
            fields.push([String(key), value]);
            meta.push({
              key: String(key),
              value: value,
              type: infer(value, extractor.type),
              unit: extractor.unit || '',
              rule: extractor.name,
              ruleId: extractor.id || extractor.name,
              pattern: extractor.pattern,
              source: input.source,
              line: input.line,
              index: found.index
            });
          }
          if (found[0].length === 0) extractor.rx.lastIndex++;
        }
      }
    }
    return { fields: fields, fieldMeta: meta };
  }

  /* ---------------------------- layer: structure ---------------------------- */

  function evaluateAnomalies(record, anomalies) {
    var matches = [];
    for (var i = 0; i < anomalies.length; i++) {
      var rule = anomalies[i];
      var result = testCondition(record, rule);
      if (!result.matched) continue;
      matches.push({
        id: rule.id || rule.name,
        name: rule.name,
        severity: rule.severity || 'error',
        field: rule.field,
        op: rule.op,
        compare: rule.value || '',
        value: result.evidence[0] ? result.evidence[0].value : undefined,
        condition: conditionLabel(rule),
        evidence: result.evidence,
        category: 'errors'
      });
    }
    matches.sort(function (a, b) {
      return (a.severity === 'error' ? 0 : 1) - (b.severity === 'error' ? 0 : 1);
    });
    return matches;
  }

  function evaluateNodes(record, nodes) {
    var matches = [];
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var results = node.conditions.map(function (condition) { return testCondition(record, condition); });
      var hit = node.combine === 'all'
        ? results.every(function (item) { return item.matched; })
        : results.some(function (item) { return item.matched; });
      if (!hit) continue;
      var evidence = [];
      results.forEach(function (item) { evidence = evidence.concat(item.evidence); });
      matches.push({
        id: node.id,
        name: node.name,
        type: node.type,
        shape: node.shape,
        color: node.color,
        combine: node.combine,
        conditions: clone(node.conditions),
        evidence: evidence,
        category: 'nodes'
      });
    }
    return matches;
  }

  /* ------------------------------ composition ------------------------------- */

  function prepare(profile) {
    validate(profile);
    return {
      rule_id: profile.id || 'default',
      name: profile.name || '默认配置',
      version: profile.version || '1.0',
      revision: profile.revision == null ? null : profile.revision,
      extractors: (profile.extractors || []).filter(function (item) { return item.enabled !== false; })
        .map(function (item) {
          var copy = {};
          for (var key in item) copy[key] = item[key];
          copy.rx = compile(item.pattern, item.name, 'gim');
          return copy;
        }),
      anomalies: (profile.anomalies || []).filter(function (item) { return item.enabled !== false; })
        .map(function (item) {
          var copy = {};
          for (var key in item) copy[key] = item[key];
          copy.rx = item.op === 'regex' ? compile(item.value, item.name) : null;
          return copy;
        }),
      nodes: (profile.nodes || []).map(normalizeNode).filter(function (item) { return item.enabled !== false; })
    };
  }

  function applyProfile(records, profile) {
    var prepared = prepare(profile);
    return records.map(function (source) {
      var record = {};
      for (var key in source) record[key] = source[key];
      record.fields = [];
      record.fieldMeta = [];
      record.issue = null;
      record.issueSeverity = null;
      record.issueMatches = [];
      record.nodeMatches = [];
      record.appliedRule = { id: prepared.rule_id, name: prepared.name, version: prepared.version };
      record.traceback = source.traceback || null;
      record.event = null;

      var extracted = extractFields(record, prepared.extractors);
      record.fields = extracted.fields;
      record.fieldMeta = extracted.fieldMeta;

      record.issueMatches = evaluateAnomalies(record, prepared.anomalies);
      if (record.issueMatches.length) {
        record.issue = record.issueMatches.map(function (item) { return item.name; }).join(' · ');
        record.issueSeverity = record.issueMatches[0].severity;
        var first = record.issueMatches[0];
        var original = record.originalEvent && ['原始错误等级', '原始告警等级'].indexOf(first.name) !== -1;
        record.event = {
          type: record.issueSeverity,
          label: original ? record.originalEvent.label : first.name,
          phase: record.phaseId
        };
      }

      record.nodeMatches = evaluateNodes(record, prepared.nodes);
      if (record.nodeMatches.length) {
        var node = record.nodeMatches[0];
        record.event = { type: node.type, label: node.name, phase: record.phaseId, shape: node.shape, color: node.color, ruleId: node.id };
      }

      if (record.traceback) record.kind = 'traceback';
      else if (record.body && record.body.length && record.fields.length > 1) record.kind = 'kv';
      else if (record.kind === 'kv' || record.kind === 'traceback') record.kind = 'text';
      return record;
    });
  }

  /* ----------------------------- signatures -------------------------------- */

  function semanticSignature(profile, layer) {
    var prepared = prepare(profile);
    var canonical;
    if (layer === 'extract') {
      canonical = prepared.extractors.map(function (item) {
        return [item.source, item.pattern, item.key || '', item.group || '', item.type || 'auto', item.unit || ''].join('~');
      }).sort().join('&');
    } else if (layer === 'search') {
      canonical = JSON.stringify(profile.projection_version || 'v1');
    } else {
      var anomalies = prepared.anomalies.map(function (item) {
        return ['a', item.field, item.op, String(item.value == null ? '' : item.value), item.caseSensitive ? 'Aa' : 'aa', item.severity || 'error'].join('~');
      }).sort();
      var nodes = prepared.nodes.map(function (item) {
        return ['n', item.type, item.combine, item.conditions.map(function (condition) {
          return [condition.field, condition.op, String(condition.value == null ? '' : condition.value), condition.caseSensitive ? 'Aa' : 'aa'].join(':');
        }).join(',')].join('~');
      }).sort();
      canonical = anomalies.concat(nodes).join('&');
    }
    return hash(SIGNATURE_PROTOCOL + '|' + (layer || 'structure') + '|' + canonical);
  }

  function signatures(profile) {
    return {
      structure: semanticSignature(profile, 'structure'),
      extract: semanticSignature(profile, 'extract'),
      search: semanticSignature(profile, 'search')
    };
  }

  /* Immutable snapshot: later draft edits cannot rewrite existing evidence. */
  function ruleSnapshot(profile) {
    var copy = clone(profile);
    return {
      rule_id: profile.id || 'default',
      name: profile.name || '默认配置',
      version: profile.version || '1.0',
      revision: profile.revision == null ? null : profile.revision,
      signatures: signatures(profile),
      profile: copy
    };
  }

  return {
    SIGNATURE_PROTOCOL: SIGNATURE_PROTOCOL,
    OPS: OPS,
    FIELD_NAMES: FIELD_NAMES,
    clone: clone,
    compile: compile,
    clean: clean,
    infer: infer,
    compareNumber: compareNumber,
    testCondition: testCondition,
    conditionLabel: conditionLabel,
    conditionValues: conditionValues,
    normalizeNode: normalizeNode,
    validate: validate,
    extractFields: extractFields,
    evaluateAnomalies: evaluateAnomalies,
    evaluateNodes: evaluateNodes,
    prepare: prepare,
    applyProfile: applyProfile,
    semanticSignature: semanticSignature,
    signatures: signatures,
    ruleSnapshot: ruleSnapshot
  };
});
