/*
 * query-jobs.js — resumable query jobs with progress and cancellation (R6-022).
 *
 * The rule this module exists to enforce is the one R5 broke: a query that has
 * not finished must never report `0` as if it were a result. Concretely:
 *
 *   running  -> relation `at_least`, coverage.complete = false, no "0 条"
 *   paused   -> relation `at_least`, resume_token present, scanned range stated
 *   done     -> relation `exact`, coverage.complete = true
 *   cancelled-> relation `unknown`, matched = null, scanned range stated
 *
 * A job scans blocks in slices. Between slices it yields with a macrotask, so a
 * 30-second search does not lock the worker (or the tab) for 30 seconds. The
 * budget pauses rather than kills: the snapshot and the scan position survive,
 * so `resume` continues the same scan instead of starting over.
 *
 * Results are paged. `hits` holds at most `pageLimit` ordinals; `matched` is a
 * count. A zero-hit search therefore costs one number, not 130k `false` flags.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBQueryJobs = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = {
    budgetMs: 30000,
    sliceMs: 40,
    blocksPerSlice: 4,
    pageLimit: 200
  };

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /**
   * @param {object} options
   *   now        injectable clock (ms)
   *   schedule   macrotask scheduler, default setTimeout
   *   budgetMs   active computing budget before the job pauses
   */
  function createQueryService(options) {
    options = options || {};
    var now = options.now || function () { return Date.now(); };
    var schedule = options.schedule || function (fn) { return setTimeout(fn, 0); };
    var settings = {};
    for (var key in DEFAULTS) settings[key] = options[key] == null ? DEFAULTS[key] : options[key];

    var jobs = Object.create(null);
    var cache = Object.create(null);
    var counter = 0;

    function snapshotOf(job) {
      var complete = job.state === 'done';
      return {
        query_id: job.id,
        query_version: job.query_version,
        state: job.state,
        signature: job.snapshot.signature,
        file_id: job.snapshot.file_id,
        generation: job.snapshot.generation,
        index_version: job.snapshot.index_version,
        count: {
          relation: complete ? 'exact' : (job.state === 'cancelled' ? 'unknown' : 'at_least'),
          matched: job.state === 'cancelled' ? null : job.matched
        },
        coverage: {
          complete: complete,
          scanned_blocks: job.scanned_blocks,
          total_blocks: job.total_blocks,
          scanned_records: job.scanned_records,
          total_records: job.total_records,
          reason: complete ? null : (job.state === 'paused' ? 'budget' : 'in_progress')
        },
        page: {
          offset: 0,
          items: job.hits.slice(),
          next_cursor: job.matched > job.hits.length ? job.hits.length : null
        },
        order_final: complete,
        resume_token: job.state === 'paused' ? job.resume_token() : null,
        progress: job.total_blocks ? job.scanned_blocks / job.total_blocks : 0
      };
    }

    /**
     * @param {object} request
     *   fileId, generation, indexVersion, signature, queryVersion
     *   blocks        total block count
     *   records       total record count
     *   fetch(from, count) -> Promise<Array<{block_no, records}>>
     *   match(record) -> boolean
     *   pageLimit
     */
    function start(request) {
      // A repeated search over the same generation with the same signature is
      // answered from the finished snapshot. The key includes the index version,
      // so a new generation invalidates it rather than mixing two files.
      var key = request.cacheKey;
      if (key && cache[key] && cache[key].index_version === (request.indexVersion == null ? request.generation : request.indexVersion)) {
        var cached = cache[key].snapshot;
        var copy = {};
        for (var field in cached) copy[field] = cached[field];
        copy.cached = true;
        return copy;
      }
      var id = 'q' + (++counter).toString(36) + '-' + Math.random().toString(36).slice(2, 6);
      var job = {
        id: id,
        query_version: request.queryVersion || 1,
        snapshot: {
          file_id: request.fileId,
          generation: request.generation,
          index_version: request.indexVersion == null ? request.generation : request.indexVersion,
          signature: request.signature || 'unsigned'
        },
        total_blocks: request.blocks || 0,
        total_records: request.records || 0,
        scanned_blocks: 0,
        scanned_records: 0,
        matched: 0,
        hits: [],
        page_limit: request.pageLimit || settings.pageLimit,
        state: 'queued',
        started_at: now(),
        active_ms: 0,
        fetch: request.fetch,
        match: request.match,
        cancelled: false,
        finished: null,
        cache_key: request.cacheKey || null,
        resume_token: function () {
          return [id, job.scanned_blocks, job.matched, job.snapshot.generation,
                  job.snapshot.signature].join(':');
        }
      };
      jobs[id] = job;
      // The caller sees the queued state: the first snapshot must already carry
      // the honest "at_least" relation rather than pretending to be a result.
      var queued = snapshotOf(job);
      run(job);
      return queued;
    }

    function run(job) {
      if (job.state === 'running') return job.finished;
      job.state = 'running';
      job.finished = new Promise(function (resolve) {
        var sliceStart = now();
        var blocksThisSlice = 0;

        function step() {
          if (job.cancelled) {
            job.state = 'cancelled';
            resolve(snapshotOf(job));
            return;
          }
          if (job.scanned_blocks >= job.total_blocks) {
            job.state = 'done';
            var finished = snapshotOf(job);
            if (job.cache_key) {
              cache[job.cache_key] = { index_version: job.snapshot.index_version, snapshot: finished };
            }
            resolve(finished);
            return;
          }
          if (job.active_ms >= settings.budgetMs) {
            job.state = 'paused';
            resolve(snapshotOf(job));
            return;
          }
          var want = Math.min(settings.blocksPerSlice - blocksThisSlice, job.total_blocks - job.scanned_blocks);
          Promise.resolve(job.fetch(job.scanned_blocks, want)).then(function (blocks) {
            var started = now();
            for (var i = 0; i < blocks.length; i++) {
              var block = blocks[i];
              var records = block.records || [];
              for (var r = 0; r < records.length; r++) {
                if (job.match(records[r])) {
                  job.matched += 1;
                  if (job.hits.length < job.page_limit) job.hits.push(records[r].ordinal);
                }
              }
              job.scanned_blocks += 1;
              job.scanned_records += records.length;
            }
            job.active_ms += now() - started;
            blocksThisSlice += blocks.length;
            if (blocksThisSlice >= settings.blocksPerSlice || now() - sliceStart >= settings.sliceMs) {
              blocksThisSlice = 0;
              sliceStart = now();
              schedule(step, 0);
            } else {
              step();
            }
          }, function (error) {
            job.state = 'failed';
            job.error = { code: (error && error.code) || 'query_failed', message: String(error && error.message || error) };
            resolve(snapshotOf(job));
          });
        }
        schedule(step, 0);
      });
      return job.finished;
    }

    function state(id) {
      var job = jobs[id];
      if (!job) throw errorOf('unknown_query', '查询不存在：' + id);
      return snapshotOf(job);
    }

    function cancel(id) {
      var job = jobs[id];
      if (!job) throw errorOf('unknown_query', '查询不存在：' + id);
      if (['done', 'cancelled', 'failed'].indexOf(job.state) !== -1) return snapshotOf(job);
      job.cancelled = true;
      job.state = 'cancelled';
      return snapshotOf(job);
    }

    function resume(id, token) {
      var job = jobs[id];
      if (!job) throw errorOf('unknown_query', '查询不存在：' + id);
      if (token && token !== job.resume_token()) {
        throw errorOf('cursor_expired', '续查令牌与当前扫描位置不一致', {
          expected: job.resume_token(), given: token
        });
      }
      if (job.state !== 'paused') return snapshotOf(job);
      // A resumed job gets a fresh budget but keeps the same snapshot and scan
      // position: it is the same query, not a new one.
      job.state = 'running';
      job.active_ms = 0;
      run(job);
      return snapshotOf(job);
    }

    function cacheKey(request) {
      return [request.fileId, request.generation, request.signature,
              request.levels || '', JSON.stringify(request.time_range || null)].join('|');
    }

    return {
      start: start,
      state: state,
      cancel: cancel,
      resume: resume,
      snapshot: state,
      cacheKey: cacheKey,
      settings: function () { var copy = {}; for (var key in settings) copy[key] = settings[key]; return copy; },
      jobs: function () { return Object.keys(jobs).length; }
    };
  }

  return {
    DEFAULTS: DEFAULTS,
    createQueryService: createQueryService
  };
});
