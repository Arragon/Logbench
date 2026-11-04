/*
 * workspace.js — incremental personal state, kept apart from task data (R6-030).
 *
 * R5 saved the whole workspace object on every scroll and progress tick, and it
 * contained the query hits. At 130k rows that is a JSON serialisation of the
 * entire result set, several times a second. The cost grew with the file size
 * while the user changed one preference.
 *
 * Here the state is split by write frequency:
 *
 *   preferences   theme, density, columns, sort, wrap, page size — small object,
 *                 written when the user changes it
 *   view          open scope, selected record, anchor — written on a debounce,
 *                 never on scroll frames
 *   collections   bookmarks and collapsed groups — append/remove one key at a
 *                 time, so a 10k-bookmark set costs one small record per change
 *
 * Two things are explicitly *not* stored: query hit arrays, and anything derived
 * from them. A view records the query signature and the projection version, so
 * the result can be recomputed instead of restored.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBWorkspace = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var KEYS = {
    preferences: 'r6/workspace/preferences',
    view: 'r6/workspace/view',
    bookmarks: 'r6/workspace/bookmarks',
    collapsed: 'r6/workspace/collapsed',
    meta: 'r6/workspace/meta'
  };

  var PREFERENCE_FIELDS = [
    'theme', 'density', 'contrast', 'font_size', 'columns', 'sort', 'wrap',
    'page_size', 'projection_version', 'level_filter', 'time_range',
    'node_page_size', 'compare_layout'
  ];

  var VIEW_FIELDS = ['scope', 'selected', 'anchor', 'query_signature', 'file_id', 'generation'];

  /** Anything that looks like a result set is refused, not stripped silently. */
  var FORBIDDEN = ['queryHits', 'hits', 'records', 'items', 'matches', 'results'];

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function pick(source, fields) {
    var out = {};
    fields.forEach(function (field) {
      if (source[field] !== undefined) out[field] = source[field];
    });
    return out;
  }

  function assertNoResults(patch) {
    function check(value, path) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return;
      FORBIDDEN.forEach(function (key) {
        if (Object.prototype.hasOwnProperty.call(value, key)) {
          throw errorOf('result_set_not_storable',
            '工作区不保存查询结果：' + path + key);
        }
      });
      Object.keys(value).forEach(function (key) {
        if (value[key] && typeof value[key] === 'object' && !Array.isArray(value[key])) {
          check(value[key], path + key + '.');
        }
      });
    }
    check(patch || {}, '');
  }

  /**
   * @param {object} options
   *   store   { get(key), put(key, value), delete(key) }  (async)
   *   now     injectable clock
   */
  function createWorkspace(options) {
    options = options || {};
    var store = options.store;
    if (!store) throw errorOf('no_store', '工作区需要存储适配器');
    var now = options.now || function () { return Date.now(); };
    var revision = 0;
    var stats = { writes: 0, bytes_written: 0, conflicts: 0, rejected: 0 };

    function read(key) {
      return Promise.resolve(store.get(key)).then(function (value) { return value == null ? null : value; });
    }

    function write(key, value) {
      var size = JSON.stringify(value).length;
      stats.writes += 1;
      stats.bytes_written += size;
      return Promise.resolve(store.put(key, value)).then(function () { return size; });
    }

    function load() {
      return Promise.all([read(KEYS.preferences), read(KEYS.view), read(KEYS.meta), read(KEYS.bookmarks)])
        .then(function (values) {
          revision = (values[2] && values[2].revision) || 0;
          return {
            preferences: values[0] || {},
            view: values[1] || {},
            revision: revision,
            bookmarks: values[3] ? Object.keys(values[3]).length : 0
          };
        });
    }

    /**
     * Save only the changed fields. `baseRevision` is the revision the caller
     * read; a mismatch is a conflict, not a merge, because two tabs silently
     * merging task state is how one of them loses its edits.
     */
    function savePatch(patch, saveOptions) {
      saveOptions = saveOptions || {};
      try {
        assertNoResults(patch);
      } catch (error) {
        stats.rejected += 1;
        return Promise.reject(error);
      }
      var known = saveOptions.baseRevision;
      return read(KEYS.meta).then(function (meta) {
        var current = (meta && meta.revision) || 0;
        if (known != null && known !== current) {
          stats.conflicts += 1;
          throw errorOf('revision_conflict', '工作区已被其他页面修改', { base: known, current: current });
        }
        var operations = [];
        if (patch.preferences) {
          operations.push(read(KEYS.preferences).then(function (existing) {
            var merged = Object.assign({}, existing || {}, pick(patch.preferences, PREFERENCE_FIELDS));
            return write(KEYS.preferences, merged);
          }));
        }
        if (patch.view) {
          operations.push(read(KEYS.view).then(function (existing) {
            var merged = Object.assign({}, existing || {}, pick(patch.view, VIEW_FIELDS));
            return write(KEYS.view, merged);
          }));
        }
        if (patch.bookmark) {
          operations.push(write(KEYS.bookmarks + '/' + patch.bookmark.key, patch.bookmark.value));
        }
        if (patch.unbookmark) {
          operations.push(Promise.resolve(store.delete(KEYS.bookmarks + '/' + patch.unbookmark)));
        }
        return Promise.all(operations).then(function (sizes) {
          revision = current + 1;
          return write(KEYS.meta, { revision: revision, saved_at: now() })
            .then(function () {
              return { revision: revision, bytes: sizes.reduce(function (a, b) { return a + (b || 0); }, 0) };
            });
        });
      });
    }

    /**
     * View state is saved on a debounce and only when it changed. Scrolling calls
     * this with the same anchor many times; the second call is a no-op.
     */
    var lastView = null;
    function saveView(view) {
      var picked = pick(view, VIEW_FIELDS);
      var serialised = JSON.stringify(picked);
      if (serialised === lastView) return Promise.resolve({ skipped: true });
      lastView = serialised;
      return savePatch({ view: picked });
    }

    function bookmarks() {
      return Promise.resolve(store.list ? store.list(KEYS.bookmarks + '/') : [])
        .then(function (keys) { return keys || []; });
    }

    return {
      load: load,
      savePatch: savePatch,
      saveView: saveView,
      bookmarks: bookmarks,
      keys: KEYS,
      fields: { preferences: PREFERENCE_FIELDS, view: VIEW_FIELDS },
      revision: function () { return revision; },
      stats: function () { var copy = {}; for (var key in stats) copy[key] = stats[key]; return copy; }
    };
  }

  return {
    KEYS: KEYS,
    PREFERENCE_FIELDS: PREFERENCE_FIELDS,
    VIEW_FIELDS: VIEW_FIELDS,
    createWorkspace: createWorkspace
  };
});
