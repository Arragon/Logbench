/* ===== R6 RESTORE ADAPTER · PRODUCTION ======================================
 * 由 tools/build_r6_frontend.py 插入原型 IIFE 内部，因此可直接访问
 * R6 / S / render / r6Btn / I / E / $ / dialog / notify / download / clone /
 * r6Event / r6CaptureDraft / r6Draft / r6Probe / r6Wizard / r6ShowFormError /
 * r6Validate / r6Source / r6Root / r6Persist / files 等同作用域成员。
 *
 * 与预览版 adapter.js 暴露完全相同的接口，故渲染层零改动：
 *   statusText / dataFreshness / scenarioSelect / paintStatus / stateBanner /
 *   state / prefs / probeResult / setScenario / savePref / datalink / backup /
 *   exportBackup / restoreBackup / connect / retry / refresh / pause / probe /
 *   saveSource / saveRoot / saveTuning / rebuildCache / exportEvents /
 *   ENDPOINTS / SCENARIOS / request / hydrate。
 *
 * 真实契约来源：server/app.py、server/collectors/*、server/storage/*、
 * server/indexing/*、server/runtime/*、server/query/*、server/uploads/*。
 * 设计层（DOM/CSS/视觉节拍）由原型 HTML 提供，本文件不定义任何 UI 结构。
 * 未定义的新状态只用原型既有组件形态表达（计划 §4.4）。
 */
const LB = (function () {
  'use strict';

  /* ---- 真实端点表 -------------------------------------------------------- */
  var ENDPOINTS = {
    me:                 ['GET',   '/api/v1/me'],
    health:             ['GET',   '/api/v1/health'],
    preferences:        ['GET',   '/api/v1/preferences'],
    putPreferences:     ['PUT',   '/api/v1/preferences'],
    capabilities:       ['GET',   '/api/v2/capabilities'],
    intake:             ['GET',   '/api/v2/intake'],
    sources:            ['GET',   '/api/v2/sources'],
    source:             ['GET',   '/api/v2/sources/{id}'],
    createSource:       ['POST',  '/api/v2/sources'],
    updateSource:       ['PATCH', '/api/v2/sources/{id}'],
    sourceState:        ['GET',   '/api/v2/sources/{id}/state'],
    mappings:           ['GET',   '/api/v2/sources/{id}/mappings'],
    probe:              ['POST',  '/api/v2/sources/{id}/probe'],
    actions:            ['POST',  '/api/v2/sources/{id}/actions'],
    credentials:        ['GET',   '/api/v2/credentials'],
    adapters:           ['GET',   '/api/v2/collectors/adapters'],
    roots:              ['GET',   '/api/v2/storage-roots'],
    createRoot:         ['POST',  '/api/v2/storage-roots'],
    updateRoot:         ['PATCH', '/api/v2/storage-roots/{id}'],
    probeRoot:          ['POST',  '/api/v2/storage-roots/{id}/probe'],
    cache:              ['GET',   '/api/v2/cache'],
    cacheUsage:         ['GET',   '/api/v2/cache/usage'],
    rebuild:            ['POST',  '/api/v2/cache/rebuild'],
    purge:              ['POST',  '/api/v2/cache/purge'],
    jobs:               ['GET',   '/api/v2/jobs'],
    job:                ['GET',   '/api/v2/jobs/{id}'],
    jobActions:         ['POST',  '/api/v2/jobs/{id}/actions'],
    indexJobs:          ['GET',   '/api/v2/index-jobs'],
    createIndexJob:     ['POST',  '/api/v2/index-jobs'],
    runtime:            ['GET',   '/api/v2/runtime'],
    budget:             ['GET',   '/api/v2/runtime/budget'],
    putBudget:          ['PUT',   '/api/v2/runtime/budget'],
    library:            ['GET',   '/api/v2/library'],
    libraryTree:        ['GET',   '/api/v2/library/tree'],
    librarySources:     ['GET',   '/api/v2/library/sources'],
    libraryFile:        ['GET',   '/api/v2/library/files/{id}'],
    fileContent:        ['GET',   '/api/v2/library/files/{id}/content'],
    fileRecords:        ['GET',   '/api/v2/files/{id}/records'],
    fileTimeline:       ['GET',   '/api/v2/files/{id}/timeline'],
    bindings:           ['GET',   '/api/v2/bindings'],
    rules:              ['GET',   '/api/v2/rules'],
    adminUsers:         ['GET',   '/api/v1/admin/users'],
    adminConfig:        ['GET',   '/api/v1/admin/config'],
    adminStats:         ['GET',   '/api/v1/admin/stats'],
    adminAudit:         ['GET',   '/api/v1/admin/audit'],
    queries:            ['POST',  '/api/v2/queries'],
    query:              ['GET',   '/api/v2/queries/{id}'],
    queryRecords:       ['GET',   '/api/v2/queries/{id}/records'],
    queryContinue:      ['POST',  '/api/v2/queries/{id}/continue'],
    queryExport:        ['GET',   '/api/v2/queries/{id}/export'],
    uploads:            ['POST',  '/api/v2/uploads'],
    upload:             ['GET',   '/api/v2/uploads/{id}'],
    uploadCommit:       ['POST',  '/api/v2/uploads/{id}/commit'],
    events:             ['GET',   '/api/v2/events'],
    pollEvents:         ['GET',   '/api/v2/events/poll']
  };

  var SCENARIOS = [
    ['ready', '正常'], ['loading', '加载中'], ['empty', '空库'],
    ['no-result', '无匹配'], ['error', '请求失败'], ['forbidden', '403 权限不足'],
    ['notfound', '404 资源不存在'], ['conflict', '409 版本冲突'],
    ['precondition', '前置条件未满足'],
    ['saving', '保存中'], ['saved', '已保存'], ['rebuilding', '重建进度'],
    ['offline', '服务离线']
  ];

  var state = {
    mode: 'live',
    scenario: 'loading',
    role: '读取中',
    freshness: '—',
    saved: '—',
    progress: 0,
    revision: 0,
    generatedAt: '',
    calls: [],
    me: null,
    capabilities: {},
    admin: false,
    qaMode: /(?:\?|&)qa=1(?:&|$)/.test(location.search)
  };

  // Remove every built-in fixture synchronously. The first paint happens in
  // L847 before boot() resolves, so leaving R6_SEED in place would briefly show
  // four fabricated sources / three roots / five events and let a user click a
  // source id that does not exist on the server (=> 404 on write).
  files.length = 0;
  R6.sources.length = 0;
  R6.roots.length = 0;
  R6.events.length = 0;
  R6.selected = null;
  R6.search = '';
  R6.sourceFilter = 'all';

  var prefs = { density: 'compact', contrast: 'normal', font_size: 13, wrap: false, time_precision: 'ms', sort: 'time' };
  var probeResult = null;
  var http = null;
  var eventsClient = null;
  var booted = false;
  var refreshTimer = null;

  /* ---- 传输层：优先复用 LBHttp（CSRF + epoch + 统一错误） ----------------- */
  function makeClient() {
    if (window.LBHttp && LBHttp.createClient) return LBHttp.createClient({});
    // Fallback: identical wire contract, used when /assets/r6.js is unavailable.
    var csrf = '';
    return {
      setCsrf: function (t) { csrf = t || ''; },
      reset: function () {}, close: function () {}, epoch: function () { return 0; },
      get: function (p) { return raw('GET', p, null); },
      post: function (p, b) { return raw('POST', p, b); },
      put: function (p, b) { return raw('PUT', p, b); },
      patch: function (p, b) { return raw('PATCH', p, b); },
      del: function (p, b) { return raw('DELETE', p, b); }
    };
    function raw(method, path, body) {
      var headers = {};
      if (body !== undefined && body !== null) headers['Content-Type'] = 'application/json';
      if (method !== 'GET' && method !== 'HEAD') headers['X-LogBench-CSRF'] = csrf;
      return fetch(path, {
        method: method, credentials: 'same-origin', headers: headers,
        body: body === undefined || body === null ? undefined : JSON.stringify(body)
      }).then(function (r) {
        return r.text().then(function (t) {
          var v = null;
          try { v = t ? JSON.parse(t) : {}; } catch (e) { v = {}; }
          if (!r.ok) {
            var d = (v && v.error) || {};
            var err = new Error(d.message || ('请求失败：' + r.status));
            err.code = d.code || ('http_' + r.status);
            err.details = d.details || {};
            err.status = r.status;
            throw err;
          }
          return v || {};
        });
      });
    }
  }

  function path(key, id, params) {
    var def = ENDPOINTS[key] || ['GET', key];
    var url = def[1].replace('/{id}', id ? '/' + encodeURIComponent(id) : '').replace('{id}', id ? encodeURIComponent(id) : '');
    if (params && window.LBHttp && LBHttp.query) url += LBHttp.query(params);
    return url;
  }

  /** Uniform result: {ok, status, data, error}. Never throws for HTTP status. */
  async function request(key, opts) {
    opts = opts || {};
    var def = ENDPOINTS[key] || ['GET', key];
    var method = opts.method || def[0];
    var url = path(key, opts.id, opts.params);
    state.calls.unshift({ at: now(), method: method, path: url });
    if (state.calls.length > 16) state.calls.length = 16;
    try {
      var fn = method === 'GET' ? 'get' : method === 'POST' ? 'post' : method === 'PUT' ? 'put' : method === 'PATCH' ? 'patch' : 'del';
      var data = await http[fn](url, method === 'GET' ? undefined : (opts.body || {}));
      return { ok: true, status: 200, data: data || {}, key: key };
    } catch (e) {
      var status = Number(e && e.status) || 0;
      var code = (e && e.code) || 'request_failed';
      var message = (e && e.message) || '请求失败';
      // Only the primary request owns the visible state. Eight parallel boot
      // requests must not let a secondary 404 overwrite the real outcome of
      // the action the user just took.
      if (opts.primary !== false) {
        state.lastError = message;
        state.lastCode = code;
        state.scenario = classifyFailure({ status: status, code: code });
      } else {
        state.secondaryErrors = (state.secondaryErrors || []).concat([
          { path: url, code: code, message: message }
        ]).slice(-8);
      }
      return { ok: false, status: status, code: code, error: message, data: {}, key: key };
    }
  }

  /** The backend uses 409 for both revision conflicts and state-machine
   *  preconditions (source_disabled / probe_required / target_overlap).
   *  They need different recovery guidance, so never collapse them. */
  function classifyFailure(r) {
    if (!r) return 'error';
    if (r.status === 403) return 'forbidden';
    if (r.status === 404) return 'notfound';
    if (r.status === 0) return 'offline';
    if (r.status === 409) {
      return ['revision_conflict', 'probe_stale', 'probe_expired'].indexOf(r.code) >= 0
        ? 'conflict' : 'precondition';
    }
    return 'error';
  }

  function actionId() {
    return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  function now() {
    return new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  function humanBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    var u = ['KiB', 'MiB', 'GiB', 'TiB'], i = -1;
    do { n /= 1024; i += 1; } while (n >= 1024 && i < 3);
    return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + ' ' + u[i];
  }

  function shortTime(v) {
    if (!v) return '—';
    var s = String(v);
    var m = s.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : s.slice(-8);
  }

  /* ---- 真实响应 → 原型视图模型 ------------------------------------------- */
  var STATUS_TONE = { queued: '', reading: 'blue', framing: 'blue', uploading: 'blue', uploaded: 'blue', indexing: 'blue', indexed: 'good', paused: '', failed: 'bad', cancelled: '' };

  function projectSources(intake, sources) {
    var byId = {};
    (sources.items || []).forEach(function (s) { byId[s.id] = s; });
    return (intake.items || []).map(function (it) {
      var full = byId[it.id] || {};
      var caps = it.capabilities || {};
      var policy = full.policy || {};
      var maps = (full.mappings || []).map(function (m) {
        return {
          remote: m.remoteDir || m.remote || '',
          root: m.rootId || m.root || 'main',
          target: m.targetSubdir || m.target || '',
          rule: m.ruleRevision || m.rule || '目录继承',
          include: m.include || '*.log, *.log.*',
          exclude: m.exclude || '.logbench-*',
          depth: m.recursive == null ? 8 : m.recursive
        };
      });
      if (!maps.length) maps = [{ remote: it.endpoint || '/', root: 'main', target: it.id || 'source', rule: '目录继承', include: '*.log', exclude: '', depth: 8 }];
      var status = it.paused ? 'paused' : !it.enabled ? 'idle'
        : it.lagBytes > 0 ? 'recovering'
        : (caps.rangeRead === 'supported' || caps.live !== false) ? 'following' : 'limited';
      return {
        id: it.id,
        name: it.label || full.label || it.id,
        protocol: (it.adapter || full.adapter || 'sftp').toLowerCase(),
        endpoint: it.endpoint || full.endpoint || '',
        port: Number(policy.port) || ((it.adapter === 'http') ? 443 : (it.adapter === 'smb') ? 445 : 22),
        credential: full.credentialRef || '服务端凭据引用',
        mode: policy.mode || (caps.rangeRead === 'supported' ? 'live' : 'schedule'),
        status: status,
        poll: Number(policy.poll) || 2,
        scan: Number(policy.scan) || 60,
        first: policy.first || 'all',
        verification: policy.verification || (caps.rangeRead === 'supported' ? 'append' : 'snapshot'),
        readers: full.acl === 'members' ? '管理员与研发组' : '仅管理员',
        count: Number(it.files) || 0,
        raw: humanBytes(it.archivedBytes),
        indexed: humanBytes(it.contiguousBytes),
        lag: it.lagBytes > 0 ? '落后 ' + humanBytes(it.lagBytes) : (it.paused ? '检查点与归档保留' : '已追上'),
        coverage: it.archivedBytes > 0 ? Math.min(100, Math.round(100 * (it.contiguousBytes || 0) / it.archivedBytes)) : 0,
        maps: maps,
        revision: Number(it.revision != null ? it.revision : full.revision) || 0,
        lastSeenAt: it.lastSeenAt,
        lastError: it.lastError,
        gaps: it.gaps || [],
        capabilities: caps
      };
    });
  }

  function projectRoots(payload) {
    return (payload.items || []).map(function (r) {
      return {
        id: r.id || r.root_id,
        name: r.label || r.id,
        path: r.path || '',
        cache: r.path ? r.path.replace(/\/$/, '') + '/.index' : '—',
        state: r.state === 'online' || r.enabled ? 'online' : 'offline',
        free: r.free_bytes == null ? '待预检' : humanBytes(r.free_bytes) + ' 可用',
        type: r.kind || (r.network_fs ? '网络挂载' : '本地磁盘'),
        defaultRule: r.defaultRule || '目录继承',
        revision: Number(r.revision) || 0,
        scope: r.scope,
        enabled: r.enabled,
        reason: r.reason
      };
    });
  }

  function projectJobs(payload) {
    return (payload.items || []).slice(0, 50).map(function (j) {
      return {
        id: j.id,
        at: shortTime(j.updated_at || j.created_at),
        source: (j.payload && (j.payload.source_id || j.payload.label)) || j.kind || '任务',
        kind: j.state || 'queued',
        tone: STATUS_TONE[j.state] || '',
        text: (j.progress != null ? '进度 ' + Math.round(Number(j.progress) * 100) + '% · ' : '') +
          (j.error || j.resume_reason || (j.kind + ' · ' + j.state)),
        state: j.state,
        revision: j.revision
      };
    });
  }

  // server/runtime/api.py Budget dataclass fields. base = administrator's
  // configured values; effective = pressure-shrunk values. The form shows base.
  function projectBudget(payload) {
    var b = (payload && (payload.base || payload.effective || payload.budget)) || {};
    var num = function (v, fallback) { return v == null ? fallback : Number(v); };
    return {
      collect: num(b.transfer_connections, 4),
      perHost: num(b.per_host_connections, 2),
      parse: num(b.parse_slots, 2),
      chunk: num(b.writer_slots, 4),
      poll: 2,
      memory: Math.round(num(b.mem_soft_bytes, 16 * 1073741824) / 1073741824),
      backfill: num(b.history_slots, 2),
      query_slots: num(b.query_slots, 2),
      total_cpu: num(b.total_cpu, 8),
      hot_cache_bytes: Math.round(num(b.hot_cache_bytes, 4 * 1073741824) / 1073741824),
      upload_slots: num(b.upload_slots, 4),
      per_principal_uploads: num(b.per_principal_uploads, 2),
      per_principal_queries: num(b.per_principal_queries, 1),
      interactive_reserve: num(b.interactive_reserve, 1),
      writer_slots: num(b.writer_slots, 4),
      revision: Number(payload && payload.revision) || 0
    };
  }

  function hydrate(d) {
    if (!d) return;
    if (d.sources) R6.sources = d.sources;
    if (d.roots) R6.roots = d.roots;
    if (d.events) R6.events = d.events;
    if (d.tuning) R6.tuning = Object.assign({}, R6.tuning, d.tuning);
    if (d.revision != null) state.revision = Number(d.revision) || state.revision;
  }

  /* ---- 启动：身份 → 并行首屏数据 → 渲染 → SSE ------------------------------ */
  async function boot() {
    http = makeClient();
    var me = await request('me');
    if (!me.ok) {
      state.scenario = classifyFailure(me);
      state.freshness = '—';
      state.saved = '未连接';
      render(false); paintStatus();
      return;
    }
    var m = me.data || {};
    state.me = m.user || null;
    state.csrfToken = m.csrfToken || '';
    state.capabilities = m.capabilities || {};
    state.admin = !!(state.capabilities && state.capabilities.admin);
    state.role = (state.me && (state.me.role === 'admin' ? '管理员' : state.me.name)) || '访客';
    if (http.setCsrf) http.setCsrf(m.csrfToken || '');

    // Preferences are v1, and carry their own revision for optimistic writes.
    var pref = await request('preferences');
    if (pref.ok && pref.data && pref.data.value) Object.assign(prefs, pref.data.value);
    state.prefRevision = (pref.data && pref.data.revision) || 0;
    if (state.admin) {
      var cred = await request('credentials', { primary: false });
      if (cred.ok) state.credentials = cred.data.items || [];
    }

    var secondary = { primary: false };
    var extra = state.admin ? await Promise.all([
      request('rules', secondary),
      request('adminUsers', secondary), request('adminConfig', secondary),
      request('adminStats', secondary),
      request('adminAudit', { primary: false, params: { limit: 12 } })
    ]) : [];
    var results = await Promise.all([
      request('intake'), request('sources', secondary), request('roots', secondary),
      request('jobs', secondary), request('budget', secondary), request('library', secondary),
      request('cache', secondary)
    ]);
    var intake = results[0], sources = results[1], roots = results[2],
        jobs = results[3], budget = results[4], library = results[5],
        cache = results[6];

    var okCount = results.filter(function (r) { return r.ok; }).length;
    if (!okCount) {
      state.scenario = 'offline';
      render(false); paintStatus();
      return;
    }
    // A viewer legitimately gets 403 on admin-only endpoints: keep the page
    // readable in read-only mode instead of replacing it with an error wall.
    var denied = results.some(function (r) { return r.status === 403; });
    if (denied && !state.admin) state.scenario = 'forbidden';

    var model = {
      sources: intake.ok ? projectSources(intake.data, sources.ok ? sources.data : { items: [] }) : R6.sources,
      roots: roots.ok ? projectRoots(roots.data) : R6.roots,
      events: jobs.ok ? projectJobs(jobs.data) : R6.events,
      tuning: budget.ok ? projectBudget(budget.data) : R6.tuning,
      revision: budget.ok ? Number(budget.data.revision) || 0 : state.revision
    };
    hydrate(model);
    state.library = library.ok ? library.data : { items: [] };
    projectLibraryToFiles(state.library.items);
    state.cache = cache.ok ? cache.data : { items: [] };
    if (extra.length) {
      state.adminData = {
        rules: extra[0].ok ? extra[0].data : { items: [] },
        users: extra[1].ok ? extra[1].data : { items: [] },
        config: extra[2].ok ? extra[2].data : {},
        stats: extra[3].ok ? extra[3].data : {},
        audit: extra[4].ok ? extra[4].data : { items: [] }
      };
      projectServerRules(state.adminData.rules.items);
    }
    // /api/v2/runtime closes the connection in this deployment (backend-side
    // status()); /api/v2/runtime/budget already carries base/effective/notes.
    state.runtime = budget.ok ? budget.data : {};
    state.generatedAt = (intake.data && intake.data.generatedAt) || now();
    state.freshness = shortTime(state.generatedAt) || now();
    state.saved = '已同步';
    if (state.scenario === 'loading' || state.scenario === 'offline') {
      state.scenario = R6.sources.length ? 'ready' : 'empty';
    }
    booted = true;
    render(false);
    paintStatus();
    startEvents();
  }

  /* ---- SSE：复用 LBEventClient；不可用时轮询 /api/v2/events/poll ----------- */
  function onLiveEvent(ev) {
    if (!ev || !ev.kind) return;
    var map = {
      archive_commit: '归档提交', index_commit: '索引提交', record_revision: '记录修订',
      rotation: '文件轮转', gap: '采集缺口', source_error: '采集异常', task_progress: '任务进度'
    };
    r6Event(ev.sourceId || ev.fileId || '采集服务', map[ev.kind] || ev.kind,
      ev.message || ev.note || (ev.kind + (ev.fileId ? ' · ' + ev.fileId : '')),
      ev.kind === 'source_error' || ev.kind === 'gap' ? 'warn' : 'good');
    if (S.view === 'sync' && R6.tab === 'events') render(true);
    scheduleRefresh();
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { refresh(true); }, 1200);
  }

  function startEvents() {
    if (window.LBEventClient && LBEventClient.createClient) {
      try {
        // LBEventClient injects both transports. Without them start() silently
        // degrades to a polling mode that never issues a request.
        var eventOptions = {
          url: '/api/v2/events',
          pollUrl: '/api/v2/events/poll',
          fetchImpl: function (url) {
            return fetch(url, { credentials: 'same-origin' }).then(function (r) {
              if (!r.ok) throw new Error('事件轮询失败：' + r.status);
              return r.json();
            });
          },
          onEvent: onLiveEvent,
          onState: function (s) {
            var mode = s && s.mode;
            state.eventMode = mode || 'idle';
            if ((mode === 'offline' || mode === 'closed') && state.scenario === 'ready') {
              state.scenario = 'offline'; paintStatus();
            } else if ((mode === 'live' || mode === 'polling') && state.scenario === 'offline') {
              state.scenario = 'ready'; paintStatus();
            }
          },
          onResync: function () { refresh(true); }
        };
        if (typeof EventSource === 'function') {
          eventOptions.streamFactory = function (url) { return new EventSource(url); };
        }
        eventsClient = LBEventClient.createClient(eventOptions);
        LBEventClient.attachPageHooks(eventsClient, window);
        // createClient() only builds the client; without start() no EventSource
        // is opened and the real-time channel is silently dead.
        eventsClient.start();
        return;
      } catch (e) { /* fall through to polling */ }
    }
    var last = 0;
    setInterval(async function () {
      var r = await request('pollEvents', { params: { last: last || '' }, primary: false });
      if (!r.ok) return;
      (r.data.events || []).forEach(function (ev) { last = Math.max(last, Number(ev.id) || 0); onLiveEvent(ev); });
    }, 4000);
  }

  /* ---- 状态文本与状态件（与预览版同接口） -------------------------------- */
  function statusText() {
    if (state.scenario === 'loading') return '加载中 · 正在读取 /api/v2/intake';
    if (state.scenario === 'empty') return '空库 · 尚无远程日志源';
    if (state.scenario === 'no-result') return '无匹配 · 筛选条件已保留，采集任务不受影响';
    if (state.scenario === 'notfound') return '资源不存在（404） · 列表已刷新';
    if (state.scenario === 'error') return '请求失败 · ' + (state.lastError || '请重试');
    if (state.scenario === 'forbidden') return state.admin
      ? '权限不足（403） · 服务端拒绝本次请求'
      : '只读身份 · 采集管理操作需要管理员权限';
    if (state.scenario === 'conflict') return '版本冲突（409） · 输入已保留为草稿';
    if (state.scenario === 'precondition') return '前置条件未满足 · ' + (state.lastError || '请先完成预检与启用');
    if (state.scenario === 'saving') return '保存中 · 等待服务端回执';
    if (state.scenario === 'saved') return '已保存 · 服务端回执 revision ' + state.revision;
    if (state.scenario === 'rebuilding') return '重建中 ' + state.progress + '% · 旧索引继续可读';
    if (state.scenario === 'offline') return '服务未连接 · 写操作已停用';
    return (state.admin ? '服务已连接' : '服务已连接 · 只读') + ' · ' +
      R6.sources.length + ' 个日志源 · ' + state.role + ' · ' + state.saved;
  }

  function dataFreshness() {
    if (state.scenario === 'rebuilding') return '重建进度 ' + state.progress + '% · 旧索引可读';
    if (state.scenario === 'saving') return '提交中 · 请勿关闭页面';
    if (state.scenario === 'saved') return '已保存 · revision ' + state.revision;
    return '上次成功响应 ' + state.freshness + ' · revision ' + state.revision;
  }

  function scenarioSelect() {
    if (!state.qaMode) return '';
    return '<label for="lb-scenario" class="r6-sub" style="margin:0 6px">状态</label>' +
      '<select id="lb-scenario" class="select" aria-label="状态检视（QA）" style="height:18px;min-height:18px;font-size:11px;padding:0 4px;margin-right:8px">' +
      SCENARIOS.map(function (s) {
        return '<option value="' + s[0] + '"' + (state.scenario === s[0] ? ' selected' : '') + '>' + s[1] + '</option>';
      }).join('') + '</select>';
  }

  function stateBanner() {
    var s = state.scenario;
    if (s === 'ready') return '';
    if (s === 'saved') return '<div class="r6-note">' + I('check', 'sm') + '已保存 · 服务端已回执（revision ' + state.revision + '）。草稿已清除。</div>';
    if (s === 'loading') return '<div class="r6-note">' + I('clock', 'sm') + '加载中 · 正在读取采集状态，已保留上次成功快照，界面不跳动。</div><div class="r6-progress" aria-label="加载中"><span style="width:45%"></span></div>';
    if (s === 'saving') return '<div class="r6-note">' + I('clock', 'sm') + '保存中 · 正在提交到服务端；关闭页面不会丢失已填内容。</div><div class="r6-progress" aria-label="保存中"><span style="width:70%"></span></div>';
    if (s === 'rebuilding') return '<div class="r6-note">' + I('clock', 'sm') + '重建规则结果 · 旧索引继续可读，原文不重新下载。</div><div class="r6-progress" aria-label="重建进度 ' + state.progress + '%"><span style="width:' + state.progress + '%"></span></div>';
    if (s === 'empty') return '<div class="r6-empty">' + I('database', 'lg') + '<strong>还没有远程日志源</strong><p>添加第一个远程目录后开始采集。已登记的存储位置可以直接使用。</p>' + (state.admin ? r6Btn('添加日志源', 'new-source', 'plus', 'primary') : '') + '</div>';
    if (s === 'no-result') return '<div class="r6-empty">' + I('search', 'lg') + '<strong>没有匹配「' + E(R6.search || '') + '」的日志源</strong><p>这不是空库。调整名称、目录或状态条件；已有采集任务不受筛选影响。</p>' + r6Btn('清除筛选', 'clear-filters', 'clear', 'sm') + '</div>';
    if (s === 'notfound') return '<div class="r6-empty">' + I('warning', 'lg') + '<strong>日志源不存在或已被删除</strong><p>详情请求返回 404。列表已刷新为服务端最新状态，原文归档与索引不受影响。</p>' + r6Btn('返回日志源列表', 'clear-filters', 'left', 'sm') + '</div>';
    if (s === 'error') return '<div class="r6-errorbox" role="alert"><strong>无法读取采集状态</strong><p>' + E(state.lastError || '请求失败') + '。已保留上次成功快照，期间没有执行写操作。</p>' + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'forbidden') return '<div class="r6-errorbox" role="alert"><strong>' + (state.admin ? '权限不足（403）' : '只读身份') + '</strong><p>' + (state.admin
      ? '服务端拒绝了本次请求。检查当前身份与服务配置后重试。'
      : '当前身份 <b>' + E(state.role) + '</b> 只有读取权限，无法暂停采集、保存配置或重建索引。页面保持只读，所有输入都已保留；联系管理员后可重试。') + '</p>' + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'conflict') return '<div class="r6-note bad" role="alert"><strong>版本冲突（409）</strong><p>该对象已被其他人更新。你填写的内容已保留为草稿，未被覆盖，也不会自动合并。</p><div class="r6-inline-actions">' + r6Btn('查看数据链路', 'datalink', 'info', 'sm') + r6Btn('载入新版本后重试', 'retry', 'refresh', 'sm') + '</div></div>';
    if (s === 'precondition') {
      var preconditionHint = (state.lastCode === 'source_disabled' || state.lastCode === 'probe_required')
        ? '远程源需要先「运行预检 → 启用」，之后才能暂停或恢复；未通过预检不会悄悄降级为可用状态。'
        : '按提示补齐前置条件后重试；已填内容与已有索引均不会被删除。';
      return '<div class="r6-errorbox" role="alert"><strong>操作未执行 · 前置条件未满足</strong>' +
        '<p>服务端返回（<code class="mono">' + E(state.lastCode || '') + '</code>）：' + E(state.lastError || '') +
        ' ' + preconditionHint + '</p>' +
        '<div class="r6-inline-actions">' + r6Btn('添加/编辑日志源', 'new-source', 'plus', 'sm') + r6Btn('重试', 'retry', 'refresh', 'sm') + '</div></div>';
    }
    if (s === 'offline') return '<div class="r6-note warn">' + I('warning', 'sm') + '服务未连接 · 显示 ' + E(state.freshness) + ' 的上次成功快照。写操作已停用，恢复连接后可重试。</div>';
    return '';
  }

  var painting = false;
  function paintStatus() {
    if (painting) return;
    painting = true;
    try {
      var row = document.querySelector('.app-status .row');
      if (row) row.innerHTML = I('lock', 'sm') + statusText();
      var extra = document.querySelector('.app-status .status-right .extra');
      if (extra) extra.textContent = dataFreshness();
      var tag = document.querySelector('.lb-header .prototype-tag');
      if (tag) tag.textContent = state.scenario === 'offline' ? '服务未连接' : (state.admin ? '服务已连接' : '只读');
      var sel = document.getElementById('lb-scenario');
      if (sel && sel.value !== state.scenario) sel.value = state.scenario;
      // Read-only / permission state must be visible on the write controls too.
      var canWrite = state.admin && ['offline', 'error', 'forbidden'].indexOf(state.scenario) < 0;
      var primary = document.querySelector('.r6-head [data-r6="new-source"], .r6-head [data-r6="new-root"]');
      if (primary) {
        primary.disabled = !canWrite;
        primary.setAttribute('aria-label', canWrite ? '添加' : '需要管理员权限且服务已连接，当前不可添加');
      }

      var html = stateBanner();
      var host = document.getElementById('lb-state');
      var pane = document.querySelector('.r6-content > .r6-pane');
      var full = ['empty', 'no-result', 'notfound', 'loading'].indexOf(state.scenario) >= 0;
      if (html && pane) {
        if (!host) {
          host = document.createElement('div');
          host.id = 'lb-state';
          pane.parentNode.insertBefore(host, pane);
        }
        host.style.cssText = full
          ? 'flex:1;min-height:0;overflow:auto;padding:12px 18px;background:#fff'
          : 'flex-shrink:0;padding:8px 18px;border-bottom:1px solid var(--ud-line);background:#fff';
        if (host.innerHTML !== html) host.innerHTML = html;
        // .r6-pane{display:flex} outranks the UA [hidden] rule, so `hidden`
        // alone does nothing and the full-screen state banner gets squeezed
        // into a half-height row. Use an explicit inline display instead.
        pane.hidden = full;
        pane.style.display = full ? 'none' : '';
      } else if (host) {
        host.remove();
        if (pane) { pane.hidden = false; pane.style.display = ''; }
      } else if (pane) {
        pane.hidden = false; pane.style.display = '';
      }
    } finally { painting = false; }
  }

  function setScenario(v) { state.scenario = v; if (v === 'rebuilding') state.progress = 0; paintStatus(); }

  /* ---- 动作：全部走真实端点 ---------------------------------------------- */
  async function refresh(silent) {
    if (!booted) return boot();
    if (!silent) { state.scenario = 'loading'; paintStatus(); }
    var secondary = { primary: false };
    var results = await Promise.all([
      request('intake'), request('sources', secondary), request('roots', secondary),
      request('jobs', secondary), request('budget', secondary), request('library', secondary),
      request('cache', secondary)
    ]);
    var intake = results[0], sources = results[1];
    if (!intake.ok) {
      state.scenario = classifyFailure(intake);
      state.lastError = intake.error;
      state.lastCode = intake.code;
      paintStatus();
      return;
    }
    hydrate({
      sources: projectSources(intake.data, sources.ok ? sources.data : { items: [] }),
      roots: results[2].ok ? projectRoots(results[2].data) : R6.roots,
      events: results[3].ok ? projectJobs(results[3].data) : R6.events,
      tuning: results[4].ok ? projectBudget(results[4].data) : R6.tuning,
      revision: results[4].ok ? Number(results[4].data.revision) || 0 : state.revision
    });
    if (results[5].ok) state.library = results[5].data;
    if (results[6].ok) state.cache = results[6].data;
    if (results[4].ok) state.runtime = results[4].data;
    state.generatedAt = intake.data.generatedAt || now();
    state.freshness = shortTime(state.generatedAt) || now();
    if (state.scenario !== 'forbidden') state.scenario = R6.sources.length ? 'ready' : 'empty';
    render(false); paintStatus();
    if (!silent) notify('已从 /api/v2/intake 更新 ' + R6.sources.length + ' 个日志源');
  }

  function connect() { refresh(); }

  function retry() {
    if (state.scenario === 'conflict') {
      state.scenario = 'ready';
      paintStatus();
      notify('已重新载入服务端版本，草稿仍保留，可重新保存');
      return;
    }
    refresh();
  }

  function requireWrite() {
    if (!state.admin) {
      state.scenario = 'forbidden'; paintStatus();
      notify('当前身份只有读取权限，无法执行此操作。', true);
      return false;
    }
    return true;
  }

  async function pause(id) {
    if (!requireWrite()) return;
    var x = r6Source(id);
    if (!x) return;
    var action = x.status === 'paused' ? 'resume' : 'pause';
    state.scenario = 'saving'; paintStatus();
    var r = await request('actions', {
      id: id,
      body: { action: action, actionId: actionId(), expectedRevision: x.revision }
    });
    if (!r.ok) { paintStatus(); return; }
    state.revision = Number(x.revision) + 1;
    state.scenario = 'ready';
    state.saved = '已保存';
    r6Event(x.name, action === 'pause' ? '暂停采集' : '继续采集',
      action === 'pause' ? '暂停新的传输；不删除原文或解析缓存。' : '从已提交检查点继续，历史归档保留。',
      action === 'pause' ? '' : 'blue');
    await refresh(true);
  }

  async function probe() {
    if (!requireWrite()) return;
    r6CaptureDraft();
    var d = r6Draft || {};
    var host = document.getElementById('r6-probe-result');
    if (host) host.innerHTML = '<div class="r6-note">正在预检 ' + E(d.endpoint || '') + ' …</div>';
    // A probe can only run against a persisted source: save the draft first.
    var id = d.id;
    if (!id) {
      var created = await request('createSource', { body: draftToApi(d) });
      if (!created.ok) {
        state.scenario = classifyFailure(created);
        state.lastError = created.error; state.lastCode = created.code;
        if (host) host.innerHTML = '<div class="r6-errorbox" role="alert">预检前需要先保存草稿：' + E(created.error) + '</div>';
        paintStatus(); return;
      }
      id = created.data.item && created.data.item.id;
      r6Draft.id = id;
      if (created.data.item) r6Draft.revision = created.data.item.revision;
    }
    var r = await request('probe', { id: id, body: {} });
    var p = (r.data && r.data.probe) || {};
    var checks = (p.checks || []).map(function (c) {
      var ok = c.ok !== false && c.status !== 'failed' && c.state !== 'failed';
      return { key: c.key || c.name || 'check', label: c.label || c.name || c.key || '检查项', ok: ok, detail: c.detail || c.message || c.reason || (ok ? '通过' : '未通过') };
    });
    var ok = r.ok && p.ok !== false && checks.every(function (c) { return c.ok; });
    r6Probe = true;
    r6ProbeKind = ok ? 'ready' : 'auth';
    LB.probeResult = { ok: ok, checks: checks, fingerprint: (r.data && r.data.fingerprint) || p.fingerprint || '', revision: (r.data && r.data.revision) || p.configRevision || 0 };
    var save = document.querySelector('#dialog [data-r6="wizard-save"]');
    if (save) save.disabled = !ok;
    if (host) {
      host.innerHTML = (checks.length ? checks.map(function (c) {
        return '<div class="r6-probe-row ' + (c.ok ? '' : 'bad') + '">' + I(c.ok ? 'check' : 'error') + '<span>' + E(c.label) + ' · ' + E(c.detail) + '</span></div>';
      }).join('') : '<div class="r6-mininote">服务端未返回检查项；预检结果：' + (ok ? '通过' : '未通过') + '</div>') +
        (ok ? '' : '<div class="r6-note bad" style="margin-top:12px">预检未通过，保存已停用。修正凭据、主机密钥或文件清单后重新预检；不会自动降级为匿名连接。</div>');
    }
    r6Event(d.name || '新日志源', '预检完成',
      ok ? '连接、目录发现、范围读取与目标权限全部通过。' : '预检未通过，配置未提交。', ok ? 'good' : 'warn');
  }

  // Credentials live server-side as cred_* refs. A label that is not a
  // registered, ready ref must be sent as empty (=> None), never as prose.
  function resolveCredentialRef(label) {
    var list = state.credentials || [];
    for (var i = 0; i < list.length; i += 1) {
      var c = list[i];
      if (c.ref === label || c.label === label || c.id === label) return c.ref || c.id || '';
    }
    return '';
  }

  function splitPatterns(v) {
    return String(v || '').split(',').map(function (s) { return s.trim(); })
      .filter(Boolean).slice(0, 32);
  }

  // Wire contract: server/collectors/sources.py POLICY_KEYS, MODES,
  // _mapping_payload(). The prototype's live/schedule/snapshot vocabulary is
  // projected onto manual/scheduled/follow; intervals go into policy.
  function draftToApi(d) {
    var adapter = (d.protocol || 'sftp').toLowerCase();
    if (['sftp', 'smb', 'http', 'scp'].indexOf(adapter) < 0) adapter = 'sftp';
    var mode = d.mode === 'live' ? 'follow' : d.mode === 'snapshot' ? 'manual' : 'scheduled';
    var policy = {
      activeInterval: Math.max(1, Math.min(3600, Number(d.poll) || 2)),
      discoveryInterval: Math.max(1, Math.min(86400, Number(d.scan) || 60)),
      maxDepth: 8,
      allowLoopback: /^(127\.|localhost|\[?::1\])/.test(String(d.endpoint || '')),
      appendContract: d.verification !== 'snapshot'
    };
    if (adapter === 'http') {
      policy.directoryMode = mode === 'manual' ? 'manifest' : 'listing';
      policy.connectTimeout = 10;
      policy.readTimeout = 30;
    }
    return {
      adapter: adapter,
      label: d.name || '',
      endpoint: d.endpoint || '',
      credentialRef: resolveCredentialRef(d.credential),
      acl: d.readers === '管理员与研发组' ? 'members' : 'admin',
      policy: policy,
      mappings: (d.maps || []).map(function (m) {
        return {
          remoteDir: m.remote || '/',
          rootId: m.root || 'legacy',
          targetSubdir: m.target || '',
          include: splitPatterns(m.include || '*.log'),
          exclude: splitPatterns(m.exclude || ''),
          recursive: true,
          mode: mode,
          ruleRevision: m.rule || ''
        };
      })
    };
  }

  async function saveSource() {
    if (!requireWrite()) return;
    r6CaptureDraft();
    var error = r6Validate(r6Draft);
    if (error) { r6ShowFormError(error); return; }
    if (!LB.probeResult || !LB.probeResult.ok) { r6ShowFormError('请先运行预检并全部通过。'); return; }
    state.scenario = 'saving'; paintStatus();
    var d = clone(r6Draft);
    var r = d.id
      ? await request('updateSource', { id: d.id, body: Object.assign({}, draftToApi(d), { expectedRevision: LB.probeResult.revision || d.revision }) })
      : await request('createSource', { body: draftToApi(d) });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code;
      r6ShowFormError('保存失败：' + r.error + '。草稿已保留，可重试。');
      paintStatus(); return;
    }
    state.revision = Number((r.data.item && r.data.item.revision) || LB.probeResult.revision) || state.revision + 1;
    state.scenario = 'saved';
    state.saved = '已保存';
    r6Event(d.name, '配置保存', '目录映射已提交到服务端；采集任务按计划启动。', 'good');
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    LB.probeResult = null;
    await refresh(true);
    state.scenario = 'saved';
    paintStatus();
  }

  async function saveRoot() {
    if (!requireWrite()) return;
    var name = document.getElementById('r6-root-name').value.trim();
    var p = document.getElementById('r6-root-path').value.trim();
    var cache = document.getElementById('r6-root-cache').value.trim();
    var err = document.getElementById('r6-root-error');
    if (!name || !p.startsWith('/') || !cache.startsWith('/') || p.includes('..') || cache.includes('..')) {
      err.textContent = '请填写名称与不包含 .. 的绝对路径。'; return;
    }
    if (R6.roots.some(function (r) {
      return r.id !== R6.editRoot && (p === r.path || p.startsWith(r.path + '/') || r.path.startsWith(p + '/'));
    })) { err.textContent = '存储根目录不可重复或相互包含。'; return; }
    state.scenario = 'saving'; paintStatus();
    var r = R6.editRoot
      ? await request('updateRoot', { id: R6.editRoot, body: { label: name, expectedRevision: (r6Root(R6.editRoot) || {}).revision || 1 } })
      : await request('createRoot', { body: { label: name, path: p, kind: 'local' } });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code; paintStatus(); return;
    }
    var id = R6.editRoot || (r.data.item && (r.data.item.id || r.data.item.root_id));
    if (id) await request('probeRoot', { id: id, body: {} });
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    R6.tab = 'storage'; S.view = 'sync';
    await refresh(true);
    notify('存储位置已保存并完成权限预检');
  }

  async function saveTuning() {
    if (!requireWrite()) return;
    var ks = ['collect', 'perHost', 'parse', 'backfill', 'memory'];
    var v = {};
    ks.forEach(function (k) { v[k] = Number(document.getElementById('r6-tune-' + k).value); });
    if (ks.some(function (k) { return !Number.isInteger(v[k]) || v[k] < 1 || v[k] > 64; }) || v.perHost > v.collect) {
      document.getElementById('r6-tune-error').textContent = '请输入 1–64 的整数，且单主机并发不能超过总并发。';
      return;
    }
    state.scenario = 'saving'; paintStatus();
    // server/runtime/api.py:345 compares expectedRevision to its own revision,
    // which legitimately starts at 0. Never coerce 0 to a truthy fallback.
    var budgetRevision = state.runtime && state.runtime.revision != null
      ? Number(state.runtime.revision)
      : (R6.tuning.revision != null ? Number(R6.tuning.revision) : 0);
    var r = await request('putBudget', {
      body: {
        expectedRevision: Number.isFinite(budgetRevision) ? budgetRevision : 0,
        value: {
          transfer_connections: v.collect,
          per_host_connections: v.perHost,
          parse_slots: v.parse,
          history_slots: v.backfill,
          mem_soft_bytes: Math.round(v.memory * 1073741824)
        }
      }
    });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.saved = '未保存';
      state.lastError = r.error; state.lastCode = r.code;
      var box = document.getElementById('r6-tune-error');
      if (box) box.textContent = '保存失败：' + r.error + '。输入已保留；刷新页面取回最新版本号后重试。';
      paintStatus(); return;
    }
    hydrate({ tuning: Object.assign({}, v, { revision: r.data.revision }) });
    state.runtime = Object.assign({}, state.runtime, r.data || {});
    state.revision = Number(r.data.revision) || state.revision;
    state.scenario = 'saved'; state.saved = '已保存';
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    render(false); paintStatus();
    notify('资源预算已保存到 /api/v2/runtime/budget');
  }

  async function rebuildCache() {
    if (!requireWrite()) return;
    var fileId = (state.cache.items && state.cache.items[0] && state.cache.items[0].fileId) || '';
    if (!fileId) {
      // 'empty' means "no remote sources" and would be the wrong recovery hint
      // here. Use a precondition with an explicit, actionable reason.
      state.lastCode = 'no_cache_entry';
      state.lastError = '当前缓存列表为空，没有可重建的索引。';
      state.scenario = 'precondition'; paintStatus();
      notify(state.lastError, true); return;
    }
    state.scenario = 'rebuilding'; state.progress = 8; paintStatus();
    var r = await request('rebuild', { body: { fileId: fileId, layers: ['rules', 'search'], actionId: actionId() } });
    if (!r.ok) {
      state.scenario = classifyFailure(r);
      state.lastError = r.error; state.lastCode = r.code; paintStatus(); return;
    }
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    var timer = setInterval(async function () {
      var jobs = await request('indexJobs', { params: { limit: 20 }, primary: false });
      var live = ((jobs.data && jobs.data.items) || []).filter(function (j) { return ['queued', 'reading', 'framing', 'indexing'].indexOf(j.state) >= 0; });
      state.progress = live.length ? Math.min(92, state.progress + 7) : 100;
      if (state.scenario === 'rebuilding') paintStatus();
      if (state.progress >= 100) {
        clearInterval(timer);
        state.scenario = 'saved'; state.saved = '已重建';
        r6Event('解析缓存', '重建完成', '新规则结果版本已验证并切换；原文未重新下载。', 'good');
        R6.tab = 'cache'; S.view = 'sync';
        await refresh(true);
        state.scenario = 'saved'; paintStatus();
      }
    }, 900);
  }

  function exportEvents() {
    download('logbench-r6-jobs.json', JSON.stringify({
      format: 'logbench.jobs/1', exported_at: new Date().toISOString(),
      revision: state.revision, jobs: R6.events
    }, null, 2), 'application/json');
  }

  async function savePref(key, value) {
    prefs[key] = value;
    var next = {};
    Object.keys(prefs).forEach(function (k) { next[k] = prefs[k]; });
    state.saved = '保存中'; paintStatus();
    var r = await request('putPreferences', { body: { value: next, expectedRevision: state.prefRevision || 0 } });
    if (!r.ok) {
      state.saved = '未保存'; state.lastError = r.error;
      state.scenario = r.status === 409 ? 'conflict' : 'error';
      paintStatus(); return;
    }
    state.prefRevision = r.data.revision || state.prefRevision;
    state.saved = '已保存'; paintStatus();
  }

  /* ---- 数据链路 / 备份 ---------------------------------------------------- */
  function datalink() {
    var rows = Object.keys(ENDPOINTS).map(function (k) {
      var e = ENDPOINTS[k];
      return '<tr><td class="mono">' + E(k) + '</td><td class="mono">' + E(e[0]) + ' ' + E(e[1]) + '</td></tr>';
    }).join('');
    var calls = state.calls.length ? state.calls.slice(0, 8).map(function (c) {
      return '<div class="r6-meta mono">' + E(c.at) + ' · ' + E(c.method) + ' ' + E(c.path) + '</div>';
    }).join('') : '<p class="r6-mininote">尚无请求记录。</p>';
    dialog('数据链路', '<div class="r6-form">' +
      '<div class="r6-note ' + (state.scenario === 'offline' ? 'warn' : '') + '">' + I('database') +
      (state.scenario === 'offline' ? '服务未连接，显示上次成功快照。' : '已连接 /api/v2 服务，界面数据来自真实回执。') + '</div>' +
      '<dl class="r6-kv"><dt>身份</dt><dd>' + E((state.me && state.me.name) || state.role) + ' · ' + E(state.role) + '</dd>' +
      '<dt>权限</dt><dd>' + (state.admin ? '管理员（可写）' : '只读') + '</dd>' +
      '<dt>数据新鲜度</dt><dd>' + E(state.freshness) + ' · ' + E(state.generatedAt || '—') + '</dd>' +
      '<dt>保存状态</dt><dd>' + E(state.saved) + ' · revision ' + state.revision + '</dd>' +
      '<dt>实时通道</dt><dd class="mono">/api/v2/events</dd>' +
      '<dt>轮询降级</dt><dd class="mono">/api/v2/events/poll</dd></dl>' +
      '<details><summary>端点映射（' + Object.keys(ENDPOINTS).length + ' 条）</summary><div class="r6-table-scroll"><table class="r6-table"><thead><tr><th>动作</th><th>方法与路径</th></tr></thead><tbody>' + rows + '</tbody></table></div></details>' +
      '<details><summary>最近调用</summary>' + calls + '</details>' +
      '</div>', r6Btn('关闭', 'modal-close', null) + r6Btn('重新连接', 'connect', 'refresh', 'primary'), true);
  }

  function backup() {
    dialog('备份与恢复', '<div class="r6-form">' +
      '<div class="r6-note warn">' + I('shield') + '<strong>恢复会替换当前工作区</strong>的阅读视角、书签与显示设置；已归档原文与解析索引不受影响。恢复失败不会留下半份文件库。</div>' +
      '<h3 class="r6-section-title">导出</h3><p class="r6-mininote">导出个人偏好与阅读视角（/api/v1/preferences）；服务端归档与索引请用运维备份工具。</p>' +
      r6Btn('导出工作区设置', 'backup-export', 'download', '') +
      '<h3 class="r6-section-title">恢复</h3><div class="field"><label class="label" for="lb-restore-file">选择备份文件</label><input id="lb-restore-file" class="input" type="file" accept=".json,application/json"></div>' +
      '<div class="r6-errorbox" id="lb-restore-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) + r6Btn('恢复并替换当前工作区', 'backup-restore', 'refresh', 'primary'), true);
  }

  function exportBackup() {
    download('logbench-workspace.json', JSON.stringify({
      format: 'LogBench Workspace Backup', version: 1,
      workspace: { schema: 'logbench.workspace', version: 1 },
      preferences: prefs, revision: state.prefRevision,
      exportedAt: new Date().toISOString()
    }, null, 2), 'application/json');
    notify('工作区设置已导出');
  }

  async function restoreBackup() {
    var input = document.getElementById('lb-restore-file');
    var err = document.getElementById('lb-restore-error');
    if (!input || !input.files || !input.files[0]) { err.textContent = '请先选择备份文件。'; return; }
    try {
      var value = JSON.parse(await input.files[0].text());
      if (value.format !== 'LogBench Workspace Backup' || value.version !== 1) throw new Error('格式或版本不受支持');
      var next = Object.assign({}, prefs, value.preferences || {});
      var r = await request('putPreferences', { body: { value: next, expectedRevision: state.prefRevision || 0 } });
      if (!r.ok) throw new Error(r.error || '服务端拒绝写入');
      Object.assign(prefs, next);
      state.prefRevision = r.data.revision || state.prefRevision;
      state.saved = '已恢复';
      var d = document.getElementById('dialog');
      if (d && d.open) d.close();
      render(false); paintStatus();
      notify('工作区设置已恢复，服务端归档数据未改变');
    } catch (e) {
      err.textContent = '恢复失败：' + e.message + '。当前工作区未改变，原文件保留。';
    }
  }

  /* ---- Rev2 补齐：本机批量任务 / 渐进搜索 / 私有上传 ----------------------- */
  // These three were added by Complete Design Rev2 and are absent from the R6
  // Remote Prototype. They use only prototype-native containers and controls.
  // Sidebar entries, not a persistent toolbar row: the project baseline
  // requires floating task panels that do not squeeze the reading area.
  function r6Rev2() {
    return '<div id="lb-rev2" class="section-head">本机任务与服务端加速</div>' +
      '<button class="nav-button" data-r6="rev2-jobs">' + I('layers') + '本机批量任务</button>' +
      '<button class="nav-button" data-r6="rev2-search">' + I('search') + '渐进检索</button>' +
      '<button class="nav-button" data-r6="rev2-upload">' + I('upload') + '上传到私有区</button>';
  }

  function jobsPanel() {
    var items = (state.jobsCache && state.jobsCache.items) || [];
    dialog('本机批量任务', '<div class="r6-form">' +
      '<p class="r6-mininote">本机导入默认只在浏览器内读取，不上传。下面的任务来自 /api/v2/index-jobs（服务端加速需显式选择）。</p>' +
      (items.length
        ? '<table class="r6-table"><thead><tr><th>任务</th><th>状态</th><th>进度</th></tr></thead><tbody>' +
          items.slice(0, 20).map(function (j) {
            return '<tr><td class="mono">' + E(j.id || j.file_id || '—') + '</td><td>' + r6Badge(E(j.state || 'queued'), j.state === 'indexed' ? 'good' : j.state === 'failed' ? 'bad' : 'blue') + '</td><td>' + (j.progress != null ? Math.round(Number(j.progress) * 100) + '%' : '—') + '</td></tr>';
          }).join('') + '</tbody></table>'
        : '<div class="r6-empty">' + I('layers', 'lg') + '<strong>当前没有任务</strong><p>打开本机日志后，任务会在这里显示；正文始终留在本浏览器。</p></div>') +
      '<div class="r6-errorbox" id="rev2-jobs-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) + r6Btn('刷新任务', 'rev2-jobs-refresh', 'refresh', ''), true);
  }

  async function refreshJobsPanel() {
    var r = await request('indexJobs', { params: { limit: 20 }, primary: false });
    if (r.ok) { state.jobsCache = r.data; jobsPanel(); }
    else {
      var e = document.getElementById('rev2-jobs-error');
      if (e) e.textContent = '读取任务失败：' + r.error;
    }
  }

  function libraryOptions() {
    var items = (state.library && state.library.items) || [];
    return items.filter(function (x) { return x.available !== false; }).slice(0, 50)
      .map(function (it) {
        return '<option value="' + E(it.fileId) + '">' + E(it.displayName || it.relPath || it.fileId) + '</option>';
      }).join('');
  }

  function searchPanel() {
    // server/query/api.py:151 requires file_id. An empty catalogue must be a
    // readable empty state, not a 400 sent to the API.
    var opts = libraryOptions();
    var fileField = opts
      ? '<div class="field"><label class="label" for="rev2-query-file">检索文件</label>' +
        '<select class="select" id="rev2-query-file">' + opts + '</select></div>'
      : '<div class="r6-empty">' + I('folder', 'lg') + '<strong>当前没有可检索的文件</strong>' +
        '<p>检索绑定到具体文件快照。先完成采集并落盘，或上传文件到私有区后重试。</p></div>';
    dialog('渐进检索', '<div class="r6-form">' +
      '<div class="field"><label class="label" for="rev2-query-text">检索文本</label><input class="input" id="rev2-query-text" placeholder="支持 Aa / 整词 / 正则与多条件"></div>' +
      fileField +
      '<p class="r6-mininote">查询在服务端按固定快照执行，结果按游标分批返回；未完成时可用「继续检索」推进，不阻塞界面。</p>' +
      '<div class="r6-progress" id="rev2-query-progress" aria-label="检索进度"><span style="width:0%"></span></div>' +
      '<div id="rev2-query-result" class="r6-mininote"></div>' +
      '<div class="r6-errorbox" id="rev2-query-error" role="alert"></div></div>',
      r6Btn('关闭', 'modal-close', null) +
      r6Btn('开始检索', 'rev2-query-start', 'search', 'primary', opts ? '' : 'disabled') +
      r6Btn('继续检索', 'rev2-query-next', 'arrow', ''), true);
  }

  async function progressiveQuery(cont) {
    var err = document.getElementById('rev2-query-error');
    var out = document.getElementById('rev2-query-result');
    var bar = document.querySelector('#rev2-query-progress span');
    if (err) err.textContent = '';
    try {
      var r;
      if (cont && state.queryId) {
        r = await request('queryContinue', { id: state.queryId, primary: false, body: { resume_token: state.queryToken || '', budget_ms: 800 } });
      } else {
        var text = (document.getElementById('rev2-query-text') || {}).value || '';
        var fileEl = document.getElementById('rev2-query-file');
        var fileId = fileEl ? fileEl.value : '';
        if (!text.trim()) { err.textContent = '请输入检索文本。'; return; }
        if (!fileId) { err.textContent = '请先选择一个可检索的文件；服务端查询必须绑定文件快照。'; return; }
        r = await request('queries', { primary: false, body: { text: text, terms: [text], file_id: fileId, limit: 200, budget_ms: 800 } });
      }
      if (!r.ok) { err.textContent = '检索失败：' + r.error; return; }
      var item = r.data.item || r.data;
      state.queryId = item.id || item.query_id || state.queryId;
      state.queryToken = item.resume_token || item.cursor || '';
      var rec = await request('queryRecords', { id: state.queryId, primary: false, params: { limit: 200, cursor: state.queryCursor || '' } });
      var rows = (rec.data && rec.data.items) || [];
      state.queryCursor = (rec.data && rec.data.next_cursor) || '';
      state.queryTotal = (state.queryTotal || 0) + rows.length;
      var st = item.state || (rec.data && rec.data.state) || 'running';
      if (out) out.textContent = '已返回 ' + state.queryTotal + ' 条 · 状态 ' + st + (state.queryCursor ? ' · 还有更多' : ' · 已完成');
      if (bar) bar.style.width = st === 'done' ? '100%' : String(Math.min(92, 18 + state.queryTotal / 2)) + '%';
    } catch (e) {
      if (err) err.textContent = '检索失败：' + (e && e.message);
    }
  }

  function uploadPanel() {
    dialog('上传到私有区', '<div class="r6-form">' +
      '<div class="r6-note warn">' + I('shield') + '上传会把文件正文发送到服务端，仅进入当前身份的私有区，不进入共享总库。不选择本入口时，本机导入始终留在本浏览器。</div>' +
      '<div class="field"><label class="label" for="rev2-upload-file">选择日志文件</label><input class="input" id="rev2-upload-file" type="file" accept=".log,.txt,.out"></div>' +
      '<p class="r6-mininote">分片上传支持断点恢复；每一片在服务端落盘确认后才计入进度。</p>' +
      '<div class="r6-progress" id="rev2-upload-progress" aria-label="上传进度"><span style="width:0%"></span></div>' +
      '<div id="rev2-upload-state" class="r6-mininote"></div>' +
      '<div class="r6-errorbox" id="rev2-upload-error" role="alert"></div></div>',
      r6Btn('取消', 'modal-close', null) + r6Btn('开始上传', 'rev2-upload-start', 'upload', 'primary'), true);
  }

  async function uploadPrivate() {
    var input = document.getElementById('rev2-upload-file');
    var err = document.getElementById('rev2-upload-error');
    var label = document.getElementById('rev2-upload-state');
    var bar = document.querySelector('#rev2-upload-progress span');
    var file = input && input.files && input.files[0];
    if (!file) { err.textContent = '请先选择文件。'; return; }
    var CHUNK = 4 * 1024 * 1024;
    var chunkSize = Math.min(CHUNK, Math.max(256 * 1024, file.size));
    try {
      var created = await request('uploads', { body: { name: file.name, size: file.size, chunkSize: chunkSize } });
      if (!created.ok) throw new Error(created.error);
      var session = created.data.item || created.data;
      var id = session.id || session.upload_id;
      var total = Math.ceil(file.size / chunkSize);
      for (var i = 0; i < total; i += 1) {
        var blob = file.slice(i * chunkSize, Math.min(file.size, (i + 1) * chunkSize));
        var buf = await blob.arrayBuffer();
        var digest = await digestHex(buf);
        var csrf = state.csrfToken || '';
        var res = await fetch('/api/v2/uploads/' + encodeURIComponent(id) + '/parts/' + i, {
          method: 'PUT', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/octet-stream', 'X-LogBench-Digest': digest, 'X-LogBench-CSRF': csrf },
          body: buf
        });
        if (!res.ok) {
          var body = await res.text();
          throw new Error('分片 ' + (i + 1) + '/' + total + ' 失败：' + (body || res.status));
        }
        if (bar) bar.style.width = Math.round(100 * (i + 1) / total) + '%';
        if (label) label.textContent = '已上传 ' + (i + 1) + ' / ' + total + ' 片';
      }
      var committed = await request('uploadCommit', { id: id, body: {} });
      if (!committed.ok) throw new Error(committed.error);
      if (label) label.textContent = '已封存到私有区，等待索引。';
      state.scenario = 'saved'; state.saved = '已上传';
      await refresh(true);
    } catch (e) {
      err.textContent = '上传失败：' + (e && e.message) + '。已上传分片保留在服务端会话中，可重试续传。';
    }
  }

  async function digestHex(buf) {
    if (!(window.crypto && crypto.subtle)) {
      var bytes = new Uint8Array(buf), h = 2166136261;
      for (var i = 0; i < bytes.length; i += 1) { h ^= bytes[i]; h = Math.imul(h, 16777619); }
      return ('00000000' + (h >>> 0).toString(16)).slice(-8);
    }
    var d = await crypto.subtle.digest('SHA-256', buf);
    return Array.prototype.map.call(new Uint8Array(d), function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }

  /* ---- 文件库 / 阅读器：真实 library 数据接入 ------------------------------
   * 原型 R5 层自带渲染与解析能力（parseInWorker / indexRecords / render），
   * 但它的 files 数组是内置 fixture。这里只替换数据源：R5 的列显隐、多行与
   * Traceback、规则证据、时间线、对比等阅读能力全部保留。
   */
  function projectLibraryToFiles(items) {
    // Keep files restored from the browser's own IndexedDB (local imports);
    // only replace the server-side catalogue projection.
    for (var i = files.length - 1; i >= 0; i -= 1) {
      if (files[i].source === 'server' && files[i].fileId) files.splice(i, 1);
    }
    (items || []).slice(0, 200).forEach(function (it) {
      if (it.available === false) return;
      files.push({
        id: 'srv-' + it.fileId,
        fileId: it.fileId,
        name: it.displayName || it.relPath || it.fileId,
        path: it.logicalPath || it.relPath || '',
        source: 'server',
        profile: it.rule || '目录继承',
        records: [], text: '', size: Number(it.size) || 0,
        modified: shortTime(it.observedAt),
        favorite: false, deleted: false,
        rootId: it.rootId, rootLabel: it.rootLabel,
        coverage: it.coverage, generation: it.generation,
        lazy: true, loaded: false
      });
    });
    if (typeof indexRecords === 'function') indexRecords();
  }

  async function loadRemoteFile(f) {
    if (!f || f.loaded) { if (f) baseOpenFile(f.id); return; }
    state.scenario = 'loading'; paintStatus();
    try {
      var res = await fetch('/api/v2/library/files/' + encodeURIComponent(f.fileId) + '/content',
        { credentials: 'same-origin' });
      if (!res.ok) {
        var body = await res.text();
        var detail = {};
        try { detail = (JSON.parse(body).error) || {}; } catch (e) { detail = {}; }
        throw new Error(detail.message || ('读取失败：' + res.status));
      }
      var text = await res.text();
      var records = await parseInWorker(text, f.id);
      f.text = text;
      f.records = records || [];
      f.size = text.length;
      f.loaded = true;
      f.lazy = false;
      if (typeof indexRecords === 'function') indexRecords();
      S.opened = S.opened.filter(function (x) { return x !== f.id; });
      S.opened.unshift(f.id);
      S.file = f.id;
      S.view = 'logs';
      state.scenario = 'ready';
      render(false); paintStatus();
    } catch (e) {
      state.scenario = 'error';
      state.lastError = e.message;
      paintStatus();
      notify('打开失败：' + e.message, true);
    }
  }

  // The R5 rule editor reads files[0].records for its sample textarea. With a
  // real (possibly empty) catalogue, seed testInput with '' so `??` short-
  // circuits; an empty rules library gets its own recoverable empty state.
  var baseRenderRules = renderRules;
  renderRules = function () {
    if (!S.profiles.length) {
      return '<div class="page-heading"><h1>规则管理</h1></div>' +
        '<section class="rule-library-layout"><div class="r6-empty">' + I('sliders', 'lg') +
        '<strong>服务端规则库为空</strong>' +
        '<p>规则定义来自 /api/v2/rules。管理员发布规则后，这里会列出可编辑的提取、异常与时间线配置。</p>' +
        '</div></section>';
    }
    if (!files.length && S.testInput == null) S.testInput = '';
    return baseRenderRules();
  };

  var baseOpenFile = openFile;
  openFile = function (id) {
    var f = files.find(function (x) { return x.id === id; });
    if (f && f.lazy && !f.loaded) { loadRemoteFile(f); return; }
    baseOpenFile(id);
  };

  r6OpenReading = function (id) {
    var f = files.find(function (x) { return x.id === id || x.fileId === id; });
    if (f) { loadRemoteFile(f); return; }
    state.scenario = 'notfound'; paintStatus();
  };

  // Same containers/classes as the prototype file library, real catalogue rows.
  r6Library = function () {
    var items = (state.library.items || []).filter(function (x) { return x.available !== false; });
    var rootId = R6.libraryRoot;
    var rows = rootId === 'all' ? items : items.filter(function (x) { return x.rootId === rootId; });
    var roots = R6.roots || [];
    var folderBtn = function (id, label, icon) {
      return '<button class="folder-item ' + (rootId === id ? 'selected' : '') +
        '" data-r6="root-browse" data-id="' + E(id) + '">' + I(icon) + E(label) + '</button>';
    };
    var body = rows.length
      ? '<table class="r6-table"><thead><tr><th>日志 / 相对目录</th><th>存储位置</th><th>原文 / 索引</th><th>规则 / 来源</th><th>操作</th></tr></thead><tbody>' +
        rows.map(function (it) {
          return '<tr><td><button class="r6-link" data-r6="read-source" data-id="' + E('srv-' + it.fileId) + '">' +
            E(it.displayName || it.relPath) + '</button><div class="r6-meta mono">' + E(it.relPath || '') + '</div></td>' +
            '<td><span>' + E(it.rootLabel || it.rootId || '—') + '</span><div class="r6-meta">代次 ' + E(it.generation || 1) + '</div></td>' +
            '<td><span class="mono">' + humanBytes(it.size) + '</span><div class="r6-meta">索引 ' + E(it.coverage || 'none') + '</div></td>' +
            '<td><span>' + E(it.rule || '目录继承') + '</span><div class="r6-meta">目录继承 · 规则版本冻结</div></td>' +
            '<td>' + r6Btn('打开', 'read-source', 'logs', 'ghost sm', 'data-id="' + E('srv-' + it.fileId) + '"') + '</td></tr>';
        }).join('') + '</tbody></table>'
      : '<div class="r6-empty">' + I('folder', 'lg') + '<strong>该存储位置没有可读取的文件</strong>' +
        '<p>已完成采集并落盘的文件会出现在这里。目录登记保留，离线位置不会被当成空库清空。</p>' +
        r6Btn('查看存储位置', 'storage-tab', 'database', 'sm') + '</div>';
    return '<div class="r6-head"><h1>文件库</h1><span class="r6-badge">' + items.length + ' 个文件</span>' +
      '<span class="spacer"></span>' + r6Btn('存储位置', 'storage-tab', 'database', 'sm') + '</div>' +
      '<section class="r6-content"><div class="r6-toolbar">' +
      r6Btn('本机临时文件', 'local-library', 'laptop', 'ghost sm') +
      '<span class="spacer"></span><span class="r6-sub">本机文件只保留在浏览器</span></div>' +
      '<div class="library-shell" style="flex:1;min-height:0"><aside class="folder-pane">' +
      '<div class="folder-title">存储位置</div>' + folderBtn('all', '所有位置', 'layers') +
      roots.map(function (r) { return folderBtn(r.id, r.name, r.state === 'online' ? 'folder' : 'warning'); }).join('') +
      '</aside><div class="library-main"><div class="r6-filebar">' + I('folder') +
      '<strong>' + E(rootId === 'all' ? '全部存储位置' : (roots.find(function (r) { return r.id === rootId; }) || {}).name || rootId) + '</strong>' +
      '<span class="spacer"></span><details class="r6-sub"><summary>默认规则</summary>共享目录默认规则保持继承；单文件覆盖见「规则绑定」。</details></div>' +
      '<div class="r6-table-scroll">' + body + '</div></div></div></section>';
  };

  // An empty job list must say why it is empty and where records will come
  // from; the prototype rendered a bare <ol> with no explanation.
  var baseR6Events = r6Events;
  r6Events = function () {
    if (!R6.events.length) {
      return '<div class="r6-toolbar">' +
        '<details class="r6-sub"><summary>记录范围</summary>采集、断线重连、归档、索引和权限事件均保留来源与时间；来自 /api/v2/jobs 与 SSE 增量。</details>' +
        '<span class="spacer"></span>' + r6Btn('导出运行记录', 'export-events', 'download', 'sm') + '</div>' +
        '<div class="r6-pane"><div class="r6-empty">' + I('clock', 'lg') +
        '<strong>还没有运行记录</strong>' +
        '<p>启用日志源后，采集、归档、索引与权限事件会由 /api/v2/jobs 与实时通道写入这里；不会用占位记录填充。</p>' +
        '</div></div>';
    }
    return baseR6Events();
  };

  // The prototype's 6th budget field (read/write chunk size) has no counterpart
  // in server/runtime Budget. Keep the six-field layout, but make that row a
  // read-only status instead of an input that silently does nothing on save.
  r6Tuning = function () {
    var t = R6.tuning;
    var row = function (label, key, value) {
      return '<div class="field"><label class="label" for="r6-tune-' + key + '">' + label + '</label>' +
        '<input id="r6-tune-' + key + '" class="input" type="number" min="1" max="64" value="' + value + '"></div>';
    };
    var body = '<p class="hint">资源上限与实时、历史任务分开设置；保存后立即生效，状态栏显示服务端回执。</p>' +
      '<div class="r6-two">' +
      row('全局传输并发', 'collect', t.collect) +
      row('每个远程主机并发', 'perHost', t.perHost) +
      row('常驻解析进程', 'parse', t.parse) +
      row('历史补采并发', 'backfill', t.backfill) +
      '<div class="field"><label class="label">读写块大小</label>' +
      '<div class="r6-meta" style="padding:8px 0">由服务端固定，不在本页面配置</div></div>' +
      row('进程组内存预算（GiB）', 'memory', t.memory) +
      '</div>' +
      '<div class="r6-note">250 GB RAM 优先留给文件系统页缓存，不将整份 1 GB 日志展开为浏览器或 Python 对象。首次建索引与已索引阅读分开限流。</div>' +
      '<details><summary>其余预算项（服务端只读）</summary><dl class="r6-kv">' +
      [['查询槽位', t.query_slots], ['CPU 总上限', t.total_cpu],
       ['热页缓存（GiB）', t.hot_cache_bytes], ['上传槽位', t.upload_slots],
       ['单身份上传', t.per_principal_uploads], ['单身份查询', t.per_principal_queries],
       ['交互保留位', t.interactive_reserve]]
        .map(function (p) { return '<dt>' + p[0] + '</dt><dd class="mono">' + E(p[1]) + '</dd>'; }).join('') +
      '</dl><p class="r6-mininote">字段来自 /api/v2/runtime/budget 的 base；effective 为压力收缩后的实际值。</p></details>' +
      '<div class="r6-errorbox" id="r6-tune-error" role="alert"></div>';
    dialog('采集与解析资源', body,
      r6Btn('取消', 'modal-close') + r6Btn('保存资源设置', 'save-tuning', 'save', 'primary'), true);
  };

  /* ---- rules / admin：接真实服务端定义 ------------------------------------ */
  // server/storage/rules_api.py returns item.profile with exactly the shape
  // R5's baseProfiles uses (extractors / anomalies / nodes / version / desc),
  // so the prototype editor can render real server rules unchanged.
  function projectServerRules(items) {
    var fallback = (typeof Engine !== 'undefined' && Engine.defaults)
      ? Engine.defaults() : { extractors: [], anomalies: [] };
    var nodes = (typeof defaultNodes !== 'undefined') ? clone(defaultNodes) : [];
    var server = (items || []).filter(function (it) { return !it.deletedAt; })
      .map(function (it) {
        var p = it.profile || {};
        return Object.assign({}, p, {
          id: it.ruleId,
          name: it.name || p.name || it.ruleId,
          desc: it.description || p.desc || '',
          version: String(it.revision || p.version || '1'),
          status: '服务端',
          deleted: false,
          extractors: p.extractors || fallback.extractors || [],
          anomalies: p.anomalies || fallback.anomalies || [],
          nodes: p.nodes || nodes
        });
      });
    var ids = {};
    server.forEach(function (p) { ids[p.id] = true; });
    // Keep locally authored drafts that do not exist on the server.
    var local = (S.profiles || []).filter(function (p) { return !ids[p.id]; });
    S.profiles = server.concat(local);
    if (!S.profiles.some(function (p) { return p.id === S.profile; })) {
      S.profile = S.profiles.length ? S.profiles[0].id : null;
    }
  }

  // Same containers/classes as the prototype admin page, real v1 admin data.
  renderAdmin = function () {
    var d = state.adminData;
    if (!d) {
      return '<div class="page-heading"><h1>管理后台</h1></div>' +
        '<section class="settings-body"><div class="r6-empty">' + I('shield', 'lg') +
        '<strong>需要管理员权限</strong>' +
        '<p>用户、服务端配置与审计记录由 /api/v1/admin/* 逐请求授权。当前身份为只读，页面不会用占位数据填充。</p>' +
        '</div></section>';
    }
    var users = (d.users && d.users.items) || [];
    var cfg = (d.config && d.config.config) || {};
    var rt = (d.config && d.config.runtime) || {};
    var stats = d.stats || {};
    var audit = (d.audit && d.audit.items) || [];
    var me = state.me || {};
    var role = me.role === 'admin' ? '管理员' : (me.name || '访客');
    var row = function (title, note, value) {
      return '<div class="settings-row"><div><strong>' + title + '</strong><small>' + E(note) +
        '</small></div><div class="mono">' + value + '</div></div>';
    };
    var userRows = users.length ? users.map(function (u) {
      return '<tr><td>' + E(u.name || u.ip) + '</td><td class="mono">' + E(u.ip) + '</td>' +
        '<td><span class="badge ' + (u.role === 'admin' ? 'blue' : '') + '">' +
        (u.role === 'admin' ? '管理员' : '成员') + '</span></td>' +
        '<td class="muted">' + (u.enabled ? '已启用' : '已停用') + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="muted">服务端未登记用户</td></tr>';
    var auditRows = audit.length ? audit.slice(0, 8).map(function (a) {
      return '<tr><td class="mono">' + E(shortTime(a.at)) + '</td><td>' + E(a.action) +
        '</td><td class="mono">' + E(a.target || '—') + '</td></tr>';
    }).join('') : '<tr><td colspan="3" class="muted">暂无审计记录</td></tr>';
    return '<div class="page-heading"><h1>管理后台</h1>' +
      '<span class="badge green">服务端已连接</span><span class="spacer"></span></div>' +
      '<section class="settings-body">' +
      '<div class="callout">' + I('info') + '<div>身份与权限由服务端逐请求校验；前端不代替鉴权。</div></div>' +
      '<h3 class="settings-title">身份与权限</h3>' +
      '<div class="settings-block">' +
      row('当前身份', E(me.name || me.ip || '—'), E(me.ip || '—') + ' · ' + E(role)) +
      row('身份模式', '由服务端配置决定', E(cfg.identityMode || 'local')) +
      row('服务端版本', 'GET /api/v1/health', E(stats.version || '—')) +
      '</div>' +
      '<div class="row" style="margin:22px 0 12px"><h3 class="settings-title" style="margin:0">用户列表</h3>' +
      '<span class="badge">' + users.length + '</span><span class="spacer"></span></div>' +
      '<div class="settings-block"><table class="settings-table"><thead><tr><th>用户</th><th>IP 地址</th><th>权限</th><th>状态</th></tr></thead><tbody>' +
      userRows + '</tbody></table></div>' +
      '<h3 class="settings-title" style="margin-top:23px">服务端配置与容量</h3>' +
      '<div class="settings-block">' +
      row('数据目录', '服务端绝对路径', E(rt.dataDirectory || '—')) +
      row('允许主机', 'Host 白名单', E((rt.allowedHosts || []).join(' / ') || '—')) +
      row('数据库大小', 'GET /api/v1/admin/stats', humanBytes(stats.databaseBytes)) +
      row('磁盘可用', 'GET /api/v1/admin/stats', humanBytes(stats.diskFreeBytes)) +
      row('规则 / 回收站', '服务端统计', E(String(stats.rules ?? 0)) + ' / ' + E(String(stats.trash ?? 0))) +
      '</div>' +
      '<h3 class="settings-title" style="margin-top:23px">最近审计</h3>' +
      '<div class="settings-block"><table class="settings-table"><thead><tr><th>时间</th><th>动作</th><th>对象</th></tr></thead><tbody>' +
      auditRows + '</tbody></table>' +
      '<p class="hint" style="padding:8px 12px">完整审计与诊断导出由 /api/v1/admin/audit 与运维工具提供。</p></div>' +
      '</section>';
  };

  /* ---- 暴露接口（与预览版一致） ------------------------------------------ */
  var api = {
    ENDPOINTS: ENDPOINTS, SCENARIOS: SCENARIOS, state: state, prefs: prefs,
    request: request, hydrate: hydrate,
    statusText: statusText, dataFreshness: dataFreshness, scenarioSelect: scenarioSelect,
    paintStatus: paintStatus, setScenario: setScenario, stateBanner: stateBanner,
    refresh: refresh, connect: connect, retry: retry, pause: pause, probe: probe,
    saveSource: saveSource, saveRoot: saveRoot, saveTuning: saveTuning,
    rebuildCache: rebuildCache, exportEvents: exportEvents, savePref: savePref,
    datalink: datalink, backup: backup, exportBackup: exportBackup, restoreBackup: restoreBackup,
    boot: boot, draftToApi: draftToApi,
    eventState: function () { return eventsClient ? eventsClient.state() : null; },
    jobsPanel: jobsPanel, refreshJobsPanel: refreshJobsPanel,
    searchPanel: searchPanel, progressiveQuery: progressiveQuery,
    uploadPanel: uploadPanel, uploadPrivate: uploadPrivate,
    r6Rev2: r6Rev2
  };
  return api;
})();
window.LB = LB;
LB.probeResult = null;

/* ---- 兼容层 --------------------------------------------------------------- */
window.LogBenchRemotePreview = Object.assign(window.LogBenchRemotePreview || {}, {
  adapter: LB, endpoints: LB.ENDPOINTS, state: R6
});
window.__r6 = Object.assign(window.__r6 || {}, {
  state: S, intake: R6, adapter: LB, api: LB.request,
  showView: function (v) { S.view = v; render(false); LB.paintStatus(); },
  applyEvent: function (e) { if (e && e.text) r6Event(e.source || 'system', e.kind || '事件', e.text, e.tone || ''); },
  scenario: LB.setScenario,
  follower: function () { return R6.liveFollow; }
});

window.addEventListener('click',function(e){const menu=document.querySelector('.app-tools[open]');if(menu&&(!menu.contains(e.target)||e.target.closest('.app-tools-menu button')))menu.open=false;},true);
document.addEventListener('keydown',function(e){const menu=document.querySelector('.app-tools[open]');if(menu&&e.key==='Escape'){menu.open=false;menu.querySelector('summary').focus();}});

/* 启动：先渲染骨架（loading 态），再拉取真实数据。 */
LB.paintStatus();
LB.boot();
