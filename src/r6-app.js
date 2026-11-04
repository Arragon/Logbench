/*
 * r6-app.js — the R6 view. Wires the eight tested pieces together; it contains
 * no matching, paging or storage logic of its own.
 *
 *   Worker + rpc        src/shared/rpc.js, src/workers/local-engine.js
 *   provider            src/providers/local.js
 *   import pipeline     src/shared/import-jobs.js
 *   query jobs          src/shared/query-jobs.js (inside the Worker)
 *   reader window       src/ui/reader-controller.js
 *   highlight           src/ui/highlight-controller.js
 *   search state        src/ui/search-controller.js
 *   drawer              src/ui/details.js
 *   timeline            src/ui/timeline.js
 *   job panel           src/ui/jobs.js
 *   workspace           src/shared/workspace.js
 *
 * Deliberate limits of this view, recorded in docs/r6/blockers.md:
 *   - it is a separate page from the R5 workbench (which keeps serving `/`);
 *   - the timeline strip shows rule/level nodes only, not a minimap;
 *   - server-side sources are not wired yet, so the provider list is local only.
 */
(function () {
  'use strict';

  var state = {
    fileId: null,
    generation: null,
    total: 0,
    mode: 'browse',           // browse | search | server | scope | compare
    searchHits: null,
    busy: false,
    rowHeight: 28,
    pageSize: 2000,
    logicalWindow: 5000,
    /* Applied cross-source time scope (R6-079). `scopeRows` holds the merged rows of
     * one bounded window; it is not a cursor into the whole library. */
    scope: null,
    scopeRows: [],
    scopeCombined: null
  };

  var els = {};
  var worker = null;
  var client = null;
  var provider = null;
  var queue = null;
  var panel = null;
  var reader = null;
  var highlighter = null;
  var search = null;
  var details = null;
  var timeline = null;
  var workspace = null;
  var pagesLoaded = new Set();
  var rowsByOrdinal = new Map();
  var lastQuery = null;
  var anchors = { search: null };
  var serverState = {
    csrf: null, uploader: null, reader: null, provider: null,
    fileId: null, sessionId: null, ready: false, file: null
  };
  /* Library and storage views (R6-056). They are built on first use so a reader
   * that never opens 文件库 does not pay for the extra requests. */
  var http = null;
  var library = null;
  var storageRoots = null;
  /* R6-079/080 surfaces, built on first use. */
  var globalTime = null;
  var compare = null;
  var rulesView = null;
  var cacheView = null;
  var runtimeView = null;
  /* Header cache meter: lives outside the cache page so it can be watched while
   * the operator is doing something else. */
  var cacheMeter = null;

  function $(id) { return document.getElementById(id); }

  function setStatus(text) { els.status.textContent = text; }

  function createWorkerClient() {
    worker = new Worker('/assets/workers/local-engine.js');
    client = LBRpc.createRpcClient({
      post: function (message, transfer) { worker.postMessage(message, transfer || []); },
      timeoutMs: 30000
    });
    worker.onmessage = function (event) { client.handleMessage(event.data); };
    worker.onerror = function (event) {
      setStatus('本机引擎异常：' + (event.message || '未知错误'));
    };
    return client;
  }

  /* ---------------------------------------------------------------- files */

  function renderFileList(files) {
    var host = els.fileList;
    host.innerHTML = '';
    files.forEach(function (file) {
      var button = document.createElement('button');
      button.className = 'r6-nav-file' + (file.file_id === state.fileId ? ' is-active' : '');
      button.textContent = file.name;
      button.title = file.name + ' · ' + file.size + 'B';
      button.addEventListener('click', function () { openFile(file); });
      host.appendChild(button);
    });
  }

  function refreshFiles() {
    return provider.listFiles().then(function (value) {
      renderFileList(value.files || []);
      return value.files || [];
    });
  }

  function openFile(file) {
    state.fileId = file.file_id;
    state.mode = 'browse';
    state.searchHits = null;
    lastQuery = null;
    pagesLoaded = new Set();
    rowsByOrdinal.clear();
    els.empty.hidden = true;
    els.fileLabel.textContent = file.name;
    setStatus('打开中…');
    return provider.ensureIndex(file.file_id).then(function (index) {
      if (index.state !== 'published') {
        setStatus('该文件尚未建立索引');
        return null;
      }
      state.generation = index.generation;
      state.total = index.watermark && index.watermark.indexed_records != null
        ? index.watermark.indexed_records
        : 0;
      reader.setTotal(state.total);
      els.reader.scrollTop = 0;
      timeline.reset({ file_id: state.fileId, generation: state.generation });
      timeline.pageSize(Number(els.tlSize.value));
      setStatus('已打开 ' + file.name + ' · ' + state.total + ' 条');
      renderCount({ text: state.total + ' 条', relation: 'exact' });
      return Promise.all([loadWindow(true), loadTimeline(0)]).then(refreshFiles);
    });
  }

  /* ---------------------------------------------------------------- paging */

  function pageSize() {
    // The provider refuses >2000 per request; a larger logical window is filled
    // by several requests rather than by one oversized message.
    return Math.min(2000, state.logicalWindow);
  }

  function loadPageForOrdinal(ordinal) {
    var size = pageSize();
    var page = Math.floor(ordinal / size);
    if (pagesLoaded.has(page)) return Promise.resolve(true);
    pagesLoaded.add(page);
    return provider.headers({
      file_id: state.fileId, generation: state.generation,
      cursor: page * size, limit: size
    }).then(function (pageData) {
      (pageData.items || []).forEach(function (item) {
        rowsByOrdinal.set(item.ordinal, item);
      });
      return true;
    }, function (error) {
      pagesLoaded.delete(page);
      setStatus('读取失败：' + error.message);
      return false;
    });
  }

  function loadSearchRows(ordinals) {
    var known = ordinals.filter(function (ordinal) { return !rowsByOrdinal.has(ordinal); });
    if (!known.length) return Promise.resolve(true);
    return provider.getRecords({
      file_id: state.fileId, generation: state.generation, ordinals: known.slice(0, 2000)
    }).then(function (value) {
      (value.items || []).forEach(function (item) { rowsByOrdinal.set(item.ordinal, item); });
      return true;
    });
  }

  function currentSequence() {
    if (state.mode === 'scope') {
      var scoped = [];
      for (var index = 0; index < state.scopeRows.length; index++) scoped.push(index);
      return scoped;
    }
    if (state.mode === 'search' && state.searchHits) return state.searchHits;
    var list = [];
    for (var i = 0; i < state.total; i++) list.push(i);
    return list;
  }

  function loadWindow(force) {
    if (!state.fileId && state.mode !== 'scope') return Promise.resolve();
    if (state.mode === 'scope') {
      renderRows(reader.windowFor(els.reader.scrollTop, els.reader.clientHeight));
      return Promise.resolve();
    }
    var win = reader.windowFor(els.reader.scrollTop, els.reader.clientHeight);
    var sequence = currentSequence();
    var slice = sequence.slice(win.first, win.last + 1);
    var work;
    if (state.mode === 'search') {
      work = loadSearchRows(slice);
    } else {
      work = Promise.all(win.missing_pages.map(function (page) {
        return loadPageForOrdinal(page * pageSize());
      }));
    }
    return work.then(function () {
      if (force) renderRows(win);
      else renderRows(win);
    });
  }

  /* ---------------------------------------------------------------- render */

  function renderCount(description) {
    els.searchCount.textContent = description.text;
    els.searchCount.classList.toggle('is-stale', !!description.stale);
    els.searchCount.classList.toggle('is-incomplete', !!description.incomplete);
  }

  function rowElement(ordinal, record) {
    var row = document.createElement('div');
    row.className = 'r6-row' + (record && record.level === 'ERROR' ? ' is-error' : '');
    row.dataset.ordinal = String(ordinal);
    row.setAttribute('role', 'row');

    var ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = record && record.ts ? record.ts : '—';
    var lv = document.createElement('span');
    lv.className = 'lv';
    lv.textContent = record && record.level ? record.level : '';
    var msg = document.createElement('span');
    msg.className = 'msg';

    if (record && lastQuery && lastQuery.ast) {
      var entry = highlighter.marksFor(record, {
        ast: lastQuery.ast, query_signature: lastQuery.signature,
        projection_version: 'v1'
      });
      var segments = highlighter.segments(record.message || '', entry.marks);
      segments.forEach(function (segment) {
        if (segment.terms) {
          var mark = document.createElement('mark');
          mark.textContent = segment.text;
          mark.dataset.terms = segment.terms.join(',');
          msg.appendChild(mark);
        } else {
          msg.appendChild(document.createTextNode(segment.text));
        }
      });
      if (record.has_trace) {
        var cont = document.createElement('span');
        cont.className = 'cont';
        cont.textContent = '⤷ 多行';
        msg.appendChild(cont);
      }
    } else {
      msg.textContent = record ? (record.message || '') : '载入中…';
    }

    row.append(ts, lv, msg);
    row.addEventListener('click', function () { openDrawer(ordinal); });
    return row;
  }

  function renderRows(win) {
    var host = els.rows;
    host.style.transform = 'translateY(' + win.offset_y + 'px)';
    els.spacer.style.height = Math.round(win.total_height) + 'px';
    var sequence = currentSequence();
    var fragment = document.createDocumentFragment();
    for (var i = win.first; i <= win.last && i < sequence.length; i++) {
      var ordinal = sequence[i];
      fragment.appendChild(rowElement(ordinal, rowsByOrdinal.get(ordinal)));
    }
    host.replaceChildren(fragment);
  }

  function renderTimeline(snapshot) {
    els.timelineMeta.textContent = '第 ' + (snapshot.page + 1) + ' 页 · ' +
      snapshot.nodes.length + '/' + snapshot.total + ' 节点';
    els.tlPrev.disabled = !snapshot.has_prev;
    els.tlNext.disabled = !snapshot.has_next;
    var host = els.timelineNodes;
    host.innerHTML = '';
    snapshot.nodes.forEach(function (node) {
      var button = document.createElement('button');
      button.className = 'r6-node' + (node.level === 'ERROR' ? ' is-error' : '');
      button.textContent = (node.ts_ns ? '' : '') + node.label;
      button.title = '第 ' + node.ordinal + ' 条';
      button.addEventListener('click', function () { jumpToOrdinal(node.ordinal); });
      host.appendChild(button);
    });
  }

  function loadTimeline(page) {
    if (!state.fileId) return Promise.resolve();
    return timeline.load(page).then(renderTimeline, function (error) {
      els.timelineMeta.textContent = '时间线不可用：' + error.message;
    });
  }

  /* ---------------------------------------------------------------- search */

  function jumpToOrdinal(ordinal) {
    if (state.mode === 'search' && state.searchHits) {
      var index = state.searchHits.indexOf(ordinal);
      if (index === -1) {
        // Restore the browsing range before jumping: a node hidden by the current
        // filter is not a reason to silently search the whole file.
        state.mode = 'browse';
        state.searchHits = null;
        reader.setTotal(state.total);
        renderCount({ text: state.total + ' 条', relation: 'exact' });
      }
    }
    reader.setTotal(state.mode === 'search' ? state.searchHits.length : state.total);
    var sequence = currentSequence();
    var position = sequence.indexOf(ordinal);
    if (position < 0) return;
    els.reader.scrollTop = position * state.rowHeight;
    loadWindow(true);
  }

  function bindSearch() {
    search = LBSearchController.createSearchController({
      provider: provider,
      getBaseSpec: function () {
        return {
          file_id: state.fileId, generation: state.generation,
          limit: 200, projection_version: 'v1'
        };
      },
      onChange: function (value) {
        var description = search.describe(value);
        renderCount(description);
        els.searchCancel.hidden = value.phase !== 'searching' && value.phase !== 'paused';
        if (value.phase === 'done' || value.phase === 'paused' || value.phase === 'partial' || value.phase === 'cancelled') {
          applySearchResult(value.result);
        }
        if (value.phase === 'searching') setStatus('搜索中…');
        else if (value.phase === 'error') setStatus(value.error.message);
        else setStatus('就绪');
      }
    });

    els.search.addEventListener('input', function () { search.input(els.search.value); });
    els.search.addEventListener('compositionstart', function () { search.compositionStart(); });
    els.search.addEventListener('compositionend', function (event) { search.compositionEnd(event.target.value); });
    els.search.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); search.submit(); }
      if (event.key === 'Escape') { els.search.value = ''; search.input(''); }
    });
    els.searchCancel.addEventListener('click', function () { search.cancel(); });
  }

  function applySearchResult(result) {
    if (!result) return;
    lastQuery = { ast: result.ast || null, signature: result.signature };
    if (!lastQuery.ast) {
      var normalized = LBQuery.normalizeQuery({ text: els.search.value });
      if (normalized.ok) lastQuery.ast = normalized.ast;
    }
    var items = (result.page && result.page.items) || [];
    state.mode = 'search';
    state.searchHits = items;
    reader.setTotal(items.length);
    rowsByOrdinal.clear();
    els.reader.scrollTop = 0;
    loadWindow(true);
  }

  /* ---------------------------------------------------------------- drawer */

  function openDrawer(ordinal) {
    if (state.mode === 'scope') {
      var row = state.scopeRows[ordinal];
      if (!row) return;
      var child = providerForFile(row.source, row.file_id);
      if (!child) { setStatus('该记录所属来源当前不可读'); return; }
      LBDetails.createDetails({ provider: child })
        .open({ file_id: row.file_id, generation: row.generation, ordinal: row.ordinal })
        .then(function (drawer) { renderDrawer(drawer); }, function (error) {
          setStatus('详情不可用：' + error.message);
        });
      return;
    }
    if (!state.fileId) return;
    if (state.mode === 'server') {
      // Server rows have no local snapshot: the detail comes from the v2 API.
      if (!serverState.reader) return;
      serverState.reader.detail(ordinal).then(function (value) {
        renderServerDrawer(value, ordinal);
      }, function (error) {
        setStatus('详情不可用：' + error.message);
      });
      return;
    }
    var snapshot = state.mode === 'search' ? { items: state.searchHits } : null;
    details.open({ file_id: state.fileId, generation: state.generation, ordinal: ordinal }, { snapshot: snapshot })
      .then(function (drawer) {
        renderDrawer(drawer);
      }, function (error) {
        setStatus('详情不可用：' + error.message);
      });
  }

  var openDrawerRef = null;

  function renderDrawer(drawer) {
    openDrawerRef = drawer;
    els.drawer.hidden = false;
    els.drawerTitle.textContent = '第 ' + drawer.ref.ordinal + ' 条 · ' + (drawer.header.level || '');
    var body = els.drawerBody;
    body.innerHTML = '';
    var dl = document.createElement('dl');
    [['时间', drawer.header.ts || '—'],
     ['行', drawer.header.lines ? drawer.header.lines.first + '–' + drawer.header.lines.last : '—'],
     ['版本', String(drawer.ref.version)]].forEach(function (pair) {
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = pair[1];
      dl.append(dt, dd);
    });
    var pre = document.createElement('div');
    pre.textContent = [drawer.header.message || ''].concat(drawer.body || []).join('\n');
    body.append(dl, pre);
    els.drawerClose.focus({ preventScroll: true });
  }

  function closeDrawer() {
    els.drawer.hidden = true;
    openDrawerRef = null;
  }

  /* --------------------------------------------------- cross-source scope (R6-079)
   * A scope query runs one child query per file. The local children stay inside the
   * Worker; the server children are ordinary v2 queries for files the server already
   * authorized. Nothing about a local file is sent anywhere to make the merge work.
   */
  function childProvider(source, fileId) {
    if (source === 'local') {
      return LBLocalProvider.createProvider({ client: client, datasetId: 'local' });
    }
    if (!serverState.provider) return null;
    return LBServerProvider.createProvider({ csrfToken: serverState.csrf || '', datasetId: fileId });
  }

  function providerForFile(source, fileId) {
    return childProvider(source, fileId);
  }

  function ensureServerProvider() {
    if (serverState.provider) return Promise.resolve(serverState.provider);
    return fetch('/api/v1/me', { credentials: 'same-origin' })
      .then(function (response) { return response.json(); })
      .then(function (value) {
        serverState.csrf = value.csrfToken;
        serverState.provider = LBServerProvider.createProvider({ csrfToken: serverState.csrf });
        return serverState.provider;
      });
  }

  function scopeSources() {
    var local = provider.listFiles().then(function (value) {
      return (value.files || []).map(function (file) {
        return { file_id: file.file_id, name: file.name, path: file.name };
      });
    }, function () { return []; });
    var shared = http.get('/api/v2/library?scope=shared&limit=200')
      .then(function (value) { return value.items || []; }, function () { return []; });
    var priv = http.get('/api/v2/library?scope=private&limit=200')
      .then(function (value) { return value.items || []; }, function () { return []; });
    return Promise.all([local, shared, priv]).then(function (values) {
      return { local: values[0], shared: values[1], private: values[2] };
    });
  }

  function applyTimeScope(snapshot) {
    var localIds = [];
    var serverIds = [];
    snapshot.files.forEach(function (file) {
      if (file.source === 'local') localIds.push(file.file_id);
      else serverIds.push(file.file_id);
    });
    if (serverIds.length) {
      return ensureServerProvider().then(function () { return runScope(snapshot, localIds, serverIds); });
    }
    return runScope(snapshot, localIds, serverIds);
  }

  function runScope(snapshot, localIds, serverIds) {
    state.scope = snapshot;
    setStatus('范围内查询中：' + snapshot.files.length + ' 个文件…');
    var combined = LBCombinedProvider.createCombinedProvider({
      local: provider,
      server: serverState.provider,
      localFactory: function (fileId) { return childProvider('local', fileId); },
      serverFactory: function (fileId) { return childProvider('server', fileId); },
      scope: { local: localIds, server: serverIds }
    });
    state.scopeCombined = combined;
    var spec = { match_all: true, limit: 400, mode: 'time',
      time_range: snapshot.range ? { from_ns: snapshot.range.from_ns, to_ns: snapshot.range.to_ns } : null };
    // A level filter from the search toolbar still applies; text search is separate.
    if (state.levelFilter) spec.levels = state.levelFilter;
    // Wait for the children to settle (bounded): rendering the server's first
    // "queued" answer as an empty range would be a wrong answer, not a fast one.
    return combined.search(spec).then(function () {
      return combined.wait({ timeout_ms: 15000 });
    }).then(function (result) {
      state.mode = 'scope';
      state.fileId = null;
      state.generation = null;
      state.scopeRows = result.items || [];
      rowsByOrdinal.clear();
      state.scopeRows.forEach(function (row, index) { rowsByOrdinal.set(index, row); });
      els.empty.hidden = true;
      els.fileLabel.textContent = '范围 · ' + snapshot.files.length + ' 个文件';
      reader.setTotal(state.scopeRows.length);
      els.reader.scrollTop = 0;
      renderRows(reader.windowFor(0, els.reader.clientHeight));
      var relation = (result.count && result.count.relation) || 'unknown';
      renderCount({
        text: (relation === 'exact' ? '共 ' : '已载入 ') + state.scopeRows.length + ' 条' +
          (relation === 'exact' ? '' : '（部分文件仍在扫描）') +
          (result.excluded && result.excluded.length
            ? ' · ' + result.excluded.length + ' 个文件无时间戳未纳入' : '') +
          (result.coverage && result.coverage.failed && result.coverage.failed.length
            ? ' · ' + result.coverage.failed.length + ' 个文件失败' : ''),
        relation: relation,
        incomplete: relation !== 'exact'
      });
      setStatus('范围就绪：本机 ' + localIds.length + ' / 服务器 ' + serverIds.length +
        ' 个文件，合并 ' + state.scopeRows.length + ' 条（窗口内）');
      if (window.__r6) window.__r6.scope = { snapshot: snapshot, result: result, combined: combined };
      return result;
    }, function (error) {
      setStatus('范围查询失败：' + error.message);
      throw error;
    });
  }

  function bindGlobalTime() {
    if (!els.timeDialog || !window.LBGlobalTime) return;
    var store = {
      load: function () {
        try { return Promise.resolve(JSON.parse(window.localStorage.getItem('logbench-r6-time-views') || '{}')); }
        catch (error) { return Promise.resolve({}); }
      },
      save: function (patch) {
        return store.load().then(function (all) {
          var next = Object.assign({}, all, patch);
          window.localStorage.setItem('logbench-r6-time-views', JSON.stringify(next));
          return next;
        });
      }
    };
    var widget = LBGlobalTime.createGlobalTime({
      loadScope: scopeSources,
      store: store,
      onStatus: setStatus,
      onApply: function (snapshot) {
        return applyTimeScope(snapshot).then(function () { viewSwitch('logs'); });
      }
    });
    widget.mount(els.timeDialog);
    globalTime = widget;
    els.timeScope.addEventListener('click', function () {
      widget.open(state.scope ? { range: state.scope.range, selection: state.scope.selection } : null);
    });
    if (window.__r6) window.__r6.timeScope = widget;
  }

  function bindCompare() {
    if (!els.timeCompare) return;
    els.timeCompare.addEventListener('click', function () {
      var files = state.scope && state.scope.files ? state.scope.files : [];
      if (files.length < 2) {
        setStatus('时间对比需要 2–4 个文件：先在“时间范围”里选择 2–4 个文件再应用');
        if (globalTime) globalTime.open(state.scope ? { range: state.scope.range } : null);
        return;
      }
      if (files.length > 4) {
        setStatus('时间对比最多 4 个文件：当前范围有 ' + files.length + ' 个，请缩小范围');
        return;
      }
      openCompare(files);
    });
  }

  function openCompare(files) {
    if (compare) {
      viewSwitch('compare');
      return compare.setFiles(files.map(function (file) {
        return { file_id: file.file_id, source: file.source, label: file.path || file.file_id };
      }));
    }
    if (!window.LBCompare) return;
    compare = LBCompare.createCompare({
      files: files.map(function (file) {
        return { file_id: file.file_id, source: file.source, label: file.path || file.file_id };
      }),
      onStatus: setStatus,
      fetchWindow: function (request) {
        var file = request.file;
        var spec = request.window;
        var child = childProvider(file.source, file.file_id);
        if (!child) return Promise.reject(new Error('该来源当前不可读'));
        var query = { match_all: true, limit: spec.limit || 400 };
        if (spec.from_ns != null && spec.to_ns != null) {
          query.time_range = { from_ns: spec.from_ns, to_ns: spec.to_ns };
        }
        var settled = ['done', 'failed', 'cancelled', 'paused'];
        var started = Date.now();
        function settle(snapshot) {
          if (settled.indexOf(snapshot.state) !== -1 || Date.now() - started > 12000) {
            return Promise.resolve(snapshot);
          }
          return child.poll(snapshot.query_id).then(settle, function () { return snapshot; });
        }
        return child.search(query).then(settle).then(function (snapshot) {
          var relation = (snapshot.count || {}).relation || 'unknown';
          var coverage = snapshot.coverage;
          var ordinals = (snapshot.page && snapshot.page.items) || null;
          if (ordinals && ordinals.length) {
            return child.getRecords({ file_id: file.file_id, generation: snapshot.generation,
              ordinals: ordinals }).then(function (value) {
              return { rows: value.items || [], relation: relation, coverage: coverage };
            });
          }
          // The server carries counts in the query state; its rows come from the page.
          if (child.queryPage) {
            return child.queryPage(snapshot.query_id, null).then(function (value) {
              return { rows: value.items || [], relation: relation, coverage: coverage };
            });
          }
          return { rows: [], relation: relation, coverage: coverage };
        });
      },
      onOpen: function (row) {
        setStatus('对比记录：' + row.record_id);
      },
      onChange: function (view) {
        if (workspace) workspace.savePatch({ preferences: { compare_layout: view.layout } }).catch(function () {});
      }
    });
    compare.mount(els.compareView);
    viewSwitch('compare');
    compare.load(state.scope && state.scope.range ? Number(state.scope.range.from_key) : null);
    if (window.__r6) window.__r6.compare = compare;
  }

  /* ---------------------------------------------------------------- jobs */

  function bindJobs(profile) {
    queue = LBImportJobs.createImportQueue({
      profile: profile,
      client: client,
      framer: LBFramer,
      rules: LBRules,
      time: LBTime,
      schedule: function (fn) { return setTimeout(fn, 0); },
      emit: function (name, payload) {
        if (name === 'task' && payload.task.phase === 'queued') {
          els.jobs.hidden = false;
        }
        if (name === 'task' && payload.task.phase === 'indexed') {
          refreshFiles().then(function (files) {
            var match = files.filter(function (file) { return file.file_id === 'local-' + payload.task.hash; })[0];
            if (match && !state.fileId) openFile(match);
          });
        }
        if (panel) panel.refresh(false);
      }
    });
    panel = LBJobPanel.createJobPanel({
      queue: queue,
      render: function (snapshot) {
        els.jobs.hidden = !snapshot.open;
        els.jobs.classList.toggle('is-min', snapshot.minimized);
        els.jobsSummary.textContent = snapshot.summary.percent == null
          ? snapshot.summary.files_ready + '/' + snapshot.summary.files_total + ' 已完成'
          : snapshot.summary.percent + '% · ' + snapshot.summary.files_ready + '/' + snapshot.summary.files_total;
        var host = els.jobsList;
        host.innerHTML = '';
        snapshot.tasks.forEach(function (task) {
          var li = document.createElement('li');
          var name = document.createElement('span');
          name.className = 'name';
          name.textContent = task.name;
          var phase = document.createElement('span');
          phase.className = 'r6-meta';
          phase.textContent = task.phase_label;
          var bar = document.createElement('div');
          bar.className = 'bar';
          var fill = document.createElement('span');
          fill.style.width = (task.size ? Math.round((task.bytes_done / task.size) * 100) : 0) + '%';
          bar.appendChild(fill);
          var ops = document.createElement('div');
          ops.className = 'ops';
          [['pause', '暂停'], ['resume', '继续'], ['cancel', '取消'], ['retry', '重试']].forEach(function (pair) {
            var button = document.createElement('button');
            button.className = 'r6-icon';
            button.textContent = pair[1];
            button.addEventListener('click', function () { panel.action(task.id, pair[0]); panel.refresh(true); });
            ops.appendChild(button);
          });
          li.append(name, phase, bar, ops);
          host.appendChild(li);
        });
      }
    });
  }

  /* ---------------------------------------------------------------- boot */

  function bindChrome() {
    els.importTrigger.addEventListener('click', function () { els.fileInput.click(); });
    els.importTrigger.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); els.fileInput.click(); }
    });
    els.fileInput.addEventListener('change', function () {
      var files = Array.prototype.slice.call(els.fileInput.files || []);
      if (!files.length) return;
      var created = queue.add(files);
      panel.onFilesChosen(created.length);
      setStatus('已登记 ' + created.length + ' 个文件，开始读取');
      els.fileInput.value = '';
    });

    var appRoot = document.getElementById('app');
    els.theme.value = appRoot.dataset.theme || 'ink';
    els.theme.addEventListener('change', function () {
      // The tokens are scoped to .ud-app[data-theme]; setting it on <html> would
      // leave the page in the default palette. This was a real defect: all four
      // themes rendered identically until the attribute moved here.
      appRoot.dataset.theme = els.theme.value;
      if (workspace) workspace.savePatch({ preferences: { theme: els.theme.value } }).catch(function () {});
    });

    els.pageSize.addEventListener('change', function () {
      state.logicalWindow = Number(els.pageSize.value);
      if (workspace) workspace.savePatch({ preferences: { page_size: state.logicalWindow } }).catch(function () {});
    });

    els.jobsClose.addEventListener('click', function () { panel.close(); });
    els.jobsMin.addEventListener('click', function () { panel.minimize(); });

    els.drawerClose.addEventListener('click', closeDrawer);
    els.drawerPrev.addEventListener('click', function () { stepDrawer(-1); });
    els.drawerNext.addEventListener('click', function () { stepDrawer(1); });

    els.tlPrev.addEventListener('click', function () { timeline.prev().then(renderTimeline); });
    els.tlNext.addEventListener('click', function () { timeline.next().then(renderTimeline); });
    els.tlSize.addEventListener('change', function () {
      timeline.pageSize(Number(els.tlSize.value));
      loadTimeline(0);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        if (!els.drawer.hidden) { closeDrawer(); els.reader.focus(); }
        else if (!els.jobs.hidden) panel.minimize();
      }
    });

    els.reader.addEventListener('scroll', function () {
      if (!state.fileId) return;
      var win = reader.windowFor(els.reader.scrollTop, els.reader.clientHeight);
      var sequence = currentSequence();
      var ordinal = sequence[win.first];
      if (ordinal != null) reader.setAnchor(ordinal, 0, String(ordinal));
      loadWindow(true);
    }, { passive: true });
  }

  function stepDrawer(delta) {
    if (!openDrawerRef) return;
    var next = details.navigate(openDrawerRef, delta, null);
    if (!next) return;
    openDrawer(next.ordinal);
  }

  function bindWorkspace() {
    // Preferences only. Query hits are never stored (R6-030 refuses them too);
    // the localStorage adapter is enough for a preference of this size and keeps
    // the page independent of IndexedDB connection lifetime.
    var KEY = 'logbench-r6-workspace';
    function readAll() {
      try { return JSON.parse(window.localStorage.getItem(KEY) || '{}'); } catch (error) { return {}; }
    }
    workspace = LBWorkspace.createWorkspace({
      store: {
        get: function (key) {
          var all = readAll();
          return all[key] == null ? null : all[key];
        },
        put: function (key, value) {
          var all = readAll();
          all[key] = value;
          window.localStorage.setItem(KEY, JSON.stringify(all));
          return true;
        },
        delete: function (key) {
          var all = readAll();
          delete all[key];
          window.localStorage.setItem(KEY, JSON.stringify(all));
          return true;
        }
      }
    });

    var saved = readAll()['r6/workspace/preferences'] || {};
    if (saved.theme) {
      els.theme.value = saved.theme;
      var root = document.getElementById('app');
      if (root) root.dataset.theme = saved.theme;
    }
    if (saved.page_size) {
      state.logicalWindow = saved.page_size;
      els.pageSize.value = String(saved.page_size);
    }
  }


  /* ------------------------------------------------------- server mode (R6-048) */

  function serverRequest(method, path, body) {
    var init = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (method !== 'GET') init.headers['X-LogBench-CSRF'] = serverState.csrf || '';
    return fetch(path, init).then(function (response) {
      return response.text().then(function (text) {
        var value = text ? JSON.parse(text) : {};
        if (!response.ok) {
          var detail = (value && value.error) || {};
          var error = new Error(detail.message || ('服务端返回 ' + response.status));
          error.code = detail.code || ('http_' + response.status);
          throw error;
        }
        return value;
      });
    });
  }

  function serverApi() {
    return {
      createUpload: function (body) { return serverRequest('POST', '/api/v2/uploads', body).then(function (v) { return v.item; }); },
      getUpload: function (id) { return serverRequest('GET', '/api/v2/uploads/' + encodeURIComponent(id)).then(function (v) { return v.item; }); },
      putPart: function (id, partNo, bytes, digest) {
        var headers = { 'Content-Type': 'application/octet-stream', 'X-LogBench-CSRF': serverState.csrf || '' };
        if (digest) headers['X-LogBench-Digest'] = digest;
        return fetch('/api/v2/uploads/' + encodeURIComponent(id) + '/parts/' + partNo, {
          method: 'PUT', credentials: 'same-origin', headers: headers, body: bytes
        }).then(function (response) {
          if (!response.ok) {
            return response.text().then(function (text) {
              var value = text ? JSON.parse(text) : {};
              var error = new Error((value.error && value.error.message) || '分片上传失败');
              error.code = (value.error && value.error.code) || ('http_' + response.status);
              throw error;
            });
          }
          return response.json();
        }).then(function (value) { return value.item; });
      },
      commitUpload: function (id) { return serverRequest('POST', '/api/v2/uploads/' + encodeURIComponent(id) + '/commit', {}).then(function (v) { return v.item; }); },
      getIndexJob: function (id) { return serverRequest('GET', '/api/v2/index-jobs/' + encodeURIComponent(id)).then(function (v) { return v.item; }); }
    };
  }

  function setServerStatus(text, percent) {
    els.serverStatus.textContent = text;
    if (percent != null) {
      els.serverBar.style.width = Math.max(0, Math.min(100, percent)) + '%';
      els.serverBarWrap.setAttribute('aria-valuenow', String(Math.round(percent)));
    }
  }

  function renderServerState(next) {
    var labels = {
      idle: '未开始', preparing: '准备中…', uploading: '上传中', sealing: '校验并封存…',
      indexing: '服务端索引中…', ready: '已就绪，可打开服务端视图', paused: '已暂停（分片已保留）',
      failed: '失败：' + (next.error ? next.error.message : '未知原因')
    };
    setServerStatus((labels[next.phase] || next.phase) +
      (next.phase === 'uploading' ? ' ' + next.percent + '%（' + next.partsDone + '/' + next.partsTotal + ' 分片）' : ''),
      next.percent);
    els.serverStart.disabled = next.phase === 'uploading';
    els.serverPause.disabled = next.phase !== 'uploading';
    els.serverResume.disabled = next.phase !== 'paused' || !next.sessionId;
    els.serverOpen.disabled = next.phase !== 'ready';
    if (next.phase === 'paused' && next.sessionId) {
      serverState.sessionId = next.sessionId;
    }
  }

  function openServerView(fileId) {
    if (!serverState.reader) return;
    setStatus('服务端视图加载中…');
    return serverState.reader.open(fileId).then(function (page) {
      state.fileId = fileId;
      state.mode = 'server';
      state.generation = page.index && page.index.generation;
      state.searchHits = null;
      lastQuery = null;
      rowsByOrdinal.clear();
      page.rows.forEach(function (row) { rowsByOrdinal.set(row.ordinal, row); });
      els.empty.hidden = true;
      els.fileLabel.textContent = '服务端 · ' + fileId;
      els.spacer.style.height = '0px';
      els.rows.style.transform = 'translateY(0)';
      var fragment = document.createDocumentFragment();
      page.rows.forEach(function (row) { fragment.appendChild(rowElement(row.ordinal, row)); });
      els.rows.replaceChildren(fragment);
      var coverage = page.index && page.index.coverage;
      var total = coverage && coverage.record_count != null ? coverage.record_count : page.rows.length;
      renderCount({ text: '服务端前 ' + page.rows.length + ' 条 / 共 ' + total + ' 条', relation: 'at_least' });
      setStatus('服务端视图：' + page.rows.length + ' 条（分页读取，未下载整文件）');
      return page;
    });
  }

  function renderServerDrawer(value, ordinal) {
    var record = value.record || {};
    openDrawerRef = null;
    els.drawer.hidden = false;
    els.drawerTitle.textContent = '第 ' + ordinal + ' 条 · ' + (record.level || '');
    var body = els.drawerBody;
    body.innerHTML = '';
    var dl = document.createElement('dl');
    [['时间', record.ts || '—'],
     ['行', record.lines ? record.lines.first + '–' + record.lines.last : '—'],
     ['来源', record.src || '—'],
     ['消息', record.message || '']].forEach(function (pair) {
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = pair[1];
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
    var bodyBlock = document.createElement('pre');
    bodyBlock.className = 'r6-drawer-body-text';
    bodyBlock.textContent = (value.body && value.body.items ? value.body.items.join('\n') : '');
    body.appendChild(dl);
    if (bodyBlock.textContent) body.appendChild(bodyBlock);
  }

  function bindServerMode() {
    if (!els.serverTrigger || !els.serverPanel) return;
    if (typeof LBServerMode === 'undefined' || typeof LBServerProvider === 'undefined') return;

    els.serverTrigger.addEventListener('click', function () {
      els.serverPanel.hidden = !els.serverPanel.hidden;
    });
    els.serverClose.addEventListener('click', function () { els.serverPanel.hidden = true; });

    // Opt-in is the gate: the file input and the start button stay disabled until
    // the user says the file may leave this machine.
    els.serverOptin.addEventListener('change', function () {
      var on = els.serverOptin.checked;
      els.serverFile.disabled = !on;
      els.serverStart.disabled = !on || !els.serverFile.files.length;
      els.serverHint.textContent = on
        ? '已开启：所选文件会分片上传到服务器，结束后可在服务端视图读取'
        : '默认关闭：只有显式开启后才会上传';
    });

    els.serverFile.addEventListener('change', function () {
      serverState.file = els.serverFile.files[0] || null;
      els.serverFileLabel.textContent = serverState.file ? serverState.file.name : '—';
      els.serverStart.disabled = !els.serverOptin.checked || !serverState.file;
      els.serverResume.disabled = true;
    });

    function ensureCsrf() {
      if (serverState.csrf) return Promise.resolve(serverState.csrf);
      return fetch('/api/v1/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); })
        .then(function (value) {
          serverState.csrf = value.csrfToken;
          serverState.provider = LBServerProvider.createProvider({ csrfToken: serverState.csrf });
          serverState.reader = LBServerMode.createServerReader({ provider: serverState.provider });
          return serverState.csrf;
        });
    }

    function startUpload(existingSession) {
      if (!serverState.file) return Promise.resolve(null);
      return ensureCsrf().then(function () {
        serverState.uploader = LBServerMode.createUploader({
          api: serverApi(), onState: renderServerState
        });
        return serverState.uploader.upload(serverState.file, existingSession);
      }).then(function (sealed) {
        els.serverOpen.disabled = true;
        return serverState.uploader.pollIndex({}, { interval: 400 }).then(function (state) {
          if (state.phase === 'ready') {
            serverState.fileId = state.fileId || (sealed && sealed.file_id);
            els.serverFileLabel.textContent = serverState.fileId || '—';
            if (window.__r6) window.__r6.serverState = serverState;
          }
          return state;
        });
      }).catch(function (error) {
        setServerStatus('失败：' + (error.message || error), 0);
      });
    }

    els.serverStart.addEventListener('click', function () { startUpload(null); });
    els.serverResume.addEventListener('click', function () { startUpload(serverState.sessionId); });
    els.serverPause.addEventListener('click', function () {
      if (serverState.uploader) serverState.uploader.cancel();
    });
    els.serverOpen.addEventListener('click', function () {
      if (serverState.fileId) openServerView(serverState.fileId);
    });

    if (window.__r6) window.__r6.server = { state: serverState, open: openServerView };
  }

  /* --------------------------------------------------- live events (R6-075)
   * One stream per tab, routed by file id. The chip in the header states the mode
   * in words — 实时 / 轮询 / 已断开 — because a coloured dot alone would claim a
   * liveness the browser cannot verify behind a buffering proxy.
   *
   * Scrolling up pauses *following*, not collection: new records accumulate in a
   * badge and the view only moves when the user asks it to.
   */
  var live = { client: null, follower: null, badge: null, count: null, chip: null,
    label: null, suppressScroll: false, lastIntakeRefresh: 0 };

  function liveChip(state) {
    if (!live.chip) return;
    live.chip.dataset.mode = state.mode;
    live.label.textContent = state.label;
    live.chip.title = '事件流：' + state.label +
      (state.reason ? '（' + state.reason + '）' : '') +
      ' · 已应用 ' + state.applied + ' 条 · 重复 ' + state.duplicates +
      ' · 缺口 ' + state.gaps;
  }

  function showNewRecords(count) {
    if (!live.badge) return;
    live.count.textContent = String(count);
    live.badge.hidden = false;
  }

  function hideNewRecords() {
    if (live.badge) live.badge.hidden = true;
  }

  /** Jump to the newest indexed record, keeping the reader's own geometry. */
  function seekNewest() {
    if (!state.fileId) return Promise.resolve();
    live.suppressScroll = true;
    var done = function () { live.suppressScroll = false; hideNewRecords(); };
    if (state.mode === 'server' && window.__r6 && window.__r6.server &&
        window.__r6.server.open) {
      // The server view is cursor-paged: the honest "newest" is a fresh open.
      return Promise.resolve(window.__r6.server.open(state.fileId)).then(done, done);
    }
    return provider.ensureIndex(state.fileId).then(function (index) {
      var total = index && index.watermark && index.watermark.indexed_records != null
        ? index.watermark.indexed_records : state.total;
      if (total > state.total) {
        state.total = total;
        reader.setTotal(total);
      }
      els.reader.scrollTop = els.reader.scrollHeight;
      return loadWindow(true);
    }, function () { return null; }).then(done, done);
  }

  function readerScrolled() {
    if (!live.follower) return;
    var before = live.follower.state().following;
    var next = live.follower.observe({
      scrollTop: els.reader.scrollTop,
      scrollHeight: els.reader.scrollHeight,
      clientHeight: els.reader.clientHeight,
      programmatic: live.suppressScroll
    });
    if (next.following && !before) hideNewRecords();
  }

  function refreshIntakeSoon() {
    var now = Date.now();
    if (now - live.lastIntakeRefresh < 1500) return;
    live.lastIntakeRefresh = now;
    if (window.__r6 && window.__r6.intake && window.__r6.intake.refresh) {
      window.__r6.intake.refresh();
    }
  }

  function handleResync(info) {
    // A hole or a dropped subscription: pull the bounded replay once instead of
    // guessing which records were missed.
    setStatus('事件出现缺口（' + info.reason + '），正在补齐…');
    if (live.client) live.client.poll();
  }

  function handleLiveEvent(event) {
    if (!event) return;
    if (event.notice) {
      setStatus('收到无法解析的事件，已忽略其中的序号');
      return;
    }
    var payload = event.payload || {};
    if (event.kind === 'task_progress') {
      if (panel && panel.refresh) panel.refresh(false);
      refreshIntakeSoon();
      return;
    }
    if (event.kind === 'source_error') {
      setStatus('采集错误：' + (payload.code || '未知') +
        (payload.retryAt ? '，稍后重试' : '，需要人工处理'));
      refreshIntakeSoon();
      return;
    }
    if (event.kind === 'rotation') {
      if (payload.fileId && payload.fileId === state.fileId) {
        setStatus('文件已切换到第 ' + (payload.generation || '新') + ' 代次，旧代次仍可读');
        showNewRecords(1);
        return;
      }
    }
    if (event.kind === 'gap') {
      if (payload.fileId && payload.fileId === state.fileId) {
        setStatus('归档存在缺口（' + (payload.state || '未知') + '），部分区间不可用');
      }
      return;
    }
    if (event.kind !== 'archive_commit' && event.kind !== 'index_commit' &&
        event.kind !== 'record_revision') {
      return;
    }
    if (!payload.fileId || payload.fileId !== state.fileId) return;
    if (event.kind === 'index_commit' && payload.state === 'failed') {
      setStatus('索引未完成：' + (payload.error || '未知原因'));
      return;
    }
    var decision = live.follower.notify(1);
    if (decision.action === 'seek') seekNewest();
    else showNewRecords(decision.count);
  }

  function bindLive() {
    live.chip = $('live-status');
    live.label = $('live-label');
    live.badge = $('new-records');
    live.count = $('new-count');
    if (!window.LBEventClient || !live.chip) return;
    live.follower = window.LBEventClient.createFollower({ threshold: 12 });
    live.client = window.LBEventClient.createClient({
      url: '/api/v2/events',
      pollUrl: '/api/v2/events/poll',
      streamFactory: function (url) {
        if (typeof EventSource === 'undefined') return null;
        return new EventSource(url, { withCredentials: true });
      },
      fetchImpl: function (url) { return http.get(url); },
      onState: liveChip,
      onEvent: handleLiveEvent,
      onResync: handleResync
    });
    window.LBEventClient.attachPageHooks(live.client, window);
    els.reader.addEventListener('scroll', readerScrolled, { passive: true });
    live.chip.addEventListener('click', function () {
      // A click never pretends to reconnect a stream the browser refused; it
      // either retries the stream or polls once, and the label follows the truth.
      if (live.client.state().mode === 'closed') live.client.start();
      else live.client.poll();
    });
    if (live.badge) {
      live.badge.addEventListener('click', function () {
        if (live.follower) live.follower.requestFollow();
        seekNewest();
      });
    }
    live.client.start();
  }

  /* ------------------------------------------------------------- views (R6-068)
   * Switching to 采集中心 hides the reading surface instead of resizing it: the
   * reader keeps its geometry, its scroll position and its anchor, so coming back
   * lands on the same record. The intake page owns its own scrolling.
   */
  function bindViews() {
    var nav = Array.prototype.slice.call(document.querySelectorAll('.r6-nav-item[data-view]'));
    var intakeHost = document.getElementById('intake-view');
    var libraryHost = document.getElementById('library-view');
    var storageHost = document.getElementById('storage-view');
    var toolbar = document.getElementById('toolbar');

    function ensureLibrary() {
      if (library || !window.LBLibrary) return library;
      library = window.LBLibrary.create({
        api: http,
        onStatus: setStatus,
        // Purging from the recycling bin also drops the file's index cache, so the
        // header total is stale the moment that succeeds.
        onCacheChanged: function () { if (cacheMeter) cacheMeter.refresh(); },
        localFiles: function () {
          return provider.listFiles().then(function (value) { return value.files || []; });
        },
        onOpenLocal: function (file) {
          var match = null;
          return provider.listFiles().then(function (value) {
            match = (value.files || []).filter(function (item) {
              return item.file_id === file.fileId;
            })[0];
            if (match) { viewSwitch('logs'); return openFile(match); }
            setStatus('本机文件已不在当前工作区');
            return null;
          });
        },
        onOpenServerFile: function (file) {
          // Reading a server file is the M4 surface's job; the library only routes
          // the click and keeps its own scroll position.
          if (window.__r6 && window.__r6.server && window.__r6.server.open) {
            viewSwitch('logs');
            window.__r6.server.open(file.fileId);
          } else {
            setStatus('服务端阅读尚未就绪：' + file.displayName);
          }
        }
      });
      library.mount(libraryHost);
      return library;
    }

    function ensureStorage() {
      if (storageRoots || !window.LBStorageRoots) return storageRoots;
      storageRoots = window.LBStorageRoots.create({ api: http, onStatus: setStatus });
      storageRoots.mount(storageHost);
      return storageRoots;
    }

    var ruleHost = document.getElementById('rule-view');
    var cacheHost = document.getElementById('cache-view');
    var runtimeHost = document.getElementById('runtime-view');
    var compareHost = document.getElementById('compare-view');

    /* The three R6-081/082/083 surfaces share one shape: mount on first use, reload on
     * every entry, and never keep polling once the tab is left. */
    function ensureAdminView(which) {
      if (which === 'rules') {
        if (!rulesView && window.LBRuleImpact) {
          rulesView = window.LBRuleImpact.create({ api: http, onStatus: setStatus });
          rulesView.mount(ruleHost);
        }
        return rulesView;
      }
      if (which === 'cache') {
        if (!cacheView && window.LBCacheManager) {
          cacheView = window.LBCacheManager.create({ api: http, onStatus: setStatus });
          cacheView.mount(cacheHost);
        }
        return cacheView;
      }
      if (!runtimeView && window.LBRuntime) {
        runtimeView = window.LBRuntime.create({ api: http, onStatus: setStatus });
        runtimeView.mount(runtimeHost);
      }
      return runtimeView;
    }

    function show(view) {
      var isIntake = view === 'intake';
      var isFiles = view === 'files';
      var isStorage = view === 'storage';
      var isRules = view === 'rules';
      var isCache = view === 'cache';
      var isRuntime = view === 'runtime';
      var isCompare = view === 'compare';
      var isLogs = !isIntake && !isFiles && !isStorage && !isRules && !isCache &&
        !isRuntime && !isCompare;
      nav.forEach(function (button) {
        button.classList.toggle('is-active', button.dataset.view === view);
      });
      if (toolbar) { toolbar.hidden = !isLogs; }
      if (els.reader) { els.reader.hidden = !isLogs; }
      if (els.timeline) { els.timeline.hidden = !isLogs; }
      if (intakeHost) { intakeHost.hidden = !isIntake; }
      if (libraryHost) { libraryHost.hidden = !isFiles; }
      if (storageHost) { storageHost.hidden = !isStorage; }
      if (ruleHost) { ruleHost.hidden = !isRules; }
      if (cacheHost) { cacheHost.hidden = !isCache; }
      if (runtimeHost) { runtimeHost.hidden = !isRuntime; }
      if (compareHost) { compareHost.hidden = !isCompare; }
      if (runtimeView && !isRuntime) runtimeView.hide();
      if (isFiles) {
        ensureLibrary();
        if (library) library.show();
        setStatus('文件库');
        return;
      }
      if (isStorage) {
        ensureStorage();
        if (storageRoots) storageRoots.show();
        setStatus('存储位置');
        return;
      }
      if (isRules) {
        var rules = ensureAdminView('rules');
        if (rules) rules.show();
        setStatus('规则');
        return;
      }
      if (isCache) {
        var cache = ensureAdminView('cache');
        if (cache) cache.show();
        // Leaving the cache page can change the totals (purge, rebuild), so the
        // meter is re-read on entry rather than waiting for its own interval.
        if (cacheMeter) cacheMeter.refresh();
        setStatus('解析缓存');
        return;
      }
      if (isRuntime) {
        var runtime = ensureAdminView('runtime');
        if (runtime) runtime.show();
        setStatus('运行记录');
        return;
      }
      if (isCompare) {
        setStatus('时间对比');
        return;
      }
      if (!isIntake) { return; }
      if (!intakeView && window.LBIntake) {
        intakeView = window.LBIntake.create({ root: intakeHost });
      } else if (intakeView) {
        intakeView.show();
      }
      setStatus('采集中心');
    }

    nav.forEach(function (button) {
      button.addEventListener('click', function () { show(button.dataset.view); });
    });

    // The storage tab is an administrator surface. Hiding it is a hint, not an
    // authorization: every write is refused by the API for a non-admin.
    if (http) {
      http.get('/api/v1/me').then(function (value) {
        var admin = !!(value.capabilities && value.capabilities.admin);
        ['nav-storage', 'nav-rules', 'nav-cache', 'nav-runtime'].forEach(function (id) {
          var button = document.getElementById(id);
          if (button && admin) button.hidden = false;
        });
      }, function () { /* viewer: leave them hidden */ });
    }
    viewSwitch = show;
  }

  /* Declared before `bindViews` runs so both the nav and the server panel can use
   * it; assigned there. */
  var viewSwitch = function () {};
  /* The intake page is built lazily; events refresh it only once it exists. */
  var intakeView = null;

  function boot() {
    /* Created before any binding runs: the binders publish their handles here, and a
     * handle published into an undefined object is a handle nobody can find. */
    window.__r6 = {};
    els = {
      status: $('status'), search: $('search'), searchCount: $('search-count'),
      searchCancel: $('search-cancel'), fileInput: $('file-input'),
      importTrigger: $('import-trigger'), theme: $('theme'), pageSize: $('page-size'),
      fileList: $('file-list'), fileLabel: $('file-label'), reader: $('reader'),
      rows: $('rows'), spacer: $('spacer'), empty: $('empty'), timeline: $('timeline'),
      timelineNodes: $('timeline-nodes'), timelineMeta: $('timeline-meta'),
      tlPrev: $('tl-prev'), tlNext: $('tl-next'), tlSize: $('tl-size'),
      drawer: $('drawer'), drawerBody: $('drawer-body'), drawerTitle: $('drawer-title'),
      drawerClose: $('drawer-close'), drawerPrev: $('drawer-prev'), drawerNext: $('drawer-next'),
      jobs: $('jobs'), jobsList: $('jobs-list'), jobsSummary: $('jobs-summary'),
      jobsClose: $('jobs-close'), jobsMin: $('jobs-min'),
      serverTrigger: $('server-trigger'), serverPanel: $('server-panel'),
      serverClose: $('server-close'), serverOptin: $('server-optin'),
      serverFile: $('server-file'), serverStart: $('server-start'),
      serverPause: $('server-pause'), serverResume: $('server-resume'),
      serverBar: $('server-bar'), serverBarWrap: $('server-bar-wrap'),
      serverStatus: $('server-status'), serverOpen: $('server-open'),
      serverFileLabel: $('server-file-label'), serverHint: $('server-hint'),
      timeScope: $('time-scope'), timeCompare: $('time-compare'),
      timeDialog: $('time-dialog'), compareView: $('compare-view')
    };

    createWorkerClient();
    http = window.LBHttp.createClient({});
    provider = LBLocalProvider.createProvider({ client: client, datasetId: 'local' });
    reader = LBReaderController.createReader({ pageSize: 2000, rowHeight: state.rowHeight });
    highlighter = LBHighlightController.createHighlighter({
      computeMarks: function (record, ast) { return LBQuery.computeMarks(record, ast, { max_marks: 200 }); },
      textOf: function (record) { return [record.message].concat(record.body || []).join('\n'); }
    });
    details = LBDetails.createDetails({ provider: provider });
    timeline = LBTimeline.createTimeline({ provider: provider });

    bindWorkspace();
    bindChrome();
    bindSearch();
    bindServerMode();
    bindViews();
    bindGlobalTime();
    bindCompare();
    bindLive();

    // The meter is mounted once, not per view: it is the only place the cache
    // total is visible while the cache page is closed.
    if (window.LBCacheMeter) {
      cacheMeter = window.LBCacheMeter.create({
        api: http,
        onStatus: setStatus,
        onOpenCachePage: function () { viewSwitch('cache'); }
      });
      cacheMeter.mount($('cache-meter'));
    }

    // The rule profile is fetched, not inlined: the same file the server ships is
    // what the importer uses, so field extraction on the page matches the CLI.
    var profilePromise = fetch('/assets/rules/default.json')
      .then(function (response) { return response.ok ? response.json() : null; })
      .catch(function () { return null; });

    profilePromise.then(function (profile) {
      bindJobs(profile);
      if (window.__r6) { window.__r6.queue = queue; window.__r6.panel = panel; }
      return provider.open('local');
    }).then(function () {
      return refreshFiles();
    }).then(function () {
      setStatus('就绪');
      window.__r6Ready = true;
    }).catch(function (error) {
      setStatus('初始化失败：' + (error && error.message));
    });

    /* Exposed for the QA harness; not part of the product surface. */
    Object.assign(window.__r6, {
      state: state, provider: provider, queue: queue, reader: reader,
      search: search, timeline: timeline, details: details, client: client,
      openFile: openFile, refreshFiles: refreshFiles, loadWindow: loadWindow,
      highlighter: highlighter, rowsByOrdinal: rowsByOrdinal,
      http: http,
      showView: function (view) { viewSwitch(view); },
      library: function () { return library; },
      storageRoots: function () { return storageRoots; },
      rules: function () { return rulesView; },
      cache: function () { return cacheView; },
      cacheMeter: function () { return cacheMeter; },
      runtime: function () { return runtimeView; },
      compareView: function () { return compare; },
      intake: function () { return intakeView; },
      live: function () { return live.client; },
      /* QA hook: feed a synthetic event through the same handler the stream uses. */
      applyEvent: handleLiveEvent,
      follower: function () { return live.follower; }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
