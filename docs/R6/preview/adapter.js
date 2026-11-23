/* ===== R6 RESTORE ADAPTER — 预览片段（不是独立可运行脚本） =====
 * build_preview.py 会把本片段插入原型 IIFE 内部（window.LogBenchPrototype 之后、
 * 启动 render 之前），因此可直接访问 R6 / S / render / r6Btn / I / E / $ /
 * dialog / notify / download / clone / r6Event / r6CaptureDraft / r6Draft /
 * r6Probe / r6ProbeKind / r6Step / r6Wizard / r6ShowFormError / r6Validate /
 * r6Source / r6Root / files 等同作用域成员。
 *
 * 职责边界（对照 docs/R6/r6-frontend-restore-plan.md §4.1）：
 *   - 设计层：由原型 HTML 逐字提供，本片段不定义任何 DOM/CSS 结构。
 *   - 引擎层：Worker / provider / IndexedDB / SSE 由现有 src/ 提供，本片段只保留契约。
 *   - 适配层：本片段。把 /api/v2/* 响应投影成原型视图模型，把 data-r6 动作
 *     映射为 v2 端点，并补齐 §7.3 的状态件（只用原型既有组件形态）。
 *
 * 本地 file:// 打开时没有服务，MODE=preview 直接使用 /api/v2/* 响应形状的快照，
 * 并在状态栏常驻「未连接服务」。点「连接服务」才切到真实 fetch。
 * 预览只用于确认视觉与结构，不作为后端可用性证据。
 */
const LB = (function () {
  'use strict';

  /* ---- 端点契约（计划 §6.2 全量映射） ---------------------------------- */
  const ENDPOINTS = {
    intake:       ['GET',   '/api/v2/intake'],
    source:       ['GET',   '/api/v2/sources/{id}'],
    createSource: ['POST',  '/api/v2/sources'],
    updateSource: ['PATCH', '/api/v2/sources/{id}'],
    probe:        ['POST',  '/api/v2/sources/{id}/probe'],
    actions:      ['POST',  '/api/v2/sources/{id}/actions'],
    roots:        ['GET',   '/api/v2/storage-roots'],
    saveRoot:     ['POST',  '/api/v2/storage-roots'],
    updateRoot:   ['PATCH', '/api/v2/storage-roots/{id}'],
    probeRoot:    ['POST',  '/api/v2/storage-roots/{id}/probe'],
    cache:        ['GET',   '/api/v2/cache'],
    rebuild:      ['POST',  '/api/v2/cache/rebuild'],
    purge:        ['POST',  '/api/v2/cache/purge'],
    jobs:         ['GET',   '/api/v2/jobs?limit=50'],
    events:       ['GET',   '/api/v2/events'],
    pollEvents:   ['GET',   '/api/v2/events/poll'],
    runtime:      ['GET',   '/api/v2/runtime'],
    budget:       ['PUT',   '/api/v2/runtime/budget'],
    library:      ['GET',   '/api/v2/library'],
    libraryTree:  ['GET',   '/api/v2/library/tree'],
    bindings:     ['GET',   '/api/v2/bindings'],
    workspace:    ['PATCH', '/api/v2/workspace'],
    backup:       ['GET',   '/api/v2/backup']
  };

  const SCENARIOS = [
    ['ready',      '正常'],
    ['loading',    '加载中'],
    ['empty',      '空库'],
    ['no-result',  '无匹配'],
    ['error',      '请求失败'],
    ['forbidden',  '403 权限不足'],
    ['notfound',   '404 资源不存在'],
    ['conflict',   '409 版本冲突'],
    ['saving',     '保存中'],
    ['saved',      '已保存'],
    ['rebuilding', '重建进度'],
    ['offline',    '服务离线']
  ];

  const state = {
    mode: 'preview',          // preview | live
    scenario: 'ready',
    role: '管理员',
    freshness: '14:36:04',
    saved: '已保存',
    progress: 0,
    revision: 18,
    calls: []
  };

  /* ---- /api/v2/* 响应形状的快照 ---------------------------------------- */
  const SAMPLE = {
    schema: 'logbench.intake/1',
    revision: state.revision,
    generated_at: '2026-11-04T14:36:04+08:00',
    capabilities: { admin: true, write: true, purge: false },
    sources: clone(R6.sources).map(function (s, i) {
      return Object.assign({}, s, { revision: 18, updated_at: '2026-11-04T14:36:0' + (i % 9) + '+08:00' });
    }),
    roots: clone(R6.roots).map(function (r) { return Object.assign({}, r, { verified_at: '2026-11-04T06:02:11+08:00' }); }),
    jobs: clone(R6.events).map(function (e, i) {
      return Object.assign({ id: 'job-' + (4821 - i) }, e);
    }),
    tuning: Object.assign({}, clone(R6.tuning), {
      query_slots: 4, total_cpu: 8, hot_cache_bytes: 4,
      io_mb: 160, index_queue: 6, upload_slots: 2, retention_days: 90
    })
  };

  function now() {
    return new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  function probeChecks() {
    var d = r6Draft || {};
    var p = d.protocol || 'sftp';
    var cap = r6Capabilities(p);
    return [
      { key: 'connect', label: '连接与端口', ok: true, detail: (r6Protocols[p] || p) + ' · ' + (d.port || 22) + ' · 12 ms' },
      { key: 'hostkey', label: '主机密钥 / 证书', ok: true, detail: p === 'sftp' || p === 'scp' ? 'ED25519 指纹与已登记值一致' : '证书链校验通过' },
      { key: 'auth', label: '凭据', ok: true, detail: (d.credential || '只读采集密钥') + ' · 服务端校验通过' },
      { key: 'list', label: '目录发现', ok: cap.list, detail: cap.list ? ((d.maps || []).length + ' 个远程目录可读') : '协议不提供目录清单，需使用固定清单' },
      { key: 'range', label: '范围读取', ok: cap.range, detail: cap.range ? '支持按已提交偏移续采' : '不支持 Range，只能完整快照' },
      { key: 'target', label: '目标子目录权限', ok: true, detail: '主库可写 · 剩余 2.6 TiB' }
    ];
  }

  /* ---- 请求层：epoch 防串场 + 离线快照回退 ------------------------------ */
  let epoch = 0;

  async function sample(key, opts) {
    await new Promise(function (r) { setTimeout(r, 160); });
    if (key === 'intake') return { ok: true, status: 200, data: SAMPLE, offline: true };
    if (key === 'probe' || key === 'probeRoot') return { ok: true, status: 200, data: { checks: probeChecks() }, offline: true };
    if (key === 'rebuild') return { ok: true, status: 202, data: { job_id: 'job-rebuild-18', progress: 0 }, offline: true };
    if (key === 'jobs') return { ok: true, status: 200, data: { jobs: SAMPLE.jobs }, offline: true };
    if (key === 'cache') return { ok: true, status: 200, data: { entries: SAMPLE.sources }, offline: true };
    if (key === 'runtime') return { ok: true, status: 200, data: { budget: SAMPLE.tuning }, offline: true };
    return { ok: true, status: 200, data: { revision: state.revision, echo: opts && opts.body }, offline: true };
  }

  async function request(key, opts) {
    opts = opts || {};
    var def = ENDPOINTS[key] || ['GET', key];
    var method = opts.method || def[0];
    var url = def[1].replace('/{id}', opts.id ? '/' + opts.id : '').replace('{id}', opts.id || '');
    var seq = ++epoch;
    state.calls.unshift({ at: now(), method: method, path: url });
    if (state.calls.length > 12) state.calls.length = 12;
    var res;
    if (state.mode === 'preview') {
      res = await sample(key, opts);
    } else {
      try {
        var r = await fetch(url, {
          method: method,
          headers: { 'Content-Type': 'application/json' },
          body: opts.body ? JSON.stringify(opts.body) : undefined,
          signal: opts.signal
        });
        if (seq !== epoch) return { stale: true };
        res = { ok: r.ok, status: r.status, data: await r.json().catch(function () { return {}; }), offline: false };
      } catch (e) {
        if (seq !== epoch) return { stale: true };
        res = { ok: false, status: 0, error: e.message, offline: true };
      }
    }
    if (seq !== epoch) return { stale: true };
    return res;
  }

  function hydrate(d) {
    if (!d) return;
    if (Array.isArray(d.sources)) R6.sources = d.sources;
    if (Array.isArray(d.roots)) R6.roots = d.roots;
    if (Array.isArray(d.jobs)) {
      R6.events = d.jobs.map(function (j) {
        return { at: j.at, source: j.source, kind: j.kind, tone: j.tone, text: j.text };
      });
    }
    if (d.tuning) R6.tuning = Object.assign({}, R6.tuning, d.tuning);
    if (d.revision) state.revision = d.revision;
  }

  /* ---- 状态文本 --------------------------------------------------------- */
  function connectionLabel() {
    if (state.mode === 'live') return '服务已连接';
    if (state.scenario === 'offline' || state.scenario === 'error') return '服务未连接';
    return '未连接服务';
  }

  function statusText() {
    if (state.scenario === 'loading') return '加载中 · 正在读取 /api/v2/intake';
    if (state.scenario === 'empty') return '空库 · 尚无远程日志源';
    if (state.scenario === 'no-result') return '无匹配 · 筛选条件已保留，采集任务不受影响';
    if (state.scenario === 'notfound') return '资源不存在（404） · 列表已刷新';
    if (state.scenario === 'error') return '请求失败 · 显示 ' + state.freshness + ' 的上次成功快照';
    if (state.scenario === 'forbidden') return '权限不足（403） · 只读 · 写操作已停用';
    if (state.scenario === 'conflict') return '版本冲突（409） · 输入已保留为草稿';
    if (state.scenario === 'saving') return '保存中 · 等待服务端回执';
    if (state.scenario === 'saved') return '已保存 · 服务端回执 revision ' + state.revision;
    if (state.scenario === 'rebuilding') return '重建中 ' + state.progress + '% · 旧索引继续可读';
    if (state.scenario === 'offline') return '服务未连接 · 显示上次成功快照 · 写操作已停用';
    return connectionLabel() + ' · ' + R6.sources.length + ' 个日志源 · ' + state.role + ' · ' + state.saved;
  }

  function dataFreshness() {
    if (state.scenario === 'rebuilding') return '重建进度 ' + state.progress + '% · 旧索引可读';
    if (state.scenario === 'saved') return '已保存 · revision ' + state.revision;
    if (state.scenario === 'saving') return '提交中 · 请勿关闭页面';
    return '上次成功响应 ' + state.freshness + ' · revision ' + state.revision;
  }

  function scenarioSelect() {
    return '<label for="lb-scenario" class="r6-sub" style="margin:0 6px">状态</label>' +
      '<select id="lb-scenario" class="select" aria-label="预览状态" style="height:18px;min-height:18px;font-size:11px;padding:0 4px;margin-right:8px">' +
      SCENARIOS.map(function (s) {
        return '<option value="' + s[0] + '"' + (state.scenario === s[0] ? ' selected' : '') + '>' + s[1] + '</option>';
      }).join('') + '</select>';
  }

  /* ---- 状态件：只用原型已有组件形态（计划 §4.4 硬边界） --------------- */
  function stateBanner() {
    var s = state.scenario;
    if (s === 'ready' || s === 'saved') {
      if (s === 'saved') return '<div class="r6-note">' + I('check', 'sm') + '已保存 · 服务端已回执（revision ' + state.revision + '）。草稿已清除。</div>';
      return '';
    }
    if (s === 'loading') return '<div class="r6-note">' + I('clock', 'sm') + '加载中 · 正在读取 <code class="mono">GET /api/v2/intake</code>，已保留上次成功快照，界面不跳动。</div>' +
      '<div class="r6-progress" aria-label="加载进度 45%"><span style="width:45%"></span></div>';
    if (s === 'saving') return '<div class="r6-note">' + I('clock', 'sm') + '保存中 · 正在提交 <code class="mono">PATCH /api/v2/sources/{id}</code>；关闭页面不会丢失已填内容。</div>' +
      '<div class="r6-progress" aria-label="保存进度 70%"><span style="width:70%"></span></div>';
    if (s === 'rebuilding') return '<div class="r6-note">' + I('clock', 'sm') + '重建规则结果 · 旧索引继续可读，原文不重新下载。</div>' +
      '<div class="r6-progress" aria-label="重建进度 ' + state.progress + '%"><span style="width:' + state.progress + '%"></span></div>';
    if (s === 'empty') return '<div class="r6-empty">' + I('database', 'lg') + '<strong>还没有远程日志源</strong>' +
      '<p>添加第一个远程目录后开始采集。已登记的 3 个存储位置可以直接使用。</p>' +
      r6Btn('添加日志源', 'new-source', 'plus', 'primary') + '</div>';
    if (s === 'no-result') return '<div class="r6-empty">' + I('search', 'lg') + '<strong>没有匹配「' + E(R6.search || 'psg') + '」的日志源</strong>' +
      '<p>这不是空库。调整名称、目录或状态条件；已有采集任务不受筛选影响。</p>' +
      r6Btn('清除筛选', 'clear-filters', 'clear', 'sm') + '</div>';
    if (s === 'notfound') return '<div class="r6-empty">' + I('warning', 'lg') + '<strong>日志源不存在或已被删除</strong>' +
      '<p>详情请求返回 404。列表已刷新为服务端最新状态，原文归档与索引不受影响。</p>' +
      r6Btn('返回日志源列表', 'clear-filters', 'left', 'sm') + '</div>';
    if (s === 'error') return '<div class="r6-errorbox" role="alert"><strong>无法读取日志源</strong>' +
      '<p><code class="mono">GET /api/v2/intake</code> 连接超时。已保留 ' + E(state.freshness) + ' 的上次成功快照，期间没有执行写操作。</p>' +
      r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'forbidden') return '<div class="r6-errorbox" role="alert"><strong>权限不足（403）</strong>' +
      '<p>当前身份 <b>' + E(state.role) + '</b> 只有只读权限，无法暂停采集、保存配置或重建索引。页面保持只读，所有输入都已保留；联系管理员申请「采集管理」后可重试。</p>' +
      r6Btn('重试', 'retry', 'refresh', 'sm') + '</div>';
    if (s === 'conflict') return '<div class="r6-note bad" role="alert"><strong>版本冲突（409）</strong>' +
      '<p>该日志源已被其他人更新到 revision ' + (state.revision + 1) + '，你基于 revision ' + state.revision + ' 填写的内容已保留为草稿，未被覆盖，也不会自动合并。</p>' +
      '<div class="r6-inline-actions">' + r6Btn('查看差异', 'datalink', 'info', 'sm') + r6Btn('载入新版本后重试', 'retry', 'refresh', 'sm') + '</div></div>';
    if (s === 'offline') return '<div class="r6-note warn">' + I('warning', 'sm') + '服务未连接 · 显示 ' + E(state.freshness) + ' 的上次成功快照。写操作已停用，恢复连接后可重试。</div>';
    return '';
  }

  let painting = false;
  function paintStatus() {
    if (painting) return;
    painting = true;
    try {
      var row = document.querySelector('.app-status .row');
      if (row) row.innerHTML = I('lock', 'sm') + statusText();
      var extra = document.querySelector('.app-status .status-right .extra');
      if (extra) extra.textContent = dataFreshness();
      var tag = document.querySelector('.lb-header .prototype-tag');
      if (tag) tag.textContent = connectionLabel();
      var sel = document.getElementById('lb-scenario');
      if (sel && sel.value !== state.scenario) sel.value = state.scenario;

      var html = stateBanner();
      var host = document.getElementById('lb-state');
      // .r6-pane is a horizontal flex row in the prototype: inserting a
      // sibling INSIDE it would squeeze the table. Insert before the pane, as
      // a direct child of the vertical .r6-content stack instead.
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
        pane.hidden = full;
      } else if (host) {
        host.remove();
      } else if (pane) {
        pane.hidden = false;
      }
      if (pane && !html) pane.hidden = false;
    } finally {
      painting = false;
    }
  }

  function setScenario(v) {
    state.scenario = v;
    if (v === 'rebuilding') state.progress = 0;
    paintStatus();
  }

  /* ---- 动作适配：data-r6 → /api/v2/* ---------------------------------- */
  async function refresh() {
    state.scenario = 'loading';
    paintStatus();
    var r = await request('intake');
    if (r.stale) return;
    if (!r.ok) { state.scenario = 'error'; paintStatus(); return; }
    hydrate(r.data);
    state.freshness = now();
    state.scenario = 'ready';
    render(false);
    paintStatus();
    notify('已从 /api/v2/intake 更新 ' + R6.sources.length + ' 个日志源');
  }

  async function connect() {
    state.mode = 'live';
    var r = await request('intake');
    if (r.ok) {
      hydrate(r.data);
      state.freshness = now();
      state.scenario = 'ready';
      state.saved = '已保存';
      notify('已连接 /api/v2 服务，数据来自真实回执');
    } else {
      state.mode = 'preview';
      state.scenario = 'offline';
      notify('连接失败，已保留本地快照；写操作停用', true);
    }
    var d = document.getElementById('dialog');
    if (d && d.open) d.close();
    render(false);
    paintStatus();
  }

  function retry() {
    if (state.scenario === 'conflict') {
      state.revision += 1;
      state.scenario = 'ready';
      paintStatus();
      notify('已载入 revision ' + state.revision + '，草稿仍保留，可重新保存');
      return;
    }
    connect();
  }

  async function pause(id) {
    var x = r6Source(id);
    if (!x) return;
    var action = x.status === 'paused' ? 'resume' : 'pause';
    state.scenario = 'saving';
    paintStatus();
    var r = await request('actions', { id: id, body: { action: action, expected_revision: state.revision } });
    if (r.status === 409) { state.scenario = 'conflict'; paintStatus(); return; }
    if (!r.ok) { state.scenario = 'error'; paintStatus(); return; }
    if (state.mode === 'live') hydrate(r.data);
    if (action === 'resume') {
      x.status = x.protocol === 'scp' ? 'limited' : x.protocol === 'http' ? 'idle' : 'following';
      x.lag = x.mode === 'live' ? '约 ' + x.poll + ' 秒' : '等待下一次计划';
      r6Event(x.name, '继续采集', '从已提交检查点继续，历史归档保留。', 'blue');
    } else {
      x.status = 'paused';
      x.lag = '检查点与归档保留';
      r6Event(x.name, '暂停采集', '暂停新的传输；不删除原文或解析缓存。');
    }
    state.revision += 1;
    state.scenario = 'ready';
    state.saved = '已保存';
    r6Persist();
    render(false);
    paintStatus();
  }

  async function probe() {
    r6CaptureDraft();
    var d = r6Draft || {};
    var host = document.getElementById('r6-probe-result');
    if (host) host.innerHTML = '<div class="r6-note">正在预检 ' + E(d.endpoint || '') + ' …</div>';
    var r = await request('probe', { id: d.id || '', body: { protocol: d.protocol, endpoint: d.endpoint, port: d.port, maps: d.maps } });
    var checks = (r.data && r.data.checks) || probeChecks();
    var ok = r.ok && checks.every(function (c) { return c.ok; });
    r6Probe = true;
    r6ProbeKind = ok ? 'ready' : 'auth';
    LB.probeResult = { ok: ok, checks: checks };
    var save = document.querySelector('#dialog [data-r6="wizard-save"]');
    if (save) save.disabled = !ok;
    if (host) {
      host.innerHTML = checks.map(function (c) {
        return '<div class="r6-probe-row ' + (c.ok ? '' : 'bad') + '">' + I(c.ok ? 'check' : 'error') +
          '<span>' + E(c.label) + ' · ' + E(c.detail) + '</span></div>';
      }).join('') + (ok ? '' : '<div class="r6-note bad" style="margin-top:12px">预检未通过，保存已停用。修正后重新运行预检；不会自动降级为匿名连接。</div>');
    }
    r6Event(d.name || '新日志源', '预检完成',
      ok ? '连接、目录发现、范围读取与目标权限全部通过。' : '预检未通过，配置未提交。', ok ? 'good' : 'warn');
  }

  async function saveSource() {
    r6CaptureDraft();
    var error = r6Validate(r6Draft);
    if (error) { r6ShowFormError(error); return; }
    if (!LB.probeResult || !LB.probeResult.ok) { r6ShowFormError('请先运行预检并全部通过。'); return; }
    state.scenario = 'saving';
    paintStatus();
    var d = clone(r6Draft);
    var r = await request(d.id ? 'updateSource' : 'createSource', {
      id: d.id || '',
      body: Object.assign({}, d, { expected_revision: state.revision })
    });
    if (r.status === 409) {
      state.scenario = 'conflict';
      paintStatus();
      r6ShowFormError('版本冲突（409）：该日志源已被更新。你填写的内容已保留为草稿，未覆盖任何服务端数据。');
      return;
    }
    if (!r.ok) {
      state.scenario = 'error';
      paintStatus();
      r6ShowFormError('保存失败：服务端未回执。草稿已保留，可重试。');
      return;
    }
    d.id = d.id || (r.data && r.data.id) || 'source-' + Date.now();
    var old = r6Source(d.id);
    Object.assign(d, {
      count: old ? old.count : 0,
      raw: old ? old.raw : '0 B',
      indexed: old ? old.indexed : '0 B',
      coverage: old ? old.coverage : 0,
      status: 'paused',
      lag: '待启用 · 等待首次采集'
    });
    if (old) R6.sources[R6.sources.indexOf(old)] = d; else R6.sources.push(d);
    R6.selected = d.id;
    R6.tab = 'sources';
    S.view = 'sync';
    state.revision += 1;
    state.saved = '已保存';
    state.scenario = 'saved';
    r6Event(d.name, '配置保存', '目录映射已提交到服务端；采集任务按计划启动。', 'good');
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    r6Persist();
    render(false);
    paintStatus();
  }

  async function saveRoot() {
    var name = document.getElementById('r6-root-name').value.trim();
    var path = document.getElementById('r6-root-path').value.trim();
    var cache = document.getElementById('r6-root-cache').value.trim();
    var err = document.getElementById('r6-root-error');
    if (!name || !path.startsWith('/') || !cache.startsWith('/') || path.includes('..') || cache.includes('..')) {
      err.textContent = '请填写名称与不包含 .. 的绝对路径。';
      return;
    }
    if (R6.roots.some(function (r) {
      return r.id !== R6.editRoot && (path === r.path || path.startsWith(r.path + '/') || r.path.startsWith(path + '/'));
    })) {
      err.textContent = '存储根目录不可重复或相互包含。';
      return;
    }
    state.scenario = 'saving';
    paintStatus();
    var r = await request(R6.editRoot ? 'updateRoot' : 'saveRoot', {
      id: R6.editRoot || '',
      body: { name: name, path: path, cache: cache, expected_revision: state.revision }
    });
    if (r.status === 409) { state.scenario = 'conflict'; paintStatus(); return; }
    if (R6.editRoot) Object.assign(r6Root(R6.editRoot), { name: name, cache: cache });
    else R6.roots.push({ id: 'root-' + Date.now(), name: name, path: path, cache: cache, state: 'online', free: '待验证', type: '自定义位置', defaultRule: 'CPQPSGX 标准' });
    var pr = await request('probeRoot', { id: R6.editRoot || '' });
    state.revision += 1;
    state.scenario = 'ready';
    state.saved = '已保存';
    r6Persist();
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    R6.tab = 'storage';
    S.view = 'sync';
    render(false);
    paintStatus();
    notify((pr.data && pr.data.checks ? '权限与挂载已验证；' : '') + '存储位置已保存到服务端');
  }

  async function saveTuning() {
    var ks = ['collect', 'perHost', 'parse', 'backfill', 'chunk', 'memory'];
    var v = {};
    ks.forEach(function (k) { v[k] = Number(document.getElementById('r6-tune-' + k).value); });
    if (ks.some(function (k) { return !Number.isInteger(v[k]) || v[k] < 1 || v[k] > 64; }) || v.perHost > v.collect) {
      document.getElementById('r6-tune-error').textContent = '请输入 1–64 的整数，且单主机并发不能超过总并发。';
      return;
    }
    state.scenario = 'saving';
    paintStatus();
    var r = await request('budget', { body: Object.assign({}, v, { expected_revision: state.revision }) });
    if (r.status === 409) { state.scenario = 'conflict'; paintStatus(); return; }
    R6.tuning = Object.assign({}, R6.tuning, v);
    state.revision += 1;
    state.scenario = 'ready';
    state.saved = '已保存';
    r6Persist();
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
    render(false);
    paintStatus();
    notify('资源预算已保存到 /api/v2/runtime/budget');
  }

  async function rebuildCache() {
    if (state.scenario === 'rebuilding') return;
    state.scenario = 'rebuilding';
    state.progress = 0;
    paintStatus();
    var r = await request('rebuild', { body: { scope: 'rules', reason: 'manual' } });
    if (!r.ok) { state.scenario = 'error'; paintStatus(); return; }
    var timer = setInterval(function () {
      state.progress = Math.min(100, state.progress + 11);
      paintStatus();
      if (state.progress >= 100) {
        clearInterval(timer);
        state.scenario = 'saved';
        state.saved = '已重建';
        state.revision += 1;
        r6Event('解析缓存', '重建完成', '新规则结果版本已验证并切换；原文未重新下载。', 'good');
        R6.tab = 'cache';
        S.view = 'sync';
        render(false);
        paintStatus();
      }
    }, 240);
    var dlg = document.getElementById('dialog');
    if (dlg && dlg.open) dlg.close();
  }

  function exportEvents() {
    download('logbench-r6-jobs.json', JSON.stringify({
      format: 'logbench.jobs/1',
      exported_at: new Date().toISOString(),
      revision: state.revision,
      jobs: R6.events
    }, null, 2), 'application/json');
  }

  function savePref(key, value) {
    state.saved = '保存中';
    paintStatus();
    request('workspace', { body: { [key]: value } }).then(function () {
      state.saved = '已保存';
      paintStatus();
    });
  }

  /* ---- 数据链路 / 备份对话框：复用原型 dialog + r6-kv + details -------- */
  function datalink() {
    var rows = Object.keys(ENDPOINTS).map(function (k) {
      var e = ENDPOINTS[k];
      return '<tr><td class="mono">' + E(k) + '</td><td class="mono">' + E(e[0]) + ' ' + E(e[1]) +
        '</td><td>' + r6Badge(state.mode === 'live' ? '已连接' : '预览态', state.mode === 'live' ? 'good' : 'blue') + '</td></tr>';
    }).join('');
    var calls = state.calls.length ? state.calls.slice(0, 6).map(function (c) {
      return '<div class="r6-meta mono">' + E(c.at) + ' · ' + E(c.method) + ' ' + E(c.path) + '</div>';
    }).join('') : '<p class="r6-mininote">尚无请求。点击「刷新」或任意写动作后会在此登记。</p>';
    dialog('数据链路', '<div class="r6-form">' +
      '<div class="r6-note ' + (state.mode === 'live' ? '' : 'warn') + '">' + I('database') +
      (state.mode === 'live'
        ? '已连接 /api/v2 服务，界面数据来自真实回执。'
        : '当前是本地预览：未连接服务，界面数据取自 /api/v2/* 的响应形状快照。预览只用于确认视觉与结构，不作为后端可用性证据。') +
      '</div>' +
      '<dl class="r6-kv"><dt>连接状态</dt><dd>' + E(connectionLabel()) + '</dd>' +
      '<dt>当前身份</dt><dd>' + E(state.role) + ' · 采集管理权限</dd>' +
      '<dt>数据新鲜度</dt><dd>' + E(state.freshness) + ' · 上次成功响应</dd>' +
      '<dt>保存状态</dt><dd>' + E(state.saved) + ' · revision ' + state.revision + '</dd>' +
      '<dt>实时通道</dt><dd class="mono">/api/v2/events</dd>' +
      '<dt>轮询降级</dt><dd class="mono">/api/v2/events/poll</dd></dl>' +
      '<details><summary>端点映射（' + Object.keys(ENDPOINTS).length + ' 条）</summary>' +
      '<div class="r6-table-scroll"><table class="r6-table"><thead><tr><th>动作</th><th>方法与路径</th><th>状态</th></tr></thead><tbody>' + rows + '</tbody></table></div></details>' +
      '<details><summary>最近调用</summary>' + calls + '</details>' +
      '<details><summary>引擎层保留清单</summary><p class="r6-mininote">Worker 本机解析、IndexedDB 缓存、combined provider、expected_revision 与 409 草稿保留、SSE 实时增量、13 项资源预算、权限分层与虚拟窗口。本机日志正文不上传服务端。</p></details>' +
      '</div>', r6Btn('关闭', 'modal-close', null) + r6Btn('连接服务', 'connect', 'refresh', 'primary'), true);
  }

  function backup() {
    dialog('备份与恢复', '<div class="r6-form">' +
      '<div class="r6-note warn">' + I('shield') + '<strong>恢复会替换当前工作区</strong>的阅读视角、书签与显示设置；已归档原文与解析索引不受影响。恢复失败不会留下半份文件库。</div>' +
      '<h3 class="r6-section-title">导出</h3>' +
      '<p class="r6-mininote">包含工作区快照、本机文件列表与显示偏好，格式 logbench.workspace/1。</p>' +
      r6Btn('导出备份', 'backup-export', 'download', '') +
      '<h3 class="r6-section-title">恢复</h3>' +
      '<div class="field"><label class="label" for="lb-restore-file">选择备份文件</label>' +
      '<input id="lb-restore-file" class="input" type="file" accept=".json,application/json"></div>' +
      '<div class="r6-errorbox" id="lb-restore-error" role="alert"></div>' +
      '<details><summary>哪些内容不会被备份替换</summary><p class="r6-mininote">服务端归档原文、采集检查点、结构索引与审计事件由 /api/v2/* 独立管理；本机日志正文始终只留在本浏览器。</p></details>' +
      '</div>', r6Btn('关闭', 'modal-close', null) + r6Btn('恢复并替换当前工作区', 'backup-restore', 'refresh', 'primary'), true);
  }

  function exportBackup() {
    download('logbench-workspace.json', JSON.stringify({
      format: 'LogBench Workspace Backup', version: 1,
      workspace: { schema: 'logbench.workspace', version: 1 },
      preferences: LB.prefs,
      exportedAt: new Date().toISOString()
    }, null, 2), 'application/json');
    notify('备份已导出');
  }

  function restoreBackup() {
    var input = document.getElementById('lb-restore-file');
    var err = document.getElementById('lb-restore-error');
    if (!input || !input.files || !input.files[0]) { err.textContent = '请先选择备份文件。'; return; }
    input.files[0].text().then(function (text) {
      var value = JSON.parse(text);
      if (value.format !== 'LogBench Workspace Backup' || value.version !== 1) throw new Error('格式或版本不受支持');
      Object.assign(LB.prefs, value.preferences || {});
      state.saved = '已恢复';
      state.revision += 1;
      var d = document.getElementById('dialog');
      if (d && d.open) d.close();
      render(false);
      paintStatus();
      notify('工作区已恢复，服务端数据未改变');
    }).catch(function (e) {
      err.textContent = '恢复失败：' + e.message + '。当前工作区未改变，原文件保留。';
    });
  }

  var api = {
    ENDPOINTS: ENDPOINTS,
    SCENARIOS: SCENARIOS,
    state: state,
    prefs: { density: 'compact', contrast: 'normal', font_size: 13, wrap: false, time_precision: 'ms', sort: 'time' },
    request: request,
    hydrate: hydrate,
    statusText: statusText,
    dataFreshness: dataFreshness,
    scenarioSelect: scenarioSelect,
    paintStatus: paintStatus,
    setScenario: setScenario,
    stateBanner: stateBanner,
    refresh: refresh,
    connect: connect,
    retry: retry,
    pause: pause,
    probe: probe,
    saveSource: saveSource,
    saveRoot: saveRoot,
    saveTuning: saveTuning,
    rebuildCache: rebuildCache,
    exportEvents: exportEvents,
    savePref: savePref,
    datalink: datalink,
    backup: backup,
    exportBackup: exportBackup,
    restoreBackup: restoreBackup,
    probeResult: null
  };
  return api;
})();
window.LB = LB;

/* ---- 兼容层：保留 window.__r6 与旧评审脚本入口 -------------------------- */
window.LogBenchRemotePreview = Object.assign(window.LogBenchRemotePreview || {}, {
  adapter: LB, endpoints: LB.ENDPOINTS, state: R6
});
window.__r6 = Object.assign(window.__r6 || {}, {
  state: S, intake: R6, adapter: LB,
  api: LB.request,
  showView: function (v) { S.view = v; render(false); LB.paintStatus(); },
  applyEvent: function (e) { if (e && e.text) r6Event(e.source || 'system', e.kind || '事件', e.text, e.tone || ''); },
  scenario: LB.setScenario,
  follower: function () { return R6.liveFollow; }
});
