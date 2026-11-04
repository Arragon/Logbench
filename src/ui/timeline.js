/*
 * timeline.js — paged nodes, coverage and stable revision updates (R6-029).
 *
 * R5 built the timeline by scanning every record. That is fine at 5k rows and
 * impossible at 500k, so nodes come from the provider by page (20/50/100/150).
 *
 * The two properties that make it usable:
 *
 *   the page boundary is real   `has_prev`/`has_next` come from the server's
 *                               answer, not from "we got fewer than pageSize".
 *                               A last page that happens to be full must still
 *                               disable "next".
 *   a revision is applied in place  a record update replaces one node and keeps
 *                               the current page and scroll position, instead of
 *                               resetting the whole strip.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBTimeline = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var PAGE_SIZES = [20, 50, 100, 150];
  var DEFAULT_PAGE_SIZE = 50;

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function createTimeline(options) {
    options = options || {};
    var provider = options.provider;
    if (!provider) throw errorOf('no_provider', '时间线需要 Provider');
    var state = {
      file_id: null,
      generation: null,
      page: 0,
      page_size: DEFAULT_PAGE_SIZE,
      nodes: [],
      total: 0,
      has_prev: false,
      has_next: false,
      count: { relation: 'unknown', matched: null },
      coverage: { complete: false },
      loading: false,
      revision: 0
    };

    function reset(params) {
      state.file_id = params.file_id;
      state.generation = params.generation;
      state.page = 0;
      state.nodes = [];
      state.revision += 1;
    }

    function pageSize(size) {
      if (PAGE_SIZES.indexOf(size) === -1) {
        throw errorOf('invalid_page_size', '时间线每页只支持 ' + PAGE_SIZES.join('/'));
      }
      state.page_size = size;
      return state.page_size;
    }

    function load(page) {
      if (state.file_id == null) return Promise.reject(errorOf('no_file', '尚未选择文件'));
      var target = page == null ? state.page : page;
      if (target < 0) return Promise.reject(errorOf('invalid_cursor', '页码不能为负'));
      state.loading = true;
      return provider.getTimeline({
        file_id: state.file_id, generation: state.generation,
        page: target, page_size: state.page_size
      }).then(function (result) {
        state.loading = false;
        state.page = result.page;
        state.nodes = result.nodes || [];
        state.total = result.total == null ? state.nodes.length : result.total;
        // Trust the provider's flags: a full last page must not enable "next".
        state.has_prev = result.has_prev === true;
        state.has_next = result.has_next === true;
        state.count = result.count || { relation: 'exact', matched: state.total };
        state.coverage = result.coverage || { complete: true };
        state.revision += 1;
        return snapshot();
      }, function (error) {
        state.loading = false;
        throw error;
      });
    }

    function next() { if (!state.has_next) return Promise.resolve(snapshot()); return load(state.page + 1); }
    function prev() { if (!state.has_prev) return Promise.resolve(snapshot()); return load(state.page - 1); }

    /**
     * Replace one node after its record was revised. If the node is not on the
     * current page, nothing happens — the page is not reloaded for it.
     */
    function applyRevision(node) {
      if (!node || node.ordinal == null) return { changed: false, page: state.page };
      for (var i = 0; i < state.nodes.length; i++) {
        if (state.nodes[i].ordinal === node.ordinal) {
          state.nodes[i] = Object.assign({}, state.nodes[i], node);
          state.revision += 1;
          return { changed: true, page: state.page, index: i };
        }
      }
      return { changed: false, page: state.page };
    }

    /** Stable position: the ordinal of the first visible node, for re-focus. */
    function anchorOf(index) {
      var node = state.nodes[index];
      return node ? { ordinal: node.ordinal, page: state.page, index: index } : null;
    }

    function snapshot() {
      return {
        file_id: state.file_id, generation: state.generation,
        page: state.page, page_size: state.page_size,
        nodes: state.nodes.slice(), total: state.total,
        has_prev: state.has_prev, has_next: state.has_next,
        count: state.count, coverage: state.coverage,
        revision: state.revision, loading: state.loading
      };
    }

    return {
      reset: reset,
      pageSize: pageSize,
      load: load,
      next: next,
      prev: prev,
      applyRevision: applyRevision,
      anchorOf: anchorOf,
      snapshot: snapshot,
      PAGE_SIZES: PAGE_SIZES
    };
  }

  return { PAGE_SIZES: PAGE_SIZES, DEFAULT_PAGE_SIZE: DEFAULT_PAGE_SIZE, createTimeline: createTimeline };
});
