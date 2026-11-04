/*
 * engine-core.js — the resident data owner's logic, without a Worker around it (R6-019).
 *
 * Everything here runs inside `local-engine.js`'s Worker. Keeping it as a plain
 * module means the routing, paging limits, LRU and generation guards can be
 * tested in Node against a fake store, while the browser harness only has to
 * prove that the same code works over real IndexedDB.
 *
 * Three invariants the tickets keep returning to:
 *
 *   1. A page is never larger than 2000 records or 4 MiB. Oversized requests are
 *      refused with `page_too_large`, not truncated: a truncated page silently
 *      changes what "next page" means.
 *   2. Every read names a generation. If the published generation moved, the
 *      answer is `stale_generation` — never another generation's rows, which is
 *      how a search silently returns the previous file's content.
 *   3. Metadata reads never touch raw chunks. `listFiles` is what the file
 *      library calls on every open.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBEngineCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MAX_PAGE_RECORDS = 2000;
  var MAX_PAGE_BYTES = 4194304;
  var DEFAULT_PAGE = 200;
  var DEFAULT_CACHE_BYTES = 32 * 1024 * 1024;
  var DEFAULT_CACHE_PAGES = 64;

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /** Bounded LRU by entry count and byte size; the reader pages through it. */
  function createPageCache(options) {
    options = options || {};
    var maxBytes = options.maxBytes || DEFAULT_CACHE_BYTES;
    var maxEntries = options.maxEntries || DEFAULT_CACHE_PAGES;
    var entries = new Map();
    var bytes = 0;
    var stats = { hits: 0, misses: 0, evictions: 0, bytes: 0, entries: 0 };

    function get(key) {
      var entry = entries.get(key);
      if (!entry) { stats.misses += 1; return null; }
      entries.delete(key);
      entries.set(key, entry);
      stats.hits += 1;
      return entry.value;
    }

    function put(key, value, size) {
      var cost = Math.max(1, size | 0);
      var existing = entries.get(key);
      if (existing) {
        bytes -= existing.size;
        entries.delete(key);
      }
      entries.set(key, { value: value, size: cost });
      bytes += cost;
      while ((bytes > maxBytes || entries.size > maxEntries) && entries.size > 1) {
        var oldest = entries.keys().next().value;
        var removed = entries.get(oldest);
        entries.delete(oldest);
        bytes -= removed.size;
        stats.evictions += 1;
      }
      stats.bytes = bytes;
      stats.entries = entries.size;
    }

    return {
      get: get,
      put: put,
      clear: function () { entries.clear(); bytes = 0; stats.bytes = 0; stats.entries = 0; },
      stats: function () { var copy = {}; for (var key in stats) copy[key] = stats[key]; return copy; }
    };
  }

  function byteLengthOf(value) {
    if (value == null) return 0;
    if (value instanceof Uint8Array) return value.byteLength;
    try { return JSON.stringify(value).length; } catch (error) { return 0; }
  }

  /**
   * @param {object} deps
   *   store    — local-store namespace handle (metadata, raw chunks)
   *   indexer  — index-store indexer (blocks + published pointer)
   *   now      — injectable clock
   */
  function createEngine(deps) {
    deps = deps || {};
    var store = deps.store;
    var indexer = deps.indexer;
    if (!store || !indexer) throw errorOf('no_deps', '引擎需要 store 与 indexer');
    var now = deps.now || function () { return Date.now(); };
    var cache = createPageCache(deps.cache);
    var queryService = null;
    if (deps.queryJobs) {
      queryService = deps.queryJobs.createQueryService({
        now: now,
        schedule: deps.schedule || function (fn) { return setTimeout(fn, 0); },
        budgetMs: deps.budgetMs || 30000,
        blocksPerSlice: deps.blocksPerSlice || 4,
        sliceMs: deps.sliceMs || 40
      });
    }
    var datasetId = deps.datasetId || 'local';
    var counters = { pages: 0, raw_reads: 0, raw_bytes: 0, searches: 0, blocks_read: 0 };

    function requireGeneration(fileId, generation) {
      return indexer.readPublished(fileId).then(function (published) {
        if (!published) {
          throw errorOf('not_indexed', '该文件尚未建立索引', { file_id: fileId });
        }
        if (generation != null && generation !== published.generation) {
          throw errorOf('stale_generation', '索引已更新，请重新加载', {
            requested: generation, published: published.generation
          });
        }
        return published;
      });
    }

    function listFiles() {
      return store.listFileMeta().then(function (files) {
        return {
          dataset_id: datasetId,
          files: files.map(function (meta) {
            return {
              file_id: meta.file_id,
              name: meta.name,
              size: meta.size,
              generation: meta.generation,
              chunks: meta.chunks,
              digest: meta.digest,
              migrated_from: meta.migrated_from || null
            };
          })
        };
      });
    }

    function getMetadata(params) {
      return store.listFileMeta().then(function (files) {
        var meta = null;
        for (var i = 0; i < files.length; i++) {
          if (files[i].file_id === params.file_id) { meta = files[i]; break; }
        }
        if (!meta) throw errorOf('missing_file', '文件不存在：' + params.file_id);
        return meta;
      });
    }

    function ensureIndex(params) {
      return indexer.readPublished(params.file_id).then(function (published) {
        if (!published) {
          return { file_id: params.file_id, state: 'missing', generation: null };
        }
        return {
          file_id: params.file_id,
          state: 'published',
          generation: published.generation,
          watermark: published.watermark,
          signatures: published.signatures,
          published_at: published.published_at
        };
      });
    }

    function readBlocks(fileId, generation, layer) {
      return indexer.getBlocks(fileId, generation, layer, 0, null).then(function (blocks) {
        counters.blocks_read += blocks.length;
        return blocks;
      });
    }

    function recordsOf(fileId, generation) {
      var key = 'records|' + fileId + '|' + generation;
      var cached = cache.get(key);
      if (cached) return Promise.resolve(cached);
      return readBlocks(fileId, generation, 'record_blocks').then(function (blocks) {
        var records = [];
        blocks.forEach(function (block) {
          (block.records || []).forEach(function (record) { records.push(record); });
        });
        cache.put(key, records, byteLengthOf(records));
        return records;
      });
    }

    function enforcePage(limit) {
      var size = limit == null ? DEFAULT_PAGE : limit;
      if (!Number.isInteger(size) || size < 1) {
        throw errorOf('invalid_page', '每页条数必须是正整数');
      }
      if (size > MAX_PAGE_RECORDS) {
        throw errorOf('page_too_large', '单页最多 ' + MAX_PAGE_RECORDS + ' 条', {
          requested: size, maximum: MAX_PAGE_RECORDS
        });
      }
      return size;
    }

    function getPage(params) {
      var limit = enforcePage(params.limit);
      var offset = params.offset == null ? 0 : params.offset;
      if (!Number.isInteger(offset) || offset < 0) throw errorOf('invalid_cursor', '游标不合法');
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          if (offset > records.length) {
            throw errorOf('cursor_expired', '游标超出当前范围', { offset: offset, total: records.length });
          }
          var slice = records.slice(offset, offset + limit);
          var items = slice.map(function (record) {
            return { ordinal: record.ordinal, level: record.level, ts: record.ts, lines: record.lines,
                     message: record.message, has_trace: record.has_trace === true };
          });
          var bytes = byteLengthOf(items);
          if (bytes > MAX_PAGE_BYTES) {
            throw errorOf('page_too_large', '单页数据超过 4 MiB，请减小每页条数', { bytes: bytes });
          }
          counters.pages += 1;
          var next = offset + items.length < records.length ? offset + items.length : null;
          return {
            file_id: params.file_id,
            generation: published.generation,
            items: items,
            next_cursor: next,
            count: { relation: 'exact', matched: records.length },
            coverage: { complete: true, scanned_bytes: published.watermark ? published.watermark.raw_bytes : null,
                        total_bytes: published.watermark ? published.watermark.raw_bytes : null },
            page_bytes: bytes
          };
        });
      });
    }

    function getRecord(params) {
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          var record = records[params.ordinal];
          if (!record) throw errorOf('missing_record', '记录不存在：' + params.ordinal);
          return {
            record_id: record.record_id || (params.file_id + ':' + params.ordinal),
            ordinal: record.ordinal,
            file_id: params.file_id,
            generation: published.generation,
            version: record.version || 1,
            header: { level: record.level, ts: record.ts, lines: record.lines, message: record.message,
                      has_trace: record.has_trace === true },
            body: record.body || [],
            raw: record.raw || null
          };
        });
      });
    }

    /** Batch fetch by ordinal, bounded like any page (search results are sparse). */
    function getRecords(params) {
      var ordinals = params.ordinals || [];
      if (ordinals.length > MAX_PAGE_RECORDS) {
        throw errorOf('page_too_large', '单次最多取 ' + MAX_PAGE_RECORDS + ' 条记录', { requested: ordinals.length });
      }
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          var items = [];
          for (var i = 0; i < ordinals.length; i++) {
            var record = records[ordinals[i]];
            if (!record) continue;
            items.push({
              ordinal: record.ordinal, record_id: record.record_id, version: record.version || 1,
              level: record.level, ts: record.ts, ts_ns: record.ts_ns || null,
              lines: record.lines, message: record.message,
              body: record.body || [], has_trace: record.has_trace === true
            });
          }
          return { file_id: params.file_id, generation: published.generation, items: items };
        });
      });
    }

    function readRaw(params) {
      var length = params.length;
      if (!Number.isInteger(length) || length < 0) throw errorOf('invalid_range', '长度不合法');
      if (length > MAX_PAGE_BYTES) {
        throw errorOf('page_too_large', '单次原文读取不得超过 4 MiB', { requested: length });
      }
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        counters.raw_reads += 1;
        counters.raw_bytes += length;
        return store.getRawRange(params.file_id, published.generation, params.offset, length,
          params.chunk_size || 1048576).then(function (bytes) {
            return { bytes: bytes, offset: params.offset, length: bytes.length, file_id: params.file_id };
          });
        });
    }

    /** Nodes are ERROR/WARN records plus anything the rule engine marked as a node. */
    function timelineNodes(records) {
      var nodes = [];
      for (var i = 0; i < records.length; i++) {
        var record = records[i];
        var marked = record.node || record.event || null;
        if (marked && marked.label) {
          nodes.push({
            ordinal: record.ordinal, ts_ns: record.ts_ns == null ? null : record.ts_ns,
            ts_key: record.ts_key == null ? timeKey(record.ts_ns) : record.ts_key,
            level: record.level, kind: marked.type || 'rule', label: marked.label || record.message,
            source: marked.ruleId ? 'rule' : 'event', rule: marked.ruleId || null,
            manual: marked.manual === true
          });
        } else if (record.level === 'ERROR' || record.level === 'FATAL') {
          nodes.push({
            ordinal: record.ordinal, ts_ns: record.ts_ns == null ? null : record.ts_ns,
            ts_key: record.ts_key == null ? timeKey(record.ts_ns) : record.ts_key,
            level: record.level, kind: 'error', label: record.message,
            source: 'level', rule: null, manual: false
          });
        }
      }
      return nodes;
    }

    function getTimeline(params) {
      var pageSize = params.page_size == null ? 50 : params.page_size;
      if ([20, 50, 100, 150].indexOf(pageSize) === -1) {
        throw errorOf('invalid_page', '时间线每页只支持 20/50/100/150');
      }
      var page = params.page == null ? 0 : params.page;
      if (!Number.isInteger(page) || page < 0) throw errorOf('invalid_cursor', '页码不合法');
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          var all = timelineNodes(records);
          var start = page * pageSize;
          return {
            file_id: params.file_id,
            generation: published.generation,
            nodes: all.slice(start, start + pageSize),
            page: page,
            page_size: pageSize,
            total: all.length,
            // The count is only exact once the file is fully framed.
            count: { relation: 'exact', matched: all.length },
            coverage: { complete: true },
            has_prev: page > 0,
            has_next: start + pageSize < all.length
          };
        });
      });
    }

    function seek(params) {
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          var found = -1;
          if (params.ordinal != null) found = params.ordinal;
          else if (params.ts_ns != null) {
            var target = timeKey(params.ts_ns);
            for (var i = 0; i < records.length; i++) {
              var key = records[i].ts_key != null ? records[i].ts_key : timeKey(records[i].ts_ns);
              if (key != null && key >= target) { found = i; break; }
            }
          }
          if (found < 0 || found >= records.length) {
            throw errorOf('missing_record', '无法定位到目标记录');
          }
          return { ordinal: found, offset: found, generation: published.generation };
        });
      });
    }

    /**
     * Synchronous exact scan over the candidate blocks. Search results are
     * ordinals only; the reader asks for headers by page, so a zero-hit search
     * never builds 130k objects.
     */
    /**
     * Progressive search. The job scans blocks in slices inside the Worker and
     * pauses on budget; `poll`/`cancelSearch`/`resume` address the same snapshot.
     */
    function startQuery(params) {
      if (!params.file_id) throw errorOf('bad_request', '查询必须指名文件');
      var parsed = deps.normalizeQuery(params.spec || {});
      if (!parsed.ok) throw errorOf(parsed.error.code, parsed.error.message);
      return requireGeneration(params.file_id, params.generation).then(function (published) {
        // No full preload: the job asks for the block range it is about to scan,
        // so a 1 GiB generation is never resident just to start a search.
        var known = published.blocks;
        if (known != null) return { parsed: parsed, published: published, blocks: known };
        return indexer.getBlocks(params.file_id, published.generation, 'record_blocks', 0, null)
          .then(function (blocks) {
            return { parsed: parsed, published: published, blocks: blocks.length };
          });
      }).then(function (context) {
        var totalRecords = (context.published.watermark && context.published.watermark.indexed_records) || 0;
        var signature = params.signature || context.parsed.signature;
        return queryService.start({
          fileId: params.file_id,
          generation: context.published.generation,
          indexVersion: context.published.generation,
          signature: signature,
          queryVersion: params.query_version || 1,
          blocks: context.blocks,
          records: totalRecords,
          pageLimit: params.limit || 200,
          cacheKey: [datasetId, params.file_id, context.published.generation, signature,
                     (context.parsed.ast.levels || []).join(','),
                     JSON.stringify(context.parsed.ast.time_range || null)].join('|'),
          fetch: function (from, count) {
            return indexer.getBlocks(params.file_id, context.published.generation,
              'record_blocks', from, from + count - 1).then(function (blocks) {
                return blocks.map(function (block) {
                  return { block_no: block.block_no, records: block.records || [] };
                });
              });
          },
          match: function (record) { return deps.matches(record, context.parsed.ast).hit; }
        });
      });
    }

    function search(params) {
      return queryService ? startQuery(params) : legacySearch(params);
    }

    /**
     * Synchronous fallback with candidate pruning. Used when the progressive
     * service is not wired in (tests, and any caller that wants one answer).
     */
    function legacySearch(params) {
      var spec = params.spec || {};
      var limit = enforcePage(params.limit);
      var candidates = null;
      if (deps.candidates) candidates = deps.candidates;
      var normalize = deps.normalizeQuery;
      if (!normalize) throw errorOf('no_query', '引擎缺少查询规范化器');
      var parsed = normalize(spec);
      if (!parsed.ok) throw errorOf(parsed.error.code, parsed.error.message);

      return requireGeneration(params.file_id, params.generation).then(function (published) {
        return recordsOf(params.file_id, published.generation).then(function (records) {
          counters.searches += 1;
          var scanned = records;
          var coverage = { scanned_blocks: null, pruned_blocks: 0, total_blocks: null };
          if (candidates && deps.buildBlockIndex) {
            // The caller supplies a prebuilt index; rebuilding per query would
            // defeat the point of an index.
            var index = deps.buildBlockIndex(params.file_id, published.generation);
            if (index && index.blocks && index.blocks.length) {
              var plan = candidates.plan(parsed.ast, index);
              coverage = plan.coverage;
              scanned = [];
              plan.candidates.forEach(function (blockNo) {
                var block = index.blocks[blockNo];
                for (var i = block.first_ordinal; i <= block.last_ordinal; i++) scanned.push(records[i]);
              });
            }
          }
          var hits = [];
          var matched = 0;
          for (var i = 0; i < scanned.length; i++) {
            if (deps.matches(scanned[i], parsed.ast).hit) {
              matched += 1;
              if (hits.length < limit) hits.push(scanned[i].ordinal);
            }
          }
          var queryId = 'q' + now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
          return {
            query_id: queryId,
            state: 'done',
            signature: parsed.signature,
            file_id: params.file_id,
            generation: published.generation,
            count: { relation: 'exact', matched: matched },
            coverage: {
              complete: true,
              scanned_blocks: coverage.scanned_blocks,
              pruned_blocks: coverage.pruned_blocks,
              total_blocks: coverage.total_blocks
            },
            page: { offset: 0, items: hits, next_cursor: matched > hits.length ? hits.length : null },
            order_final: true
          };
        });
      });
    }

    /**
     * Stage and publish one generation's derived layers. The importer decides
     * what a record is; the engine only guarantees the publish is atomic.
     */
    function stageIndex(params) {
      var records = params.records || [];
      var blocks = [];
      for (var i = 0; i < records.length; i += 512) {
        blocks.push({ block_no: blocks.length, records: records.slice(i, i + 512) });
      }
      var plan = {
        file_id: params.file_id,
        generation: params.generation,
        size: params.size,
        signatures: params.signatures || { structure: 'unknown', extract: 'unknown', search: 'unknown' },
        watermark: {
          raw_bytes: params.raw_bytes || 0,
          framed_lines: params.framed_lines == null ? records.length : params.framed_lines,
          indexed_records: records.length
        },
        record_blocks: blocks,
        rule_blocks: [],
        search_blocks: [],
        blocks: blocks.length
      };
      return indexer.stage(plan).then(function () { return indexer.publish(plan); });
    }

    function stageBlock(params) {
      return indexer.stageBlock({
        file_id: params.file_id, generation: params.generation,
        block_no: params.block_no, records: params.records,
        signatures: params.signatures, layer: 'record_blocks'
      });
    }

    function publishIndex(params) {
      return indexer.publishIndex({
        file_id: params.file_id,
        generation: params.generation,
        size: params.size,
        signatures: params.signatures || { structure: 'unknown', extract: 'unknown', search: 'v1' },
        watermark: params.watermark || { raw_bytes: 0, framed_lines: 0, indexed_records: 0 },
        blocks: params.blocks
      });
    }

    /* Import-side writes. The pipeline owns chunking; the engine owns durability. */
    function putFile(params) {
      return store.putFileMeta({
        file_id: params.file_id, name: params.name, size: params.size,
        chunk_size: params.chunk_size || 2097152, generation: params.generation || 1,
        chunks: params.chunks || 0, digest: params.digest || null,
        saved_at: now()
      }).then(function () { return { file_id: params.file_id, saved: true }; });
    }

    function appendChunk(params) {
      return store.putRawChunk(params.file_id, params.generation || 1, params.chunk_no,
        params.bytes, { size: params.bytes.length }).then(function () {
          return { file_id: params.file_id, chunk_no: params.chunk_no, written: params.bytes.length };
        });
    }

    function finalizeFile(params) {
      return store.putFileMeta({
        file_id: params.file_id, name: params.name, size: params.size,
        chunk_size: params.chunk_size || 2097152, generation: params.generation || 1,
        chunks: params.chunks, digest: params.digest || null, saved_at: now()
      }).then(function () { return { file_id: params.file_id, finalized: true }; });
    }

    function handle(method, params) {
      switch (method) {
        case 'open': datasetId = params.dataset_id || datasetId; return Promise.resolve({ dataset_id: datasetId, dataset_token: datasetId + ':' + now() });
        case 'close': cache.clear(); return Promise.resolve({ closed: true });
        case 'listFiles': return listFiles();
        case 'getMetadata': return getMetadata(params);
        case 'ensureIndex': return ensureIndex(params);
        case 'headers':
        case 'getPage': return getPage(params);
        case 'getRecord': return getRecord(params);
        case 'detail': return getRecord(params);
        case 'readRaw': return readRaw(params);
        case 'getRecords': return getRecords(params);
        case 'getTimeline': return getTimeline(params);
        case 'seek': return seek(params);
        case 'search': return search(params);
        case 'poll': return Promise.resolve(queryService ? queryService.state(params.query_id)
          : Promise.reject(errorOf('not_implemented', '查询作业未启用')));
        case 'cancelSearch':
          if (!queryService) return Promise.reject(errorOf('not_implemented', '查询作业未启用'));
          try { return Promise.resolve(queryService.cancel(params.query_id)); }
          catch (error) { return Promise.reject(error); }
        case 'resume':
          if (!queryService) return Promise.reject(errorOf('not_implemented', '查询作业未启用'));
          try { return Promise.resolve(queryService.resume(params.query_id, params.resume_token)); }
          catch (error) { return Promise.reject(error); }
        case 'stageIndex': return stageIndex(params);
        case 'stageBlock': return stageBlock(params);
        case 'publishIndex': return publishIndex(params);
        case 'putFile': return putFile(params);
        case 'appendChunk': return appendChunk(params);
        case 'finalizeFile': return finalizeFile(params);
        default: return Promise.reject(errorOf('unknown_method', '未实现的方法：' + method));
      }
    }

    return {
      handle: handle,
      stats: function () {
        var copy = {};
        for (var key in counters) copy[key] = counters[key];
        copy.cache = cache.stats();
        return copy;
      },
      clearCache: cache.clear
    };
  }

  return {
    MAX_PAGE_RECORDS: MAX_PAGE_RECORDS,
    MAX_PAGE_BYTES: MAX_PAGE_BYTES,
    DEFAULT_PAGE: DEFAULT_PAGE,
    errorOf: errorOf,
    byteLengthOf: byteLengthOf,
    createPageCache: createPageCache,
    createEngine: createEngine
  };
});
