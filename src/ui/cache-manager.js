/*
 * cache-manager.js — 解析缓存分层状态、重建与清理 (R6-082).
 *
 * What this page refuses to do:
 *   - show a layer as "完成" because a row exists. The state comes from the
 *     publication pointer; a staging or failed attempt is shown beside it as the
 *     reason, never instead of it.
 *   - treat "clear index" and "delete file" as one action. Purge only accepts derived
 *     layers, states that the source bytes are kept, and names the active references
 *     (pins) that keep the old version readable.
 *   - run a batch that hides its scope. Selecting rows shows how many files and which
 *     layers are about to change before the button is enabled.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBCacheManager = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LAYERS = [
    ['structure', '结构'],
    ['rules', '规则'],
    ['search', '检索']
  ];
  var STATE_LABEL = {
    complete: '完成', pending: '未建', building: '重建中', failed: '失败', stale: '待重建'
  };
  var STATE_TONE = { complete: 'good', pending: 'quiet', building: 'warn', failed: 'bad', stale: 'warn' };
  var COVERAGE_LABEL = { none: '无缓存', structure: '结构', rules: '规则', search: '检索' };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function bytes(value) {
    var size = Number(value || 0);
    if (!size) return '—';
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)) + ' ' + units[index];
  }

  function create(options) {
    options = options || {};
    var api = options.api;
    var onStatus = options.onStatus || function () {};
    var host = null;
    var parts = {};
    var state = {
      items: [],
      nextCursor: null,
      summary: null,
      admin: false,
      loading: false,
      error: null,
      scope: 'shared',
      layer: '',
      filter: '',
      selected: {},
      expanded: {},
      dialog: null
    };

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.className = 'cache-view';

      parts.bar = el('div', 'r6-toolbar');
      parts.summary = el('span', 'r6-meta', '解析缓存');
      parts.scope = el('select', 'r6-input cm-scope');
      [['shared', '服务器总库'], ['private', '服务器私有']].forEach(function (pair) {
        var option = el('option', null, pair[1]);
        option.value = pair[0];
        parts.scope.appendChild(option);
      });
      parts.scope.setAttribute('aria-label', '范围');
      parts.scope.addEventListener('change', function () {
        state.scope = parts.scope.value;
        state.selected = {};
        load(true);
      });
      parts.layer = el('select', 'r6-input');
      [['', '全部层'], ['structure', '只看结构'], ['rules', '只看规则'], ['search', '只看检索']]
        .forEach(function (pair) {
          var option = el('option', null, pair[1]);
          option.value = pair[0];
          parts.layer.appendChild(option);
        });
      parts.layer.setAttribute('aria-label', '缓存层');
      parts.layer.addEventListener('change', function () {
        state.layer = parts.layer.value;
        load(true);
      });
      parts.filter = el('input', 'r6-input cm-search');
      parts.filter.type = 'search';
      parts.filter.placeholder = '搜索文件名或路径';
      parts.filter.setAttribute('aria-label', '搜索文件');
      var searchTimer = null;
      parts.filter.addEventListener('input', function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () {
          state.filter = parts.filter.value.trim();
          load(true);
        }, 200);
      });
      parts.reload = el('button', 'r6-icon', '刷新');
      parts.reload.type = 'button';
      parts.reload.addEventListener('click', function () { load(true); });
      parts.bar.append(parts.summary, el('span', 'spacer'), parts.scope, parts.layer,
        parts.filter, parts.reload);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'alert');
      parts.note.hidden = true;

      parts.batch = el('div', 'r6-toolbar cm-batch');
      parts.batch.hidden = true;
      parts.batchText = el('span', 'r6-meta', '');
      parts.rebuildSel = el('button', 'r6-button is-small', '重建所选派生层');
      parts.rebuildSel.type = 'button';
      parts.rebuildSel.addEventListener('click', function () { batch('rebuild'); });
      parts.purgeSel = el('button', 'r6-button is-small', '清理所选派生层');
      parts.purgeSel.type = 'button';
      parts.purgeSel.addEventListener('click', function () { batch('purge'); });
      parts.clearSel = el('button', 'r6-icon', '清除选择');
      parts.clearSel.type = 'button';
      parts.clearSel.addEventListener('click', function () {
        state.selected = {};
        render();
      });
      parts.batch.append(parts.batchText, el('span', 'spacer'), parts.clearSel,
        parts.rebuildSel, parts.purgeSel);

      parts.scroll = el('div', 'r6-table-scroll cm-scroll');
      parts.table = el('table', 'r6-table cm-table');
      parts.scroll.appendChild(parts.table);
      parts.more = el('div', 'cm-more');
      parts.more.hidden = true;
      parts.moreButton = el('button', 'r6-button is-small', '加载更多');
      parts.moreButton.type = 'button';
      parts.moreButton.addEventListener('click', function () { load(false); });
      parts.more.appendChild(parts.moreButton);

      parts.dialog = el('div', 'r6-dialog cm-dialog');
      parts.dialog.setAttribute('role', 'dialog');
      parts.dialog.setAttribute('aria-modal', 'true');
      parts.dialog.setAttribute('aria-label', '确认缓存操作');
      parts.dialog.hidden = true;

      parts.foot = el('div', 'r6-foot');
      parts.foot.appendChild(el('span', 'r6-meta',
        '清理缓存只移除可再生的派生层指针；原文与索引分片不会因为清理而被删除。'));

      host.append(parts.bar, parts.note, parts.batch, parts.scroll, parts.more, parts.foot, parts.dialog);
      host.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !parts.dialog.hidden) closeDialog();
      });
      return api;
    }

    function setNotice(text, tone) {
      parts.note.hidden = !text;
      parts.note.textContent = text || '';
      parts.note.dataset.tone = tone || 'info';
    }

    function load(reset) {
      if (reset) {
        state.items = [];
        state.nextCursor = null;
      }
      state.loading = true;
      var query = '/api/v2/cache?limit=50&scope=' + encodeURIComponent(state.scope);
      if (state.layer) query += '&layer=' + encodeURIComponent(state.layer);
      if (state.filter) query += '&q=' + encodeURIComponent(state.filter);
      if (!reset && state.nextCursor) query += '&cursor=' + encodeURIComponent(state.nextCursor);
      return api.get('/api/v1/me').then(function (me) {
        api.setCsrf(me.csrfToken || '');
        state.admin = !!(me.capabilities && me.capabilities.admin);
        return api.get(query);
      }).then(function (value) {
        state.loading = false;
        state.items = reset ? (value.items || []) : state.items.concat(value.items || []);
        state.nextCursor = value.nextCursor || null;
        state.summary = value.summary || null;
        state.error = null;
        setNotice(null);
        render();
      }, function (error) {
        state.loading = false;
        state.error = error.message || String(error);
        render();
        setNotice(error.status === 403
          ? '解析缓存管理需要管理员权限；日志仍然可以只读浏览。'
          : ('无法读取缓存状态：' + (error.message || error)),
          error.status === 403 ? 'warn' : 'error');
      });
    }

    function render() {
      var selectedCount = Object.keys(state.selected).length;
      parts.scope.value = state.scope;
      parts.layer.value = state.layer;
      parts.batch.hidden = !selectedCount || !state.admin;
      if (selectedCount) {
        parts.batchText.textContent = '已选 ' + selectedCount + ' 个文件；操作只影响派生层';
      }
      parts.summary.textContent = state.summary
        ? state.summary.files + ' 个文件 · ' +
          LAYERS.map(function (pair) {
            var count = (state.summary.byCoverage || {})[pair[0]] || 0;
            return pair[1] + ' ' + count;
          }).join(' / ')
        : '解析缓存';
      var head = el('thead');
      var headRow = el('tr');
      ['', '文件', '结构 / 规则 / 检索', '覆盖', '索引占用', '操作'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var body = el('tbody');
      if (!state.items.length) {
        var tr = el('tr');
        var td = el('td', 'lib-empty-cell');
        td.colSpan = 6;
        td.append(el('strong', null, state.loading ? '读取中…' : '没有匹配的文件'),
          el('p', null, '缓存状态来自已登记的日志库文件；本机浏览器文件不上传，因此不出现在这里。'));
        tr.appendChild(td);
        body.appendChild(tr);
      }
      state.items.forEach(function (item) {
        body.appendChild(fileRow(item));
        if (state.expanded[item.fileId]) body.appendChild(detailRow(item));
      });
      parts.table.innerHTML = '';
      parts.table.append(head, body);
      parts.more.hidden = !state.nextCursor;
    }

    function fileRow(item) {
      var tr = el('tr');
      tr.classList.toggle('is-selected', !!state.selected[item.fileId]);
      var checkCell = el('td', 'cm-check');
      var box = el('input');
      box.type = 'checkbox';
      box.checked = !!state.selected[item.fileId];
      box.disabled = !state.admin;
      box.setAttribute('aria-label', '选择 ' + item.displayName);
      box.addEventListener('change', function () {
        if (box.checked) state.selected[item.fileId] = item;
        else delete state.selected[item.fileId];
        render();
      });
      checkCell.appendChild(box);

      var nameCell = el('td');
      var name = el('button', 'r6-link', item.displayName);
      name.type = 'button';
      name.title = item.relPath || item.fileId;
      name.addEventListener('click', function () {
        state.expanded[item.fileId] = !state.expanded[item.fileId];
        render();
      });
      nameCell.append(name);
      var meta = el('div', 'r6-meta mono');
      meta.textContent = (item.rootLabel || '') + ' · ' + bytes(item.size) + ' · ' +
        item.fileId.slice(0, 12);
      nameCell.appendChild(meta);
      if (item.missing) {
        nameCell.appendChild(el('div', 'r6-cell-warn', '原文缺失：缓存不可重建'));
      }

      var layersCell = el('td');
      var wrap = el('div', 'r6-badges');
      LAYERS.forEach(function (pair) {
        var entry = item.layers[pair[0]] || {};
        var badge = el('span', 'r6-badge', pair[1]);
        badge.classList.add('is-' + (STATE_TONE[entry.state] || 'quiet'));
        badge.title = (STATE_LABEL[entry.state] || entry.state) +
          (entry.rows ? ' · ' + entry.rows + ' 行' : '') +
          (entry.reason ? ' · ' + entry.reason : '');
        badge.appendChild(el('span', 'cm-badge-text', STATE_LABEL[entry.state] || entry.state));
        wrap.appendChild(badge);
      });
      layersCell.appendChild(wrap);

      var coverageCell = el('td');
      coverageCell.append(el('div', 'r6-cell-strong', COVERAGE_LABEL[item.coverage] || item.coverage));
      if (item.layers.rules && item.layers.rules.rows) {
        coverageCell.appendChild(el('div', 'r6-meta', '规则行 ' + item.layers.rules.rows));
      }
      var sizeCell = el('td', 'r6-meta mono');
      sizeCell.textContent = bytes(item.shardBytes);
      var ops = el('td');
      var opsWrap = el('div', 'r6-inline-actions');
      var rebuild = el('button', 'r6-icon', '重建');
      rebuild.type = 'button';
      rebuild.title = '复用结构层，只重建规则与检索';
      rebuild.disabled = !state.admin || item.missing;
      rebuild.addEventListener('click', function () { confirmAction(item, 'rebuild'); });
      var purge = el('button', 'r6-icon is-danger', '清理');
      purge.type = 'button';
      purge.title = '移除规则/检索指针，保留原文';
      purge.disabled = !state.admin || !item.purgeable.length;
      purge.addEventListener('click', function () { confirmAction(item, 'purge'); });
      opsWrap.append(rebuild, purge);
      ops.appendChild(opsWrap);
      tr.append(checkCell, nameCell, layersCell, coverageCell, sizeCell, ops);
      return tr;
    }

    function detailRow(item) {
      var tr = el('tr', 'cm-detail');
      var td = el('td');
      td.colSpan = 6;
      var box = el('div', 'cm-detail-box');
      LAYERS.forEach(function (pair) {
        var entry = item.layers[pair[0]] || {};
        var line = el('div', 'cm-detail-line');
        line.appendChild(el('strong', null, pair[1] + '：' + (STATE_LABEL[entry.state] || entry.state)));
        if (entry.version) line.appendChild(el('span', 'r6-meta mono', ' ' + entry.version));
        if (entry.publishedAt) {
          line.appendChild(el('span', 'r6-meta', ' · 发布于 ' +
            String(entry.publishedAt).replace('T', ' ').slice(0, 19)));
        }
        if (entry.reason) line.appendChild(el('div', 'r6-meta', entry.reason));
        if (entry.attempt) {
          line.appendChild(el('div', 'r6-meta', '最近一次尝试：' +
            (STATE_LABEL[entry.attempt.state] || entry.attempt.state) + ' · ' +
            String(entry.attempt.createdAt || '').replace('T', ' ').slice(0, 19) +
            (entry.attempt.error ? ' · 错误 ' + entry.attempt.error : '')));
        }
        if (entry.pins && entry.pins.length) {
          line.appendChild(el('div', 'r6-meta', entry.pins.length +
            ' 个活动引用（pin）仍读取该版本；清理后旧版本保留到引用过期。'));
        }
        box.appendChild(line);
      });
      td.appendChild(box);
      tr.appendChild(td);
      return tr;
    }

    /* ------------------------------------------------------------------ dialog */
    function confirmAction(item, action) {
      state.dialog = { action: action, items: [item] };
      openDialog();
    }

    function batch(action) {
      var items = Object.keys(state.selected).map(function (key) { return state.selected[key]; });
      if (!items.length) return;
      state.dialog = { action: action, items: items };
      openDialog();
    }

    function openDialog() {
      parts.dialog.hidden = false;
      parts.dialog.innerHTML = '';
      var dialog = state.dialog;
      var targets = dialog.items;
      var head = el('div', 'cm-dialog-head');
      head.append(el('strong', null, dialog.action === 'rebuild' ? '重建解析缓存' : '清理派生缓存'),
        el('span', 'r6-meta', targets.length + ' 个文件'));
      var body = el('div', 'cm-dialog-body');
      var list = el('ul', 'cm-dialog-list');
      targets.forEach(function (item) {
        var layers = dialog.action === 'rebuild'
          ? ['rules', 'search']
          : (item.purgeable || []);
        list.appendChild(el('li', null, item.displayName + ' · ' +
          (layers.length ? layers.map(function (layer) {
            var label = LAYERS.filter(function (pair) { return pair[0] === layer; })[0];
            return label ? label[1] : layer;
          }).join(' + ') : '没有可清理的派生层')));
      });
      body.appendChild(list);
      var explain = el('p', 'r6-hint', dialog.action === 'rebuild'
        ? '重建复用结构层：不会重新分帧原文；新版本完成前，当前版本继续可读。'
        : '清理只移除派生层指针，原文与索引分片保留；活动引用过期后旧版本才会被回收。');
      body.appendChild(explain);
      var error = el('div', 'r6-errorbox');
      error.hidden = true;
      error.setAttribute('role', 'alert');
      var actions = el('div', 'r6-dialog-actions');
      var cancel = el('button', 'r6-icon', '取消');
      cancel.type = 'button';
      cancel.addEventListener('click', closeDialog);
      var submit = el('button', 'r6-button is-primary',
        dialog.action === 'rebuild' ? '开始重建' : '确认清理');
      submit.type = 'button';
      submit.addEventListener('click', function () {
        submit.disabled = true;
        runAction(dialog, error).then(function () {
          closeDialog();
          state.selected = {};
          return load(true);
        }, function (failure) {
          submit.disabled = false;
          error.hidden = false;
          error.textContent = failure.message || String(failure);
        });
      });
      actions.append(cancel, submit);
      parts.dialog.append(head, body, error, actions);
      submit.focus();
    }

    function closeDialog() {
      parts.dialog.hidden = true;
      parts.dialog.innerHTML = '';
      state.dialog = null;
    }

    function runAction(dialog, errorBox) {
      var sequence = Promise.resolve();
      var done = [];
      dialog.items.forEach(function (item) {
        sequence = sequence.then(function () {
          if (dialog.action === 'rebuild') {
            return api.post('/api/v2/cache/rebuild', {
              fileId: item.fileId, layers: ['rules', 'search'],
              actionId: 'cache-rebuild:' + item.fileId + ':' + Date.now().toString(36)
            }).then(function (value) { done.push({ file: item.displayName, job: value.item.id }); });
          }
          var layers = item.purgeable || [];
          if (!layers.length) return null;
          return api.post('/api/v2/cache/purge', {
            fileId: item.fileId, layers: layers, confirm: 'purge-derived'
          }).then(function (value) { done.push({ file: item.displayName, removed: value.removed.length }); });
        });
      });
      return sequence.then(function () {
        setNotice(dialog.action === 'rebuild'
          ? '已排队 ' + done.length + ' 个重建任务，进度见日志页任务面板。'
          : '已清理 ' + done.length + ' 个文件的派生缓存；原文保留。', 'ok');
        onStatus(dialog.action === 'rebuild' ? '缓存重建已排队' : '已清理派生缓存');
        return done;
      }, function (failure) {
        if (errorBox) errorBox.hidden = false;
        setNotice('部分操作失败：' + (failure.message || failure), 'error');
        throw failure;
      });
    }

    return {
      mount: mount,
      show: function () { return load(true); },
      load: load,
      state: state,
      parts: parts
    };
  }

  return { create: create, STATE_LABEL: STATE_LABEL, LAYERS: LAYERS };
});
