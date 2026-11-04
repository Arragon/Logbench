/*
 * server-mode.js — explicit server acceleration: upload, resume, index, read (R6-048).
 *
 * The reader never uploads anything by accident. A file goes to the server only
 * after the user turns on "服务端加速", and the panel keeps saying what is
 * happening: `preparing`, `uploading 42%`, `sealed`, `indexing 60%`, `ready`.
 *
 * Resumability is the point of the part protocol rather than a nice-to-have:
 * `resumeFrom(status)` returns the missing part numbers, so a reloaded page, a
 * re-picked file, or a retried request continues instead of starting over. The
 * uploader never guesses which parts the server has.
 *
 * `api` is injected so this module can be tested without a browser; the real HTTP
 * wiring lives in `bind()`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBServerMode = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var PART_BYTES = 4 * 1024 * 1024;

  function sha256Hex(buffer) {
    if (typeof crypto !== 'undefined' && crypto.subtle && crypto.subtle.digest) {
      return crypto.subtle.digest('SHA-256', buffer).then(function (digest) {
        return Array.prototype.map.call(new Uint8Array(digest), function (byte) {
          return ('0' + byte.toString(16)).slice(-2);
        }).join('');
      });
    }
    return Promise.resolve(null);
  }

  /** Missing part numbers for a session, from the server's own answer. */
  function resumeFrom(status) {
    if (!status) return [];
    if (Array.isArray(status.resume)) return status.resume.slice();
    var have = {};
    (status.parts || []).forEach(function (part) { have[part.part_no] = true; });
    var missing = [];
    for (var number = 1; number <= (status.parts_expected || 0); number += 1) {
      if (!have[number]) missing.push(number);
    }
    return missing;
  }

  /**
   * @param {object} options
   *   api        — {createUpload, putPart, commitUpload, getUpload, getIndexJob}
   *   onState    — (state) => void, called on every transition
   *   partBytes  — optional override
   */
  function createUploader(options) {
    options = options || {};
    var api = options.api;
    if (!api) throw new Error('server-mode needs an api');
    var partBytes = options.partBytes || PART_BYTES;
    var cancelled = false;
    var state = { phase: 'idle', sessionId: null, name: null, bytesDone: 0, bytesTotal: 0,
      partsDone: 0, partsTotal: 0, error: null, jobId: null, fileId: null };

    function emit(patch) {
      for (var key in patch) state[key] = patch[key];
      // Clamp: the last part may be short, so `parts_done * chunk` can overshoot,
      // and a negative percentage would make the bar jump backwards.
      state.bytesDone = Math.max(0, Math.min(state.bytesTotal || 0, state.bytesDone || 0));
      state.percent = state.bytesTotal
        ? Math.min(100, Math.round(state.bytesDone * 100 / state.bytesTotal))
        : 0;
      if (options.onState) options.onState(JSON.parse(JSON.stringify(state)));
      return state;
    }

    function cancel() {
      // Pausing an upload is client-side only: the parts already stored stay, and
      // resuming asks the server which ones are missing.
      cancelled = true;
      return emit({ phase: 'paused' });
    }

    function upload(file, existingSessionId) {
      cancelled = false;
      var sessionPromise = existingSessionId
        ? api.getUpload(existingSessionId)
        : api.createUpload({ name: file.name, size: file.size, chunkSize: partBytes });
      emit({ phase: 'preparing', name: file.name, bytesTotal: file.size, error: null });
      return sessionPromise.then(function (session) {
        var missing = existingSessionId ? resumeFrom(session) : rangeFrom(session);
        var present = Math.max(0, session.parts_expected - missing.length);
        emit({
          phase: 'uploading',
          sessionId: session.session_id,
          name: session.name,
          bytesTotal: session.size,
          bytesDone: Math.min(session.size, present * session.chunk_size),
          partsDone: present,
          partsTotal: session.parts_expected
        });
        var chain = Promise.resolve();
        missing.forEach(function (partNo) {
          chain = chain.then(function () {
            if (cancelled) return null;
            var start = (partNo - 1) * session.chunk_size;
            var end = Math.min(session.size, start + session.chunk_size);
            var chunk = file.slice(start, end);
            return chunk.arrayBuffer().then(function (buffer) {
              return sha256Hex(buffer).then(function (digest) {
                var part = new Uint8Array(buffer);
                return api.putPart(session.session_id, partNo, part, digest).then(function (result) {
                  return emit({
                    bytesDone: state.bytesDone + (end - start),
                    partsDone: state.partsDone + 1,
                    lastPart: result.idempotent ? 'reused' : 'stored'
                  });
                });
              });
            });
          });
        });
        return chain.then(function () {
          if (cancelled) return state;
          emit({ phase: 'sealing' });
          return api.commitUpload(session.session_id).then(function (sealed) {
            emit({ phase: 'indexing', jobId: sealed.job_id, fileId: sealed.file_id,
                   sealedPath: sealed.sealed_path });
            return sealed;
          });
        });
      }).catch(function (error) {
        emit({ phase: 'failed', error: { code: error.code || 'upload_failed',
                                         message: error.message || String(error) } });
        throw error;
      });
    }

    function rangeFrom(session) {
      var list = [];
      for (var number = 1; number <= session.parts_expected; number += 1) list.push(number);
      return list;
    }

    function pollIndex(sealed, options2) {
      options2 = options2 || {};
      var interval = options2.interval == null ? 300 : options2.interval;
      var deadline = Date.now() + (options2.timeout || 10 * 60 * 1000);
      function tick() {
        return api.getIndexJob(state.jobId).then(function (job) {
          var progress = job.progress || {};
          emit({ phase: job.state === 'indexed' ? 'ready' : 'indexing', jobState: job.state,
                 indexPhase: progress.phase || null });
          if (job.state === 'indexed') {
            emit({ phase: 'ready', fileId: state.fileId || job.payload && job.payload.file_id });
            return state;
          }
          if (job.state === 'failed' || job.state === 'cancelled') {
            emit({ phase: 'failed', error: job.error || { code: job.state, message: '索引未完成' } });
            return state;
          }
          if (Date.now() > deadline) {
            emit({ phase: 'failed', error: { code: 'index_timeout', message: '索引超时，可稍后重试' } });
            return state;
          }
          return new Promise(function (resolve) { setTimeout(resolve, interval); }).then(tick);
        });
      }
      return tick();
    }

    return {
      upload: upload,
      cancel: cancel,
      pollIndex: pollIndex,
      resumeFrom: resumeFrom,
      state: function () { return JSON.parse(JSON.stringify(state)); }
    };
  }

  /** Reads a server-hosted file through the v2 API (no full download). */
  function createServerReader(options) {
    options = options || {};
    var provider = options.provider;
    var pageLimit = Math.min(options.limit || 200, 2000);
    var current = { fileId: null, cursor: null, rows: [], index: null };

    function open(fileId) {
      return provider.open(fileId).then(function (opened) {
        current.fileId = fileId;
        current.index = opened.index || null;
        return provider.headers({ file_id: fileId, limit: pageLimit });
      }).then(function (page) {
        current.rows = page.items || [];
        current.cursor = page.next_cursor;
        return { rows: current.rows, cursor: current.cursor, index: current.index };
      });
    }

    function next() {
      if (current.cursor == null) return Promise.resolve({ rows: [], cursor: null, done: true });
      return provider.headers({ file_id: current.fileId, cursor: current.cursor, limit: pageLimit })
        .then(function (page) {
          current.rows = page.items || [];
          current.cursor = page.next_cursor;
          return { rows: current.rows, cursor: current.cursor, done: current.cursor == null };
        });
    }

    function detail(ordinal) {
      return provider.detail({ file_id: current.fileId, ordinal: ordinal });
    }

    function search(spec) {
      return provider.search(Object.assign({ file_id: current.fileId }, spec || {}));
    }

    return { open: open, next: next, detail: detail, search: search,
             state: function () { return { fileId: current.fileId, cursor: current.cursor,
                                           rows: current.rows.slice(), index: current.index }; } };
  }

  return {
    PART_BYTES: PART_BYTES,
    resumeFrom: resumeFrom,
    createUploader: createUploader,
    createServerReader: createServerReader
  };
});
