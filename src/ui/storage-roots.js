/*
 * storage-roots.js — 存储位置管理 (R6-056, backend R6-052/053).
 *
 * The page is an administrator surface, so it says so when the viewer is not one:
 * the table is still readable (no paths for non-admins), every mutation is
 * disabled, and the API would refuse the write anyway.
 *
 * The table deliberately shows the *result of the last probe* rather than a green
 * dot: "online" here means "the service account could write a byte, the volume id
 * matches what was registered, and the sentinel is present", which is the only
 * claim worth making.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBStorageRoots = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

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

  var KIND_LABEL = { shared_raw: '共享原文', private_raw: '私有原文', index: '索引' };
  var STATE_LABEL = { online: '在线', offline: '离线', unauthorized: '无写权限', disabled: '已停用', unknown: '未探测' };

  function create(options) {
    options = options || {};
    var api = options.api;
    var host = null;
    var parts = {};
    var state = { items: [], admin: false, loading: false, error: null, editing: null };

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.classList.add('storage-root-view');

      var bar = el('div', 'r6-toolbar');
      parts.summary = el('span', 'r6-meta', '存储位置');
      parts.add = el('button', 'r6-button', '添加存储位置');
      parts.add.type = 'button';
      parts.add.addEventListener('click', function () { openDialog(null); });
      parts.reload = el('button', 'r6-icon', '重新探测全部');
      parts.reload.type = 'button';
      parts.reload.addEventListener('click', function () { probeAll(); });
      bar.append(parts.summary, el('span', 'spacer'), parts.reload, parts.add);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'alert');
      parts.note.hidden = true;

      parts.scroll = el('div', 'r6-table-scroll storage-scroll');
      parts.table = el('table', 'r6-table storage-table');
      parts.scroll.appendChild(parts.table);

      parts.foot = el('div', 'r6-foot');
      parts.footText = el('span', 'r6-meta',
        '登记只保存路径与身份，不修改系统权限、不执行挂载；日志库由多个物理根合并。');

      parts.dialog = el('div', 'r6-dialog storage-dialog');
      parts.dialog.setAttribute('role', 'dialog');
      parts.dialog.setAttribute('aria-modal', 'true');
      parts.dialog.setAttribute('aria-label', '添加存储位置');
      parts.dialog.hidden = true;

      parts.foot.appendChild(parts.footText);
      host.append(bar, parts.note, parts.scroll, parts.foot, parts.dialog);

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

    function load() {
      state.loading = true;
      return api.get('/api/v1/me').then(function (value) {
        api.setCsrf(value.csrfToken || '');
        state.admin = !!(value.capabilities && value.capabilities.admin);
        return api.get('/api/v2/storage-roots');
      }).then(function (value) {
        state.items = value.items || [];
        state.summary = value.summary || {};
        state.loading = false;
        render();
      }, function (error) {
        state.loading = false;
        state.items = [];
        render();
        setNotice(error.status === 403
          ? '存储位置需要管理员权限；共享目录仍可在文件库中只读浏览。'
          : ('无法读取存储位置：' + (error.message || error)), error.status === 403 ? 'warn' : 'error');
      });
    }

    function render() {
      parts.add.disabled = !state.admin;
      parts.reload.disabled = !state.admin;
      parts.summary.textContent = state.items.length
        ? state.items.length + ' 个存储位置 · ' + (state.summary.online || 0) + ' 在线 · ' +
          (state.summary.offline || 0) + ' 离线'
        : '尚未登记存储位置';
      var head = el('thead');
      var row = el('tr');
      ['存储位置', '物理目录', '用途 / 状态', '容量', '文件 / 已索引', '操作'].forEach(function (title) {
        row.appendChild(el('th', null, title));
      });
      head.appendChild(row);
      var body = el('tbody');
      if (!state.items.length) {
        var tr = el('tr');
        var td = el('td', 'lib-empty-cell');
        td.colSpan = 6;
        td.append(el('strong', null, '只有部署数据目录'),
          el('p', null, '默认已把 data/ 登记为 legacy 根，原有文件与绑定保持不变；外部磁盘或共享目录需要管理员显式添加。'));
        tr.appendChild(td);
        body.appendChild(tr);
      }
      state.items.forEach(function (item) {
        var tr = el('tr');
        var nameCell = el('td');
        nameCell.append(el('div', 'lib-root-name', item.label),
          el('div', 'r6-meta mono', item.root_id));
        var pathCell = el('td');
        var path = el('div', 'mono lib-path');
        path.textContent = item.path;
        path.title = item.path;
        pathCell.appendChild(path);
        if (item.fs_type) pathCell.appendChild(el('div', 'r6-meta', '文件系统：' + item.fs_type));
        var stateCell = el('td');
        var badge = el('span', 'r6-badge', STATE_LABEL[item.state] || item.state);
        badge.classList.add('is-' + (item.state === 'online' ? 'good'
          : (item.state === 'offline' ? 'bad' : 'warn')));
        stateCell.append(badge, el('div', 'r6-meta', KIND_LABEL[item.kind] || item.kind));
        if (item.state !== 'online' && item.reason) {
          stateCell.appendChild(el('div', 'r6-meta', '原因：' + item.reason));
        }
        var capacity = el('td');
        capacity.append(el('div', 'mono', '可用 ' + bytes(item.free_bytes)),
          el('div', 'r6-meta', '配额 ' + (item.quota_bytes ? bytes(item.quota_bytes) : '不限')));
        var counts = el('td');
        counts.append(el('div', 'mono', item.files + ' 个文件'),
          el('div', 'r6-meta', item.indexed + ' 个已索引 · ' + bytes(item.bytes)));
        var ops = el('td');
        var opsWrap = el('div', 'r6-inline-actions');
        var probe = el('button', 'r6-icon', '探测');
        probe.type = 'button';
        probe.disabled = !state.admin;
        probe.title = '以服务账号写入并删除一个临时文件，复核挂载身份与剩余空间';
        probe.addEventListener('click', function () { probe(item.root_id, probe); });
        var edit = el('button', 'r6-icon', '设置');
        edit.type = 'button';
        edit.disabled = !state.admin;
        edit.addEventListener('click', function () { openDialog(item); });
        opsWrap.append(probe, edit);
        ops.appendChild(opsWrap);
        tr.append(nameCell, pathCell, stateCell, capacity, counts, ops);
        body.appendChild(tr);
      });
      parts.table.innerHTML = '';
      parts.table.append(head, body);
    }

    function probe(rootId, button) {
      button.disabled = true;
      button.textContent = '探测中';
      api.post('/api/v2/storage-roots/' + encodeURIComponent(rootId) + '/probe', {}).then(function (value) {
        var item = value.item || {};
        setNotice(item.writable
          ? '探测通过：可写入，挂载身份一致。'
          : ('探测未通过：' + (item.reason || 'unknown') + '，已停止对该位置的写入。'),
          item.writable ? 'ok' : 'warn');
        return load();
      }, function (error) {
        button.disabled = false;
        button.textContent = '探测';
        setNotice(error.message || '探测失败', 'error');
      });
    }

    function probeAll() {
      parts.reload.disabled = true;
      var chain = Promise.resolve();
      state.items.forEach(function (item) {
        chain = chain.then(function () {
          return api.post('/api/v2/storage-roots/' + encodeURIComponent(item.root_id) + '/probe', {})
            .catch(function () { return null; });
        });
      });
      chain.then(function () {
        parts.reload.disabled = false;
        return load();
      });
    }

    /* ----------------------------------------------------------------- dialog */
    function openDialog(item) {
      state.editing = item;
      parts.dialog.hidden = false;
      parts.dialog.innerHTML = '';
      var form = el('div', 'r6-form');
      var title = el('strong', null, item ? '存储位置设置' : '添加存储位置');
      var labelField = el('div', 'field');
      var labelInput = el('input', 'input');
      labelInput.id = 'storage-root-label';
      labelInput.value = item ? item.label : '';
      labelInput.maxLength = 80;
      var labelTag = el('label', 'label', '位置名称');
      labelTag.setAttribute('for', labelInput.id);
      labelField.append(labelTag, labelInput);

      var pathField = el('div', 'field');
      var pathInput = el('input', 'input mono');
      pathInput.id = 'storage-root-path';
      pathInput.value = item ? item.path : '';
      pathInput.placeholder = '/log/Logbench/archive';
      if (item) pathInput.readOnly = true;
      var pathTag = el('label', 'label', '服务器绝对路径');
      pathTag.setAttribute('for', pathInput.id);
      pathField.append(pathTag, pathInput,
        el('p', 'hint', '修改已有位置的物理目录需要单独迁移，不在此处直接重定向。'));

      var kindField = el('div', 'field');
      var kindSelect = el('select', 'select');
      kindSelect.id = 'storage-root-kind';
      [['shared_raw', '共享原文（进入总库）'], ['private_raw', '私有原文（仅上传者可见）'],
       ['index', '索引（本地文件系统）']].forEach(function (pair) {
        var option = el('option', null, pair[1]);
        option.value = pair[0];
        if (item && item.kind === pair[0]) option.selected = true;
        kindSelect.appendChild(option);
      });
      kindSelect.disabled = !!item && item.kind === 'shared_raw' && item.files > 0;
      var kindTag = el('label', 'label', '用途');
      kindTag.setAttribute('for', kindSelect.id);
      kindField.append(kindTag, kindSelect);

      var quotaField = el('div', 'field');
      var quotaInput = el('input', 'input mono');
      quotaInput.id = 'storage-root-quota';
      quotaInput.type = 'number';
      quotaInput.min = '0';
      quotaInput.value = String(item && item.quota_bytes ? item.quota_bytes : 0);
      var quotaTag = el('label', 'label', '配额（字节，0 表示不限）');
      quotaTag.setAttribute('for', quotaInput.id);
      quotaField.append(quotaTag, quotaInput);

      var error = el('div', 'r6-errorbox');
      error.setAttribute('role', 'alert');
      error.hidden = true;

      var actions = el('div', 'r6-dialog-actions');
      var cancel = el('button', 'r6-icon', '取消');
      cancel.type = 'button';
      cancel.addEventListener('click', closeDialog);
      var submit = el('button', 'r6-button', item ? '保存设置' : '登记位置');
      submit.type = 'button';
      submit.disabled = !state.admin;
      submit.addEventListener('click', function () {
        error.hidden = true;
        submit.disabled = true;
        var body = {
          label: labelInput.value.trim(),
          kind: kindSelect.value,
          quotaBytes: Number(quotaInput.value || 0)
        };
        var work;
        if (item) {
          body.expectedRevision = item.revision;
          if (item.path !== pathInput.value.trim()) body.path = pathInput.value.trim();
          work = api.patch('/api/v2/storage-roots/' + encodeURIComponent(item.root_id), body);
        } else {
          body.path = pathInput.value.trim();
          work = api.post('/api/v2/storage-roots', body);
        }
        work.then(function () {
          closeDialog();
          setNotice(item ? '已更新存储位置登记' : '已登记存储位置，预检通过', 'ok');
          return load();
        }, function (failure) {
          submit.disabled = false;
          error.hidden = false;
          error.textContent = failure.message || '保存失败';
          if (failure.code === 'root_path_migration_required') {
            error.textContent += '（已有文件登记，请建立迁移任务）';
          }
          labelInput.focus();
        });
      });
      actions.append(cancel, submit);
      form.append(title, labelField, pathField, kindField, quotaField, error, actions);
      parts.dialog.appendChild(form);
      labelInput.focus();
    }

    function closeDialog() {
      parts.dialog.hidden = true;
      parts.dialog.innerHTML = '';
      state.editing = null;
    }

    return {
      mount: mount,
      show: function () { return load(); },
      load: load,
      state: state,
      parts: parts
    };
  }

  return { create: create };
});
