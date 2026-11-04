/*
 * library.js — the unified file library (R6-056).
 *
 * Three sources, one surface:
 *   本机      files the browser holds (never uploaded, never on the server)
 *   总库      shared raw roots merged into one logical tree
 *   私有      the current identity's private rows, and nothing else
 *
 * What this view refuses to do:
 *   - show "0 个文件" for a root that is merely offline. An unmounted root keeps
 *     its registration, and the view says so with the reason;
 *   - read log content to build the tree. Browsing stats entries; a file opens
 *     only when the user asks for it;
 *   - offer a write button to a viewer. The API still authorises every request,
 *     the hidden button is only the honest UI half of it;
 *   - lose a rule draft on 409. The select keeps the value the user chose and the
 *     conflict is explained next to it.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBLibrary = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var FAVOURITES = 'logbench-r6-library-favourites';
  var PAGE = 100;

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

  function relativeTime(iso) {
    if (!iso) return '—';
    var then = Date.parse(iso);
    if (isNaN(then)) return iso;
    var delta = Math.round((Date.now() - then) / 1000);
    if (delta < 60) return '刚刚';
    if (delta < 3600) return Math.round(delta / 60) + ' 分钟前';
    if (delta < 86400) return Math.round(delta / 3600) + ' 小时前';
    return Math.round(delta / 86400) + ' 天前';
  }

  function coverageLabel(file) {
    if (file.state === 'missing') return { text: '原文缺失', tone: 'warn' };
    if (file.coverage === 'search') return { text: '可检索', tone: 'good' };
    if (file.coverage === 'rules') return { text: '规则已算', tone: 'quiet' };
    if (file.coverage === 'structure') return { text: '结构索引', tone: 'quiet' };
    return { text: '未建索引', tone: '' };
  }

  var LAYER_LABEL = {
    personal_file: '我的单文件', personal_directory: '我的目录',
    shared_file: '共享单文件', shared_directory: '共享目录',
    mapping_default: '映射默认', service_default: '服务默认', none: '未匹配'
  };

  function readFavourites() {
    try {
      var parsed = JSON.parse(window.localStorage.getItem(FAVOURITES) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function writeFavourites(list) {
    try { window.localStorage.setItem(FAVOURITES, JSON.stringify(list.slice(0, 200))); }
    catch (error) { /* a full quota must not break the library */ }
  }

  function create(options) {
    function ruleBarTitle() {
      var node = el('span', 'lib-rule-title', '目录规则');
      return node;
    }

    options = options || {};
    var api = options.api;
    var onStatus = options.onStatus || function () {};
    var localFiles = options.localFiles || function () { return Promise.resolve([]); };
    var onOpenLocal = options.onOpenLocal || function () {};

    var state = {
      scope: 'shared',
      rootId: '',
      path: '',
      q: '',
      loading: false,
      rows: [],
      cursor: null,
      sources: [],
      tree: {},
      expanded: {},
      treePending: {},
      selectedDir: '',
      ruleDraft: null,
      readOnly: true,
      admin: false,
      trashOpen: false,
      error: null,
      notice: null,
      favourites: readFavourites()
    };
    var host = null;
    var parts = {};
    var rules = [];

    /* ------------------------------------------------------------- identity */
    function loadIdentity() {
      return api.get('/api/v1/me').then(function (value) {
        api.setCsrf(value.csrfToken || '');
        state.admin = !!(value.capabilities && value.capabilities.admin);
        state.readOnly = !state.admin;
        return value;
      }, function () {
        // The page can still read the shared library as a viewer; saying so is
        // better than pretending the whole view is offline.
        state.admin = false;
        state.readOnly = true;
        return null;
      });
    }

    function loadRules() {
      return api.get('/api/v1/rules').then(function (value) {
        rules = (value.items || []).map(function (item) {
          return { id: item.id, name: item.name, revision: item.revision };
        });
      }, function () { rules = []; });
    }

    /* ----------------------------------------------------------------- shell */
    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.classList.add('lib-root');

      var shell = el('div', 'library-shell');
      var pane = el('aside', 'folder-pane');
      pane.setAttribute('aria-label', '文件来源');
      parts.scopeList = el('div', 'folder-list');
      parts.scopeList.setAttribute('role', 'tablist');
      [['local', '本机文件'], ['shared', '服务器总库'], ['private', '服务器私有']]
        .forEach(function (pair) {
          var button = el('button', 'folder-item', pair[1]);
          button.type = 'button';
          button.dataset.scope = pair[0];
          button.setAttribute('role', 'tab');
          button.addEventListener('click', function () { setScope(pair[0]); });
          parts.scopeList.appendChild(button);
        });
      parts.tree = el('div', 'folder-tree');
      parts.tree.setAttribute('role', 'tree');
      parts.tree.setAttribute('aria-label', '目录');
      var foot = el('div', 'folder-foot');
      parts.footNote = el('span', 'r6-meta', '选择来源以浏览');
      foot.appendChild(parts.footNote);
      pane.append(el('div', 'folder-title', '文件来源'), parts.scopeList, parts.tree, foot);

      var main = el('div', 'library-main');
      var bars = el('div', 'lib-bars');
      var bar = el('div', 'r6-filebar lib-bar-main');
      parts.crumb = el('div', 'lib-crumb');
      parts.crumb.setAttribute('role', 'navigation');
      parts.crumb.setAttribute('aria-label', '当前目录');
      parts.search = el('input', 'lib-search');
      parts.search.type = 'search';
      parts.search.placeholder = '搜索文件名或路径';
      parts.search.setAttribute('aria-label', '搜索文件');
      parts.search.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') { state.q = parts.search.value.trim(); refresh(true); }
        if (event.key === 'Escape') { parts.search.value = ''; state.q = ''; refresh(true); }
      });
      parts.search.addEventListener('search', function () {
        state.q = parts.search.value.trim();
        refresh(true);
      });
      parts.trash = el('button', 'r6-icon', '回收站');
      parts.trash.type = 'button';
      parts.trash.addEventListener('click', function () { toggleTrash(); });
      parts.refresh = el('button', 'r6-icon', '刷新');
      parts.refresh.type = 'button';
      parts.refresh.addEventListener('click', function () { refresh(true); });
      bar.append(parts.crumb, parts.search, parts.trash, parts.refresh);

      // Rule controls get their own compact row: seven controls on one line forced
      // the labels to wrap onto three lines at 1366px, which is how the previous
      // single-row version looked broken.
      var ruleBar = el('div', 'r6-filebar lib-bar-rule');
      parts.ruleSelect = el('select', 'select lib-rule-select');
      parts.ruleSelect.setAttribute('aria-label', '目录默认规则');
      parts.ruleSelect.disabled = true;
      parts.ruleSelect.addEventListener('change', function () {
        state.ruleDraft = parts.ruleSelect.value || null;
        parts.saveRule.disabled = state.readOnly || !state.selectedDir;
        parts.saveMine.disabled = state.readOnly || !state.selectedDir;
      });
      parts.saveRule = el('button', 'r6-icon', '保存共享规则');
      parts.saveRule.type = 'button';
      parts.saveRule.disabled = true;
      parts.saveRule.addEventListener('click', function () { saveDirectoryRule('shared'); });
      parts.saveMine = el('button', 'r6-icon', '仅我');
      parts.saveMine.type = 'button';
      parts.saveMine.disabled = true;
      parts.saveMine.title = '只对本人生效，其他用户仍使用共享规则';
      parts.saveMine.addEventListener('click', function () { saveDirectoryRule('personal'); });
      parts.clearRule = el('button', 'r6-icon', '恢复继承');
      parts.clearRule.type = 'button';
      parts.clearRule.disabled = true;
      parts.clearRule.addEventListener('click', function () { clearDirectoryRule(); });
      parts.explain = el('span', 'r6-meta lib-explain', '');
      ruleBar.append(ruleBarTitle(), parts.ruleSelect, parts.saveRule, parts.saveMine,
        parts.clearRule, parts.explain);
      ruleBar.hidden = true;
      parts.ruleBar = ruleBar;
      bars.append(bar, ruleBar);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'alert');
      parts.note.hidden = true;

      parts.scroll = el('div', 'r6-table-scroll lib-scroll');
      parts.table = el('table', 'r6-table lib-table');
      parts.scroll.appendChild(parts.table);

      parts.foot = el('div', 'r6-foot');
      parts.footText = el('span', 'r6-meta', '');
      parts.footMore = el('button', 'r6-icon', '加载更多');
      parts.footMore.type = 'button';
      parts.footMore.hidden = true;
      parts.footMore.addEventListener('click', function () { loadFiles(false); });
      parts.foot.append(parts.footText, el('span', 'spacer'), parts.footMore);

      main.append(bars, parts.note, parts.scroll, parts.foot);
      shell.append(pane, main);
      host.appendChild(shell);

      parts.scopeList.addEventListener('keydown', function (event) {
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        var buttons = Array.prototype.slice.call(parts.scopeList.querySelectorAll('.folder-item'));
        var index = buttons.indexOf(document.activeElement);
        if (index < 0) return;
        event.preventDefault();
        var next = buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length];
        next.focus();
      });
      return api;
    }

    /* ----------------------------------------------------------------- state */
    function setScope(scope) {
      if (state.scope === scope) return Promise.resolve();
      api.reset();
      state.scope = scope;
      state.rootId = '';
      state.path = '';
      state.selectedDir = '';
      state.q = '';
      state.rows = [];
      state.cursor = null;
      state.trashOpen = false;
      if (parts.search) parts.search.value = '';
      renderScopes();
      return refresh(true);
    }

    function setNotice(text, tone) {
      state.notice = text ? { text: text, tone: tone || 'info' } : null;
      if (!parts.note) return;
      parts.note.hidden = !text;
      parts.note.textContent = text || '';
      parts.note.dataset.tone = tone || 'info';
    }

    function setError(error) {
      state.error = error || null;
      if (!error) { setNotice('', ''); return; }
      var message = error.message || String(error);
      if (error.code === 'storage_root_offline') {
        message += ' 目录离线时不会按空库处理，挂载恢复后可继续。';
      }
      setNotice(message, error.code === 'storage_root_offline' ? 'warn' : 'error');
    }

    /* ------------------------------------------------------------------- data */
    function loadSources() {
      return api.get('/api/v2/library/sources').then(function (value) {
        state.sources = value.items || [];
        renderScopes();
        return value;
      }, function (error) {
        setError(error);
        return { items: [] };
      });
    }

    function loadTree(rootId, path, opts) {
      opts = opts || {};
      var key = rootId + ':' + path;
      if (state.tree[key] && !opts.force) return Promise.resolve(state.tree[key]);
      if (state.treePending[key]) return state.treePending[key];
      // The tree call is also the scan: it registers the directory's metadata
      // before answering. The table must therefore wait for it instead of listing
      // a database that has not seen these files yet (that race produced "2 项"
      // for a 180-file directory).
      var pending = api.get('/api/v2/library/tree' + query({
        root: rootId, path: path, scope: state.scope,
        sync: opts.sync === false ? '0' : '1'
      })).then(function (value) {
        state.tree[key] = value;
        delete state.treePending[key];
        return value;
      }, function (error) {
        delete state.treePending[key];
        throw error;
      });
      state.treePending[key] = pending;
      return pending;
    }

    function loadFiles(reset) {
      if (reset) {
        state.rows = [];
        state.cursor = null;
      }
      parts.footMore.disabled = true;
      state.loading = true;
      var params = { scope: state.scope, limit: PAGE };
      if (state.rootId) params.root = state.rootId;
      if (state.q) params.q = state.q;
      if (state.path) params.directory = state.path;
      if (state.cursor) params.cursor = state.cursor;
      return api.get('/api/v2/library' + query(params)).then(function (value) {
        state.loading = false;
        state.rows = state.rows.concat(value.items || []);
        state.cursor = value.nextCursor || null;
        if (value.roots) {
          // Offline is a state of the source, not an empty result.
          value.roots.forEach(function (item) {
            var existing = state.sources.filter(function (row) { return row.rootId === item.root_id; })[0];
            if (existing) { existing.state = item.state; existing.reason = item.reason; }
          });
          renderScopes();
        }
        renderTable();
        parts.footText.textContent = state.q
          ? '搜索到 ' + value.total + ' 项 · 仅显示已登记元数据'
          : '共 ' + value.total + ' 项 · 浏览不读取原文';
        parts.footMore.hidden = !state.cursor;
        parts.footMore.disabled = false;
        return value;
      }, function (error) {
        state.loading = false;
        parts.footMore.disabled = false;
        setError(error);
        renderTable();
        return null;
      });
    }

    function refresh(force) {
      if (!host) return Promise.resolve();
      parts.refresh.disabled = true;
      var work;
      if (state.scope === 'local') {
        work = loadIdentity().then(function () { return localFiles(); }).then(function (files) {
          state.rows = (files || []).map(function (file) {
            return {
              fileId: file.file_id, displayName: file.name, relPath: '本机',
              rootLabel: '本机存储', size: file.size, scope: 'local',
              state: 'active', coverage: file.indexed ? 'search' : 'none',
              rule: null, observedAt: file.updated_at || null
            };
          });
          state.cursor = null;
          renderTable();
          parts.footText.textContent = state.rows.length + ' 个本机文件 · 原文只在浏览器';
          parts.footMore.hidden = true;
          return null;
        });
      } else {
        work = loadIdentity().then(loadRules).then(loadSources).then(function () {
          return loadFiles(true);
        }).then(function () {
          var root = state.sources.filter(function (item) { return item.rootId === state.rootId; })[0];
          if (root && root.state !== 'online') {
            setNotice('存储位置「' + root.label + '」当前离线（' + (root.reason || 'unknown') +
              '）。已保留登记与文件元数据，恢复挂载后可继续。', 'warn');
          }
          return null;
        });
      }
      return work.then(function () {
        parts.refresh.disabled = false;
        renderBar();
      }, function (error) {
        parts.refresh.disabled = false;
        setError(error);
      });
    }

    /* ----------------------------------------------------------------- render */
    function renderScopes() {
      Array.prototype.forEach.call(parts.scopeList.querySelectorAll('.folder-item'), function (button) {
        var active = button.dataset.scope === state.scope;
        button.classList.toggle('selected', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      parts.tree.innerHTML = '';
      if (state.scope === 'local') {
        parts.footNote.textContent = '本机导入不上传';
        return;
      }
      var scope = state.scope === 'private' ? 'private' : 'shared';
      var roots = state.sources.filter(function (item) {
        return scope === 'private' ? item.scope === 'private' : item.scope !== 'private';
      });
      if (!roots.length) {
        parts.footNote.textContent = scope === 'private' ? '没有私有存储位置' : '没有已登记的共享位置';
        return;
      }
      parts.footNote.textContent = roots.length + ' 个存储位置';
      roots.forEach(function (item) {
        var node = el('button', 'folder-item folder-root' + (state.rootId === item.rootId ? ' selected' : ''));
        node.type = 'button';
        node.dataset.root = item.rootId;
        node.setAttribute('role', 'treeitem');
        node.setAttribute('aria-expanded', state.expanded[item.rootId + ':'] ? 'true' : 'false');
        var dot = el('span', 'lib-dot');
        dot.dataset.state = item.state || 'unknown';
        dot.title = item.state === 'online' ? '在线' : ('不可用：' + (item.reason || 'unknown'));
        node.append(dot, el('span', 'lib-root-label', item.label),
          el('span', 'r6-meta', item.state === 'online' ? String(item.files) : '离线'));
        node.addEventListener('click', function () {
          state.rootId = item.rootId;
          state.path = '';
          state.selectedDir = '';
          state.expanded[item.rootId + ':'] = !state.expanded[item.rootId + ':'];
          renderTree();
          renderBar();
          loadTree(item.rootId, '').then(function () { refresh(true); },
            function () { refresh(true); });
        });
        parts.tree.appendChild(node);
        if (state.expanded[item.rootId + ':']) renderChildren(item.rootId, '', 1);
      });
    }

    function renderTree() {
      renderScopes();
    }

    function renderChildren(rootId, path, depth) {
      loadTree(rootId, path).then(function (value) {
        if (!value) return;
        var container = el('div', 'folder-children');
        container.style.paddingLeft = (depth * 12) + 'px';
        if (value.state !== 'online') {
          container.appendChild(el('div', 'lib-offline',
            '离线：' + (value.reason || 'unknown') + '（登记保留）'));
          parts.tree.appendChild(container);
          return;
        }
        if (!(value.items || []).length) {
          container.appendChild(el('div', 'r6-meta lib-tree-empty', '空目录'));
          parts.tree.appendChild(container);
          return;
        }
        value.items.forEach(function (item) {
          if (item.type !== 'directory') return;
          var key = rootId + ':' + item.relPath;
          var button = el('button', 'folder-item folder-dir' + (state.path === item.relPath ? ' selected' : ''));
          button.type = 'button';
          button.setAttribute('role', 'treeitem');
          button.setAttribute('aria-expanded', state.expanded[key] ? 'true' : 'false');
          button.append(el('span', 'lib-dir-name', item.name),
            el('span', 'r6-meta', item.files ? String(item.files) : ''));
          button.title = item.relPath;
          button.addEventListener('click', function () {
            state.path = item.relPath;
            state.selectedDir = item.relPath;
            state.expanded[key] = !state.expanded[key];
            state.ruleDraft = null;
            renderTree();
            renderBar();
            loadTree(rootId, item.relPath).then(function () { refresh(true); },
              function () { refresh(true); });
          });
          container.appendChild(button);
          if (state.expanded[key]) renderChildren(rootId, item.relPath, depth + 1);
        });
        parts.tree.appendChild(container);
      }, function (error) {
        parts.tree.appendChild(el('div', 'lib-offline', error.message || '目录不可读'));
      });
    }

    function renderBar() {
      parts.crumb.innerHTML = '';
      var root = state.sources.filter(function (item) { return item.rootId === state.rootId; })[0];
      var crumb = el('button', 'lib-crumb-item', state.scope === 'local' ? '本机文件'
        : (root ? root.label : '全部位置'));
      crumb.type = 'button';
      crumb.addEventListener('click', function () {
        state.path = '';
        state.selectedDir = '';
        state.rootId = state.scope === 'local' ? '' : (root ? root.rootId : '');
        renderTree(); renderBar();
        if (state.rootId) loadTree(state.rootId, '').then(function () { refresh(true); });
        else refresh(true);
      });
      parts.crumb.appendChild(crumb);
      (state.path || '').split('/').filter(Boolean).forEach(function (part, index, list) {
        var target = list.slice(0, index + 1).join('/');
        parts.crumb.appendChild(el('span', 'lib-crumb-sep', '/'));
        var button = el('button', 'lib-crumb-item', part);
        button.type = 'button';
        button.addEventListener('click', function () {
          state.path = target; state.selectedDir = target;
          renderTree(); renderBar();
          loadTree(state.rootId, target).then(function () { refresh(true); },
            function () { refresh(true); });
        });
        parts.crumb.appendChild(button);
      });
      var canWrite = !state.readOnly && state.scope !== 'local' && !!state.selectedDir;
      parts.ruleBar.hidden = !(state.selectedDir && state.scope !== 'local');
      var titleNode = parts.ruleBar.querySelector('.lib-rule-title');
      if (titleNode) {
        titleNode.textContent = state.selectedDir ? ('目录规则 · ' + state.selectedDir) : '目录规则';
      }
      parts.ruleSelect.disabled = !state.scope || state.scope === 'local' || !state.selectedDir;
      parts.ruleSelect.innerHTML = '';
      parts.ruleSelect.appendChild(el('option', null, state.selectedDir ? '选择目录默认规则…' : '未选择目录'));
      rules.forEach(function (rule) {
        var option = el('option', null, rule.name);
        option.value = rule.id;
        if (state.ruleDraft === rule.id) option.selected = true;
        parts.ruleSelect.appendChild(option);
      });
      parts.saveRule.disabled = !canWrite || !state.ruleDraft;
      parts.saveMine.disabled = !canWrite || !state.ruleDraft;
      parts.clearRule.disabled = !state.selectedDir || state.scope === 'local';
      parts.explain.textContent = state.selectedDir
        ? '目录规则按「我的文件 → 我的目录 → 共享文件 → 共享目录 → 映射默认 → 服务默认」继承'
        : '';
      parts.explain.title = parts.explain.textContent;
      if (state.selectedDir) explainDirectory();
    }

    function explainDirectory() {
      api.get('/api/v2/bindings/effective' + query({ rootId: state.rootId, path: state.selectedDir }))
        .then(function (value) {
          if (!value) return;
          state.effective = value;
          var current = value.rule ? (value.rule.name || value.rule.id) : '无';
          parts.explain.textContent = '当前生效：' + current + '（' + (value.explanation || '') + '）';
          parts.explain.title = parts.explain.textContent;
          if (!state.ruleDraft && value.ruleId) {
            state.ruleDraft = value.ruleId;
            Array.prototype.forEach.call(parts.ruleSelect.options, function (option) {
              option.selected = option.value === value.ruleId;
            });
            parts.saveRule.disabled = state.readOnly;
            parts.saveMine.disabled = state.readOnly;
          }
        }, function () { /* explanation is supplementary */ });
    }

    function actionButton(label, handler, opts) {
      opts = opts || {};
      var button = el('button', 'r6-icon', label);
      button.type = 'button';
      if (opts.title) button.title = opts.title;
      if (opts.danger) button.classList.add('is-danger');
      button.addEventListener('click', handler);
      return button;
    }

    function renderTable() {
      var head = el('thead');
      var headRow = el('tr');
      ['日志 / 路径', '存储位置', '大小', '索引 / 代次', '规则来源', '操作'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var body = el('tbody');
      var rows = state.rows.slice();
      rows.sort(function (left, right) {
        var leftFav = state.favourites.indexOf(left.fileId) >= 0 ? 0 : 1;
        var rightFav = state.favourites.indexOf(right.fileId) >= 0 ? 0 : 1;
        if (leftFav !== rightFav) return leftFav - rightFav;
        return String(left.relPath).localeCompare(String(right.relPath));
      });
      if (!rows.length) {
        parts.table.innerHTML = '';
        parts.table.append(head);
        var empty = el('tbody');
        var tr = el('tr');
        var td = el('td', 'lib-empty-cell');
        td.colSpan = 6;
        var offline = state.sources.filter(function (item) {
          return item.rootId === state.rootId && item.state !== 'online';
        })[0];
        if (offline) {
          td.append(el('strong', null, '存储位置不可用'),
            el('p', null, '原因：' + (offline.reason || 'unknown') + '。登记与文件元数据仍然保留，不会按空库处理。'));
        } else if (state.scope === 'private') {
          td.append(el('strong', null, '没有私有文件'),
            el('p', null, '私有区只对当前身份可见；服务端加速上传的文件会出现在这里。'));
        } else {
          td.append(el('strong', null, '该目录没有已登记文件'),
            el('p', null, '展开目录会按需登记元数据，不读取日志正文。'));
        }
        tr.appendChild(td);
        empty.appendChild(tr);
        parts.table.append(empty);
        return;
      }
      rows.forEach(function (file) {
        var tr = el('tr');
        if (state.favourites.indexOf(file.fileId) >= 0) tr.classList.add('is-pinned');
        var nameCell = el('td');
        var name = el('button', 'r6-link', file.displayName || file.relPath);
        name.type = 'button';
        name.addEventListener('click', function () { openFile(file); });
        var path = el('div', 'r6-meta mono lib-path');
        path.textContent = file.relPath;
        path.title = file.logicalPath || file.relPath;
        nameCell.append(name, path);
        var rootCell = el('td');
        rootCell.append(el('div', null, file.rootLabel || '—'),
          el('div', 'r6-meta', file.scope === 'private' ? '私有' :
            (file.scope === 'local' ? '本机' : '共享')));
        var sizeCell = el('td', 'mono');
        sizeCell.textContent = bytes(file.size);
        var indexCell = el('td');
        var coverage = coverageLabel(file);
        var badge = el('span', 'r6-badge', coverage.text);
        if (coverage.tone) badge.classList.add('is-' + coverage.tone);
        var indexLine = el('div', 'lib-index-line');
        indexLine.append(badge,
          el('span', 'r6-meta mono', 'g' + (file.generation == null ? '—' : file.generation)));
        indexCell.appendChild(indexLine);
        var ruleCell = el('td');
        var ruleName = null;
        if (file.rule && file.rule.ruleId) {
          rules.forEach(function (rule) { if (rule.id === file.rule.ruleId) ruleName = rule.name; });
          ruleName = ruleName || file.rule.ruleId;
        }
        ruleCell.append(el('div', 'lib-rule-name', ruleName || '继承默认'),
          el('div', 'r6-meta', LAYER_LABEL[(file.rule && file.rule.layer) || 'none'] || '—'));
        if (file.rule && file.rule.explanation) ruleCell.title = file.rule.explanation;
        var ops = el('td');
        var opsWrap = el('div', 'r6-inline-actions');
        if (file.scope === 'local') {
          opsWrap.appendChild(actionButton('打开', function () { onOpenLocal(file); }));
        } else {
          opsWrap.appendChild(actionButton('打开', function () { openFile(file); }));
          if (!state.readOnly) {
            opsWrap.appendChild(actionButton('删除', function (event) {
              confirmDelete(file, event.currentTarget);
            }, { danger: true, title: '移入该存储位置的回收站，可恢复' }));
          }
        }
        var star = actionButton(state.favourites.indexOf(file.fileId) >= 0 ? '★' : '☆', function () {
          var index = state.favourites.indexOf(file.fileId);
          if (index >= 0) state.favourites.splice(index, 1);
          else state.favourites.push(file.fileId);
          writeFavourites(state.favourites);
          renderTable();
        }, { title: '收藏置顶（仅本机）' });
        opsWrap.appendChild(star);
        ops.appendChild(opsWrap);
        tr.append(nameCell, rootCell, sizeCell, indexCell, ruleCell, ops);
        body.appendChild(tr);
      });
      parts.table.innerHTML = '';
      parts.table.append(head, body);
    }

    /* ---------------------------------------------------------------- actions */
    function openFile(file) {
      if (file.scope === 'local') { onOpenLocal(file); return; }
      if (file.state === 'missing' || file.available === false) {
        setNotice('该文件当前不可读取：' + (file.state === 'missing' ? '原文缺失' : '存储位置离线'), 'warn');
        return;
      }
      if (options.onOpenServerFile) options.onOpenServerFile(file);
      else onStatus('打开 ' + file.displayName);
    }

    function confirmDelete(file, button) {
      if (button.dataset.confirm === '1') {
        button.disabled = true;
        api.del('/api/v2/library/files/' + encodeURIComponent(file.fileId)).then(function (value) {
          setNotice('已移入回收站：' + file.relPath, 'ok');
          refresh(true);
          return value;
        }, function (error) {
          button.disabled = false;
          if (error.code === 'binding_active') {
            setNotice('该文件仍被规则绑定引用。再次点击「删除」将解除绑定后删除。', 'warn');
            button.dataset.confirm = '2';
            button.textContent = '解除绑定并删除';
          } else if (error.code === 'active_references') {
            setNotice('仍有任务或快照引用该文件，请先结束它们。', 'warn');
          } else {
            setError(error);
          }
        });
        return;
      }
      if (button.dataset.confirm === '2') {
        button.disabled = true;
        api.del('/api/v2/library/files/' + encodeURIComponent(file.fileId) +
          '?detachBindings=1').then(function () {
          setNotice('已解除绑定并删除：' + file.relPath, 'ok');
          refresh(true);
        }, function (error) {
          button.disabled = false;
          setError(error);
        });
        return;
      }
      button.dataset.confirm = '1';
      button.textContent = '确认删除';
      setTimeout(function () {
        if (button.dataset.confirm === '1') {
          button.dataset.confirm = '';
          button.textContent = '删除';
        }
      }, 4000);
    }

    function saveDirectoryRule(scope) {
      var ruleId = state.ruleDraft;
      if (!ruleId || !state.selectedDir) return;
      var revision = currentRevision(scope);
      var body = {
        scope: 'directory', rootId: state.rootId, path: state.selectedDir,
        ruleId: ruleId, expectedRevision: revision
      };
      var path = scope === 'personal' ? '/api/v2/bindings/personal' : '/api/v2/bindings';
      parts.saveRule.disabled = true;
      parts.saveMine.disabled = true;
      api.put(path, body).then(function () {
        setNotice(scope === 'personal' ? '已保存本目录的个人规则' : '已保存共享目录规则', 'ok');
        state.ruleDraft = null;
        state.tree = {};
        refresh(true);
      }, function (error) {
        parts.saveRule.disabled = false;
        parts.saveMine.disabled = false;
        if (error.code === 'REVISION_CONFLICT' || error.code === 'revision_conflict'
            || error.status === 409) {
          // The draft stays selected: losing the choice on a conflict is the
          // behaviour this check exists to prevent.
          setNotice('规则已被其他人修改，草稿已保留。刷新后再提交。', 'warn');
          parts.saveRule.disabled = false;
        } else {
          setError(error);
        }
      });
    }

    function clearDirectoryRule() {
      if (!state.selectedDir) return;
      var body = {
        scope: 'directory', rootId: state.rootId, path: state.selectedDir,
        expectedRevision: currentRevision('personal')
      };
      api.del('/api/v2/bindings/personal', body).then(function () {
        setNotice('已恢复目录继承', 'ok');
        state.ruleDraft = null;
        state.tree = {};
        refresh(true);
      }, function (error) {
        if (error.status === 404 || error.code === 'revision_conflict') {
          setNotice('没有可清除的个人覆盖。', 'warn');
        } else {
          setError(error);
        }
      });
    }

    function currentRevision(scope) {
      var cache = state.effective || {};
      var layer = scope === 'personal' ? 'personal_directory' : 'shared_directory';
      var entry = (cache.chain || []).filter(function (row) {
        return row.layer === layer && row.path === state.selectedDir;
      })[0];
      return entry && entry.revision != null ? entry.revision : 0;
    }

    /* ------------------------------------------------------------------ trash */
    function toggleTrash() {
      state.trashOpen = !state.trashOpen;
      parts.trash.textContent = state.trashOpen ? '返回文件' : '回收站';
      if (!state.trashOpen) { refresh(true); return; }
      api.get('/api/v2/library/trash').then(function (value) {
        renderTrash(value.items || []);
      }, function (error) { setError(error); });
    }

    function renderTrash(items) {
      var head = el('thead');
      var headRow = el('tr');
      ['已删除文件', '存储位置', '删除时间', '状态', '', '操作'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var body = el('tbody');
      if (!items.length) {
        var tr = el('tr');
        var td = el('td', 'lib-empty-cell');
        td.colSpan = 6;
        td.append(el('strong', null, '回收站是空的'),
          el('p', null, '删除的文件按存储位置分别存放，恢复时不会覆盖同名文件。'));
        tr.appendChild(td);
        body.appendChild(tr);
      }
      items.forEach(function (item) {
        var tr = el('tr');
        tr.append(el('td', null, item.path),
          el('td', null, item.rootId === 'legacy' ? '部署数据目录' : (item.rootState || item.rootId)),
          el('td', null, relativeTime(item.deletedAt)),
          el('td', null, item.restorable ? '可恢复' : '存储位置离线'),
          el('td', null, bytes(item.size)));
        var ops = el('td');
        var button = actionButton('恢复', function () {
          button.disabled = true;
          api.post('/api/v2/library/trash/' + encodeURIComponent(item.id) + '/restore', {})
            .then(function () {
              setNotice('已恢复：' + item.path, 'ok');
              toggleTrash();
            }, function (error) {
              button.disabled = false;
              setError(error);
            });
        }, { title: '恢复到原路径，同名不覆盖' });
        button.disabled = !item.restorable;
        ops.appendChild(button);
        tr.appendChild(ops);
        body.appendChild(tr);
      });
      parts.table.innerHTML = '';
      parts.table.append(head, body);
    }

    function query(params) {
      var parts2 = [];
      Object.keys(params || {}).forEach(function (key) {
        var value = params[key];
        if (value === undefined || value === null || value === '') return;
        parts2.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
      });
      return parts2.length ? '?' + parts2.join('&') : '';
    }

    return {
      mount: mount,
      show: function () { return refresh(false); },
      refresh: refresh,
      setScope: setScope,
      state: state,
      parts: parts
    };
  }

  return { create: create };
});
