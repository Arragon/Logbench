/*
 * server-engine.cjs — resident parse worker for the server side (R6-035).
 *
 * It reuses the same shared modules as the browser (`framer`, `rules`, `time`),
 * so a record framed on the server has the same boundaries as one framed in the
 * page. That is the whole reason this is JavaScript and not a second Python
 * parser: two parsers drift, one parser cannot.
 *
 * Protocol: newline-delimited JSON. stdin carries requests, stdout carries
 * replies and nothing else. stderr is for diagnostics and is never trusted as
 * protocol. Payloads are base64 and capped by the parent (4 MiB), so a single
 * line stays bounded.
 *
 * Commands:
 *   {"type":"hello"}                         -> {"type":"ready", ...}
 *   {"type":"parse","id","job","token","data","eof","profile"?,...}
 *   {"type":"restore","id","job","checkpoint"?,"start_ordinal"?,"start_position"?,...}
 *   {"type":"apply","id","job","records","profile"?}
 *   {"type":"query-open","id","spec"}         -> {"type":"query-opened","qid",...}
 *   {"type":"query-eval","id","qid","records"} -> {"type":"query-batch","hits"}
 *   {"type":"query-close","id","qid"}
 *   {"type":"signature","id","profile"?}      -> {"type":"signatures",...}
 *   {"type":"sleep","id","ms"}               -> test hook for the watchdog
 *   {"type":"reset","job"}                   -> drop a job's streaming state
 *   {"type":"shutdown"}                      -> {"type":"bye"} then exit
 *
 * `parse` replies carry the framer checkpoint, so the parent can persist it in
 * the same transaction as the batch. Resume restores it instead of re-framing
 * from zero: that is what makes "crash in the middle" lose and duplicate nothing.
 */
'use strict';

const path = require('path');
const readline = require('readline');

const SHARED = path.resolve(__dirname, '..', 'shared');
const framerModule = require(path.join(SHARED, 'framer.js'));
const rulesModule = require(path.join(SHARED, 'rules.js'));
const timeModule = require(path.join(SHARED, 'time.js'));
const queryModule = require(path.join(SHARED, 'query.js'));

const PROTOCOL = 'logbench.node/1';
const MAX_PAYLOAD = 4 * 1024 * 1024;
// base64 inflates by 4/3 plus padding, so the *encoded* guard must be the larger
// number. Comparing the encoded length against MAX_PAYLOAD rejects a legal 4 MiB
// block by a few bytes of padding, which is exactly the kind of off-by-one that
// only shows up on a big file.
const MAX_ENCODED = Math.ceil(MAX_PAYLOAD / 3) * 4 + 16;
const MAX_RECORDS_PER_REPLY = 2000;
//: Parse batches may be larger than a reader page: the cost of a small batch is
//: re-sending the same block over the wire (the framer cannot rewind), and that
//: amplification dominates cold-index throughput on short-line files.
const MAX_BATCH_RECORDS = 4000;
//: Records are not a size: a 600-byte log line becomes a ~1.5 KB record once it
//: carries body, raw lines and rule output. The batch stops at whichever limit
//: comes first, so a reply cannot exceed what the parent will read.
const MAX_BATCH_BYTES = 4 * 1024 * 1024;
//: Feed the framer in slices so a batch can stop at a record cap without dropping
//: anything: the parent continues from `consumed`. A slice must itself stay under
//: the batch byte budget even for one-byte lines, hence 32 KiB and not 1 MiB.
const SLICE = 32 * 1024;

const state = {
  framers: new Map(),
  profiles: new Map(),
  jobs: new Set(),
  queries: new Map(),
  querySeq: 0,
  started: Date.now(),
  requests: 0,
  records: 0,
  bytes: 0
};

function send(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

function fail(id, job, token, code, message) {
  send({ type: 'error', id: id, job: job, token: token, code: code, message: message });
}

function framerFor(job) {
  if (!state.framers.has(job)) {
    state.framers.set(job, framerModule.createFramer({ datasetId: job }));
  }
  return state.framers.get(job);
}

/*
 * The framer checkpoint holds a byte buffer (`pending`). JSON arrays of numbers
 * would inflate a long line by an order of magnitude, so the wire form is base64
 * and the two conversions live here rather than in the parent.
 */
function encodeCheckpoint(framer) {
  const checkpoint = framer.checkpoint();
  checkpoint.pending = Buffer.from(Uint8Array.from(checkpoint.pending || [])).toString('base64');
  return checkpoint;
}

function decodeCheckpoint(checkpoint) {
  const out = Object.assign({}, checkpoint);
  if (typeof out.pending === 'string') out.pending = Array.from(Buffer.from(out.pending, 'base64'));
  else if (!Array.isArray(out.pending)) out.pending = [];
  return out;
}

function annotate(record, job) {
  const out = Object.assign({}, record);
  out.file_id = job;
  out.record_id = job + ':' + out.ordinal;
  out.version = 1;
  if (out.ts) {
    const parsed = timeModule.parseTs(out.ts);
    out.ts_ns = parsed.ns;
    out.ts_key = parsed.key;
  } else {
    out.ts_ns = null;
    out.ts_key = null;
  }
  return out;
}

function applyRules(records, job, profile) {
  const annotated = records.map(record => annotate(record, job));
  if (!profile) return annotated;
  try {
    return rulesModule.applyProfile(annotated, profile);
  } catch (error) {
    // A broken rule profile must not lose the framed records: return them with a
    // flag so the caller can store structure and mark rules as failed.
    annotated.forEach(record => { record.rules_failed = String(error && error.message || error).slice(0, 200); });
    return annotated;
  }
}

function handleParse(message) {
  const job = String(message.job || '');
  const token = message.token == null ? null : message.token;
  if (!job) return fail(message.id, job, token, 'bad_request', 'missing job');
  const payload = message.data || '';
  if (payload.length > MAX_ENCODED) {
    return fail(message.id, job, token, 'payload_too_large', 'payload exceeds 4 MiB encoded');
  }
  if (message.profile) state.profiles.set(job, message.profile);

  let bytes;
  try {
    bytes = Buffer.from(payload, 'base64');
  } catch (error) {
    return fail(message.id, job, token, 'bad_base64', 'payload is not base64');
  }
  if (bytes.length > MAX_PAYLOAD) {
    return fail(message.id, job, token, 'payload_too_large', 'block exceeds 4 MiB');
  }

  let produced = [];
  let consumed = 0;
  let eofFlushed = false;
  let producedBytes = 0;
  try {
    const framer = framerFor(job);
    while (consumed < bytes.length) {
      // Stop before pushing the next slice, never after: slicing records off the
      // reply would lose them, and the parent has no way to ask for them again.
      if (produced.length >= MAX_BATCH_RECORDS || producedBytes >= MAX_BATCH_BYTES) break;
      const end = Math.min(bytes.length, consumed + SLICE);
      const out = framer.push(bytes.subarray(consumed, end));
      consumed = end;
      if (out && out.length) {
        produced = produced.concat(out);
        producedBytes += JSON.stringify(out).length;
      }
    }
    if (message.eof && consumed === bytes.length) {
      produced = produced.concat(framer.flush({ eof: true }) || []);
      eofFlushed = true;
    }
  } catch (error) {
    return fail(message.id, job, token, 'frame_failed', String(error && error.message || error));
  }

  const enriched = applyRules(produced, job, state.profiles.get(job));
  state.requests += 1;
  state.records += enriched.length;
  state.bytes += consumed;
  state.jobs.add(job);

  send({
    type: 'batch',
    id: message.id,
    job: job,
    token: token,
    records: enriched,
    framed_bytes: consumed,
    // `consumed < requested` means the caller must resume from `consumed`, not
    // that the block was rejected.
    consumed: consumed,
    requested: bytes.length,
    partial: consumed < bytes.length,
    framed_lines: enriched.length && enriched[enriched.length - 1].lines
      ? enriched[enriched.length - 1].lines.last : 0,
    checkpoint: encodeCheckpoint(framerFor(job)),
    eof: eofFlushed
  });
}

/*
 * Query semantics live in src/shared/query.js and nowhere else. The server asks
 * this worker to normalise a spec and to evaluate batches, so "the server found
 * a different set of rows than the page" cannot happen by construction. The AST
 * holds compiled RegExp objects, so it stays here and the parent gets a token.
 */
function handleQueryOpen(message) {
  const parsed = queryModule.normalizeQuery(message.spec || {});
  if (!parsed.ok) {
    return fail(message.id, null, null, parsed.error.code, parsed.error.message);
  }
  state.querySeq += 1;
  const qid = 'q' + state.querySeq.toString(36) + '-' + process.pid.toString(36);
  state.queries.set(qid, parsed.ast);
  return send({
    type: 'query-opened', id: message.id, qid: qid,
    protocol: queryModule.PROTOCOL,
    signature: parsed.signature,
    summary: {
      joiner: parsed.ast.joiner,
      projection_version: parsed.ast.projection_version,
      levels: parsed.ast.levels,
      time_range: parsed.ast.time_range,
      terms: parsed.ast.terms.map(term => ({
        kind: term.kind, value: term.value, case_sensitive: term.case_sensitive,
        whole_word: term.whole_word, ascii_fast_path: term.ascii_fast_path
      }))
    }
  });
}

function handleQueryEval(message) {
  const qid = String(message.qid || '');
  const ast = state.queries.get(qid);
  if (!ast) return fail(message.id, null, null, 'unknown_query', 'query token is unknown or expired');
  const records = message.records || [];
  if (!Array.isArray(records) || records.length > MAX_BATCH_RECORDS) {
    return fail(message.id, null, null, 'batch_too_large', 'query batch is too large');
  }
  const hits = [];
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index];
    let outcome;
    try {
      outcome = queryModule.matches(record, ast);
    } catch (error) {
      return fail(message.id, null, null, 'match_failed', String(error && error.message || error));
    }
    if (outcome.hit) hits.push(record.ordinal);
  }
  state.requests += 1;
  state.records += records.length;
  return send({ type: 'query-batch', id: message.id, qid: qid, hits: hits,
    scanned: records.length, terms: ast.terms.length });
}

/* Rules-only replay: no framing, no source bytes, ordinals preserved. */
function handleApply(message) {
  const job = String(message.job || '');
  const records = message.records || [];
  if (!job) return fail(message.id, job, null, 'bad_request', 'missing job');
  if (!Array.isArray(records)) return fail(message.id, job, null, 'bad_request', 'records must be an array');
  if (records.length > MAX_RECORDS_PER_REPLY) {
    return fail(message.id, job, null, 'batch_too_large', 'too many records in one apply');
  }
  let applied;
  try {
    applied = applyRules(records, job, message.profile);
  } catch (error) {
    return fail(message.id, job, null, 'rules_failed', String(error && error.message || error));
  }
  state.requests += 1;
  state.records += applied.length;
  state.jobs.add(job);
  const failed = applied.some(record => record.rules_failed);
  return send({
    type: 'applied', id: message.id, job: job, records: applied,
    rules_failed: failed, rules_failed_reason: failed ? applied[0].rules_failed : null
  });
}

/* Semantic rule signatures. One implementation, so the server cannot drift from
 * the page about which rule edit invalidates which layer. */
function handleSignature(message) {
  const job = String(message.job || 'signature');
  const profile = message.profile || null;
  try {
    if (profile) state.profiles.set(job, profile);
    const effective = profile || state.profiles.get(job) || null;
    const sigs = effective ? rulesModule.signatures(effective) : { structure: null, extract: null, search: null };
    return send({
      type: 'signatures', id: message.id, job: job, signatures: sigs,
      rule: effective ? { id: effective.id || 'default', name: effective.name || '', version: effective.version || '' } : null
    });
  } catch (error) {
    return fail(message.id, job, null, 'signature_failed', String(error && error.message || error));
  }
}

function handleRestore(message) {
  const job = String(message.job || '');
  if (!job) return fail(message.id, job, null, 'bad_request', 'missing job');
  try {
    const framer = framerModule.createFramer({
      datasetId: job,
      startOrdinal: Number(message.start_ordinal) || 0,
      startPosition: Number(message.start_position) || 0,
      startLine: Number(message.start_line) || 0
    });
    if (message.checkpoint) framer.restore(decodeCheckpoint(message.checkpoint));
    state.framers.set(job, framer);
    if (message.profile) state.profiles.set(job, message.profile);
    state.jobs.add(job);
    const checkpoint = framer.checkpoint();
    return send({
      type: 'restored', id: message.id, job: job,
      ordinal: checkpoint.ordinal, position: checkpoint.position, line_no: checkpoint.line_no
    });
  } catch (error) {
    return fail(message.id, job, null, 'restore_failed',
      String(error && error.message || error));
  }
}

function main() {
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', line => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let message;
    try {
      message = JSON.parse(trimmed);
    } catch (error) {
      return fail(null, null, null, 'bad_json', 'request is not JSON');
    }
    switch (message.type) {
      case 'hello':
        return send({
          type: 'ready',
          protocol: PROTOCOL,
          runtime: process.version,
          pid: process.pid,
          engine: {
            framer: 'r6-009',
            rules: 'r6-011',
            time: 'r6-020',
            resume: 'framer-checkpoint/1',
            max_payload: MAX_PAYLOAD,
            max_records: MAX_RECORDS_PER_REPLY
          }
        });
      case 'parse':
        return handleParse(message);
      case 'apply':
        return handleApply(message);
      case 'query-open':
        return handleQueryOpen(message);
      case 'query-eval':
        return handleQueryEval(message);
      case 'query-close':
        state.queries.delete(String(message.qid));
        return send({ type: 'query-closed', id: message.id, qid: message.qid });
      case 'signature':
        return handleSignature(message);
      case 'restore':
        return handleRestore(message);
      case 'sleep':
        // Test hook: the parent's watchdog must kill a worker that stops
        // answering, and this is the cheapest way to make it stop.
        return setTimeout(() => send({ type: 'slept', id: message.id, ms: message.ms || 0 }),
          Math.min(60000, Number(message.ms) || 0));
      case 'reset':
        state.framers.delete(String(message.job));
        state.profiles.delete(String(message.job));
        return send({ type: 'reset', job: message.job });
      case 'stats':
        return send({
          type: 'stats', protocol: PROTOCOL, jobs: [...state.jobs],
          requests: state.requests, records: state.records, bytes: state.bytes,
          uptime_ms: Date.now() - state.started, rss: process.memoryUsage().rss
        });
      case 'shutdown':
        send({ type: 'bye', protocol: PROTOCOL });
        return setTimeout(() => process.exit(0), 10);
      default:
        return fail(message.id, message.job, null, 'unknown_command', 'unknown type ' + message.type);
    }
  });
  rl.on('close', () => process.exit(0));
}

main();
