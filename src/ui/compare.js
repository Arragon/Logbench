/*
 * compare.js — 2–4 file time comparison aligned on a shared timestamp anchor (R6-080).
 *
 * R5 compared files by loading every timestamp from every file and taking the union
 * on the main thread. That is why it degraded on large files, and why it could not
 * tell "this file has no record at 10:00:03" from "the union skipped it".
 *
 * Here each pane asks its provider for one **bounded window** starting at a shared
 * anchor. The merge is a slot matrix: every distinct timestamp in the window is one
 * slot, every pane contributes zero, one or several records to that slot, and a pane
 * with no record gets an empty slot rather than the previous value copied down.
 *
 * Rules made explicit:
 *   - the sync anchor is a timestamp from the pane being scrolled, never a scroll
 *     percentage and never a row number: row 100 of one file and row 100 of another
 *     are different times.
 *   - a file with no timestamps is not placed on the axis. It is listed with a reason
 *     instead of being silently mixed in at position zero.
 *   - ties and duplicates are preserved: two records with the same timestamp in one
 *     pane occupy two lines of the same slot.
 *   - layouts: 2–3 files are columns by default, 4 files are a 2×2 grid; the user can
 *     force columns or rows, and the choice is part of the saved view.
 *
 * UMD-lite: window.LBCompare in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(
    require('../shared/time.js'));
  else root.LBCompare = factory(root.LBTime);
})(typeof self !== 'undefined' ? self : this, function (time) {
  'use strict';

  var MAX_FILES = 4;
  var MIN_FILES = 2;
  var DEFAULT_SPAN_NS = 10 * 60 * 1000 * 1000000;   // ten minutes
  var DEFAULT_LIMIT = 400;
  var ROW_H = 28;

  function errorOf(code, message, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  /* --------------------------------------------------------------- pure model */

  function defaultLayout(count, choice) {
    if (choice === 'columns' || choice === 'row' || choice === 'grid') return choice;
    return count >= 4 ? 'grid' : 'columns';
  }

  function validateFiles(files) {
    if (!Array.isArray(files)) throw errorOf('bad_files', '对比需要文件列表');
    if (files.length < MIN_FILES) {
      throw errorOf('too_few_files', '时间对比至少需要 2 个文件', { count: files.length });
    }
    if (files.length > MAX_FILES) {
      throw errorOf('too_many_files', '时间对比最多 4 个文件', { count: files.length });
    }
    var seen = {};
    files.forEach(function (file) {
      if (!file || !file.file_id) throw errorOf('bad_files', '文件缺少 file_id');
      if (seen[file.file_id]) throw errorOf('duplicate_file', '同一个文件不能对比两次',
        { file_id: file.file_id });
      seen[file.file_id] = true;
    });
    return files;
  }

  function keyOf(row) {
    if (row.ts_key != null) return row.ts_key;
    if (row.ts_ns != null) return time.keyOf(row.ts_ns);
    return time.keyOf(row.ts);
  }

  /**
   * Build the slot matrix for one window. Pure: given the same rows it always
   * produces the same matrix, which is what makes the alignment testable.
   *
   * @param {Array} panes [{file_id, label, source, rows: [...]}]
   */
  function buildMatrix(panes, options) {
    options = options || {};
    var maxSlots = options.max_slots || 500;
    var columns = panes.map(function (pane) {
      return { file_id: pane.file_id, label: pane.label || pane.file_id,
        source: pane.source || 'local', state: pane.state || 'ready',
        relation: pane.relation || 'unknown', reason: pane.reason || null };
    });
    var untimed = panes.map(function () { return []; });
    var slots = {};
    var order = [];
    panes.forEach(function (pane, index) {
      (pane.rows || []).forEach(function (row) {
        var key = keyOf(row);
        if (key == null) {
          untimed[index].push({ row: row, pane: index });
          return;
        }
        if (!slots[key]) {
          slots[key] = { ts_key: key, ts: row.ts || null, cells: panes.map(function () { return []; }),
            lines: 0 };
          order.push(key);
        }
        if (!slots[key].ts && row.ts) slots[key].ts = row.ts;
        slots[key].cells[index].push(row);
      });
    });
    order.sort(function (a, b) { return a - b; });
    var truncated = order.length > maxSlots;
    var result = order.slice(0, maxSlots).map(function (key) {
      var slot = slots[key];
      var lines = 0;
      slot.cells.forEach(function (cell) { if (cell.length > lines) lines = cell.length; });
      slot.lines = lines;
      // Sort duplicates by ordinal so the same input always renders identically.
      slot.cells.forEach(function (cell) {
        cell.sort(function (a, b) { return a.ordinal - b.ordinal; });
      });
      return slot;
    });
    return {
      columns: columns,
      slots: result,
      untimed: untimed,
      untimed_count: untimed.reduce(function (sum, list) { return sum + list.length; }, 0),
      truncated: truncated,
      first_ts_key: result.length ? result[0].ts_key : null,
      last_ts_key: result.length ? result[result.length - 1].ts_key : null
    };
  }

  /** The anchor a pane's top visible row carries, or null when it is empty. */
  function anchorOf(matrix, fromSlot) {
    var start = Math.max(0, fromSlot || 0);
    for (var index = start; index < matrix.slots.length; index++) {
      if (matrix.slots[index].ts_key != null) return matrix.slots[index].ts_key;
    }
    return null;
  }

  /** The window each pane asks for: same start, same span, same limit. */
  function windowsFor(files, anchorKey, spanNs, limit) {
    var span = spanNs == null ? DEFAULT_SPAN_NS : spanNs;
    var fromNs = null;
    if (anchorKey != null) {
      fromNs = (BigInt(Math.trunc(Number(anchorKey))) * 1000000n).toString();
    }
    var toNs = fromNs == null ? null : (BigInt(fromNs) + BigInt(span)).toString();
    return files.map(function (file) {
      return { file_id: file.file_id, source: file.source,
        from_ns: fromNs, to_ns: toNs, limit: limit || DEFAULT_LIMIT };
    });
  }

  function layoutStyle(layout, count) {
    if (layout === 'grid') return { columns: 2, rows: Math.ceil(count / 2) };
    if (layout === 'row') return { columns: count, rows: 1 };
    return { columns: count, rows: 1 };
  }

  /* ------------------------------------------------------------------ widget */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /**
   * @param {object} options
   *   files       [{file_id, label, source, generation}]
   *   fetchWindow ({file, window}) -> Promise<{rows, coverage, relation, error}>
   *   onStatus(text)
   *   onChange(view)  called when layout/sync/anchor changes (for saving)
   */
  function createCompare(options) {
    options = options || {};
    var files = validateFiles(options.files || []);
    var host = null;
    var parts = { panes: [] };
    var state = {
      files: files,
      layout: defaultLayout(files.length, options.layout),
      sync: options.sync !== false,
      /* Milliseconds: the axis unit `buildMatrix` and `windowsFor` agree on. Stored as
       * ns it would be misread by the window builder (and rendered as 1970). */
      anchor_key: null,
      span_ns: DEFAULT_SPAN_NS,
      matrix: null,
      loading: false,
      error: null,
      version: 0,
      excluded: []
    };

    function view() {
      return {
        files: state.files.map(function (file) { return { file_id: file.file_id,
          source: file.source, label: file.label }; }),
        layout: state.layout, sync: state.sync, anchor_key: state.anchor_key,
        span_ns: state.span_ns
      };
    }

    function emit() {
      if (options.onChange) options.onChange(view());
    }

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.className = 'cmp-view';

      parts.bar = el('div', 'cmp-bar');
      parts.title = el('span', 'cmp-title', state.files.length + ' 个文件按时间对齐');
      parts.syncLabel = el('label', 'cmp-toggle');
      parts.syncBox = el('input');
      parts.syncBox.type = 'checkbox';
      parts.syncBox.checked = state.sync;
      parts.syncBox.addEventListener('change', function () {
        state.sync = parts.syncBox.checked;
        if (state.sync && parts.panes.length) {
          // Restoring sync must anchor somewhere explicit: the left-most pane's current
          // top slot, applied to the others as the same slot index.
          var index = Math.round(parts.panes[0].body.scrollTop / ROW_H);
          syncOthers(index, parts.panes[0].body);
        }
        emit();
      });
      parts.syncLabel.append(parts.syncBox, el('span', null, '同步滚动按时间'));
      parts.layoutSelect = el('select', 'cmp-select');
      [['columns', '横向排列'], ['grid', '2×2 网格'], ['row', '单行铺开']].forEach(function (pair) {
        var option = el('option', null, pair[1]);
        option.value = pair[0];
        if (pair[0] === state.layout) option.selected = true;
        parts.layoutSelect.appendChild(option);
      });
      parts.layoutSelect.setAttribute('aria-label', '布局');
      parts.layoutSelect.addEventListener('change', function () {
        state.layout = parts.layoutSelect.value;
        renderGrid();
        emit();
      });
      parts.anchorText = el('span', 'cmp-anchor');
      parts.reload = el('button', 'r6-icon', '重新对齐');
      parts.reload.type = 'button';
      parts.reload.addEventListener('click', function () {
        var key = currentAnchor();
        load(key == null ? state.anchor_key : key);
      });
      parts.bar.append(parts.title, parts.syncLabel, parts.layoutSelect, parts.anchorText,
        el('span', 'spacer'), parts.reload);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'status');
      parts.note.hidden = true;

      parts.grid = el('div', 'cmp-grid');
      host.append(parts.bar, parts.note, parts.grid);
      renderGrid();
      return { load: load, state: state, view: view };
    }

    function renderGrid() {
      var style = layoutStyle(state.layout, state.files.length);
      parts.grid.style.gridTemplateColumns = 'repeat(' + style.columns + ', minmax(0, 1fr))';
      parts.grid.innerHTML = '';
      parts.panes = state.files.map(function (file, index) {
        var pane = el('section', 'cmp-pane');
        pane.setAttribute('aria-label', '文件 ' + (file.label || file.file_id));
        var head = el('div', 'cmp-pane-head');
        var name = el('span', 'cmp-pane-name', file.label || file.file_id);
        name.title = file.file_id;
        var badge = el('span', 'gt-src', file.source === 'server' ? '服务器' : '本机');
        var meta = el('span', 'cmp-pane-meta', '—');
        head.append(name, badge, el('span', 'spacer'), meta);
        var body = el('div', 'cmp-body');
        body.tabIndex = 0;
        body.setAttribute('role', 'region');
        body.setAttribute('aria-label', (file.label || file.file_id) + ' 的记录');
        var rowsHost = el('div', 'cmp-rows');
        body.appendChild(rowsHost);
        body.addEventListener('scroll', function () {
          if (body.dataset.applying === '1') return;
          var index = Math.round(body.scrollTop / ROW_H);
          updateAnchor(index);
          if (state.sync) syncOthers(index, body);
          maybeLoadMore(index, body);
        }, { passive: true });
        body.addEventListener('keydown', function (event) {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            var delta = event.key === 'ArrowDown' ? 1 : -1;
            body.scrollTop = Math.max(0, body.scrollTop + delta * 28);
            event.preventDefault();
          }
          if (event.key === 'PageDown' || event.key === 'PageUp') {
            var page = event.key === 'PageDown' ? 1 : -1;
            body.scrollTop = Math.max(0, body.scrollTop + page * body.clientHeight);
            event.preventDefault();
          }
        });
        pane.append(head, body);
        parts.grid.appendChild(pane);
        return { file: file, node: pane, body: body, rowsHost: rowsHost, meta: meta,
          rows: [], slotIndex: 0 };
      });
    }

    function anchorAt(index) {
      var matrix = state.matrix;
      if (!matrix) return null;
      var slot = matrix.slots[index];
      return slot ? slot.ts_key : null;
    }

    /**
     * Every pane renders the same slot list, so "same timestamp" is the same slot index
     * — and therefore the same pixel offset. Syncing is a scroll offset copy, not a
     * re-query: re-anchoring on every scroll event made the panes jump and made
     * independent reading impossible.
     */
    function syncOthers(index, source) {
      var target = index * ROW_H;
      parts.panes.forEach(function (pane) {
        if (pane.body === source) return;
        if (Math.abs(pane.body.scrollTop - target) < 1) return;
        pane.body.dataset.applying = '1';
        pane.body.scrollTop = target;
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(function () { pane.body.dataset.applying = '0'; });
        } else {
          setTimeout(function () { pane.body.dataset.applying = '0'; }, 0);
        }
      });
    }

    function updateAnchor(index) {
      var key = anchorAt(index);
      if (key == null) return;
      state.anchor_key = key;
      parts.anchorText.textContent = describe(key);
    }

    /**
     * Load the next window only at the true bottom of a window that is already full.
     * Re-anchoring earlier than that turned an ordinary scroll into a re-query and made
     * the panes jump back to the first slot.
     */
    function maybeLoadMore(index, body) {
      var matrix = state.matrix;
      if (!matrix || state.loading || matrix.slots.length < 50) return;
      if (body.scrollTop + body.clientHeight < body.scrollHeight - 2) return;
      var last = matrix.slots[matrix.slots.length - 1].ts_key;
      if (last == null || state.loading_tail === last) return;
      state.loading_tail = last;
      load(Number(last) + 1, null);
    }

    function currentAnchor() {
      for (var index = 0; index < parts.panes.length; index++) {
        var pane = parts.panes[index];
        if (!state.matrix) continue;
        var slotIndex = Math.max(0, Math.round(pane.body.scrollTop / 28));
        var key = anchorAt(slotIndex);
        if (key != null) return key;
      }
      return state.anchor_key;
    }

    /**
     * Load one aligned window. Every pane asks for the same [anchor, anchor+span]:
     * that is what "synchronised" means here — not equal scroll offsets.
     */
    function load(anchorKey, keepSlot, loadOptions) {
      state.version += 1;
      var version = state.version;
      var anchor = anchorKey == null ? state.anchor_key : anchorKey;
      state.loading = true;
      setNote('对齐到 ' + describe(anchor) + ' · 读取窗口中…');
      var specs = windowsFor(state.files, anchor, state.span_ns, DEFAULT_LIMIT);
      var tasks = specs.map(function (spec, index) {
        var file = state.files[index];
        return Promise.resolve(options.fetchWindow({ file: file, window: spec }))
          .then(function (result) {
            return { file: file, rows: (result && result.rows) || [], coverage: result && result.coverage,
              relation: (result && result.relation) || 'unknown', error: null };
          }, function (error) {
            return { file: file, rows: [], coverage: null, relation: 'unknown',
              error: error || { message: '读取失败' } };
          });
      });
      return Promise.all(tasks).then(function (results) {
        if (version !== state.version) {
          // A newer anchor was requested while this window was loading. Rendering it
          // now would move the panes back in time.
          throw errorOf('stale_window', '已有更新的对齐位置', { requested: version,
            current: state.version });
        }
        state.loading = false;
        state.anchor_key = anchor;
        state.excluded = [];
        var panes = results.map(function (result, index) {
          if (result.error) {
            state.excluded.push({ file_id: result.file.file_id,
              reason: (result.error.code || 'window_failed'),
              message: result.error.message });
          }
          var rows = result.rows.slice().sort(function (a, b) {
            var left = keyOf(a), right = keyOf(b);
            if (left == null && right == null) return a.ordinal - b.ordinal;
            if (left == null) return 1;
            if (right == null) return -1;
            return left - right || a.ordinal - b.ordinal;
          });
          return { file_id: result.file.file_id, label: result.file.label,
            source: result.file.source, rows: rows, relation: result.relation,
            state: result.error ? 'failed' : 'ready',
            reason: result.error ? result.error.message : null };
        });
        state.matrix = buildMatrix(panes, { max_slots: 500 });
        renderMatrix(keepSlot);
        setNote(describeMatrix());
        emit();
        return state.matrix;
      }, function (error) {
        state.loading = false;
        if (error && error.code === 'stale_window') return state.matrix;
        setNote(error && error.message ? error.message : '对齐失败', 'error');
        throw error;
      });
    }

    function describeMatrix() {
      var matrix = state.matrix;
      if (!matrix) return '';
      var parts_ = [matrix.slots.length + ' 个对齐时点'];
      if (matrix.untimed_count) {
        parts_.push(matrix.untimed_count + ' 条无时间戳记录未参与对齐');
      }
      var incomplete = matrix.columns.filter(function (column) {
        return column.state === 'failed' || column.relation !== 'exact';
      }).length;
      if (incomplete) parts_.push(incomplete + ' 个文件仅取到窗口内部分记录');
      if (matrix.truncated) parts_.push('窗口内时点超过上限，已截断');
      return parts_.join(' · ');
    }

    function renderMatrix(keepSlot) {
      var matrix = state.matrix;
      if (!matrix) return;
      parts.panes.forEach(function (pane, index) {
        var column = matrix.columns[index];
        pane.meta.textContent = (column.state === 'failed' ? '读取失败' : (column.relation === 'exact'
          ? '窗口完整' : '窗口部分')) + ' · ' + matrix.slots.filter(function (slot) {
          return slot.cells[index].length > 0;
        }).length + ' 个时点';
        // Set before touching the DOM: replacing the children resets scrollTop to 0 and
        // fires a scroll event, which would otherwise be read as "the user scrolled to
        // the first slot" and start a competing load.
        pane.body.dataset.applying = '1';
        pane.rowsHost.innerHTML = '';
        var fragment = document.createDocumentFragment();
        // Every slot occupies the same number of lines in every pane: a slot with a
        // duplicate in one file pads the others with empty rows. That is what keeps
        // "same scroll offset" equal to "same timestamp" without measuring anything.
        matrix.slots.forEach(function (slot) {
          var cell = slot.cells[index];
          var lines = Math.max(1, slot.lines);
          for (var line = 0; line < lines; line++) {
            fragment.appendChild(cell[line] ? rowElement(cell[line]) : emptyRow(!!cell.length));
          }
        });
        if (!matrix.slots.length) {
          var blank = el('p', 'cmp-empty', '该时间窗口内没有记录');
          fragment.appendChild(blank);
        }
        pane.rowsHost.appendChild(fragment);
        var target = (keepSlot == null ? 0 : keepSlot) * ROW_H;
        // Force layout before scrolling: right after `replaceChildren` the new rows are
        // not measured yet, so scrollTop would be clamped to 0 and the panes would jump
        // back to the first slot on every alignment.
        void pane.rowsHost.offsetHeight;
        pane.body.scrollTop = target;
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(function () {
            if (pane.body.scrollTop !== target) pane.body.scrollTop = target;
            pane.body.dataset.applying = '0';
          });
        } else {
          setTimeout(function () {
            if (pane.body.scrollTop !== target) pane.body.scrollTop = target;
            pane.body.dataset.applying = '0';
          }, 0);
        }
      });
      parts.anchorText.textContent = describe(state.anchor_key);
    }

    function emptyRow(padded) {
      var row = el('div', 'cmp-row is-empty' + (padded ? ' is-pad' : ''));
      row.setAttribute('aria-label', padded
        ? '同一时点的额外行，此文件无对应记录'
        : '该时点此文件无记录');
      row.append(el('span', 'cmp-ts', ''), el('span', 'cmp-msg', padded ? '' : '— 无记录'));
      return row;
    }

    function rowElement(row) {
      var node = el('div', 'cmp-row' + (row.level === 'ERROR' ? ' is-error' : ''));
      var ts = el('span', 'cmp-ts', row.ts ? String(row.ts).slice(11, 23) : '—');
      var message = el('span', 'cmp-msg', row.message || '');
      message.title = row.message || '';
      node.append(ts, message);
      node.dataset.ordinal = String(row.ordinal);
      if (options.onOpen) {
        node.addEventListener('click', function () { options.onOpen(row); });
        node.tabIndex = 0;
        node.addEventListener('keydown', function (event) {
          if (event.key === 'Enter') { event.preventDefault(); options.onOpen(row); }
        });
      }
      return node;
    }

    function setNote(text, tone) {
      parts.note.hidden = !text;
      parts.note.textContent = text || '';
      parts.note.dataset.tone = tone || 'info';
    }

    function describe(key) {
      if (key == null) return '未对齐';
      return new Date(Number(key)).toISOString().replace('T', ' ').slice(0, 23);
    }

    function setFiles(next) {
      files = validateFiles(next);
      state.files = files;
      state.matrix = null;
      renderGrid();
      return load(state.anchor_key);
    }

    function applyView(saved) {
      if (saved && saved.layout) {
        state.layout = saved.layout;
        if (parts.layoutSelect) parts.layoutSelect.value = saved.layout;
      }
      if (saved && typeof saved.sync === 'boolean') {
        state.sync = saved.sync;
        if (parts.syncBox) parts.syncBox.checked = saved.sync;
      }
      if (saved && saved.anchor_key != null) state.anchor_key = saved.anchor_key;
      renderGrid();
      return load(state.anchor_key);
    }

    return {
      mount: mount,
      load: load,
      view: view,
      applyView: applyView,
      setFiles: setFiles,
      state: state
    };
  }

  return {
    MIN_FILES: MIN_FILES,
    MAX_FILES: MAX_FILES,
    DEFAULT_SPAN_NS: DEFAULT_SPAN_NS,
    defaultLayout: defaultLayout,
    buildMatrix: buildMatrix,
    anchorOf: anchorOf,
    windowsFor: windowsFor,
    layoutStyle: layoutStyle,
    validateFiles: validateFiles,
    createCompare: createCompare
  };
});
