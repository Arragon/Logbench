/*
 * global-time.js — cross-source time scope: date range + file tree (R6-079).
 *
 * This replaces R5's two-pane time dialog with the same shape but a different
 * contract: the selection is a *scope over files*, not a list of copied timestamps.
 * Applying resolves the currently authorized descendants of every selected folder
 * and freezes that file set, the time range and the rule signature into one snapshot.
 * Re-applying a saved view resolves the folders again, so a file added to a selected
 * directory later is included — and a file that disappeared is reported as missing
 * instead of being replaced by a sample.
 *
 * The draft/applied split is deliberate: editing the dialog never changes what the
 * reader is showing. Only Apply does, and Cancel restores the draft to the applied
 * state rather than leaving half-edited controls behind.
 *
 * Nothing here parses log timestamps from the files. It builds a range from typed
 * input (interpreted as UTC, which is stated in the UI) and hands it to the query
 * layer, where `shared/query.js` applies the same predicate on both transports.
 *
 * UMD-lite: window.LBGlobalTime in the page, module.exports in Node.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(
    require('../shared/time.js'));
  else root.LBGlobalTime = factory(root.LBTime);
})(typeof self !== 'undefined' ? self : this, function (time) {
  'use strict';

  var SOURCE_LABEL = { local: '本机（仅此浏览器）', private: '服务器私有', shared: '服务器总库' };

  /* --------------------------------------------------------------- pure model */

  function tokenizePath(path) {
    return String(path || '').split('/').filter(Boolean);
  }

  /**
   * Build the selectable tree. Local files are namespaced under `local`, server files
   * by scope, because "the same name in two roots" is two files, not one.
   */
  function buildTree(input) {
    input = input || {};
    var roots = [];
    var localFiles = input.local || [];
    if (localFiles.length) {
      roots.push({ id: 'root:local', label: SOURCE_LABEL.local, type: 'root',
        source: 'local', children: nest(localFiles.map(function (file) {
          return { file_id: file.file_id, path: file.path || file.name || file.file_id,
            label: file.name || file.file_id, source: 'local' };
        })) });
    }
    ['shared', 'private'].forEach(function (scope) {
      var files = (input[scope] || []).map(function (file) {
        return { file_id: file.fileId || file.file_id,
          path: file.relPath || file.path || file.displayName,
          label: file.displayName || file.label || file.fileId, source: 'server',
          scope: scope, root_label: file.rootLabel };
      });
      if (!files.length) return;
      roots.push({ id: 'root:' + scope, label: SOURCE_LABEL[scope], type: 'root',
        source: 'server', scope: scope, children: nest(files) });
    });
    return roots;
  }

  function nest(files) {
    var rootChildren = [];
    var dirs = {};
    files.forEach(function (file) {
      var parts = tokenizePath(file.path);
      var name = parts.pop() || file.label;
      var current = rootChildren;
      var prefix = '';
      parts.forEach(function (part) {
        prefix = prefix ? prefix + '/' + part : part;
        var id = 'dir:' + file.source + ':' + (file.scope || '') + ':' + prefix;
        if (!dirs[id]) {
          dirs[id] = { id: id, label: part, type: 'dir', source: file.source,
            scope: file.scope, path: prefix, children: [] };
          current.push(dirs[id]);
        }
        current = dirs[id].children;
      });
      current.push({ id: 'file:' + file.file_id, label: name, type: 'file',
        source: file.source, scope: file.scope, file_id: file.file_id, path: file.path });
    });
    sortNodes(rootChildren);
    return rootChildren;
  }

  function sortNodes(nodes) {
    nodes.sort(function (a, b) {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
      return String(a.label).localeCompare(String(b.label), 'zh-Hans-CN');
    });
    nodes.forEach(function (node) { if (node.children) sortNodes(node.children); });
  }

  function filesUnder(node, out) {
    out = out || [];
    if (node.type === 'file') { out.push(node); return out; }
    (node.children || []).forEach(function (child) { filesUnder(child, out); });
    return out;
  }

  function findNode(roots, predicate) {
    var stack = roots.slice();
    while (stack.length) {
      var node = stack.shift();
      if (predicate(node)) return node;
      if (node.children) stack = node.children.concat(stack);
    }
    return null;
  }

  function emptySelection() {
    return { include: [], exclude: [] };
  }

  function withValue(list, value, present) {
    var out = list.filter(function (item) { return item !== value; });
    if (present) out.push(value);
    return out;
  }

  /**
   * Effective file set. A folder contributes its descendants; an explicit exclusion
   * under an included folder wins, which is what "取消个别文件" means.
   */
  function resolveSelection(roots, selection) {
    var include = selection.include || [];
    var exclude = selection.exclude || [];
    var files = [];
    function walk(node, inherited) {
      var included = inherited || include.indexOf(node.id) !== -1;
      if (node.type === 'file') {
        if (included && exclude.indexOf(node.file_id) === -1) files.push(node);
        return;
      }
      (node.children || []).forEach(function (child) { walk(child, included); });
    }
    roots.forEach(function (node) { walk(node, false); });
    var seen = {};
    files = files.filter(function (file) {
      if (seen[file.file_id]) return false;
      seen[file.file_id] = true;
      return true;
    });
    files.sort(function (a, b) { return String(a.path).localeCompare(String(b.path)); });
    // A directory is "partial" when some — not all — of its descendants are selected.
    var partial = [];
    (function scan(nodes) {
      nodes.forEach(function (node) {
        if (node.type === 'file') return;
        var all = filesUnder(node);
        var count = all.filter(function (file) { return seen[file.file_id]; }).length;
        if (count > 0 && count < all.length) partial.push(node.id);
        if (node.children) scan(node.children);
      });
    })(roots);
    return { files: files, partial: partial };
  }

  function stateOf(roots, selection, node) {
    var files = filesUnder(node);
    if (!files.length) return 'off';
    var resolved = resolveSelection(roots, selection).files;
    var selected = files.filter(function (file) {
      return resolved.some(function (item) { return item.file_id === file.file_id; });
    }).length;
    if (!selected) return 'off';
    return selected === files.length ? 'on' : 'mixed';
  }

  function toggle(roots, selection, node) {
    var next = { include: (selection.include || []).slice(),
      exclude: (selection.exclude || []).slice() };
    var state = stateOf(roots, selection, node);
    if (node.type === 'file') {
      if (state === 'on') {
        next.exclude = withValue(next.exclude, node.file_id, true);
        return next;
      }
      next.exclude = withValue(next.exclude, node.file_id, false);
      // Selecting a file inside an unselected folder selects just that file, so the
      // folder itself stays off and a later refresh does not add its siblings.
      var effective = resolveSelection(roots, next).files.some(function (file) {
        return file.file_id === node.file_id;
      });
      if (!effective) next.include = withValue(next.include, node.id, true);
      return next;
    }
    var descendants = filesUnder(node).map(function (file) { return file.file_id; });
    if (state === 'on') {
      next.include = withValue(next.include, node.id, false);
      descendants.forEach(function (id) { next.exclude = withValue(next.exclude, id, true); });
    } else {
      next.include = withValue(next.include, node.id, true);
      descendants.forEach(function (id) { next.exclude = withValue(next.exclude, id, false); });
    }
    return next;
  }

  /* ------------------------------------------------------------------- ranges */

  function two(value) { return (value < 10 ? '0' : '') + value; }

  /** `YYYY-MM-DD` + `HH:MM:SS` + optional fraction → exact ns string (UTC). */
  function composeRange(date, clock, fraction) {
    if (!date || !clock) {
      return { ok: false, error: { code: 'missing_time', message: '请填写开始与结束的日期和时间。' } };
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      return { ok: false, error: { code: 'invalid_date', message: '日期格式应为 YYYY-MM-DD。' } };
    }
    var parts = clock.split(':');
    if (parts.length < 2 || parts.length > 3) {
      return { ok: false, error: { code: 'invalid_time', message: '时间格式应为 HH:MM 或 HH:MM:SS。' } };
    }
    var seconds = (parts[2] || '0');
    var fractionText = String(fraction == null ? '' : fraction).trim();
    if (fractionText && !/^\d{1,9}$/.test(fractionText)) {
      return { ok: false, error: { code: 'invalid_fraction',
        message: '小数秒应为 1–9 位数字。' } };
    }
    var text = date + 'T' + two(Number(parts[0])) + ':' + two(Number(parts[1])) + ':' +
      two(Number(seconds)) + (fractionText ? '.' + fractionText.padEnd(9, '0') : '') + 'Z';
    var parsed = time.parseTs(text);
    if (!parsed.valid) {
      // Distinguish "the date part is out of range" from "the clock part is": the
      // page highlights the field the user has to fix.
      var dateReason = parsed.reason === 'month' || parsed.reason === 'day';
      return { ok: false, error: { code: dateReason ? 'invalid_date' : 'invalid_time',
        message: (dateReason ? '日期不合法：' : '时间不合法：') + parsed.reason } };
    }
    return { ok: true, ns: parsed.ns, key: parsed.key, text: text };
  }

  function makeRange(from, to) {
    var start = composeRange(from.date, from.clock, from.fraction);
    if (!start.ok) return { ok: false, error: start.error };
    var end = composeRange(to.date, to.clock, to.fraction);
    if (!end.ok) return { ok: false, error: end.error };
    if (BigInt(start.ns) > BigInt(end.ns)) {
      return { ok: false, error: { code: 'range_inverted', message: '开始时间不能晚于结束时间。' } };
    }
    return {
      ok: true,
      range: { from_ns: start.ns, to_ns: end.ns, from_text: start.text, to_text: end.text,
        from_key: start.key, to_key: end.key, closed: true, timezone: 'UTC' }
    };
  }

  function splitTimestamp(ns) {
    if (ns == null) return { date: '', clock: '', fraction: '' };
    var text = new Date(Number(BigInt(String(ns)) / 1000000n)).toISOString();
    return { date: text.slice(0, 10), clock: text.slice(11, 19), fraction: '' };
  }

  /* --------------------------------------------------------------- saved views */

  /**
   * A saved view stores stable references — folder ids and file ids — plus the range.
   * Re-applying resolves the ids against the current tree, so new descendants appear
   * and vanished ones are reported.
   */
  function viewFrom(state, name) {
    return {
      id: 'view-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6),
      name: name || '时间视角',
      include: (state.selection.include || []).slice(),
      exclude: (state.selection.exclude || []).slice(),
      range: state.range ? { from_text: state.range.from_text, to_text: state.range.to_text } : null,
      search: state.search ? { text: state.search.text || '', flags: state.search.flags || {} } : null,
      saved_at: new Date().toISOString()
    };
  }

  function applyView(roots, view) {
    var missing = [];
    var include = [];
    (view.include || []).forEach(function (id) {
      if (findNode(roots, function (node) { return node.id === id; })) include.push(id);
      else missing.push({ id: id, reason: 'folder_missing' });
    });
    var exclude = [];
    (view.exclude || []).forEach(function (fileId) {
      if (findNode(roots, function (node) { return node.file_id === fileId; })) exclude.push(fileId);
      else missing.push({ id: fileId, reason: 'file_missing' });
    });
    var resolved = resolveSelection(roots, { include: include, exclude: exclude });
    return { selection: { include: include, exclude: exclude }, files: resolved.files,
      missing: missing, range: view.range, search: view.search };
  }

  /* -------------------------------------------------------------------- widget */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /**
   * @param {object} options
   *   loadScope()  -> Promise<{local, shared, private, unauthorized}>
   *   store        {load(), save(patch)} for recent ranges and saved views
   *   onApply(snapshot)
   */
  function createGlobalTime(options) {
    options = options || {};
    var host = null;
    var parts = {};
    var roots = [];
    var state = {
      open: false,
      selection: emptySelection(),
      range: null,
      search: { text: '', flags: {} },
      applied: null,
      views: [],
      recent: [],
      missing: [],
      sourceInfo: null,
      busy: false,
      error: null,
      /* Set when the user types before the scope has finished loading. The load must
       * then not overwrite the range or the selection underneath them. */
      touched: false
    };
    var openToken = 0;

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.hidden = true;
      host.className = 'gt-dialog';
      host.setAttribute('role', 'dialog');
      host.setAttribute('aria-modal', 'true');
      host.setAttribute('aria-label', '全局时间范围');

      parts.head = el('div', 'gt-head');
      parts.title = el('strong', null, '时间范围与范围文件');
      parts.close = el('button', 'r6-icon', '✕');
      parts.close.type = 'button';
      parts.close.setAttribute('aria-label', '关闭时间范围');
      parts.close.addEventListener('click', function () { close(); });
      parts.head.append(parts.title, parts.close);

      parts.body = el('div', 'gt-body');
      parts.left = el('div', 'gt-pane gt-left');
      parts.right = el('div', 'gt-pane gt-right');
      parts.body.append(parts.left, parts.right);

      parts.foot = el('div', 'gt-foot');
      parts.summary = el('span', 'r6-meta', '尚未选择范围');
      parts.note = el('span', 'gt-note');
      parts.note.hidden = true;
      // Save/load results belong to this dialog, not to the header: the header
      // reports what the reader is currently looking at. Keep them separate
      // from .gt-note, which renderFoot() owns for missing-file warnings.
      parts.status = el('span', 'gt-status');
      parts.status.setAttribute('role', 'status');
      parts.status.hidden = true;
      parts.cancel = el('button', 'r6-button', '取消');
      parts.cancel.type = 'button';
      parts.cancel.addEventListener('click', function () { close(); });
      parts.save = el('button', 'r6-button', '保存视角');
      parts.save.type = 'button';
      parts.save.addEventListener('click', function () { saveView(); });
      parts.apply = el('button', 'r6-button is-primary', '应用范围');
      parts.apply.type = 'button';
      parts.apply.addEventListener('click', function () { apply(); });
      parts.foot.append(parts.summary, parts.note, parts.status,
        parts.cancel, parts.save, parts.apply);

      host.append(parts.head, parts.body, parts.foot);
      host.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { event.stopPropagation(); close(); }
        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          apply();
        }
      });
      buildLeft();
      buildRight();
      render();
      return { open: open, close: close, state: state };
    }

    /* ------------------------------------------------------------------ left */
    function buildLeft() {
      parts.left.innerHTML = '';
      var section = el('div', 'gt-section');
      section.appendChild(el('div', 'gt-label', '起止时间（UTC）'));
      parts.fromDate = dateInput('gt-from-date');
      parts.fromClock = clockInput('gt-from-clock');
      parts.fromFraction = fractionInput('gt-from-fraction');
      parts.fromFraction.setAttribute('aria-label', '开始小数秒');
      var fromRow = el('div', 'gt-row');
      fromRow.append(el('span', 'gt-caption', '开始'), parts.fromDate, parts.fromClock,
        parts.fromFraction);
      parts.toDate = dateInput('gt-to-date');
      parts.toClock = clockInput('gt-to-clock');
      parts.toFraction = fractionInput('gt-to-fraction');
      parts.toFraction.setAttribute('aria-label', '结束小数秒');
      var toRow = el('div', 'gt-row');
      toRow.append(el('span', 'gt-caption', '结束'), parts.toDate, parts.toClock, parts.toFraction);
      parts.rangeError = el('div', 'r6-errorbox');
      parts.rangeError.hidden = true;
      parts.rangeError.setAttribute('role', 'alert');
      section.append(fromRow, toRow, parts.rangeError,
        el('p', 'r6-hint', '日志时间若不带时区，按 UTC 解析；不做自动时区换算。'));
      [parts.fromDate, parts.fromClock, parts.fromFraction, parts.toDate, parts.toClock,
        parts.toFraction].forEach(function (input) {
        input.addEventListener('change', onTimeEdited);
        input.addEventListener('input', onTimeEdited);
      });

      var quick = el('div', 'gt-quick');
      [['近 1 小时', 1], ['近 6 小时', 6], ['近 24 小时', 24]].forEach(function (pair) {
        var button = el('button', 'r6-icon', pair[0]);
        button.type = 'button';
        button.addEventListener('click', function () { quickRange(pair[1]); });
        quick.appendChild(button);
      });
      section.appendChild(quick);
      parts.left.appendChild(section);

      parts.recent = el('div', 'gt-section');
      parts.recent.appendChild(el('div', 'gt-label', '最近使用'));
      parts.recentList = el('div', 'gt-list');
      parts.recent.appendChild(parts.recentList);
      parts.left.appendChild(parts.recent);

      parts.views = el('div', 'gt-section');
      parts.views.appendChild(el('div', 'gt-label', '收藏视角'));
      parts.viewList = el('div', 'gt-list');
      parts.views.appendChild(parts.viewList);
      parts.left.appendChild(parts.views);
    }

    function dateInput(id) {
      var input = el('input', 'gt-input');
      input.type = 'date';
      input.id = id;
      input.addEventListener('change', onTimeEdited);
      return input;
    }
    function clockInput(id) {
      var input = el('input', 'gt-input');
      input.type = 'time';
      input.step = '1';
      input.id = id;
      input.addEventListener('change', onTimeEdited);
      return input;
    }
    function fractionInput(id) {
      var input = el('input', 'gt-input gt-fraction');
      input.type = 'text';
      input.inputMode = 'numeric';
      input.maxLength = 9;
      input.placeholder = '小数秒';
      input.id = id;
      input.addEventListener('change', onTimeEdited);
      return input;
    }

    function onTimeEdited() {
      state.touched = true;
      var range = makeRange(
        { date: parts.fromDate.value, clock: parts.fromClock.value, fraction: parts.fromFraction.value },
        { date: parts.toDate.value, clock: parts.toClock.value, fraction: parts.toFraction.value });
      if (!range.ok) {
        state.range = null;
        showError(range.error.message);
      } else {
        state.range = range.range;
        showError(null);
      }
      renderFoot();
    }

    function quickRange(hours) {
      var now = Date.now();
      var from = new Date(now - hours * 3600 * 1000).toISOString();
      setTimeInputs(from, new Date(now).toISOString());
      onTimeEdited();
    }

    function setTimeInputs(fromIso, toIso) {
      parts.fromDate.value = fromIso.slice(0, 10);
      parts.fromClock.value = fromIso.slice(11, 19);
      parts.fromFraction.value = '';
      parts.toDate.value = toIso.slice(0, 10);
      parts.toClock.value = toIso.slice(11, 19);
      parts.toFraction.value = '';
    }

    function showError(message) {
      parts.rangeError.hidden = !message;
      parts.rangeError.textContent = message || '';
    }

    function showResult(message) {
      parts.status.hidden = !message;
      parts.status.textContent = message || '';
    }

    /* ----------------------------------------------------------------- right */
    function buildRight() {
      parts.right.innerHTML = '';
      var bar = el('div', 'gt-treebar');
      parts.filter = el('input', 'gt-input gt-search');
      parts.filter.type = 'search';
      parts.filter.placeholder = '搜索文件或目录';
      parts.filter.setAttribute('aria-label', '搜索范围文件');
      parts.filter.addEventListener('input', function () { renderTree(); });
      parts.selectAll = el('button', 'r6-icon', '全选可见');
      parts.selectAll.type = 'button';
      parts.selectAll.addEventListener('click', function () { selectVisible(true); });
      parts.clearAll = el('button', 'r6-icon', '清空');
      parts.clearAll.type = 'button';
      parts.clearAll.addEventListener('click', function () { selectVisible(false); });
      bar.append(parts.filter, parts.selectAll, parts.clearAll);
      parts.tree = el('div', 'gt-tree');
      parts.tree.setAttribute('role', 'group');
      parts.tree.setAttribute('aria-label', '范围文件');
      parts.right.append(bar, parts.tree);
    }

    function visibleMatch(node, needle) {
      if (!needle) return true;
      if (String(node.label).toLowerCase().indexOf(needle) !== -1) return true;
      if (node.file_id && String(node.file_id).toLowerCase().indexOf(needle) !== -1) return true;
      if (node.path && String(node.path).toLowerCase().indexOf(needle) !== -1) return true;
      return (node.children || []).some(function (child) { return visibleMatch(child, needle); });
    }

    function renderTree() {
      var needle = (parts.filter.value || '').trim().toLowerCase();
      parts.tree.innerHTML = '';
      if (!roots.length) {
        parts.tree.appendChild(el('p', 'gt-empty',
          '没有可选文件。本机文件需要先导入，服务器文件需要已授权且有索引。'));
        return;
      }
      roots.forEach(function (root) {
        if (!visibleMatch(root, needle)) return;
        parts.tree.appendChild(renderNode(root, needle, 0));
      });
    }

    function renderNode(node, needle, depth) {
      var wrap = el('div', 'gt-node');
      if (!visibleMatch(node, needle)) return wrap;
      var rowEl = el('label', 'gt-node-row');
      rowEl.style.paddingLeft = (depth * 12 + 4) + 'px';
      var box = el('input');
      box.type = 'checkbox';
      var status = stateOf(roots, state.selection, node);
      box.checked = status === 'on';
      box.indeterminate = status === 'mixed';
      box.setAttribute('aria-label', node.label);
      box.addEventListener('change', function () {
        state.selection = toggle(roots, state.selection, node);
        renderTree();
        renderFoot();
      });
      var label = el('span', 'gt-node-label', node.label);
      label.title = node.path || node.label;
      rowEl.append(box, label);
      if (node.type === 'root') {
        rowEl.appendChild(el('span', 'gt-badge is-quiet', String(filesUnder(node).length)));
      }
      if (node.type === 'file' && node.scope) {
        rowEl.appendChild(el('span', 'gt-src', node.scope === 'private' ? '私有' : '共享'));
      }
      wrap.appendChild(rowEl);
      if (node.children && node.children.length) {
        node.children.forEach(function (child) {
          if (visibleMatch(child, needle)) wrap.appendChild(renderNode(child, needle, depth + 1));
        });
      }
      return wrap;
    }

    function isSelectable(node) {
      return node.type === 'file';
    }
    function selectVisible(on) {
      var needle = (parts.filter.value || '').trim().toLowerCase();
      roots.forEach(function (root) {
        if (!visibleMatch(root, needle)) return;
        var targets = needle
          ? filesUnder(root).filter(function (file) { return visibleMatch(file, needle); })
          : filesUnder(root);
        targets.forEach(function (file) {
          var status = stateOf(roots, state.selection, file);
          if (on && status !== 'on') state.selection = toggle(roots, state.selection, file);
          if (!on && status === 'on') state.selection = toggle(roots, state.selection, file);
        });
      });
      renderTree();
      renderFoot();
    }

    /* ----------------------------------------------------------------- render */
    function render() {
      renderTree();
      renderLists();
      renderFoot();
      if (state.range && !state.touched) {
        parts.fromDate.value = state.range.from_text.slice(0, 10);
        parts.fromClock.value = state.range.from_text.slice(11, 19);
        parts.toDate.value = state.range.to_text.slice(0, 10);
        parts.toClock.value = state.range.to_text.slice(11, 19);
      }
    }

    function renderLists() {
      parts.recentList.innerHTML = '';
      if (!state.recent.length) parts.recentList.appendChild(el('p', 'gt-empty', '暂无最近范围'));
      state.recent.slice(0, 5).forEach(function (item) {
        var button = el('button', 'gt-list-item', item.label);
        button.type = 'button';
        button.addEventListener('click', function () {
          setTimeInputs(item.from_text, item.to_text);
          onTimeEdited();
        });
        parts.recentList.appendChild(button);
      });
      parts.viewList.innerHTML = '';
      if (!state.views.length) parts.viewList.appendChild(el('p', 'gt-empty', '暂无保存的视角'));
      state.views.slice(0, 8).forEach(function (view) {
        var rowEl = el('div', 'gt-view-row');
        var button = el('button', 'gt-list-item', view.name);
        button.type = 'button';
        button.addEventListener('click', function () { loadView(view); });
        var remove = el('button', 'r6-icon', '移除');
        remove.type = 'button';
        remove.addEventListener('click', function () { removeView(view); });
        rowEl.append(button, remove);
        parts.viewList.appendChild(rowEl);
      });
    }

    function renderFoot() {
      var resolved = resolveSelection(roots, state.selection);
      var files = resolved.files;
      var label = files.length
        ? '已选 ' + files.length + ' 个文件' + (resolved.partial.length
          ? '（含部分选择目录 ' + resolved.partial.length + '）' : '')
        : '尚未选择文件';
      parts.summary.textContent = label;
      var blocked = !files.length || !state.range || state.busy;
      parts.apply.disabled = blocked;
      parts.save.disabled = !files.length || !state.range || state.busy;
      if (state.missing.length) {
        parts.note.hidden = false;
        parts.note.textContent = '有 ' + state.missing.length +
          ' 项在重新解析时已不存在（缺失或撤权），已跳过且未替换为样例。';
        parts.note.dataset.tone = 'warn';
      } else {
        parts.note.hidden = true;
      }
    }

    /* ---------------------------------------------------------------- actions */
    function snapshot() {
      var resolved = resolveSelection(roots, state.selection);
      var selection = { include: state.selection.include.slice(), exclude: state.selection.exclude.slice() };
      return {
        files: resolved.files.map(function (file) {
          return { source: file.source, scope: file.scope || (file.source === 'local' ? 'local' : 'shared'),
            file_id: file.file_id, path: file.path, label: file.label };
        }),
        partial: resolved.partial,
        selection: selection,
        range: state.range,
        search: state.search,
        discovery: selection.include.some(function (id) { return id.indexOf('dir:') === 0 ||
          id.indexOf('root:') === 0; }) ? 'folder' : 'explicit',
        missing: state.missing.slice(),
        applied_at: new Date().toISOString()
      };
    }

    function apply() {
      var result = snapshot();
      if (!result.files.length || !result.range) return null;
      state.applied = result;
      state.busy = true;
      renderFoot();
      rememberRange(result.range);
      Promise.resolve(options.onApply ? options.onApply(result) : null).then(function () {
        state.busy = false;
        renderFoot();
        close();
      }, function (error) {
        state.busy = false;
        showError(error && error.message ? error.message : '应用范围失败');
        renderFoot();
      });
      return result;
    }

    function rememberRange(range) {
      var label = range.from_text.replace('T', ' ').replace('Z', '') + ' → ' +
        range.to_text.replace('T', ' ').replace('Z', '');
      var next = state.recent.filter(function (item) {
        return item.from_text !== range.from_text || item.to_text !== range.to_text;
      });
      next.unshift({ label: label, from_text: range.from_text, to_text: range.to_text });
      state.recent = next.slice(0, 8);
      if (options.store) options.store.save({ recent: state.recent }).catch(function () {});
      renderLists();
    }

    function saveView() {
      var name = (window.prompt && window.prompt('视角名称', '时间视角')) || null;
      if (name === null) return;
      var view = viewFrom({ selection: state.selection, range: state.range, search: state.search }, name);
      state.views = [view].concat(state.views).slice(0, 20);
      if (options.store) options.store.save({ views: state.views }).catch(function () {});
      renderLists();
      showResult('已保存视角：' + view.name);
    }

    function removeView(view) {
      state.views = state.views.filter(function (item) { return item.id !== view.id; });
      if (options.store) options.store.save({ views: state.views }).catch(function () {});
      renderLists();
    }

    function loadView(view) {
      var result = applyView(roots, view);
      state.selection = result.selection;
      state.missing = result.missing;
      if (view.range && view.range.from_text) {
        setTimeInputs(view.range.from_text, view.range.to_text);
        onTimeEdited();
      }
      state.search = view.search || { text: '', flags: {} };
      if (state.search.text && parts.filter) parts.filter.value = state.search.text;
      renderTree();
      renderLists();
      renderFoot();
      showResult('已载入视角：' + view.name +
        '（重新解析为 ' + result.files.length + ' 个文件）');
      return result;
    }

    /* ------------------------------------------------------------------ open */
    function open(initial) {
      var token = ++openToken;
      host.hidden = false;
      state.open = true;
      state.busy = false;
      state.touched = false;
      showError(null);
      showResult(null);
      return Promise.resolve()
        .then(function () { return options.loadScope ? options.loadScope() : { local: [], shared: [], private: [] }; })
        .then(function (scope) {
          if (token !== openToken) return null;
          state.sourceInfo = scope || {};
          roots = buildTree(scope || {});
          if (options.store) {
            return Promise.resolve(options.store.load()).then(function (saved) {
              saved = saved || {};
              state.views = Array.isArray(saved.views) ? saved.views : [];
              state.recent = Array.isArray(saved.recent) ? saved.recent : [];
            }, function () {});
          }
          return null;
        })
        .then(function () {
          if (token !== openToken) return state;
          /* Only fill in the initial range while the user has not typed anything: a
           * late load must not silently replace a range that is being edited. */
          if (!state.touched) {
            if (initial && initial.range) {
              state.range = initial.range;
              setTimeInputs(initial.range.from_text, initial.range.to_text);
            } else if (!parts.fromDate.value) {
              state.range = null;
              quickRange(24);
            }
            if (initial && initial.selection) state.selection = initial.selection;
            if (initial && initial.search) state.search = initial.search;
          }
          render();
          parts.filter.focus();
          return state;
        });
    }

    function close() {
      host.hidden = true;
      state.open = false;
      // Cancel does not half-apply: the draft is reset to what the reader is using.
      if (state.applied) {
        state.selection = { include: state.applied.selection.include.slice(),
          exclude: state.applied.selection.exclude.slice() };
      }
      parts.note.hidden = true;
      showResult(null);
    }

    return {
      mount: mount,
      open: open,
      close: close,
      apply: apply,
      state: state,
      setRoots: function (next) { roots = next || []; render(); },
      roots: function () { return roots; }
    };
  }

  return {
    SOURCE_LABEL: SOURCE_LABEL,
    buildTree: buildTree,
    filesUnder: filesUnder,
    resolveSelection: resolveSelection,
    stateOf: stateOf,
    toggle: toggle,
    emptySelection: emptySelection,
    composeRange: composeRange,
    makeRange: makeRange,
    splitTimestamp: splitTimestamp,
    viewFrom: viewFrom,
    applyView: applyView,
    createGlobalTime: createGlobalTime
  };
});
