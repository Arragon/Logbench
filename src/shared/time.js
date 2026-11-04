/*
 * time.js — log timestamps to an exact ns string plus an ordinal key (R6-020).
 *
 * The frozen contract (`RecordHeader.ts_ns: DecimalString`) exists because
 * 2026-01-01 in nanoseconds is ~1.79e18, well past `Number.MAX_SAFE_INTEGER`.
 * Storing that as a JS number silently loses the last three digits, so two
 * records 400 ns apart compare equal and a stable sort hides the mistake.
 *
 * Therefore:
 *   ts_ns   decimal string, exact, what the contract and the wire carry
 *   ts_key  Number in **milliseconds**, used only for ordering and range filters
 *
 * Millisecond resolution is enough to order log lines, and ties fall back to
 * ordinal, which is stable. Anyone who needs sub-millisecond ordering must
 * compare the ns strings with BigInt — `compareNs()` does that when asked.
 *
 * A timestamp with no offset is read as UTC. That is a decision, not a default:
 * the alternative is guessing the server's local zone, which produces ordering
 * that changes with the machine.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBTime = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var PATTERN = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$/;

  function daysInMonth(year, month) {
    if (month === 2) {
      var leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
      return leap ? 29 : 28;
    }
    return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  }

  /** Parse to {ns, key, valid, reason}. `ns` is a decimal string, never a Number. */
  function parseTs(text) {
    if (text == null || text === '') return { ns: null, key: null, valid: false, reason: 'empty' };
    var match = PATTERN.exec(String(text).trim());
    if (!match) return { ns: null, key: null, valid: false, reason: 'shape' };
    var year = +match[1];
    var month = +match[2];
    var day = +match[3];
    var hour = +match[4];
    var minute = +match[5];
    var second = +match[6];
    if (month < 1 || month > 12) return { ns: null, key: null, valid: false, reason: 'month' };
    if (day < 1 || day > daysInMonth(year, month)) return { ns: null, key: null, valid: false, reason: 'day' };
    if (hour > 23) return { ns: null, key: null, valid: false, reason: 'hour' };
    if (minute > 59) return { ns: null, key: null, valid: false, reason: 'minute' };
    if (second > 60) return { ns: null, key: null, valid: false, reason: 'second' };

    var fraction = match[7] || '';
    var nanos = fraction === '' ? 0 : +fraction.padEnd(9, '0').slice(0, 9);
    var offsetMinutes = 0;
    var zone = match[8];
    if (zone && zone !== 'Z') {
      var sign = zone[0] === '-' ? -1 : 1;
      offsetMinutes = sign * (parseInt(zone.slice(1, 3), 10) * 60 + parseInt(zone.slice(4, 6), 10));
    }
    var base = Date.UTC(year, month - 1, day, hour, minute, second);
    var ms = base - offsetMinutes * 60000;
    var totalMs = BigInt(ms) * 1000000n + BigInt(nanos);
    return {
      ns: totalMs.toString(),
      // Milliseconds *including* the sub-second part, so ordering by key agrees
      // with ordering by ns at the precision the key can represent.
      key: ms + Math.floor(nanos / 1000000),
      valid: true,
      reason: null,
      normalised: new Date(ms).toISOString()
    };
  }

  /** Ordering key in ms for any accepted representation. */
  function keyOf(value) {
    if (value == null) return null;
    if (typeof value === 'number') {
      // Tolerate callers that already hold nanoseconds.
      return value > 1e14 ? Math.floor(value / 1e6) : value;
    }
    if (typeof value === 'string') {
      if (!/^-?\d+$/.test(value)) return null;
      return Number(BigInt(value) / 1000000n);
    }
    return null;
  }

  function compareNs(left, right) {
    var a = BigInt(String(left));
    var b = BigInt(String(right));
    return a < b ? -1 : a > b ? 1 : 0;
  }

  /** Convert a range bound (ns string or number) to the ms key used in filters. */
  function rangeKey(value) {
    if (value == null) return null;
    if (typeof value === 'number') return value > 1e14 ? Math.floor(value / 1e6) : value;
    return keyOf(value);
  }

  return {
    parseTs: parseTs,
    keyOf: keyOf,
    compareNs: compareNs,
    rangeKey: rangeKey
  };
});
