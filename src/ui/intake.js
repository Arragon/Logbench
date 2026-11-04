/*
 * intake.js — 采集中心 (R6-068).
 *
 * Everything shown here comes from `/api/v2/intake`, `/api/v2/sources/{id}` and
 * `/api/v2/jobs`. There is no timer-driven progress, no random lag and no
 * "全部成功" badge: a source that is connected while its archive is behind shows
 * exactly that.
 *
 * Layout follows the confirmed workbench rules:
 *   - the page keeps one dense table as the primary surface;
 *   - the source detail is an overlay drawer, so opening it never moves the table
 *     or the reader behind it;
 *   - a member sees a summary row and no physical paths, endpoints or edit action.
 */
(function () {
  'use strict';

  var STATUS_LABEL = {
    disabled: '未启用',
    paused: '已暂停',
    gap: '有缺口',
    catching: '续采 / 追赶',
    following: '持续追踪',
    snapshot: '快照模式',
    error: '需要处理'
  };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === 'class') { node.className = attrs[key]; }
        else if (key === 'text') { node.textContent = attrs[key]; }
        else if (key === 'dataset') {
          Object.keys(attrs.dataset).forEach(function (name) { node.dataset[name] = attrs.dataset[name]; });
        } else if (key.slice(0, 2) === 'on') {
          node.addEventListener(key.slice(2).toLowerCase(), attrs[key]);
        } else if (attrs[key] !== null && attrs[key] !== undefined) {
          node.setAttribute(key, attrs[key]);
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) { return; }
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function bytes(value) {
    var size = Number(value || 0);
    if (!size) { return '0 B'; }
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var index = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)));
    var scaled = size / Math.pow(1024, index);
    return (scaled >= 100 || index === 0 ? scaled.toFixed(0) : scaled.toFixed(1)) + ' ' + units[index];
  }

  function createApi() {
    var state = { csrf: '', role: 'viewer', ready: null };
    function load() {
      if (state.ready) { return state.ready; }
      state.ready = fetch('/api/v1/me', { credentials: 'same-origin' })
        .then(function (response) { return response.json(); })
        .then(function (value) {
          state.csrf = value.csrfToken || '';
          state.role = (value.user && value.user.role) || 'viewer';
          return state;
        });
      return state.ready;
    }
    function request(method, path, body) {
      return load().then(function () {
        var options = { method: method, credentials: 'same-origin', headers: {} };
        if (method !== 'GET') {
          options.headers['Content-Type'] = 'application/json';
          options.headers['X-LogBench-CSRF'] = state.csrf;
        }
        if (body !== undefined) { options.body = JSON.stringify(body); }
        return fetch(path, options).then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (value) {
            if (!response.ok) {
              var error = (value && value.error) || {};
              var failure = new Error(error.message || ('请求失败（' + response.status + '）'));
              failure.code = error.code || 'http_' + response.status;
              failure.status = response.status;
              failure.details = error.details || {};
              throw failure;
            }
            return value;
          });
        });
      });
    }
    return { request: request, load: load, state: state };
  }

  function statusOf(item) {
    if (!item.enabled) { return 'disabled'; }
    if (item.paused) { return 'paused'; }
    if (item.lastError) { return 'error'; }
    if (item.gaps) { return 'gap'; }
    if (item.lagBytes > 0) { return 'catching'; }
    if (item.capabilities && item.capabilities.followCapable === false) { return 'snapshot'; }
    return 'following';
  }

  function badge(kind, text) {
    return el('span', { class: 'r6-badge is-' + kind, text: text });
  }

  function createIntake(options) {
    var host = options.root;
    var api = options.api || createApi();
    var state = {
      tab: 'sources',
      items: [],
      adapters: [],
      selected: null,
      detail: null,
      jobs: [],
      loading: false,
      error: null,
      query: '',
      filter: 'all',
      busy: {}
    };
    var nodes = {};

    /* --------------------------------------------------------------- shell */
    function build() {
      nodes.tabs = el('div', { class: 'r6-tabs', role: 'tablist' });
      nodes.table = el('tbody');
      nodes.empty = el('div', { class: 'r6-empty-state', hidden: 'hidden' });
      nodes.banner = el('div', { class: 'r6-banner', role: 'status', 'aria-live': 'polite' });
      nodes.drawer = el('aside', {
        class: 'r6-source-drawer', role: 'dialog', 'aria-modal': 'false',
        'aria-label': '日志源详情', hidden: 'hidden'
      });
      nodes.search = el('input', {
        type: 'search', id: 'intake-search', class: 'r6-input',
        placeholder: '搜索名称、目录或地址', 'aria-label': '搜索日志源'
      });
      nodes.search.addEventListener('input', function () {
        state.query = nodes.search.value.trim().toLowerCase();
        renderTable();
      });
      nodes.filter = el('select', { class: 'r6-input', 'aria-label': '按状态筛选' }, [
        el('option', { value: 'all', text: '全部状态' }),
        el('option', { value: 'following', text: '持续追踪' }),
        el('option', { value: 'catching', text: '续采 / 追赶' }),
        el('option', { value: 'paused', text: '已暂停' }),
        el('option', { value: 'disabled', text: '未启用' }),
        el('option', { value: 'gap', text: '有缺口' }),
        el('option', { value: 'snapshot', text: '快照模式' })
      ]);
      nodes.filter.addEventListener('change', function () {
        state.filter = nodes.filter.value;
        renderTable();
      });
      var head = el('div', { class: 'r6-intake-head' }, [
        el('div', { class: 'r6-intake-titles' }, [
          el('h2', { class: 'r6-h2', text: '采集中心' }),
          el('p', { class: 'r6-meta', text: '远程目录 → 原文归档 → 可复用索引' })
        ]),
        el('div', { class: 'r6-intake-actions' }, [
          el('button', { class: 'r6-button', type: 'button', text: '资源设置', onclick: function () { setTab('cache'); } }),
          el('button', {
            class: 'r6-button is-primary', type: 'button', text: '添加日志源',
            onclick: function () { openWizard(); }
          })
        ])
      ]);
      nodes.body = el('div', { class: 'r6-intake-body' });
      var page = el('section', { class: 'r6-intake' }, [head, nodes.tabs, nodes.banner, nodes.body]);
      host.appendChild(page);
      host.appendChild(nodes.drawer);
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !nodes.drawer.hidden) { closeDetail(); }
      });
    }

    function setTab(tab) {
      state.tab = tab;
      api.load().then(render);
    }

    function openWizard() {
      if (!window.LBSourceWizard) { return; }
      window.LBSourceWizard.open({
        api: api,
        adapters: state.adapters,
        onSaved: function (item) {
          showBanner('已保存“' + item.label + '”。未启用时不会启动采集。', 'info');
          refresh().then(function () { openDetail(item.id); });
        }
      });
    }

    /* ---------------------------------------------------------------- data */
    function refresh() {
      state.loading = true;
      state.error = null;
      renderBanner();
      return api.request('GET', '/api/v2/intake').then(function (value) {
        state.items = value.items || [];
        state.adapters = value.adapters || [];
        state.loading = false;
        render();
        if (state.selected) { return loadDetail(state.selected); }
      }).catch(function (error) {
        state.loading = false;
        state.error = error;
        render();
      });
    }

    function loadJobs() {
      return api.request('GET', '/api/v2/jobs?limit=50').then(function (value) {
        state.jobs = (value.items || []).filter(function (job) { return job.kind === 'collect'; });
        render();
      }).catch(function (error) {
        state.jobs = [];
        showBanner('运行记录读取失败：' + error.message, 'error');
      });
    }

    function loadDetail(id) {
      return api.request('GET', '/api/v2/sources/' + id).then(function (value) {
        state.detail = value.item;
        renderDrawer();
      }).catch(function (error) {
        showBanner('详情读取失败：' + error.message, 'error');
      });
    }

    /* --------------------------------------------------------------- render */
    function render() {
      renderTabs();
      if (state.tab === 'sources') { renderSources(); }
      else if (state.tab === 'storage') { renderStorage(); }
      else if (state.tab === 'cache') { renderCache(); }
      else { renderJobs(); }
      renderBanner();
    }

    function renderTabs() {
      var tabs = [
        ['sources', '远程日志源', state.items.length],
        ['storage', '存储位置', ''],
        ['cache', '解析缓存', ''],
        ['events', '运行记录', state.jobs.length || '']
      ];
      nodes.tabs.textContent = '';
      tabs.forEach(function (entry) {
        var button = el('button', {
          class: 'r6-tab' + (state.tab === entry[0] ? ' is-active' : ''),
          type: 'button', role: 'tab',
          'aria-selected': state.tab === entry[0] ? 'true' : 'false',
          onclick: function () {
            setTab(entry[0]);
            if (entry[0] === 'events') { loadJobs(); }
          }
        }, [entry[1]]);
        if (entry[2] !== '') {
          button.appendChild(el('span', { class: 'r6-tab-count', text: String(entry[2]) }));
        }
        nodes.tabs.appendChild(button);
      });
    }

    function filtered() {
      return state.items.filter(function (item) {
        if (state.filter !== 'all' && statusOf(item) !== state.filter) { return false; }
        if (!state.query) { return true; }
        var haystack = [item.label, item.adapter, item.endpoint || ''].join(' ').toLowerCase();
        return haystack.indexOf(state.query) >= 0;
      });
    }

    function renderSources() {
      nodes.body.textContent = '';
      var toolbar = el('div', { class: 'r6-intake-toolbar' }, [
        nodes.search, nodes.filter,
        el('span', { class: 'r6-meta', text: sourceSummary() }),
        el('button', {
          class: 'r6-button', type: 'button', text: '刷新',
          onclick: function () { refresh(); }
        })
      ]);
      var table = el('table', { class: 'r6-table' }, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: '日志源 / 连接' }),
          el('th', { text: '目录 → 存储位置' }),
          el('th', { text: '同步状态' }),
          el('th', { text: '归档 / 滞后' }),
          el('th', { text: '操作' })
        ])]),
        nodes.table
      ]);
      nodes.body.appendChild(toolbar);
      nodes.body.appendChild(el('div', { class: 'r6-table-wrap' }, [table]));
      nodes.body.appendChild(nodes.empty);
      renderTable();
      if (state.loading) {
        nodes.body.appendChild(el('p', { class: 'r6-meta', text: '正在读取采集状态…' }));
      }
    }

    function sourceSummary() {
      var following = state.items.filter(function (item) { return statusOf(item) === 'following'; }).length;
      var catching = state.items.filter(function (item) { return statusOf(item) === 'catching'; }).length;
      return state.items.length + ' 个日志源 · ' + following + ' 追踪中 · ' + catching + ' 续采中';
    }

    function renderTable() {
      if (!nodes.table) { return; }
      nodes.table.textContent = '';
      var rows = filtered();
      rows.forEach(function (item) {
        var status = statusOf(item);
        var coverage = el('div', { class: 'r6-cell-stack' }, [
          el('span', { text: '已归档 ' + bytes(item.archivedBytes) }),
          item.lagBytes > 0
            ? el('span', { class: 'r6-cell-warn', text: '待补 ' + bytes(item.lagBytes) })
            : el('span', { class: 'r6-meta', text: item.contiguousBytes ? '连续水位已对齐' : '尚未采集' })
        ]);
        var actions = el('div', { class: 'r6-row-actions' }, [
          el('button', {
            class: 'r6-button is-small', type: 'button', text: '查看',
            onclick: function () { openDetail(item.id); }
          }),
          item.paused
            ? el('button', {
              class: 'r6-button is-small', type: 'button', text: '继续',
              disabled: !item.enabled ? 'disabled' : null,
              onclick: function () { act(item, 'resume'); }
            })
            : el('button', {
              class: 'r6-button is-small', type: 'button', text: '暂停',
              disabled: !item.enabled ? 'disabled' : null,
              onclick: function () { act(item, 'pause'); }
            })
        ]);
        nodes.table.appendChild(el('tr', {}, [
          el('td', {}, [
            el('div', { class: 'r6-cell-stack' }, [
              el('span', { class: 'r6-cell-strong', text: item.label }),
              el('span', { class: 'r6-meta', text: (item.adapter || '') + (item.endpoint ? ' · ' + item.endpoint : '') })
            ])
          ]),
          el('td', {}, [el('span', { class: 'r6-meta', text: item.mappingCount + ' 个目录映射' })]),
          el('td', {}, [badge(status === 'following' ? 'good' : status === 'error' ? 'bad'
            : status === 'catching' ? 'warn' : 'quiet', STATUS_LABEL[status] || status)]),
          el('td', {}, [coverage]),
          el('td', {}, [actions])
        ]));
      });
      nodes.empty.hidden = rows.length ? 'hidden' : null;
      if (!rows.length) {
        nodes.empty.textContent = '';
        nodes.empty.appendChild(el('strong', { text: state.items.length ? '没有匹配的日志源' : '还没有远程日志源' }));
        nodes.empty.appendChild(el('p', {
          class: 'r6-meta',
          text: state.items.length
            ? '调整名称或状态条件；已有采集任务不受筛选影响。'
            : '添加一个 SFTP、SMB 或 HTTP 源后会先做能力预检；预检通过才允许启用。'
        }));
        if (state.items.length) {
          nodes.empty.appendChild(el('button', {
            class: 'r6-button', type: 'button', text: '清除筛选',
            onclick: function () {
              state.filter = 'all'; state.query = ''; nodes.filter.value = 'all';
              nodes.search.value = ''; renderTable();
            }
          }));
        } else {
          nodes.empty.appendChild(el('button', {
            class: 'r6-button is-primary', type: 'button', text: '添加日志源',
            onclick: openWizard
          }));
        }
      }
    }

    function renderStorage() {
      nodes.body.textContent = '';
      nodes.body.appendChild(el('div', { class: 'r6-note' }, [
        el('strong', { text: '存储位置由“存储位置管理”维护（R6-056）。' }),
        el('p', {
          class: 'r6-meta',
          text: '采集源的映射目标是存储位置 ID + 子目录，不是任意路径。这里不重复实现第二套根管理页面；'
            + '源详情中会显示实际目标路径。'
        })
      ]));
    }

    function renderCache() {
      nodes.body.textContent = '';
      nodes.body.appendChild(el('div', { class: 'r6-note' }, [
        el('strong', { text: '解析缓存与重建管理在 M8（R6-082）提供。' }),
        el('p', {
          class: 'r6-meta',
          text: '本页只显示采集侧的原文水位；结构与检索层的版本、覆盖率与可清理项不在 M6 范围内，'
            + '避免在没有真实数据时给出“全部已索引”的假状态。'
        })
      ]));
    }

    function renderJobs() {
      nodes.body.textContent = '';
      if (!state.jobs.length) {
        nodes.body.appendChild(el('div', { class: 'r6-empty-state' }, [
          el('strong', { text: '还没有采集任务记录' }),
          el('p', { class: 'r6-meta', text: '启用日志源或执行“立即扫描”后，任务会出现在这里。' })
        ]));
        return;
      }
      var table = el('table', { class: 'r6-table' }, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: '任务' }), el('th', { text: '状态' }),
          el('th', { text: '阶段' }), el('th', { text: '更新时间' })
        ])]),
        el('tbody', {}, state.jobs.map(function (job) {
          var phase = (job.progress && job.progress.phase) || job.state;
          return el('tr', {}, [
            el('td', { class: 'r6-mono', text: job.id }),
            el('td', {}, [badge(job.state === 'indexed' ? 'good' : job.state === 'failed' ? 'bad' : 'quiet',
              job.state)]),
            el('td', { text: String(phase) }),
            el('td', { class: 'r6-mono', text: job.updated_at || '' })
          ]);
        }))
      ]);
      nodes.body.appendChild(el('div', { class: 'r6-table-wrap' }, [table]));
    }

    function renderBanner() {
      if (!nodes.banner) { return; }
      nodes.banner.textContent = '';
      if (state.error) {
        nodes.banner.className = 'r6-banner is-error';
        nodes.banner.appendChild(el('span', { text: '采集状态读取失败：' + state.error.message }));
        nodes.banner.appendChild(el('button', {
          class: 'r6-button is-small', type: 'button', text: '重试',
          onclick: function () { refresh(); }
        }));
        return;
      }
      if (!nodes.banner.dataset.message) { nodes.banner.hidden = 'hidden'; return; }
      nodes.banner.hidden = null;
      nodes.banner.className = 'r6-banner is-' + (nodes.banner.dataset.kind || 'info');
      nodes.banner.appendChild(el('span', { text: nodes.banner.dataset.message }));
    }

    function showBanner(message, kind) {
      nodes.banner.dataset.message = message;
      nodes.banner.dataset.kind = kind || 'info';
      renderBanner();
    }

    /* -------------------------------------------------------------- actions */
    function act(item, action) {
      var key = item.id + ':' + action;
      if (state.busy[key]) { return; }
      state.busy[key] = true;
      showBanner('正在提交“' + action + '”…', 'info');
      var actionId = [item.id, action, Date.now()].join(':');
      api.request('POST', '/api/v2/sources/' + item.id + '/actions',
        { action: action, actionId: actionId }).then(function () {
        state.busy[key] = false;
        showBanner('“' + action + '”已提交；任务状态见运行记录。', 'info');
        refresh();
      }).catch(function (error) {
        state.busy[key] = false;
        showBanner('“' + action + '”未生效：' + error.message, 'error');
        if (error.code === 'probe_required' || error.code === 'probe_stale') {
          openDetail(item.id);
        }
      });
    }

    /* --------------------------------------------------------------- drawer */
    function openDetail(id) {
      state.selected = id;
      nodes.drawer.hidden = null;
      nodes.drawer.textContent = '';
      nodes.drawer.appendChild(el('p', { class: 'r6-meta', text: '正在读取源详情…' }));
      loadDetail(id);
    }

    function closeDetail() {
      state.selected = null;
      state.detail = null;
      nodes.drawer.hidden = 'hidden';
    }

    function renderDrawer() {
      var item = state.detail;
      if (!item) { return; }
      nodes.drawer.textContent = '';
      var caps = item.capabilities || {};
      var probe = item.probe;
      nodes.drawer.appendChild(el('div', { class: 'r6-drawer-head' }, [
        el('span', { class: 'r6-cell-strong', text: item.label }),
        el('button', {
          class: 'r6-button is-small', type: 'button', text: '关闭',
          onclick: closeDetail
        })
      ]));
      var body = el('div', { class: 'r6-drawer-body' });
      nodes.drawer.appendChild(body);

      body.appendChild(section('当前采集链路', [
        el('ul', { class: 'r6-facts' }, [
          fact('文件登记', (item.files ? item.files.files : 0) + ' 个 · 已归档 ' +
            bytes(item.files ? item.files.archivedBytes : 0)),
          fact('滞后', bytes(item.files ? Math.max(0, item.files.bytes - item.files.contiguousBytes) : 0)),
          fact('缺口', (item.files ? item.files.gaps : 0) + ' 个（缺口不会被补成“成功”）'),
          fact('最后联系', (item.files && item.files.lastSeenAt) || '尚未采集')
        ]),
        el('p', {
          class: 'r6-meta',
          text: item.enabled
            ? '原文归档与索引分开记录进度；连接正常不代表索引完成。'
            : '源未启用：不会排队采集。预检通过后才允许启用。'
        })
      ]));

      var mappingRows = Array.isArray(item.mappings) ? item.mappings : [];
      body.appendChild(section('目录映射', mappingRows.length ? mappingRows.map(function (mapping) {
        return el('div', { class: 'r6-mapping-row' }, [
          el('span', { class: 'r6-mono', text: mapping.remoteDir }),
          el('span', { class: 'r6-meta', text: '→' }),
          el('span', { class: 'r6-mono', text: mapping.rootId + '/' + mapping.targetSubdir }),
          badge(mapping.mode === 'follow' ? 'warn' : 'quiet',
            mapping.mode === 'follow' ? '持续' : mapping.mode === 'manual' ? '手动' : '定时')
        ]);
      }) : [el('p', { class: 'r6-meta', text: '尚未配置目录映射。' })]));

      body.appendChild(section('连接能力', [
        probe
          ? el('div', { class: 'r6-badges' }, [
            badge(caps.directoryDiscovery === 'supported' ? 'good' : 'quiet',
              caps.directoryDiscovery === 'supported' ? '目录发现' : '目录清单需配置'),
            badge(caps.rangeRead === 'supported' ? 'good' : 'warn',
              caps.rangeRead === 'supported' ? '范围读取' : '无范围续传'),
            badge(caps.followCapable ? 'good' : 'warn',
              caps.followCapable ? '按约定可追踪' : '仅稳定快照'),
            badge('quiet', '标识：' + (caps.stableIdentity || '未知'))
          ])
          : el('div', { class: 'r6-badges' }, [badge('quiet', '尚未预检 · 能力未知')]),
        el('p', {
          class: 'r6-meta',
          text: probe
            ? '最近预检：' + (probe.createdAt || '') + ' · 配置修订 ' + probe.revision +
              (probe.matches ? '' : ' · 配置已修改，预检失效') + (probe.expired ? ' · 已过期' : '')
            : '尚未预检。'
        })
      ]));

      if (probe && probe.result && probe.result.checks) {
        body.appendChild(section('预检项目', probe.result.checks.map(function (check) {
          return el('div', { class: 'r6-check-row' }, [
            badge(check.state === 'pass' ? 'good' : check.state === 'fail' ? 'bad' : 'quiet', check.id),
            el('span', { text: check.detail })
          ]);
        })));
      }

      body.appendChild(section('归档与访问', [
        el('ul', { class: 'r6-facts' }, [
          fact('源文件删除', '保留本地归档，不传播删除'),
          fact('文件轮转', '按代次归档，旧代次继续补齐'),
          fact('访问范围', item.acl === 'members' ? '管理员与成员可见' : '仅管理员'),
          fact('采集身份', (item.credentialRef || '无凭据') + ' · 仅引用，不显示密钥')
        ])
      ]));

      body.appendChild(el('div', { class: 'r6-drawer-actions' }, [
        el('button', {
          class: 'r6-button is-primary', type: 'button', text: '运行预检',
          onclick: function () { runProbe(item); }
        }),
        el('button', {
          class: 'r6-button', type: 'button', text: item.paused ? '继续' : '暂停',
          disabled: !item.enabled ? 'disabled' : null,
          onclick: function () { act(item, item.paused ? 'resume' : 'pause'); }
        }),
        el('button', {
          class: 'r6-button', type: 'button', text: '立即扫描',
          disabled: !item.enabled ? 'disabled' : null,
          onclick: function () { act(item, 'scan'); }
        })
      ]));
    }

    function runProbe(item) {
      showBanner('正在执行预检（会真实连接远端）…', 'info');
      api.request('POST', '/api/v2/sources/' + item.id + '/probe', {}).then(function (value) {
        var result = value.probe || {};
        showBanner(result.ok ? '预检通过。' : '预检未通过：请按项目逐条修正。',
          result.ok ? 'info' : 'error');
        loadDetail(item.id);
        refresh();
      }).catch(function (error) {
        showBanner('预检失败：' + error.message, 'error');
      });
    }

    function section(title, children) {
      return el('section', { class: 'r6-drawer-section' }, [
        el('h3', { class: 'r6-h3', text: title })
      ].concat(children || []));
    }

    function fact(label, value) {
      return el('li', {}, [
        el('span', { class: 'r6-meta', text: label + '：' }),
        el('span', { text: String(value) })
      ]);
    }

    /* --------------------------------------------------------------- public */
    build();
    refresh();
    return {
      refresh: refresh,
      setTab: setTab,
      show: function () { setTab('sources'); },
      state: state
    };
  }

  window.LBIntake = { create: createIntake, _status: statusOf };
})();
