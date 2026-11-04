/*
 * cache-meter.js — the header's cache meter and its quota dialog.
 *
 * Why this is not inside the cache page: the number has to be visible while the
 * operator is doing something else (importing, reading, deleting). A meter you
 * must navigate to is a meter nobody watches, and the whole point of a quota is
 * that someone notices before the disk does.
 *
 * Two rules carried over from the cache page:
 *
 * * the meter never blocks anything. Exceeding the quota turns it amber and
 *   says so; the server keeps indexing, because a full cache is a cost problem,
 *   not a correctness problem.
 * * the dialog edits one number (bytes) and shows where the bytes went. It does
 *   not offer "clean everything" — the cache page already owns that action, and
 *   a destructive batch button in the header is a misclick waiting to happen.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBCacheMeter = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LAYER_LABEL = { structure: '结构', rules: '规则', search: '检索' };
  var PRESETS = [256, 512, 1024, 2048];

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /** Bytes for humans, with the unit the number actually needs. */
  function bytes(value) {
    var number = Number(value);
    if (!isFinite(number) || number <= 0) return '0 B';
    if (number < 1024) return number + ' B';
    if (number < 1024 * 1024) return (number / 1024).toFixed(0) + ' KB';
    if (number < 1024 * 1024 * 1024) return (number / (1024 * 1024)).toFixed(1) + ' MB';
    return (number / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  function create(options) {
    options = options || {};
    var api = options.api;
    var onStatus = options.onStatus || function () {};
    var onOpenCachePage = options.onOpenCachePage || function () {};

    var state = {
      usage: null,
      admin: false,
      loading: false,
      error: null,
      dialogOpen: false,
      timer: null,
      saving: false
    };
    var parts = {};
    var lastFocus = null;
    var host = null;

    /* ------------------------------------------------------------------ mount */
    function mount(button) {
      parts.button = button;
      parts.label = document.getElementById('cache-meter-value');
      parts.fill = document.getElementById('cache-meter-fill');
      host = document.getElementById('app') || document.body;
      button.addEventListener('click', openDialog);
      document.addEventListener('keydown', onKeydown);
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !state.dialogOpen) refresh();
      });
      loadIdentity().then(refresh, refresh);
      // One minute is deliberate: a faster poll would be noise on a number that
      // changes only when an index job finishes.
      state.timer = setInterval(function () {
        if (document.visibilityState === 'visible' && !state.dialogOpen) refresh();
      }, 60000);
      return button;
    }

    function loadIdentity() {
      return api.get('/api/v1/me').then(function (value) {
        api.setCsrf(value.csrfToken || '');
        state.admin = !!(value.capabilities && value.capabilities.admin);
        return value;
      }, function () {
        state.admin = false;
        return null;
      });
    }

    /* ------------------------------------------------------------------ meter */
    function refresh() {
      if (state.loading) return Promise.resolve(state.usage);
      state.loading = true;
      return api.get('/api/v2/cache/usage').then(function (value) {
        state.loading = false;
        state.usage = value;
        state.error = null;
        renderMeter();
        if (state.dialogOpen) renderDialog();
        return value;
      }, function (error) {
        state.loading = false;
        // 403 is not an error the meter should shout about: a viewer keeps seeing
        // "—" rather than a red number they cannot act on.
        state.error = error;
        renderMeter();
        return null;
      });
    }

    function renderMeter() {
      if (!parts.button) return;
      var usage = state.usage;
      var button = parts.button;
      if (!usage) {
        button.dataset.state = state.error ? 'unknown' : 'idle';
        if (parts.label) parts.label.textContent = '—';
        if (parts.fill) parts.fill.style.width = '0%';
        button.title = state.error
          ? ('缓存状态不可用：' + (state.error.message || state.error))
          : '解析缓存占用；点击设置限额';
        button.setAttribute('aria-label', button.title);
        return;
      }
      var used = usage.usedBytes || 0;
      var quota = usage.quotaBytes;
      var unlimited = !!usage.unlimited;
      var ratio = unlimited ? 0 : Math.min(1, used / (quota || 1));
      var over = !!usage.overQuota;
      button.dataset.state = over ? 'over' : 'ok';
      if (parts.label) {
        parts.label.textContent = unlimited
          ? bytes(used) + ' / 不限'
          : bytes(used) + ' / ' + bytes(quota);
      }
      if (parts.fill) {
        // A cache that is empty still shows a hairline: a bar at 0% reads as
        // "broken", a 2px sliver reads as "nothing cached yet".
        parts.fill.style.width = unlimited ? '0%' : Math.max(2, ratio * 100) + '%';
      }
      button.title = unlimited
        ? ('解析缓存已使用 ' + bytes(used) + '，当前不限制；点击设置限额')
        : ('解析缓存已使用 ' + bytes(used) + '，限额 ' + bytes(quota) +
           '（' + Math.round(ratio * 100) + '%' + (over ? '，已超出' : '') + '）；点击设置限额');
      button.setAttribute('aria-label', button.title);
    }

    /* ----------------------------------------------------------------- dialog */
    function onKeydown(event) {
      if (event.key === 'Escape' && state.dialogOpen) closeDialog();
    }

    function openDialog() {
      if (!parts.button) return;
      state.dialogOpen = true;
      lastFocus = document.activeElement;
      if (!parts.dialog) {
        parts.dialog = el('div', 'r6-dialog cm-dialog cache-quota-dialog');
        parts.dialog.setAttribute('role', 'dialog');
        parts.dialog.setAttribute('aria-modal', 'true');
        parts.dialog.setAttribute('aria-label', '缓存空间');
        parts.dialog.hidden = true;
        host.appendChild(parts.dialog);
      }
      parts.dialog.hidden = false;
      renderDialog();
      refresh();
    }

    function closeDialog() {
      state.dialogOpen = false;
      if (parts.dialog) {
        parts.dialog.hidden = true;
        parts.dialog.innerHTML = '';
      }
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    function renderDialog() {
      var dialog = parts.dialog;
      if (!dialog) return;
      dialog.innerHTML = '';
      var usage = state.usage;

      var head = el('div', 'cm-dialog-head');
      head.append(el('strong', null, '缓存空间'),
        el('span', 'r6-meta', usage ? (usage.files || 0) + ' 个文件有派生层' : '读取中…'));
      var close = el('button', 'r6-icon', '✕');
      close.type = 'button';
      close.setAttribute('aria-label', '关闭');
      close.addEventListener('click', closeDialog);
      head.appendChild(close);

      var body = el('div', 'cm-dialog-body');
      body.appendChild(summaryBlock(usage));
      if (usage) {
        body.appendChild(layerTable(usage));
        body.appendChild(el('p', 'r6-hint',
          '派生层由原文重建，不计入原文本身。超出限额只提示，不会中断索引任务。'));
      }
      body.appendChild(quotaForm(usage));

      var error = el('div', 'r6-errorbox');
      error.hidden = true;
      error.setAttribute('role', 'alert');
      parts.errorBox = error;

      var actions = el('div', 'r6-dialog-actions');
      var toPage = el('button', 'r6-icon', '打开解析缓存');
      toPage.type = 'button';
      toPage.addEventListener('click', function () {
        closeDialog();
        onOpenCachePage();
      });
      var done = el('button', 'r6-button', '关闭');
      done.type = 'button';
      done.addEventListener('click', closeDialog);
      actions.append(toPage, el('span', 'spacer'), done);

      dialog.append(head, body, error, actions);
      dialog.setAttribute('aria-describedby', 'cache-quota-summary');
    }

    function summaryBlock(usage) {
      var wrap = el('div', 'cache-quota-summary');
      wrap.id = 'cache-quota-summary';
      var figure = el('div', 'cache-quota-figure');
      var used = usage ? usage.usedBytes || 0 : 0;
      var unlimited = usage ? !!usage.unlimited : false;
      var quota = usage ? usage.quotaBytes : null;
      figure.append(el('strong', null, bytes(used)),
        el('span', 'r6-meta', unlimited ? '/ 不限' : '/ ' + bytes(quota)));
      wrap.appendChild(figure);

      var track = el('div', 'cache-quota-track');
      var fill = el('span', 'cache-quota-fill');
      var ratio = unlimited || !quota ? 0 : Math.min(1, used / quota);
      fill.style.width = unlimited ? '0%' : Math.max(1, ratio * 100) + '%';
      track.appendChild(fill);
      wrap.appendChild(track);

      var note = el('p', 'r6-meta',
        !usage ? '读取缓存状态…'
          : unlimited ? '当前未设置限额。'
            : (usage.overQuota
              ? '已超出限额 ' + bytes(used - quota) + '：建议清理不再需要的派生层。'
              : '剩余 ' + bytes(Math.max(0, quota - used)) + '。'));
      if (usage && usage.overQuota) note.dataset.tone = 'warn';
      wrap.appendChild(note);
      return wrap;
    }

    function layerTable(usage) {
      var table = el('table', 'r6-table cache-quota-layers');
      var head = el('thead');
      var headRow = el('tr');
      ['层', '占用', '版本', '已发布'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var body = el('tbody');
      ['structure', 'rules', 'search'].forEach(function (layer) {
        var entry = (usage.layers || {})[layer] || { bytes: 0, versions: 0, publishedBytes: 0 };
        var row = el('tr');
        row.append(el('td', null, LAYER_LABEL[layer] || layer),
          el('td', 'mono', bytes(entry.bytes)),
          el('td', 'mono', String(entry.versions || 0)),
          el('td', 'mono', bytes(entry.publishedBytes)));
        body.appendChild(row);
      });
      table.append(head, body);
      return table;
    }

    function quotaForm(usage) {
      var form = el('div', 'cache-quota-form');
      var label = el('label', 'cache-quota-label');
      label.appendChild(el('span', null, '限额'));
      var input = el('input', 'r6-input');
      input.type = 'number';
      input.min = '16';
      input.step = '16';
      input.id = 'cache-quota-input';
      input.setAttribute('aria-describedby', 'cache-quota-unit');
      var unlimitedBox = el('input');
      unlimitedBox.type = 'checkbox';
      unlimitedBox.id = 'cache-quota-unlimited';
      var unlimitedLabel = el('label', 'cache-quota-inline');
      unlimitedLabel.htmlFor = 'cache-quota-unlimited';
      unlimitedLabel.append(unlimitedBox, el('span', null, '不限制'));
      var unit = el('span', 'r6-meta', 'MiB');
      unit.id = 'cache-quota-unit';
      label.append(input, unit);
      form.appendChild(label);

      var current = usage ? usage.quotaBytes : null;
      if (usage && !usage.unlimited && current) input.value = String(Math.round(current / 1048576));
      unlimitedBox.checked = !!(usage && usage.unlimited);
      input.disabled = !state.admin || unlimitedBox.checked;
      unlimitedBox.disabled = !state.admin;

      var presets = el('div', 'cache-quota-presets');
      PRESETS.forEach(function (mib) {
        var button = el('button', 'r6-icon', mib >= 1024 ? (mib / 1024) + ' GB' : mib + ' MB');
        button.type = 'button';
        button.disabled = !state.admin;
        button.addEventListener('click', function () {
          unlimitedBox.checked = false;
          input.disabled = !state.admin;
          input.value = String(mib);
          input.focus();
        });
        presets.appendChild(button);
      });
      presets.appendChild(unlimitedLabel);
      form.appendChild(presets);

      unlimitedBox.addEventListener('change', function () {
        input.disabled = !state.admin || unlimitedBox.checked;
        if (unlimitedBox.checked) input.value = '';
      });

      var save = el('button', 'r6-button is-primary', '保存限额');
      save.type = 'button';
      save.id = 'cache-quota-save';
      save.disabled = !state.admin;
      save.addEventListener('click', function () {
        var value = unlimitedBox.checked ? null : Number(input.value) * 1048576;
        if (!unlimitedBox.checked && (!isFinite(value) || value < 16 * 1048576)) {
          showError('限额至少 16 MiB；如需不限，请勾选「不限制」。');
          return;
        }
        save.disabled = true;
        state.saving = true;
        api.put('/api/v2/cache/quota', { quotaBytes: value }).then(function () {
          state.saving = false;
          save.disabled = false;
          onStatus(value == null ? '缓存限额已设为不限' : '缓存限额已保存：' + bytes(value));
          return refresh();
        }, function (error) {
          state.saving = false;
          save.disabled = false;
          showError(error.message || String(error));
        });
      });
      form.appendChild(save);
      if (!state.admin) {
        form.appendChild(el('p', 'r6-hint', '只有管理员可以修改限额；当前身份为只读。'));
      }
      parts.input = input;
      parts.unlimited = unlimitedBox;
      return form;
    }

    function showError(message) {
      if (!parts.errorBox) return;
      parts.errorBox.hidden = false;
      parts.errorBox.textContent = message;
    }

    return {
      mount: mount,
      refresh: refresh,
      openDialog: openDialog,
      closeDialog: closeDialog,
      state: state
    };
  }

  return { create: create, bytes: bytes };
});
