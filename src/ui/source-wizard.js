/*
 * source-wizard.js — 连接 → 目录映射 → 采集策略 → 确认与预检 (R6-067).
 *
 * Rules this wizard obeys:
 *   - the options offered are the ones the adapter actually supports; a protocol
 *     without range reads never shows "持续追踪", and a plain HTTP URL without a
 *     manifest cannot claim an append contract;
 *   - the probe runs against the *saved* configuration revision, so what the review
 *     step reports is what enabling will use;
 *   - the draft survives stepping back, and a 409 keeps it instead of resetting;
 *   - no password is kept in the browser: the connection step selects a
 *     credential reference that an administrator created server-side.
 */
(function () {
  'use strict';

  var PROTOCOLS = [
    { id: 'sftp', label: 'SSH / SFTP', port: 22, follow: 'declared' },
    { id: 'smb', label: 'SMB 2/3', port: 445, follow: 'declared' },
    { id: 'http', label: 'HTTP(S)', port: 443, follow: 'manifest' },
    { id: 'scp', label: '传统 SCP（快照）', port: 22, follow: false }
  ];
  var STEPS = ['连接', '目录映射', '采集策略', '确认与预检'];

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === 'class') { node.className = attrs[key]; }
        else if (key === 'text') { node.textContent = attrs[key]; }
        else if (key.slice(0, 2) === 'on') { node.addEventListener(key.slice(2).toLowerCase(), attrs[key]); }
        else if (attrs[key] !== null && attrs[key] !== undefined && attrs[key] !== false) {
          node.setAttribute(key, attrs[key] === true ? '' : attrs[key]);
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) { return; }
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function protocol(id) {
    for (var index = 0; index < PROTOCOLS.length; index += 1) {
      if (PROTOCOLS[index].id === id) { return PROTOCOLS[index]; }
    }
    return PROTOCOLS[0];
  }

  function field(label, control, hint) {
    var id = control.getAttribute('id');
    return el('div', { class: 'r6-field' }, [
      el('label', { for: id, text: label }),
      control,
      hint ? el('p', { class: 'r6-hint', text: hint }) : null
    ]);
  }

  function input(id, value, attrs) {
    var node = el('input', Object.assign({ id: id, class: 'r6-input', value: value || '' }, attrs || {}));
    return node;
  }

  function select(id, options, value) {
    return el('select', { id: id, class: 'r6-input' }, options.map(function (option) {
      return el('option', {
        value: option.value,
        text: option.label,
        selected: String(option.value) === String(value) ? 'selected' : null,
        disabled: option.disabled ? 'disabled' : null
      });
    }));
  }

  var draft = null;
  var state = { step: 0, api: null, adapters: [], roots: [], credentials: [], error: null,
                probe: null, saved: null, busy: false, options: null };

  function freshDraft(adapters) {
    return {
      protocol: 'sftp', name: '', endpoint: '', port: 22, user: '', credentialRef: '',
      knownHosts: '', share: '', caFile: '',
      mappings: [{ remoteDir: '/var/log/', rootId: 'legacy', targetSubdir: '', include: '',
                   exclude: '', recursive: true, mode: 'scheduled' }],
      mode: 'scheduled', poll: 2, scan: 60, acl: 'admin', appendDeclared: false, allowLoopback: false,
      ruleRevision: '', draftVersion: 1
    };
  }

  function restore(adapters) {
    try {
      var raw = sessionStorage.getItem('lb.r6.wizard');
      if (!raw) { return freshDraft(adapters); }
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.protocol) { return freshDraft(adapters); }
      return parsed;
    } catch (error) {
      return freshDraft(adapters);
    }
  }

  function persist() {
    try { sessionStorage.setItem('lb.r6.wizard', JSON.stringify(draft)); } catch (error) { /* quota */ }
  }

  /* ------------------------------------------------------------- validation */
  function followAllowed() {
    var meta = protocol(draft.protocol);
    if (meta.follow === false) { return { ok: false, reason: '传统 SCP 只能整文件快照，不提供持续追踪。' }; }
    if (meta.follow === 'manifest') {
      if (!draft.appendDeclared) {
        return { ok: false, reason: 'HTTP 持续追踪需要源端提供代次/不可变前缀约定；未确认前只开放稳定快照。' };
      }
    }
    if (meta.follow === 'declared' && !draft.appendDeclared) {
      return { ok: false, reason: '需要确认“已写前缀不会被改写”；未确认前只能定时快照。' };
    }
    return { ok: true };
  }

  function validateStep(step) {
    if (step === 0) {
      if (!draft.name.trim()) { return { field: 'wizard-name', message: '请输入日志源名称。' }; }
      if (!draft.endpoint.trim()) { return { field: 'wizard-endpoint', message: '请输入服务器或地址。' }; }
      if (!/^\d+$/.test(String(draft.port)) || draft.port < 1 || draft.port > 65535) {
        return { field: 'wizard-port', message: '端口须为 1–65535 的整数。' };
      }
      if (/:\/\/[^/]*@/.test(draft.endpoint)) {
        return { field: 'wizard-endpoint', message: '地址中不能包含用户名或口令，请使用凭据引用。' };
      }
    }
    if (step === 1) {
      var seen = {};
      for (var index = 0; index < draft.mappings.length; index += 1) {
        var mapping = draft.mappings[index];
        if (!mapping.remoteDir || mapping.remoteDir.charAt(0) !== '/') {
          return { field: 'mapping-remote-' + index, message: '远端目录必须是绝对路径。' };
        }
        if (/(^|\/)\.\.(\/|$)/.test(mapping.remoteDir)) {
          return { field: 'mapping-remote-' + index, message: '远端目录不能包含上级目录。' };
        }
        if (!mapping.targetSubdir) {
          return { field: 'mapping-target-' + index, message: '请填写存储子目录。' };
        }
        var key = mapping.rootId + '/' + mapping.targetSubdir;
        if (seen[key]) {
          return { field: 'mapping-target-' + index, message: '目标子目录重复；两个映射不能写入同一位置。' };
        }
        Object.keys(seen).forEach(function (existing) {
          if (existing === key) { return; }
          if (existing.indexOf(key + '/') === 0 || key.indexOf(existing + '/') === 0) {
            seen.__conflict = true;
          }
        });
        if (seen.__conflict) {
          return { field: 'mapping-target-' + index, message: '目标子目录与另一个映射重叠。' };
        }
        seen[key] = true;
      }
    }
    if (step === 2) {
      if (draft.mode === 'follow' && !followAllowed().ok) {
        return { field: 'wizard-mode-follow', message: followAllowed().reason };
      }
      if (!/^\d+$/.test(String(draft.poll)) || draft.poll < 1) {
        return { field: 'wizard-poll', message: '活动检查间隔至少 1 秒。' };
      }
      if (!/^\d+$/.test(String(draft.scan)) || draft.scan < 5) {
        return { field: 'wizard-scan', message: '目录发现间隔至少 5 秒。' };
      }
    }
    return null;
  }

  /* ------------------------------------------------------------------- view */
  function render() {
    var dialog = document.getElementById('wizard-dialog');
    if (!dialog) { return; }
    dialog.textContent = '';
    var body = el('div', { class: 'r6-wizard-body' });
    dialog.appendChild(el('div', { class: 'r6-wizard-head' }, [
      el('strong', { text: '添加日志源' }),
      el('button', { class: 'r6-button is-small', type: 'button', text: '关闭',
                     onclick: close })
    ]));
    var steps = el('ol', { class: 'r6-steps' });
    STEPS.forEach(function (title, index) {
      steps.appendChild(el('li', {
        class: 'r6-step' + (index === state.step ? ' is-active' : index < state.step ? ' is-done' : ''),
        'aria-current': index === state.step ? 'step' : null
      }, [el('span', { class: 'r6-step-no', text: String(index + 1) }), title]));
    });
    dialog.appendChild(steps);
    if (state.error) {
      dialog.appendChild(el('div', { class: 'r6-banner is-error', role: 'alert' }, [
        el('span', { text: state.error.message }),
        state.error.code === 'revision_conflict'
          ? el('button', { class: 'r6-button is-small', type: 'button', text: '重新读取',
                           onclick: function () { close(); } }) : null
      ]));
    }
    if (state.step === 0) { body.appendChild(stepConnection()); }
    if (state.step === 1) { body.appendChild(stepMappings()); }
    if (state.step === 2) { body.appendChild(stepPolicy()); }
    if (state.step === 3) { body.appendChild(stepReview()); }
    dialog.appendChild(body);
    dialog.appendChild(el('div', { class: 'r6-wizard-foot' }, [
      el('button', {
        class: 'r6-button', type: 'button', text: '上一步',
        disabled: state.step === 0 ? 'disabled' : null,
        onclick: function () { go(state.step - 1); }
      }),
      el('span', { class: 'r6-meta', text: '第 ' + (state.step + 1) + ' / ' + STEPS.length + ' 步' }),
      state.step < STEPS.length - 1
        ? el('button', { class: 'r6-button is-primary', type: 'button', text: '下一步',
                         onclick: function () { go(state.step + 1); } })
        : el('button', { class: 'r6-button is-primary', type: 'button', text: '保存停用',
                         disabled: state.busy ? 'disabled' : null,
                         onclick: function () { save(false); } })
    ]));
    var first = dialog.querySelector('input, select, button');
    if (first && state.step !== 3) { first.focus(); }
  }

  function go(step) {
    if (step > state.step) {
      var problem = validateStep(state.step);
      if (problem) {
        state.error = problem;
        render();
        var target = document.getElementById(problem.field);
        if (target) { target.focus(); }
        return;
      }
    }
    state.error = null;
    state.step = Math.max(0, Math.min(STEPS.length - 1, step));
    persist();
    render();
  }

  /* -------------------------------------------------------------- step one */
  function stepConnection() {
    var meta = protocol(draft.protocol);
    var adapters = {};
    (state.adapters || []).forEach(function (entry) { adapters[entry.adapter] = entry; });
    var chips = el('div', { class: 'r6-protocols', role: 'group', 'aria-label': '传输协议' });
    PROTOCOLS.forEach(function (item) {
      var stateInfo = adapters[item.id];
      var unavailable = stateInfo && stateInfo.available === false;
      chips.appendChild(el('button', {
        class: 'r6-chip' + (draft.protocol === item.id ? ' is-active' : '') + (unavailable ? ' is-disabled' : ''),
        type: 'button',
        'aria-pressed': draft.protocol === item.id ? 'true' : 'false',
        disabled: unavailable ? 'disabled' : null,
        title: unavailable ? '缺少运行组件：' + (stateInfo.missingTools || []).join(', ') : null,
        onclick: function () {
          draft.protocol = item.id;
          draft.port = item.port;
          draft.mode = 'scheduled';
          persist();
          render();
        }
      }, [item.label + (unavailable ? '（不可用）' : '')]));
    });
    var endpointLabel = draft.protocol === 'http' ? 'HTTP 基础地址'
      : draft.protocol === 'smb' ? '服务器 / 共享名称' : '服务器地址';
    var placeholder = draft.protocol === 'http' ? 'https://host.example/logs/'
      : draft.protocol === 'smb' ? '//host.example/logs' : 'host.example';
    var credentialOptions = [{ value: '', label: state.credentials.length ? '不使用凭据' : '尚无可用凭据' }]
      .concat(state.credentials.map(function (item) {
        return { value: item.ref, label: item.label + ' · ' + item.kind };
      }));
    var children = [
      chips,
      field('日志源名称', input('wizard-name', draft.name, {
        placeholder: '例如：机台 A · 主控日志',
        oninput: function (event) { draft.name = event.target.value; persist(); }
      })),
      field(endpointLabel, input('wizard-endpoint', draft.endpoint, {
        placeholder: placeholder, spellcheck: 'false',
        oninput: function (event) { draft.endpoint = event.target.value; persist(); }
      }), '只填主机与目录，不把账号口令写进地址。'),
      field('端口', input('wizard-port', String(draft.port), {
        type: 'number', min: '1', max: '65535',
        oninput: function (event) { draft.port = Number(event.target.value); persist(); }
      }))
    ];
    if (draft.protocol === 'sftp' || draft.protocol === 'scp') {
      children.push(field('用户名', input('wizard-user', draft.user, {
        autocomplete: 'off',
        oninput: function (event) { draft.user = event.target.value; persist(); }
      })));
      children.push(field('known_hosts 文件（服务端路径）', input('wizard-known', draft.knownHosts, {
        placeholder: '/etc/logbench/known_hosts',
        oninput: function (event) { draft.knownHosts = event.target.value; persist(); }
      }), draft.protocol === 'scp'
        ? '传统 SCP 必须提供 known_hosts；没有它预检会失败，不会退回不校验主机。'
        : '留空则预检会明确失败：SSH 主机身份必须来自可信渠道。'));
    }
    if (draft.protocol === 'smb') {
      children.push(field('共享名称', input('wizard-share', draft.share, {
        placeholder: 'logs',
        oninput: function (event) { draft.share = event.target.value; persist(); }
      })));
    }
    children.push(field('凭据引用', select('wizard-credential', credentialOptions, draft.credentialRef),
      '生产版由管理员在服务端创建；浏览器只保存引用，不保存密码或私钥。'));
    if (draft.protocol === 'http') {
      children.push(field('组织 CA 文件（可选）', input('wizard-ca', draft.caFile, {
        placeholder: '/etc/ssl/certs/org-ca.pem',
        oninput: function (event) { draft.caFile = event.target.value; persist(); }
      }), '证书始终校验；私有 CA 通过该文件加入信任，不跳过验证。'));
    }
    children.push(el('label', { class: 'r6-check' }, [
      el('input', {
        type: 'checkbox', id: 'wizard-loopback',
        checked: draft.allowLoopback ? 'checked' : null,
        onchange: function (event) { draft.allowLoopback = event.target.checked; persist(); }
      }),
      '允许连接环回 / 本机地址（仅限受控测试；默认拒绝）'
    ]));
    var note = adapters[draft.protocol];
    children.push(el('p', { class: 'r6-hint', text: note && note.available === false
      ? '该协议在当前环境不可用：缺少 ' + (note.missingTools || []).join(', ') + '。可以保存配置，但预检会失败。'
      : (note && note.notes && note.notes[0]) || '' }));
    return el('div', { class: 'r6-form-grid' }, children);
  }

  /* -------------------------------------------------------------- step two */
  function stepMappings() {
    var host = el('div', { class: 'r6-mappings' });
    draft.mappings.forEach(function (mapping, index) {
      var rootOptions = state.roots.length ? state.roots.map(function (root) {
        return { value: root.id, label: root.label + (root.state === 'offline' ? ' · 离线' : '') };
      }) : [{ value: 'legacy', label: '部署目录 data/（legacy）' }];
      host.appendChild(el('fieldset', { class: 'r6-mapping' }, [
        el('legend', { text: '目录映射 ' + (index + 1) }),
        el('div', { class: 'r6-mapping-grid' }, [
          field('远程目录', input('mapping-remote-' + index, mapping.remoteDir, {
            oninput: function (event) { mapping.remoteDir = event.target.value; persist(); }
          })),
          field('存储位置', select('mapping-root-' + index, rootOptions, mapping.rootId)),
          field('存储子目录', input('mapping-target-' + index, mapping.targetSubdir, {
            placeholder: 'psg-a/debug',
            oninput: function (event) { mapping.targetSubdir = event.target.value; persist(); }
          })),
          field('包含模式', input('mapping-include-' + index, mapping.include, {
            placeholder: '*.log, *.trace',
            oninput: function (event) { mapping.include = event.target.value; persist(); }
          }), '逗号分隔；留空表示全部文件。'),
          field('排除模式', input('mapping-exclude-' + index, mapping.exclude, {
            placeholder: '*.gz, archive*',
            oninput: function (event) { mapping.exclude = event.target.value; persist(); }
          }))
        ]),
        el('p', { class: 'r6-hint' }, [
          '最终路径：',
          el('span', { class: 'r6-mono', text: previewPath(mapping) }),
          '（后端仍会复核根、重叠与权限）'
        ]),
        el('div', { class: 'r6-mapping-actions' }, [
          el('label', { class: 'r6-check' }, [
            el('input', {
              type: 'checkbox', checked: mapping.recursive ? 'checked' : null,
              onchange: function (event) { mapping.recursive = event.target.checked; persist(); }
            }),
            '递归子目录'
          ]),
          el('button', {
            class: 'r6-button is-small', type: 'button', text: '移除',
            disabled: draft.mappings.length === 1 ? 'disabled' : null,
            onclick: function () {
              draft.mappings.splice(index, 1); persist(); render();
            }
          })
        ])
      ]));
      var rootSelect = host.querySelector('#mapping-root-' + index);
      if (rootSelect) {
        rootSelect.addEventListener('change', function (event) {
          mapping.rootId = event.target.value; persist(); render();
        });
      }
    });
    host.appendChild(el('button', {
      class: 'r6-button', type: 'button', text: '增加一个远程目录',
      disabled: draft.mappings.length >= 8 ? 'disabled' : null,
      onclick: function () {
        draft.mappings.push({ remoteDir: '/var/log/', rootId: 'legacy', targetSubdir: '',
                              include: '', exclude: '', recursive: true, mode: 'scheduled' });
        persist(); render();
      }
    }));
    return host;
  }

  function previewPath(mapping) {
    var root = null;
    state.roots.forEach(function (item) { if (item.id === mapping.rootId) { root = item; } });
    var base = root && root.path ? root.path.replace(/\/$/, '') : 'data';
    return base + '/' + String(mapping.targetSubdir || '…').replace(/^\/+/, '');
  }

  /* ------------------------------------------------------------ step three */
  function stepPolicy() {
    var follow = followAllowed();
    var modes = [
      { id: 'manual', label: '手动 / 按需扫描', hint: '只在你点击“立即扫描”时读取远端。' },
      { id: 'scheduled', label: '定时同步', hint: '按目录发现间隔扫描元数据，再读取新增范围。' },
      { id: 'follow', label: '持续追踪', hint: '活动文件按较短间隔检查；需要源端追加约定。',
        disabled: !follow.ok, reason: follow.reason }
    ];
    var radios = el('div', { class: 'r6-modes', role: 'radiogroup', 'aria-label': '同步方式' });
    modes.forEach(function (mode) {
      radios.appendChild(el('label', {
        class: 'r6-mode' + (draft.mode === mode.id ? ' is-active' : '') + (mode.disabled ? ' is-disabled' : '')
      }, [
        el('input', {
          type: 'radio', name: 'wizard-mode', id: 'wizard-mode-' + mode.id,
          checked: draft.mode === mode.id ? 'checked' : null,
          disabled: mode.disabled ? 'disabled' : null,
          onchange: function () { draft.mode = mode.id; persist(); render(); }
        }),
        el('span', { class: 'r6-cell-strong', text: mode.label }),
        el('span', { class: 'r6-hint', text: mode.disabled ? mode.reason : mode.hint })
      ]));
    });
    var appendToggle = el('label', { class: 'r6-check' }, [
      el('input', {
        type: 'checkbox', checked: draft.appendDeclared ? 'checked' : null,
        onchange: function (event) {
          draft.appendDeclared = event.target.checked;
          if (!event.target.checked && draft.mode === 'follow') { draft.mode = 'scheduled'; }
          persist(); render();
        }
      }),
      '源端保证已写前缀不会被改写（追加/轮转保留约定）'
    ]);
    return el('div', { class: 'r6-form-grid' }, [
      el('h3', { class: 'r6-h3', text: '同步方式' }),
      radios,
      appendToggle,
      field('活动文件检查间隔（秒）', input('wizard-poll', String(draft.poll), {
        type: 'number', min: '1', max: '3600',
        oninput: function (event) { draft.poll = Number(event.target.value); persist(); }
      }), '这是目标检查周期；可见延迟还包含远端刷盘、传输与索引排队。'),
      field('目录发现间隔（秒）', input('wizard-scan', String(draft.scan), {
        type: 'number', min: '5', max: '86400',
        oninput: function (event) { draft.scan = Number(event.target.value); persist(); }
      }), '只扫描元数据；不会每次重读整份日志。'),
      field('访问范围', select('wizard-acl', [
        { value: 'admin', label: '仅管理员' },
        { value: 'members', label: '管理员与成员（摘要）' }
      ], draft.acl), '新远程日志默认不向未登记访客开放。'),
      el('div', { class: 'r6-note' }, [
        el('strong', { text: '可靠性策略（固定行为，不可关闭）' }),
        el('ul', { class: 'r6-facts' }, [
          el('li', { text: '断线后从已确认检查点继续，并重新验证文件代次。' }),
          el('li', { text: '保留轮转与截断前的原文，不传播远端删除。' }),
          el('li', { text: '存储离线、权限失败或空间不足时暂停并明确告警。' }),
          el('li', { text: '断线期间源端已删除或覆盖的内容无法补回；系统标记缺口，不宣称零丢失。' })
        ])
      ])
    ]);
  }

  /* ------------------------------------------------------------- step four */
  function stepReview() {
    var meta = protocol(draft.protocol);
    var probe = state.probe;
    var children = [
      reviewRow('协议', meta.label),
      reviewRow('服务器', /^[a-z]+:\/\//.test(draft.endpoint) && draft.endpoint.indexOf(':', 8) > -1
        ? draft.endpoint : draft.endpoint + ':' + draft.port),
      reviewRow('访问范围', draft.acl === 'members' ? '管理员与成员' : '仅管理员'),
      reviewRow('同步方式', { manual: '手动扫描', scheduled: '定时同步', follow: '持续追踪' }[draft.mode]),
      reviewRow('目录映射', draft.mappings.length + ' 个'),
      reviewRow('追加约定', draft.appendDeclared ? '源端已确认前缀不变' : '未确认（仅稳定快照）'),
      el('div', { class: 'r6-wizard-probe' }, [
        el('button', {
          class: 'r6-button', type: 'button', text: probe ? '重新预检' : '运行预检',
          disabled: state.busy ? 'disabled' : null,
          onclick: function () { save(true); }
        }),
        el('span', {
          class: 'r6-meta',
          text: '预检会真实连接远端并绑定当前配置修订；保存后修改任何字段都会使结果失效。'
        })
      ])
    ];
    if (probe) {
      children.push(el('div', { class: 'r6-probe-result' }, [
        el('div', { class: 'r6-badges' }, [
          el('span', { class: 'r6-badge is-' + (probe.ok ? 'good' : 'bad'),
                       text: probe.ok ? '预检通过' : '预检未通过' }),
          el('span', { class: 'r6-badge is-quiet', text: '配置修订 ' + probe.configRevision }),
          el('span', { class: 'r6-badge is-quiet', text: '指纹 ' + String(probe.fingerprint).slice(0, 10) })
        ]),
        el('div', { class: 'r6-probe-checks' }, (probe.checks || []).map(function (check) {
          return el('div', { class: 'r6-check-row' }, [
            el('span', { class: 'r6-badge is-' + (check.state === 'pass' ? 'good'
              : check.state === 'fail' ? 'bad' : 'quiet'), text: check.id }),
            el('span', { text: check.detail })
          ]);
        }))
      ].concat((probe.warnings || []).map(function (warning) {
        // Warnings go in as siblings, not as a nested array: appendChild only
        // accepts a Node, and an array here used to blank the whole step.
        return el('p', { class: 'r6-hint', text: '注意：' + warning });
      }))));
      if (probe.ok && state.saved) {
        children.push(el('button', {
          class: 'r6-button is-primary', type: 'button', text: '预检后启用',
          disabled: state.busy ? 'disabled' : null,
          onclick: function () { enable(); }
        }));
      } else if (!probe.ok) {
        children.push(el('p', { class: 'r6-hint',
          text: '逐条修正上面的失败项后重新预检；系统不会把失败降级成匿名连接或静默跳过。' }));
      }
    } else {
      children.push(el('p', { class: 'r6-meta',
        text: '“运行预检”会先保存为停用状态并读取真实能力；未预检的配置不能启用。' }));
    }
    return el('div', { class: 'r6-review' }, children);
  }

  function reviewRow(label, value) {
    return el('div', { class: 'r6-review-row' }, [
      el('span', { class: 'r6-meta', text: label }),
      el('span', { text: String(value) })
    ]);
  }

  /* ------------------------------------------------------------------ save */
  function payload() {
    var policy = {};
    if (draft.protocol === 'sftp' || draft.protocol === 'scp') {
      policy.user = draft.user;
      if (draft.knownHosts) { policy.knownHostsFile = draft.knownHosts; }
    }
    if (draft.protocol === 'smb') { policy.user = draft.user; policy.share = draft.share; }
    if (draft.protocol === 'http' && draft.caFile) { policy.caFile = draft.caFile; }
    if (draft.appendDeclared) { policy.appendContract = 'declared'; }
    policy.allowedHosts = [String(draft.endpoint).replace(/^[a-z]+:\/\//, '').split('/')[0]
      .split(':')[0].replace(/^\/\//, '')].filter(Boolean);
    if (draft.allowLoopback) { policy.allowLoopback = true; }
    policy.readTimeout = 30;
    if (draft.poll) { policy.activeInterval = draft.poll; }
    if (draft.scan) { policy.discoveryInterval = draft.scan; }
    return {
      adapter: draft.protocol,
      label: draft.name.trim(),
      endpoint: normalizeEndpoint(draft.protocol, draft.endpoint.trim()),
      credentialRef: draft.credentialRef || null,
      acl: draft.acl,
      policy: policy,
      mappings: draft.mappings.map(function (mapping) {
        return {
          remoteDir: mapping.remoteDir,
          rootId: mapping.rootId,
          targetSubdir: mapping.targetSubdir,
          include: split(mapping.include),
          exclude: split(mapping.exclude),
          recursive: !!mapping.recursive,
          mode: draft.mode === 'manual' ? 'manual' : draft.mode === 'follow' ? 'follow' : 'scheduled'
        };
      })
    };
  }

  function split(value) {
    return String(value || '').split(',').map(function (item) { return item.trim(); })
      .filter(Boolean);
  }

  function normalizeEndpoint(kind, value) {
    if (/^[a-z]+:\/\//.test(value)) { return value; }
    if (kind === 'http') { return 'https://' + value; }
    if (kind === 'smb') { return (value.indexOf('//') === 0 ? 'smb:' + value : 'smb://' + value); }
    return kind + '://' + value;
  }

  function save(withProbe) {
    if (state.busy) { return; }
    var problem = validateStep(0) || validateStep(1) || validateStep(2);
    if (problem) {
      state.error = problem;
      state.step = Number(problem.field.indexOf('mapping') === 0 ? 1 : problem.field.indexOf('wizard-mode') === 0
        || problem.field.indexOf('wizard-poll') === 0 || problem.field.indexOf('wizard-scan') === 0 ? 2 : 0);
      render();
      return;
    }
    state.busy = true;
    state.error = null;
    render();
    var request = state.saved
      ? state.api.request('PATCH', '/api/v2/sources/' + state.saved.id,
        Object.assign(payload(), { expectedRevision: state.saved.revision }))
      : state.api.request('POST', '/api/v2/sources', payload());
    request.then(function (value) {
      state.saved = value.item;
      if (!withProbe) {
        state.busy = false;
        if (state.options.onSaved) { state.options.onSaved(value.item); }
        close();
        return null;
      }
      return state.api.request('POST', '/api/v2/sources/' + state.saved.id + '/probe', {})
        .then(function (probe) {
          state.probe = probe.probe;
          state.busy = false;
          render();
        });
    }).catch(function (error) {
      state.busy = false;
      state.error = error;
      render();
    });
  }

  function enable() {
    if (state.busy || !state.saved) { return; }
    state.busy = true;
    render();
    state.api.request('POST', '/api/v2/sources/' + state.saved.id + '/actions', {
      action: 'enable',
      actionId: 'wizard-enable:' + state.saved.id + ':' + Date.now()
    }).then(function () {
      state.busy = false;
      if (state.options.onSaved) { state.options.onSaved(state.saved); }
      close();
    }).catch(function (error) {
      state.busy = false;
      state.error = error;
      render();
    });
  }

  function close() {
    var dialog = document.getElementById('wizard-dialog');
    if (dialog) { dialog.hidden = 'hidden'; dialog.textContent = ''; }
    document.removeEventListener('keydown', onKey);
    if (state.options && state.options.onClose) { state.options.onClose(); }
  }

  function onKey(event) {
    if (event.key === 'Escape') { close(); }
  }

  function open(options) {
    state.api = options.api;
    state.adapters = options.adapters || [];
    state.options = options;
    state.step = 0;
    state.error = null;
    state.probe = null;
    state.saved = null;
    draft = restore(state.adapters);
    var dialog = document.getElementById('wizard-dialog');
    dialog.hidden = null;
    document.addEventListener('keydown', onKey);
    Promise.all([
      state.api.request('GET', '/api/v2/credentials').then(function (value) {
        state.credentials = value.items || [];
      }).catch(function () { state.credentials = []; }),
      state.api.request('GET', '/api/v2/storage/roots').then(function (value) {
        state.roots = value.items || [];
      }).catch(function () { state.roots = []; })
    ]).then(render, render);
  }

  window.LBSourceWizard = { open: open, _draft: function () { return draft; } };
})();
