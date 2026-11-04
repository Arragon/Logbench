/*
 * import-jobs.js — bounded import pipeline with backpressure and resume (R6-020).
 *
 * R5's importer read the whole file with `file.text()`, sent it to a Worker and
 * got a whole array back. That is why one 100 MiB file froze the page and why
 * the batch had no progress that could be trusted. This pipeline replaces it
 * with five stages that each have a bound:
 *
 *   read    one `chunkSize` slice at a time (default 2 MiB); the whole file is
 *           never in memory
 *   save    raw chunk -> engine, awaited, with at most `maxInFlight` writes
 *   frame   pushed into the streaming framer; records are emitted as they close
 *   rules   field extraction / anomaly flags, per record
 *   index   blocks of `blockRecords` records, staged one block at a time
 *
 * Three properties the ticket asks for, and how they are obtained:
 *
 *   "visible within 100ms"  `add()` creates tasks synchronously and emits them
 *                           before any I/O starts.
 *   "pause at a safe boundary"  the loop checks the pause flag between chunks and
 *                           between block commits; a checkpoint records bytes,
 *                           blocks and ordinal so resume continues instead of
 *                           re-reading the file.
 *   "a failed file does not cancel the batch"  every task has its own promise
 *                           chain and its own error; the queue only aggregates.
 *
 * Yielding is a macrotask (`setTimeout(0)`), not a resolved promise: a promise
 * chain would let the event loop starve while the browser still considers the
 * main thread "responsive" in a microtask sense.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBImportJobs = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = {
    chunkSize: 2 * 1024 * 1024,
    blockRecords: 512,
    maxInFlight: 2,
    maxConcurrentFiles: 2,
    maxBufferedBlocks: 2,
    yieldMs: 12,
    progressIntervalMs: 250
  };

  var PHASES = ['queued', 'reading', 'saving', 'framing', 'indexing', 'indexed', 'paused', 'cancelled', 'failed'];

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function noop() {}

  /**
   * @param {object} options
   *   client      rpc client to the local engine (putFile/appendChunk/stageBlock/publishIndex)
   *   framer      LBFramer (createFramer)
   *   rules       optional LBRules (applyProfile) for fields/anomalies
   *   time        optional LBTime (parseTs)
   *   clock/now   injectable time source
   *   schedule    optional macrotask scheduler, default setTimeout
   *   emit        optional event sink (name, payload)
   */
  function createImportQueue(options) {
    options = options || {};
    var client = options.client;
    if (!client) throw errorOf('no_client', '导入队列需要本机引擎客户端');
    var framer = options.framer;
    if (!framer) throw errorOf('no_framer', '导入队列需要分帧器');
    var rules = options.rules || null;
    var time = options.time || null;
    var now = options.now || function () { return Date.now(); };
    var schedule = options.schedule || function (fn, ms) { return setTimeout(fn, ms || 0); };
    var emit = options.emit || noop;
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];

    var tasks = [];
    var byId = Object.create(null);
    var running = 0;
    var inFlight = 0;
    var slots = [];
    var lastProgressAt = 0;
    var pausedAll = false;
    var finished = 0;
    var activeFiles = 0;
    var fileQueue = [];

    function progressSnapshot() {
      var bytesTotal = 0;
      var bytesDone = 0;
      var ready = 0;
      var failed = 0;
      var files = tasks.map(function (task) {
        bytesTotal += task.size;
        bytesDone += task.bytes_done;
        if (task.phase === 'indexed') ready += 1;
        if (task.phase === 'failed') failed += 1;
        return {
          id: task.id, name: task.name, size: task.size, phase: task.phase,
          bytes_done: task.bytes_done, records: task.records,
          blocks: task.blocks_written, error: task.error ? task.error.code : null
        };
      });
      return {
        bytes_total: bytesTotal,
        bytes_done: bytesDone,
        files_total: tasks.length,
        files_ready: ready,
        files_failed: failed,
        // No percentage when the denominator is unknown; every task is registered
        // with a real size, so this is always a true fraction of known bytes.
        ratio: bytesTotal > 0 ? bytesDone / bytesTotal : null,
        tasks: files
      };
    }

    function emitProgress(force) {
      var stamp = now();
      if (!force && stamp - lastProgressAt < settings.progressIntervalMs) return;
      lastProgressAt = stamp;
      emit('progress', progressSnapshot());
    }

    function acquireSlot() {
      if (inFlight < settings.maxInFlight) {
        inFlight += 1;
        return Promise.resolve(function release() { inFlight -= 1; var next = slots.shift(); if (next) next(); });
      }
      return new Promise(function (resolve) {
        slots.push(function () {
          inFlight += 1;
          resolve(function release() { inFlight -= 1; var next = slots.shift(); if (next) next(); });
        });
      });
    }

    function yieldToLoop() {
      return new Promise(function (resolve) { schedule(resolve, 0); });
    }

    function fileIdOf(task) {
      return 'local-' + task.hash;
    }

    function makeTask(file, index) {
      return {
        id: 'task-' + (index + 1) + '-' + Math.random().toString(36).slice(2, 6),
        name: file.name || ('file-' + (index + 1)),
        size: file.size || 0,
        file: file,
        // Stable across a reload of the same file; collisions only mean the
        // same bytes are being re-imported, which resume already handles.
        hash: (file.name || 'f') + '-' + (file.size || 0) + '-' + (file.lastModified || 0),
        phase: 'queued',
        bytes_done: 0,
        records: 0,
        blocks_written: 0,
        checkpoint: { bytes_read: 0, blocks_written: 0, ordinal: 0 },
        eager: false,
        error: null,
        paused: false,
        cancelled: false,
        done: null
      };
    }

    function findTask(id) {
      return byId[id] || null;
    }

    function failTask(task, error) {
      task.phase = 'failed';
      task.error = { code: (error && error.code) || 'import_failed', message: String(error && error.message || error) };
      finished += 1;
      emit('task', { task: task, reason: 'failed' });
      emitProgress(true);
    }

    async function runTask(task) {
      if (task.phase === 'indexed' || task.phase === 'failed') return;
      var generation = 1;
      var fileId = fileIdOf(task);
      var size = task.size;
      var chunks = Math.max(1, Math.ceil(size / settings.chunkSize));
      task.phase = 'reading';
      emit('task', { task: task, reason: 'started' });

      // One framer per task for its whole life: pausing between chunks must not
      // restart framing at a byte that is not a line boundary.
      if (!task.framer) {
        task.framer = framer.createFramer({ datasetId: fileId, startOrdinal: task.checkpoint.ordinal });
      }
      var framerState = task.framer;
      var buffer = [];
      var framedLines = 0;

      /**
       * Identity and time first, then rules over the whole block. `applyProfile`
       * is a batch API (it prepares the profile once); calling it per record both
       * crashed and recompiled the rule set 512 times per block.
       */
      function annotate(record) {
        var out = record;
        out.file_id = fileId;
        out.record_id = fileId + ':' + out.ordinal;
        out.version = 1;
        if (time && out.ts) {
          var parsed = time.parseTs(out.ts);
          out.ts_ns = parsed.ns;
          out.ts_key = parsed.key;
        } else {
          out.ts_ns = null;
          out.ts_key = null;
        }
        return out;
      }

      function enrichBlock(records) {
        var prepared = records.map(annotate);
        if (rules && rules.applyProfile && options.profile) {
          return rules.applyProfile(prepared, options.profile);
        }
        return prepared;
      }

      async function flushBlock(force) {
        if (!buffer.length) return;
        if (!force && buffer.length < settings.blockRecords) return;
        if (task.cancelled) return;
        var release = await acquireSlot();
        try {
          var records = enrichBlock(buffer.splice(0, buffer.length));
          // A very long record can exceed the page budget on its own; the block
          // is the unit we commit, so it is measured here.
          await client.call('stageBlock', {
            file_id: fileId, generation: generation,
            block_no: task.blocks_written, records: records,
            signatures: options.signatures || { structure: 'default', extract: 'default', search: 'v1' }
          });
          task.blocks_written += 1;
          task.records += records.length;
          task.checkpoint.blocks_written = task.blocks_written;
          task.checkpoint.ordinal += records.length;
          if (task.blocks_written === 1) {
            // The first committed block is the moment the file becomes readable,
            // but the *published* pointer must wait for the whole file.
            emit('task', { task: task, reason: 'first_block' });
          }
        } finally {
          release();
        }
        emitProgress();
      }

      try {
        await client.call('putFile', {
          file_id: fileId, name: task.name, size: size, generation: generation,
          chunks: chunks, digest: null
        });

        var offset = task.checkpoint.bytes_read;
        var chunkNo = Math.floor(offset / settings.chunkSize);
        while (offset < size) {
          if (task.cancelled) {
            task.phase = 'cancelled';
            return;
          }
          if (task.paused || pausedAll) {
            task.phase = 'paused';
            emit('task', { task: task, reason: 'paused' });
            return;
          }
          var end = Math.min(size, offset + settings.chunkSize);
          var chunk = new Uint8Array(await task.file.slice(offset, end).arrayBuffer());
          if (task.cancelled) { task.phase = 'cancelled'; return; }

          task.phase = 'saving';
          var release = await acquireSlot();
          try {
            await client.call('appendChunk', {
              file_id: fileId, generation: generation, chunk_no: chunkNo,
              bytes: chunk, offset: offset
            });
          } finally {
            release();
          }
          task.bytes_done = end;
          task.checkpoint.bytes_read = end;
          chunkNo += 1;
          emitProgress();

          task.phase = 'framing';
          var produced = framerState.push(chunk);
          if (produced && produced.length) {
            for (var i = 0; i < produced.length; i++) {
              buffer.push(produced[i]);
              framedLines = Math.max(framedLines, produced[i].lines ? produced[i].lines.last : framedLines);
              if (buffer.length >= settings.blockRecords) {
                task.phase = 'indexing';
                await flushBlock(false);
                if (task.cancelled) { task.phase = 'cancelled'; return; }
                if (task.paused || pausedAll) { task.phase = 'paused'; return; }
              }
            }
          }
          offset = end;
          await yieldToLoop();
        }

        var tail = framerState.flush({ eof: true });
        if (tail && tail.length) {
          for (var j = 0; j < tail.length; j++) {
            buffer.push(tail[j]);
            framedLines = Math.max(framedLines, tail[j].lines ? tail[j].lines.last : framedLines);
          }
        }
        task.phase = 'indexing';
        await flushBlock(true);
        if (task.cancelled) { task.phase = 'cancelled'; return; }
        if (task.paused || pausedAll) { task.phase = 'paused'; return; }

        await client.call('publishIndex', {
          file_id: fileId, generation: generation, size: size,
          signatures: options.signatures || { structure: 'default', extract: 'default', search: 'v1' },
          blocks: task.blocks_written,
          watermark: {
            raw_bytes: task.bytes_done,
            framed_lines: Math.max(framedLines, task.records),
            indexed_records: task.records
          }
        });
        task.phase = 'indexed';
        finished += 1;
        emit('task', { task: task, reason: 'indexed' });
        emitProgress(true);
      } catch (error) {
        if (task.cancelled) { task.phase = 'cancelled'; return; }
        failTask(task, error);
      }
    }

    /**
     * Start queued tasks, at most `maxConcurrentFiles` at a time. Two imports run
     * in parallel; the rest wait, because "ten files at once" is how the old
     * importer turned a batch into a frozen tab.
     */
    function pump() {
      var limit = settings.maxConcurrentFiles;
      tasks.forEach(function (task) {
        if (task.done || task.phase !== 'queued') return;
        if (activeFiles >= limit) { if (fileQueue.indexOf(task) === -1) fileQueue.push(task); return; }
        activeFiles += 1;
        task.done = runTask(task).then(function () {
          activeFiles -= 1;
          var next = fileQueue.shift();
          if (next && next.phase === 'queued') { next.done = null; pump(); }
        }, function (error) {
          activeFiles -= 1;
          failTask(task, error);
        });
      });
    }

    function add(files, addOptions) {
      var list = Array.prototype.slice.call(files || []);
      var created = [];
      list.forEach(function (file) {
        var task = makeTask(file, tasks.length);
        if (addOptions && addOptions.eager) task.eager = true;
        tasks.push(task);
        byId[task.id] = task;
        created.push(task);
        // Registration is synchronous: the UI can show the queue in the same
        // tick, before any bytes are read.
        emit('task', { task: task, reason: 'queued' });
      });
      emitProgress(true);
      // Start on a macrotask so the caller can paint the queue first. Registering
      // and starting in the same tick is what made the old importer feel like a
      // click that either did nothing or froze.
      schedule(pump, 0);
      return created;
    }

    function pause(id) {
      if (id) {
        var task = findTask(id);
        if (!task) return false;
        task.paused = true;
        // A queued file pauses immediately; a running one stops at the next
        // safe boundary and reports `paused` itself.
        if (task.phase === 'queued') task.phase = 'paused';
        emitProgress(true);
        return true;
      }
      pausedAll = true;
      tasks.forEach(function (task) { if (task.phase === 'queued') task.phase = 'paused'; });
      emitProgress(true);
      return true;
    }

    function resume(id) {
      if (id) {
        var task = findTask(id);
        if (!task || task.phase !== 'paused') return false;
        task.paused = false;
        task.phase = 'queued';
        task.done = null;
        pump();
        return true;
      }
      pausedAll = false;
      tasks.forEach(function (task) {
        if (task.phase === 'paused' && !task.cancelled) {
          task.paused = false;
          task.phase = 'queued';
          task.done = null;
        }
      });
      pump();
      return true;
    }

    function cancel(id) {
      var task = findTask(id);
      if (!task) return false;
      task.cancelled = true;
      task.paused = false;
      if (task.phase === 'queued' || task.phase === 'paused') task.phase = 'cancelled';
      // Raw chunks and committed blocks stay: cancel is not delete (C8).
      emit('task', { task: task, reason: 'cancelled' });
      emitProgress(true);
      return true;
    }

    function retry(id) {
      var task = findTask(id);
      if (!task || task.phase !== 'failed') return false;
      task.error = null;
      task.phase = 'queued';
      task.paused = false;
      task.cancelled = false;
      task.done = null;
      pump();
      return true;
    }

    function raisePriority(id) {
      var task = findTask(id);
      if (!task) return false;
      var index = tasks.indexOf(task);
      if (index > 0) { tasks.splice(index, 1); tasks.unshift(task); }
      task.eager = true;
      return true;
    }

    return {
      add: add,
      pause: pause,
      resume: resume,
      cancel: cancel,
      retry: retry,
      raisePriority: raisePriority,
      tasks: function () { return tasks.slice(); },
      snapshot: progressSnapshot,
      settings: function () { var copy = {}; for (var key in settings) copy[key] = settings[key]; return copy; },
      stats: function () {
        return {
          tasks: tasks.length,
          finished: finished,
          running: tasks.filter(function (t) { return ['reading', 'saving', 'framing', 'indexing'].indexOf(t.phase) !== -1; }).length,
          in_flight: inFlight,
          waiting: slots.length
        };
      }
    };
  }

  return {
    DEFAULTS: DEFAULTS,
    PHASES: PHASES,
    createImportQueue: createImportQueue
  };
});
