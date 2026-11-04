/*
 * runtime.js — 运行记录与资源预算 (R6-083, backend R6-083 API).
 *
 * Two pages in one view, because they answer one question: "is the machine doing what
 * the configuration says?" The job table shows what is queued and why; the budget form
 * shows what the limits are and which of them took effect.
 *
 * Deliberate limits, so the page cannot overstate itself:
 *   - every number comes from the API. A metric that the server did not measure is
 *     printed as 未采样, never as 0.
 *   - a budget field is labelled 即时 / 下个任务 / 需重启 from the server's own `applies`
 *     table, and after saving the page shows what the server reported as applied —
 *     including "busy" when a worker could not be stopped.
 *   - the queue table polls only while the view is visible and only every 5 s; a
 *     runtime page that hammers the server it is describing is its own problem.
 *   - pause/cancel/retry go through the job action API with an action id, so a double
 *     click is one transition, not two.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBRuntime = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var POLL_MS = 5000;

  var STATE_LABEL = {
    queued: '排队', reading: '读取', framing: '分帧', uploading: '上传', uploaded: '已上传',
    indexing: '索引', indexed: '完成', paused: '暂停', failed: '失败', cancelled: '取消'
  };
  var KIND_LABEL = { index: '索引', query: '查询', upload: '上传', collect: '采集' };
  var APPLY_LABEL = { live: '即时', next_job: '下个任务', next_restart: '需重启' };
  var BUDGET_FIELDS = [
    ['parse_slots', '解析槽位', '常驻解析/规则 CPU 进程数'],
    ['query_slots', '查询槽位', '并发查询上限'],
    ['total_cpu', 'CPU 总上限', '解析+查询不得突破'],
    ['history_slots', '历史补采槽位', '包含在解析槽位内'],
    ['interactive_reserve', '交互保留', '重算时至少留出的解析槽'],
    ['writer_slots', '索引写入并发', '分片单写，机械盘应更低'],
    ['upload_slots', '上传会话上限', '全局同时进行的私有上传'],
    ['per_principal_uploads', '每身份上传', '单个身份同时上传数'],
    ['per_principal_queries', '每身份查询', '单个身份同时查询数'],
    ['transfer_connections', '传输连接', '远程采集总连接'],
    ['per_host_connections', '每主机连接', '单主机连接上限'],
    ['mem_soft_bytes', '内存软预算', '含解析/查询/传输进程'],
    ['hot_cache_bytes', '热页缓存', '包含在内存软预算内']
  ];

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

  function number(value) {
    return value == null ? '未采样' : String(value);
  }

  function ms(value) {
    return value == null ? '未采样' : value + ' ms';
  }

  function create(options) {
    options = options || {};
    var api = options.api;
    var host = null;
    var parts = {};
    var state = {
      tab: 'jobs',
      admin: false,
      loading: false,
      error: null,
      runtime: null,
      budget: null,
      draft: {},
      saving: false,
      timer: null,
      visible: false,
      lastAction: null,
      /* The server's confirmation of the last saved edit. Kept in state because the
       * panel is rebuilt on the refresh that follows a save. */
      appliedResult: null
    };

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.className = 'runtime-view';
      parts.bar = el('div', 'r6-toolbar');
      parts.title = el('span', 'r6-meta', '运行记录');
      parts.tabs = el('div', 'r6-tabs rt-tabs');
      state.tabs = [
        ['jobs', '运行记录', '队列、水位与失败'],
        ['budget', '资源预算', '池、内存、磁盘与生效范围']
      ].map(function (pair) {
        var button = el('button', 'r6-tab', pair[1]);
        button.type = 'button';
        button.title = pair[2];
        button.addEventListener('click', function () { showTab(pair[0]); });
        parts.tabs.appendChild(button);
        return { id: pair[0], node: button };
      });
      parts.refresh = el('button', 'r6-icon', '刷新');
      parts.refresh.type = 'button';
      parts.refresh.addEventListener('click', function () { load(true); });
      parts.diag = el('button', 'r6-icon', '导出诊断');
      parts.diag.type = 'button';
      parts.diag.title = '导出脱敏诊断：不含路径、原文、搜索词或凭据';
      parts.diag.addEventListener('click', function () { exportDiagnostics(); });
      parts.bar.append(parts.title, el('span', 'spacer'), parts.refresh, parts.diag);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'alert');
      parts.note.hidden = true;

      parts.body = el('div', 'rt-body');
      host.append(parts.bar, parts.tabs, parts.note, parts.body);
      return api;
    }

    function setNotice(text, tone) {
      parts.note.hidden = !text;
      parts.note.textContent = text || '';
      parts.note.dataset.tone = tone || 'info';
    }

    function showTab(tab) {
      state.tab = tab;
      state.tabs.forEach(function (entry) {
        entry.node.classList.toggle('is-active', entry.id === tab);
        entry.node.setAttribute('aria-selected', entry.id === tab ? 'true' : 'false');
      });
      render();
      load(false);
    }

    function load(force) {
      if (!state.visible && !force) return Promise.resolve();
      state.loading = true;
      return api.get('/api/v1/me').then(function (me) {
        api.setCsrf(me.csrfToken || '');
        state.admin = !!(me.capabilities && me.capabilities.admin);
        if (!state.admin) {
          state.error = 'forbidden';
          state.loading = false;
          render();
          setNotice('运行记录与资源预算需要管理员权限；非管理员看不到池、队列和路径指标。', 'warn');
          return null;
        }
        return api.get('/api/v2/runtime' + (state.tab === 'budget' ? '?probe=disk' : ''))
          .then(function (value) {
            state.runtime = value;
            state.budget = value.budget;
            state.loading = false;
            state.error = null;
            setNotice(null);
            if (!Object.keys(state.draft).length) seedDraft();
            render();
            return value;
          });
      }).catch(function (error) {
        state.loading = false;
        state.error = error.message || String(error);
        render();
        setNotice(error.status === 403
          ? '运行记录需要管理员权限。'
          : ('无法读取运行状态：' + (error.message || error)), error.status === 403 ? 'warn' : 'error');
      });
    }

    function seedDraft() {
      var source = (state.budget && state.budget.base) || {};
      Object.keys(source).forEach(function (key) { state.draft[key] = source[key]; });
    }

    /* ------------------------------------------------------------------ render */
    function render() {
      parts.body.innerHTML = '';
      state.tabs.forEach(function (entry) {
        entry.node.hidden = !state.admin && state.tab !== 'jobs';
      });
      if (state.error === 'forbidden') {
        parts.body.appendChild(emptyState('需要管理员权限',
          '运行记录包含池、队列与主机资源指标，仅管理员可见。任务面板仍可在日志页查看自己的任务。'));
        return;
      }
      if (state.tab === 'jobs') renderJobs();
      else renderBudget();
    }

    function emptyState(title, body) {
      var box = el('div', 'r6-empty-state');
      box.append(el('strong', null, title), el('p', null, body));
      return box;
    }

    function renderJobs() {
      var runtime = state.runtime;
      if (!runtime) {
        parts.body.appendChild(el('p', 'r6-meta', state.loading ? '读取中…' : '暂无数据'));
        return;
      }
      var stats = el('div', 'rt-stats');
      var counts = runtime.jobs.counts;
      var failed = (counts.by_state && counts.by_state.failed) || 0;
      [
        ['排队中', runtime.jobs.queued, null],
        ['运行中', (counts.by_state && (counts.by_state.indexing || 0)) +
          (counts.by_state && (counts.by_state.framing || 0)) || 0, null],
        ['失败', failed, failed ? 'bad' : null],
        ['索引积压', runtime.index.backlog, runtime.index.backlog ? 'warn' : null],
        ['查询首屏 p50', ms(runtime.queries.first_page_p50_ms), null],
        ['查询首屏 p95', ms(runtime.queries.first_page_p95_ms), null]
      ].forEach(function (item) {
        var cell = el('div', 'rt-stat');
        cell.append(el('span', 'rt-stat-label', item[0]), el('strong', 'rt-stat-value', String(item[1])));
        if (item[2]) cell.dataset.tone = item[2];
        stats.appendChild(cell);
      });
      parts.body.appendChild(stats);

      var pools = el('div', 'rt-pools');
      pools.appendChild(poolCard('解析池', [
        ['进程', runtime.pools.parse.workers + ' / 预算 ' + runtime.pools.parse.budget_slots],
        ['Node', runtime.pools.parse.available ? (runtime.capabilities.node.version || '可用') : '不可用'],
        ['堆上限', (runtime.pools.parse.heap_mb || '—') + ' MiB'],
        ['请求', String((runtime.pools.parse.stats || {}).requests || 0)]
      ]));
      pools.appendChild(poolCard('查询池', [
        ['并发', runtime.pools.query.gate.active + ' / ' + runtime.pools.query.gate.slots],
        ['等待', String(runtime.pools.query.gate.waiting)],
        ['模式', runtime.pools.query.mode === 'async' ? '异步' : '内联'],
        ['完成', runtime.queries.created + ' 次']
      ]));
      pools.appendChild(poolCard('上传与传输', [
        ['上传会话', number(runtime.pools.upload.open_sessions)],
        ['上传上限', String(runtime.pools.upload.max_active_global)],
        ['传输', runtime.pools.transfer.state === 'not_running' ? '未启动' : String(runtime.pools.transfer.running)],
        ['RSS', bytes(runtime.resources.rss_bytes)]
      ]));
      parts.body.appendChild(pools);

      var wrap = el('div', 'r6-table-wrap rt-table-wrap');
      var table = el('table', 'r6-table rt-table');
      var head = el('thead');
      var headRow = el('tr');
      ['任务', '来源 / 文件', '状态', '排队原因', '水位', '更新时间', '操作'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var body = el('tbody');
      var jobs = runtime.jobs.queue || [];
      if (!jobs.length) {
        var tr = el('tr');
        var td = el('td', 'lib-empty-cell');
        td.colSpan = 7;
        td.textContent = '当前没有任务。索引、查询与上传任务会在这里出现。';
        tr.appendChild(td);
        body.appendChild(tr);
      }
      jobs.forEach(function (job) { body.appendChild(jobRow(job)); });
      table.append(head, body);
      wrap.appendChild(table);
      parts.body.appendChild(wrap);
      parts.body.appendChild(el('p', 'r6-foot',
        '任务列表按创建时间倒序，最多 100 条；水位来自任务检查点，不代表最终完成量。'
        + (runtime.queries.result_cache ? ' 查询结果缓存：' + runtime.queries.result_cache.reason : '')));
    }

    function poolCard(title, rows) {
      var card = el('div', 'rt-pool');
      card.appendChild(el('div', 'rt-pool-title', title));
      var list = el('dl', 'rt-pool-list');
      rows.forEach(function (pair) {
        list.append(el('dt', null, pair[0]), el('dd', null, pair[1]));
      });
      card.appendChild(list);
      return card;
    }

    function jobRow(job) {
      var tr = el('tr');
      var kind = el('td');
      kind.append(el('div', 'r6-cell-strong', KIND_LABEL[job.kind] || job.kind),
        el('div', 'r6-meta mono', job.id.slice(0, 14)));
      var source = el('td');
      source.append(el('div', 'mono', job.file || '—'),
        el('div', 'r6-meta', (job.source || '—') + ' · ' + (job.execution || 'server')));
      var stateCell = el('td');
      var badge = el('span', 'r6-badge', STATE_LABEL[job.state] || job.state);
      badge.classList.add('is-' + (job.state === 'indexed' ? 'good'
        : (job.state === 'failed' ? 'bad' : (job.state === 'cancelled' ? 'quiet' : 'warn'))));
      stateCell.appendChild(badge);
      if (job.error) {
        stateCell.appendChild(el('div', 'r6-meta', (job.error.code || '') + ' ' +
          String(job.error.message || '').slice(0, 60)));
      }
      var reason = el('td', 'r6-meta');
      reason.textContent = job.resume_reason || ((job.progress && job.progress.phase) || '—');
      var watermark = el('td', 'r6-meta mono');
      var wm = job.watermark || {};
      var keys = Object.keys(wm).filter(function (key) {
        return typeof wm[key] === 'number';
      }).slice(0, 2);
      watermark.textContent = keys.length ? keys.map(function (key) {
        return key + '=' + wm[key];
      }).join(' ') : '—';
      var updated = el('td', 'r6-meta mono');
      updated.textContent = String(job.updatedAt || '').replace('T', ' ').slice(0, 19);
      var ops = el('td');
      var wrap = el('div', 'r6-inline-actions');
      [['cancel', '取消', job.cancellable], ['retry', '重试', job.retryable]].forEach(function (pair) {
        var button = el('button', 'r6-icon' + (pair[0] === 'cancel' ? ' is-danger' : ''), pair[1]);
        button.type = 'button';
        button.disabled = !pair[2] || !state.admin;
        button.addEventListener('click', function () { act(job, pair[0], button); });
        wrap.appendChild(button);
      });
      ops.appendChild(wrap);
      tr.append(kind, source, stateCell, reason, watermark, updated, ops);
      return tr;
    }

    function act(job, action, button) {
      if (!state.admin) return;
      var actionId = action + ':' + job.id + ':' + Date.now().toString(36);
      button.disabled = true;
      button.textContent = '…';
      api.post('/api/v2/jobs/' + encodeURIComponent(job.id) + '/actions',
        { action: action, actionId: actionId, expectedRevision: job.revision })
        .then(function (value) {
          state.lastAction = { action: action, job: job.id, at: new Date().toISOString(),
            state: value && value.job ? value.job.state : null };
          setNotice('任务 ' + job.id.slice(0, 10) + ' 已' +
            (action === 'cancel' ? '请求取消' : '重试为 ' + ((value.job || {}).id || '新任务')),
            'ok');
          return load(true);
        }, function (error) {
          button.disabled = false;
          button.textContent = action === 'cancel' ? '取消' : '重试';
          setNotice('操作被拒绝：' + (error.message || error) +
            (error.code === 'pause_not_supported_in_state' ? '（该阶段不支持暂停）' : ''), 'error');
        });
    }

    /* ------------------------------------------------------------------ budget */
    function renderBudget() {
      var budget = state.budget;
      if (!budget) {
        parts.body.appendChild(el('p', 'r6-meta', state.loading ? '读取中…' : '暂无数据'));
        return;
      }
      var head = el('div', 'rt-budget-head');
      head.append(el('strong', null, '资源预算'),
        el('span', 'r6-meta', '修订 ' + budget.revision +
          (budget.notes && budget.notes.length ? ' · 已按压力收缩：' + budget.notes.join('、') : '')),
        el('span', 'spacer'),
        el('span', 'r6-meta', 'RSS ' + bytes((state.runtime.resources || {}).rss_bytes) +
          ' · 磁盘可用 ' + bytes(((state.runtime.resources || {}).disk || {}).free_bytes) +
          (((state.runtime.resources || {}).disk || {}).latency_ms != null
            ? ' · 写入 ' + ((state.runtime.resources || {}).disk || {}).latency_ms + ' ms' : ' · 写入未采样')));
      parts.body.appendChild(head);
      parts.body.appendChild(el('p', 'r6-hint', budget.derivation));

      var form = el('div', 'rt-form');
      BUDGET_FIELDS.forEach(function (field) {
        var row = el('div', 'rt-field');
        var label = el('label', 'rt-field-label', field[1]);
        label.setAttribute('for', 'rt-' + field[0]);
        var input = el('input', 'r6-input mono');
        input.id = 'rt-' + field[0];
        input.type = 'number';
        input.min = '0';
        input.step = '1';
        input.value = String(state.draft[field[0]] == null ? '' : state.draft[field[0]]);
        input.addEventListener('input', function () {
          var value = Number(input.value);
          state.draft[field[0]] = Number.isFinite(value) ? Math.trunc(value) : input.value;
          renderDiff();
        });
        var apply = el('span', 'rt-apply', APPLY_LABEL[budget.applies[field[0]]] || '—');
        apply.dataset.mode = budget.applies[field[0]] || '';
        var effective = el('span', 'r6-meta',
          '当前生效 ' + (budget.effective[field[0]] == null ? '—'
            : (field[0].indexOf('bytes') !== -1 ? bytes(budget.effective[field[0]])
              : String(budget.effective[field[0]]))));
        var hint = el('p', 'r6-hint', field[2]);
        row.append(label, input, apply, effective, hint);
        form.appendChild(row);
      });
      parts.body.appendChild(form);

      parts.diff = el('div', 'rt-diff');
      parts.diff.hidden = true;
      parts.body.appendChild(parts.diff);

      var actions = el('div', 'rt-actions');
      parts.save = el('button', 'r6-button is-primary', '保存预算');
      parts.save.type = 'button';
      parts.save.disabled = !state.admin || state.saving;
      parts.save.addEventListener('click', function () { save(); });
      var reset = el('button', 'r6-button', '撤销更改');
      reset.type = 'button';
      reset.addEventListener('click', function () {
        state.draft = {};
        seedDraft();
        render();
      });
      actions.append(reset, parts.save);
      parts.body.appendChild(actions);

      parts.result = el('div', 'rt-result');
      parts.result.hidden = true;
      parts.body.appendChild(parts.result);
      renderDiff();
      renderApplied();
    }

    function renderDiff() {
      if (!parts.diff) return;
      var base = (state.budget && state.budget.base) || {};
      var changes = Object.keys(state.draft).filter(function (key) {
        return state.draft[key] !== base[key];
      });
      parts.diff.hidden = !changes.length;
      parts.diff.innerHTML = '';
      if (!changes.length) return;
      parts.diff.appendChild(el('strong', null, '将要修改 ' + changes.length + ' 项'));
      var list = el('ul', 'rt-diff-list');
      changes.forEach(function (key) {
        var mode = (state.budget.applies || {})[key] || '—';
        list.appendChild(el('li', null, key + '：' + base[key] + ' → ' + state.draft[key] +
          '（' + (APPLY_LABEL[mode] || mode) + '）'));
      });
      parts.diff.appendChild(list);
    }

    function save() {
      var changes = {};
      var base = state.budget.base || {};
      Object.keys(state.draft).forEach(function (key) {
        if (state.draft[key] !== base[key]) changes[key] = state.draft[key];
      });
      if (!Object.keys(changes).length) {
        setNotice('没有需要保存的修改。', 'warn');
        return;
      }
      state.saving = true;
      if (parts.save) parts.save.disabled = true;
      api.put('/api/v2/runtime/budget',
        { value: changes, expectedRevision: state.budget.revision })
        .then(function (value) {
          state.saving = false;
          state.budget = { revision: value.revision, base: value.base,
            effective: value.effective, notes: value.notes, applies: value.applies,
            derivation: state.budget.derivation };
          state.draft = {};
          state.appliedResult = { applied: value.applied, changes: changes,
            revision: value.revision };
          seedDraft();
          // The applied-result panel carries the confirmation inline; the header
          // status is reserved for the reader's current context.
          render();
          return load(true);
        }, function (error) {
          state.saving = false;
          if (parts.save) parts.save.disabled = false;
          if (error.code === 'revision_conflict') {
            setNotice('预算已被其他管理员修改，已重新读取最新值；你的输入没有提交。', 'warn');
            return load(true);
          }
          setNotice('保存失败：' + (error.message || error), 'error');
          return null;
        });
    }

    function renderApplied() {
      if (!parts.result) return;
      if (!state.appliedResult) {
        parts.result.hidden = true;
        parts.result.innerHTML = '';
        return;
      }
      var applied = state.appliedResult.applied || {};
      var changes = state.appliedResult.changes || {};
      parts.result.hidden = false;
      parts.result.innerHTML = '';
      parts.result.appendChild(el('strong', null,
        '预算已保存（修订 ' + state.appliedResult.revision + '）· 服务端确认的生效结果'));
      var list = el('ul', 'rt-diff-list');
      if (applied.parse) {
        var parse = applied.parse;
        list.appendChild(el('li', null, '解析池：' + (parse.error ? ('失败 ' + parse.error)
          : (parse.workers + ' 个进程（目标 ' + parse.wanted + '，启动 ' + (parse.spawned || 0) +
            '，停止 ' + (parse.stopped || 0) + '）' +
            (parse.busy && parse.busy.length ? '；有进程正忙，未能缩容到目标值' : '')))));
      }
      if (applied.query) {
        list.appendChild(el('li', null, '查询并发：' + (applied.query.error ? ('失败 ' + applied.query.error)
          : (applied.query.slots + ' 个槽位，当前运行 ' + applied.query.active))));
      }
      if (applied.upload) {
        list.appendChild(el('li', null, '上传上限：全局 ' + applied.upload.max_active_global +
          '，每身份 ' + applied.upload.max_active_per_principal));
      }
      (applied.deferred || []).forEach(function (item) {
        list.appendChild(el('li', null, item.field + ' 需重启后生效'));
      });
      Object.keys(changes).forEach(function (key) {
        if (APPLY_LABEL[(state.budget.applies || {})[key]] === '需重启') {
          list.appendChild(el('li', null, key + ' 已记录，重启服务后生效'));
        }
      });
      parts.result.appendChild(list);
    }

    /* -------------------------------------------------------------- diagnostics */
    function exportDiagnostics() {
      if (!state.admin) return;
      api.get('/api/v2/runtime/diagnostics').then(function (value) {
        var text = JSON.stringify(value, null, 1);
        var blob = new Blob([text], { type: 'application/json' });
        var url = URL.createObjectURL(blob);
        var anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = 'logbench-diagnostics.json';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        // Released after the click: a live object URL leaks the whole report.
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        setNotice('已导出脱敏诊断（不含路径、原文、搜索词与凭据）。', 'ok');
      }, function (error) {
        setNotice('导出失败：' + (error.message || error), 'error');
      });
    }

    /* -------------------------------------------------------------------- poll */
    function start() {
      state.visible = true;
      load(true);
      if (state.timer) clearInterval(state.timer);
      state.timer = setInterval(function () {
        if (state.visible && !state.loading && state.tab === 'jobs') load(false);
      }, POLL_MS);
    }

    function stop() {
      state.visible = false;
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }

    return {
      mount: mount,
      show: function () { showTab(state.tab); start(); },
      hide: stop,
      load: load,
      state: state,
      parts: parts
    };
  }

  return { create: create, STATE_LABEL: STATE_LABEL, BUDGET_FIELDS: BUDGET_FIELDS };
});
