/*
 * rule-impact.js — 规则编辑、影响预览、分层重算与历史证据 (R6-081).
 *
 * The editor keeps R5's editing surface — extractors with source/regex/key/type/unit,
 * anomaly conditions, nodes with AND/OR, priority, shape, colour — and adds the two
 * things R5 could not answer:
 *
 *   1. **what changes**, per layer, before saving. The preview comes from the server's
 *      semantic signatures; a colour or name edit reports zero affected files because
 *      those fields are not part of any signature.
 *   2. **what it matches**, on text the user pastes. The trial frames the sample with
 *      the same worker the index uses, so the evidence shown is the evidence the
 *      pipeline would produce — and a local file is never a valid input, because the
 *      server cannot read one.
 *
 * Draft handling: editing never touches the saved rule. `保存并应用` requires a preview
 * of the *current* draft (the digest is compared), and a 409 keeps the draft while the
 * stored revision is refreshed. Old evidence stays attributable: applying creates new
 * layer versions instead of rewriting the ones a record was judged under.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBRuleImpact = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var FIELDS = ['level', 'logger', 'message', 'body', 'raw', 'fn', 'src', 'pid', 'tid',
    'phase', 'traceback.type'];
  var OPS = [['equals', '等于'], ['notEquals', '不等于'], ['contains', '包含'],
    ['notContains', '不包含'], ['startsWith', '开头为'], ['endsWith', '结尾为'],
    ['regex', '匹配正则'], ['nonzero', '非零'], ['gt', '>'], ['gte', '≥'], ['lt', '<'],
    ['lte', '≤'], ['exists', '存在'], ['missing', '不存在']];
  var SOURCES = [['message', '消息行'], ['body', '多行正文'], ['both', '消息+正文'], ['raw', '原始行']];
  var TYPES = [['auto', '自动'], ['string', '字符串'], ['number', '数字'],
    ['boolean', '布尔'], ['hex', '十六进制']];
  var NODE_TYPES = [['start', '开始'], ['end', '结束'], ['error', '错误'],
    ['warning', '告警'], ['custom', '自定义']];
  var SHAPES = ['circle', 'diamond', 'square', 'triangle', 'star', 'flag', 'hexagon',
    'pin', 'bolt', 'cross'];
  var COLOURS = ['#047857', '#475569', '#b91c1c', '#a16207', '#7e22ce', '#0e7490',
    '#2563eb', '#9333ea', '#db2777', '#065f46', '#b45309', '#1f2937'];
  var LAYER_LABEL = { structure: '结构层', rules: '规则层', search: '检索层' };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function hash(value) {
    var text = JSON.stringify(value);
    var out = 0x811c9dc5;
    for (var index = 0; index < text.length; index++) {
      out ^= text.charCodeAt(index);
      out = (out + ((out << 1) + (out << 4) + (out << 7) + (out << 8) + (out << 24))) >>> 0;
    }
    return ('00000000' + out.toString(16)).slice(-8);
  }

  function select(options, value, onChange, className) {
    var node = el('select', 'r6-input' + (className ? ' ' + className : ''));
    options.forEach(function (pair) {
      var option = el('option', null, pair[1]);
      option.value = pair[0];
      if (String(pair[0]) === String(value)) option.selected = true;
      node.appendChild(option);
    });
    node.addEventListener('change', function () { onChange(node.value); });
    return node;
  }

  function input(value, onChange, className, type) {
    var node = el('input', 'r6-input' + (className ? ' ' + className : ''));
    node.type = type || 'text';
    node.value = value == null ? '' : String(value);
    node.addEventListener('change', function () { onChange(node.value); });
    return node;
  }

  function create(options) {
    options = options || {};
    var api = options.api;
    var onStatus = options.onStatus || function () {};
    var host = null;
    var parts = {};
    var state = {
      admin: false,
      loading: false,
      error: null,
      rules: [],
      ruleId: null,
      revision: 0,
      name: '',
      draft: null,
      saved: null,
      tab: 'extractors',
      preview: null,
      previewDigest: null,
      trial: null,
      trialSample: '',
      applyResult: null,
      busy: false,
      notice: null
    };

    function mount(container) {
      host = container;
      host.innerHTML = '';
      host.className = 'rule-view';

      parts.bar = el('div', 'r6-toolbar');
      parts.title = el('span', 'r6-meta', '规则管理');
      parts.dirty = el('span', 'r6-badge', '未修改');
      parts.previewButton = el('button', 'r6-button is-small', '预览影响');
      parts.previewButton.type = 'button';
      parts.previewButton.addEventListener('click', function () { preview(); });
      parts.applyButton = el('button', 'r6-button is-small is-primary', '保存并应用');
      parts.applyButton.type = 'button';
      parts.applyButton.addEventListener('click', function () { apply(); });
      parts.reload = el('button', 'r6-icon', '重新载入');
      parts.reload.type = 'button';
      parts.reload.addEventListener('click', function () {
        if (dirty() && !window.confirm('放弃当前草稿并重新载入？')) return;
        load(true);
      });
      parts.bar.append(parts.title, parts.dirty, el('span', 'spacer'), parts.reload,
        parts.previewButton, parts.applyButton);

      parts.note = el('div', 'lib-note');
      parts.note.setAttribute('role', 'alert');
      parts.note.hidden = true;

      parts.shell = el('div', 'rule-shell');
      parts.list = el('nav', 'rule-list');
      parts.list.setAttribute('aria-label', '规则列表');
      parts.main = el('div', 'rule-main');
      parts.shell.append(parts.list, parts.main);

      parts.tabs = el('div', 'r6-tabs rule-tabs');
      [['extractors', '提取'], ['anomalies', '异常'], ['nodes', '节点'],
       ['trial', '试跑'], ['impact', '影响']].forEach(function (pair) {
        var button = el('button', 'r6-tab', pair[1]);
        button.type = 'button';
        button.addEventListener('click', function () {
          state.tab = pair[0];
          renderTabs();
          renderMain();
        });
        parts.tabs.appendChild(button);
      });

      host.append(parts.bar, parts.note, parts.shell);
      return api;
    }

    function setNotice(text, tone) {
      parts.note.hidden = !text;
      parts.note.textContent = text || '';
      parts.note.dataset.tone = tone || 'info';
    }

    function dirty() {
      if (!state.draft || !state.saved) return false;
      return hash(state.draft) !== hash(state.saved);
    }

    /* -------------------------------------------------------------------- load */
    function load(force) {
      state.loading = true;
      return api.get('/api/v1/me').then(function (me) {
        api.setCsrf(me.csrfToken || '');
        state.admin = !!(me.capabilities && me.capabilities.admin);
        return api.get('/api/v2/rules?signatures=1');
      }).then(function (value) {
        state.rules = value.items || [];
        state.loading = false;
        if (!state.rules.length) {
          state.error = 'no_rules';
          render();
          setNotice('没有可用规则。管理员可在 R5 工作台创建，或等待服务端种子规则。', 'warn');
          return null;
        }
        var keep = state.ruleId && state.rules.filter(function (rule) {
          return rule.ruleId === state.ruleId;
        })[0];
        var target = (force || !keep) ? state.rules[0] : keep;
        if (force || !state.draft || !keep) selectRule(target);
        render();
        setNotice(null);
        return value;
      }, function (error) {
        state.loading = false;
        state.error = error.message || String(error);
        render();
        setNotice(error.status === 403
          ? '规则编辑需要管理员权限。'
          : ('无法读取规则：' + (error.message || error)), error.status === 403 ? 'warn' : 'error');
      });
    }

    function selectRule(rule) {
      state.ruleId = rule.ruleId;
      state.revision = rule.revision;
      state.name = rule.name;
      state.draft = clone(rule.profile);
      state.saved = clone(rule.profile);
      state.preview = null;
      state.previewDigest = null;
      state.trial = null;
      state.applyResult = null;
      state.signatureError = rule.signatureError || null;
      state.signatures = rule.signatures || null;
    }

    /* ------------------------------------------------------------------ render */
    function render() {
      renderList();
      renderTabs();
      renderMain();
      renderBar();
    }

    function renderBar() {
      var isDirty = dirty();
      parts.dirty.textContent = isDirty ? '未保存草稿' : '与生效版本一致';
      parts.dirty.classList.toggle('is-warn', isDirty);
      parts.dirty.classList.toggle('is-quiet', !isDirty);
      parts.previewButton.disabled = !state.admin || !state.draft || state.busy;
      var previewed = state.preview && state.previewDigest === hash(state.draft);
      parts.applyButton.disabled = !state.admin || !state.draft || state.busy || !previewed;
      parts.applyButton.title = previewed
        ? '保存并使受影响文件重建派生层'
        : '先预览影响，确认范围后才能保存并应用';
      parts.title.textContent = state.ruleId
        ? '规则管理 · ' + state.name + '（修订 ' + state.revision + '）'
        : '规则管理';
    }

    function renderList() {
      parts.list.innerHTML = '';
      var head = el('div', 'rule-list-head');
      head.append(el('span', 'folder-title', '规则'),
        el('span', 'r6-meta', state.admin ? '管理员' : '只读'));
      parts.list.appendChild(head);
      if (!state.rules.length) {
        parts.list.appendChild(el('p', 'gt-empty', state.loading ? '读取中…' : '没有规则'));
      }
      state.rules.forEach(function (rule) {
        var button = el('button', 'folder-item' + (rule.ruleId === state.ruleId ? ' selected' : ''));
        button.type = 'button';
        var label = el('span', 'lib-root-label', rule.name);
        var meta = el('span', 'r6-meta',
          '修订 ' + rule.revision + ' · 提取 ' + rule.semantic.extractors +
          ' · 异常 ' + rule.semantic.anomalies + ' · 节点 ' + rule.semantic.nodes);
        var stack = el('span', 'r6-cell-stack');
        stack.append(label, meta);
        button.appendChild(stack);
        button.addEventListener('click', function () {
          if (dirty() && !window.confirm('放弃当前草稿并切换规则？')) return;
          selectRule(rule);
          render();
        });
        parts.list.appendChild(button);
      });
    }

    function renderTabs() {
      Array.prototype.forEach.call(parts.tabs.children, function (button, index) {
        var ids = ['extractors', 'anomalies', 'nodes', 'trial', 'impact'];
        button.classList.toggle('is-active', ids[index] === state.tab);
        button.setAttribute('aria-selected', ids[index] === state.tab ? 'true' : 'false');
      });
    }

    function renderMain() {
      parts.main.innerHTML = '';
      if (state.error === 'no_rules') {
        var box = el('div', 'r6-empty-state');
        box.append(el('strong', null, '没有可用规则'),
          el('p', null, '规则由服务端种子或管理员创建；这里不凭空生成一条空规则。'));
        parts.main.appendChild(box);
        return;
      }
      if (!state.draft) {
        parts.main.appendChild(el('p', 'r6-meta', state.loading ? '读取中…' : '请选择规则'));
        return;
      }
      parts.main.appendChild(parts.tabs);
      var body = el('div', 'rule-body');
      parts.main.appendChild(body);
      if (!state.admin) {
        body.appendChild(el('p', 'r6-hint', '当前身份只读：可以查看规则与影响，不能保存。'));
      }
      if (state.tab === 'extractors') renderExtractors(body);
      else if (state.tab === 'anomalies') renderAnomalies(body);
      else if (state.tab === 'nodes') renderNodes(body);
      else if (state.tab === 'trial') renderTrial(body);
      else renderImpact(body);
    }

    /* -------------------------------------------------------------- extractors */
    function renderExtractors(body) {
      var list = state.draft.extractors = state.draft.extractors || [];
      var table = el('table', 'r6-table rule-table');
      var head = el('thead');
      var headRow = el('tr');
      ['启用', '名称', '来源', '正则', '键', '分组', '类型', '单位', ''].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var tbody = el('tbody');
      list.forEach(function (row, index) {
        var tr = el('tr');
        var enabledCell = el('td');
        var box = el('input');
        box.type = 'checkbox';
        box.checked = row.enabled !== false;
        box.setAttribute('aria-label', '启用提取规则 ' + (row.name || index + 1));
        box.addEventListener('change', function () {
          row.enabled = box.checked;
          touch();
        });
        enabledCell.appendChild(box);
        tr.append(
          enabledCell,
          cell(input(row.name, function (value) { row.name = value; touch(); })),
          cell(select(SOURCES, row.source || 'message', function (value) {
            row.source = value; touch();
          })),
          cell(input(row.pattern, function (value) { row.pattern = value; touch(); }, 'mono')),
          cell(input(row.key, function (value) { row.key = value; touch(); }, 'mono')),
          cell(input(row.group, function (value) { row.group = value; touch(); }, 'mono')),
          cell(select(TYPES, row.type || 'auto', function (value) { row.type = value; touch(); })),
          cell(input(row.unit, function (value) { row.unit = value; touch(); })),
          opsCell(function () { list.splice(index, 1); touch(true); })
        );
        tbody.appendChild(tr);
      });
      table.append(head, tbody);
      body.appendChild(table);
      body.appendChild(addButton('添加提取规则', function () {
        list.push({ id: 'extract-' + (list.length + 1), name: '新提取规则', enabled: true,
          source: 'message', pattern: '(\\w+)=(\\S+)', key: '', group: '2', type: 'auto', unit: '' });
        touch(true);
      }));
      body.appendChild(el('p', 'r6-hint',
        '键为空时使用命名分组 key 或第一个捕获组；分组默认取第 2 组。修改正则或来源会重算规则层与检索层。'));
    }

    /* --------------------------------------------------------------- anomalies */
    function renderAnomalies(body) {
      var list = state.draft.anomalies = state.draft.anomalies || [];
      var table = el('table', 'r6-table rule-table');
      var head = el('thead');
      var headRow = el('tr');
      ['启用', '名称', '字段', '条件', '值', '严重度', '区分大小写', ''].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var tbody = el('tbody');
      list.forEach(function (row, index) {
        var tr = el('tr');
        var enabledCell = el('td');
        var box = el('input');
        box.type = 'checkbox';
        box.checked = row.enabled !== false;
        box.setAttribute('aria-label', '启用异常规则 ' + (row.name || index + 1));
        box.addEventListener('change', function () { row.enabled = box.checked; touch(); });
        enabledCell.appendChild(box);
        tr.append(
          enabledCell,
          cell(input(row.name, function (value) { row.name = value; touch(); })),
          cell(select(FIELDS.map(function (field) { return [field, field]; }), row.field,
            function (value) { row.field = value; touch(); })),
          cell(select(OPS, row.op, function (value) { row.op = value; touch(); })),
          cell(input(row.value, function (value) { row.value = value; touch(); }, 'mono')),
          cell(select([['error', '错误'], ['warning', '告警']], row.severity || 'error',
            function (value) { row.severity = value; touch(); })),
          cell(checkbox(row.caseSensitive === true, function (value) {
            row.caseSensitive = value; touch();
          }, '区分大小写')),
          opsCell(function () { list.splice(index, 1); touch(true); })
        );
        tbody.appendChild(tr);
      });
      table.append(head, tbody);
      body.appendChild(table);
      body.appendChild(addButton('添加异常条件', function () {
        list.push({ id: 'anomaly-' + (list.length + 1), name: '新异常条件', enabled: true,
          field: 'message', op: 'contains', value: '', severity: 'error', caseSensitive: false });
        touch(true);
      }));
      body.appendChild(el('p', 'r6-hint',
        '异常条件是判断，不是显示：修改它会重算规则层与检索层，但不会重新分帧原文。'));
    }

    /* ------------------------------------------------------------------- nodes */
    function renderNodes(body) {
      var list = state.draft.nodes = state.draft.nodes || [];
      list.forEach(function (node, index) {
        var card = el('fieldset', 'rule-node');
        var legend = el('legend', null, '节点 ' + (index + 1));
        card.appendChild(legend);
        var row = el('div', 'rule-node-row');
        row.append(
          labelled('名称', input(node.name, function (value) { node.name = value; touch(); })),
          labelled('类型', select(NODE_TYPES, node.type || 'custom', function (value) {
            node.type = value; touch();
          })),
          labelled('条件关系', select([['all', '全部满足'], ['any', '任一满足']],
            node.combine || 'all', function (value) { node.combine = value; touch(); })),
          labelled('形状', select(SHAPES.map(function (shape) { return [shape, shape]; }),
            node.shape || 'star', function (value) { node.shape = value; touch(); })),
          labelled('颜色', colourPicker(node.color || '#7e22ce', function (value) {
            node.color = value; touch();
          })),
          labelled('优先级', input(node.priority == null ? 0 : node.priority,
            function (value) { node.priority = Number(value) || 0; touch(); }, 'mono', 'number')),
          labelled('启用', checkbox(node.enabled !== false, function (value) {
            node.enabled = value; touch();
          }, '启用'))
        );
        card.appendChild(row);
        var conditions = node.conditions = node.conditions || [];
        var condTable = el('table', 'r6-table rule-cond');
        var condHead = el('thead');
        var headRow = el('tr');
        ['字段', '条件', '值', '区分大小写', ''].forEach(function (title) {
          headRow.appendChild(el('th', null, title));
        });
        condHead.appendChild(headRow);
        var condBody = el('tbody');
        conditions.forEach(function (condition, position) {
          var tr = el('tr');
          tr.append(
            cell(select(FIELDS.map(function (field) { return [field, field]; }), condition.field,
              function (value) { condition.field = value; touch(); })),
            cell(select(OPS, condition.op, function (value) { condition.op = value; touch(); })),
            cell(input(condition.value, function (value) { condition.value = value; touch(); }, 'mono')),
            cell(checkbox(condition.caseSensitive === true, function (value) {
              condition.caseSensitive = value; touch();
            }, '区分大小写')),
            opsCell(function () { conditions.splice(position, 1); touch(true); })
          );
          condBody.appendChild(tr);
        });
        condTable.append(condHead, condBody);
        card.appendChild(condTable);
        var actions = el('div', 'rule-node-actions');
        var addCond = el('button', 'r6-icon', '添加条件');
        addCond.type = 'button';
        addCond.addEventListener('click', function () {
          conditions.push({ field: 'message', op: 'contains', value: '', caseSensitive: false });
          touch(true);
        });
        var remove = el('button', 'r6-icon is-danger', '删除节点');
        remove.type = 'button';
        remove.addEventListener('click', function () { list.splice(index, 1); touch(true); });
        actions.append(addCond, remove);
        card.appendChild(actions);
        body.appendChild(card);
      });
      body.appendChild(addButton('添加节点', function () {
        list.push({ id: 'node-' + (list.length + 1), name: '新节点', type: 'custom',
          combine: 'all', enabled: true, shape: 'star', color: '#7e22ce', priority: 0,
          conditions: [{ field: 'message', op: 'contains', value: '', caseSensitive: false }] });
        touch(true);
      }));
      body.appendChild(el('p', 'r6-hint',
        '形状、颜色、名称和优先级只影响呈现，不进入语义签名；修改它们不会触发重算。'));
    }

    /* ------------------------------------------------------------------- trial */
    function renderTrial(body) {
      body.appendChild(el('p', 'r6-hint',
        '试跑只使用你粘贴的样本文本（≤256 KiB），通过服务端同一个分帧与规则引擎执行；不会读取或上传任何本机文件。'));
      var area = el('textarea', 'r6-input rule-sample');
      area.rows = 8;
      area.value = state.trialSample;
      area.placeholder = '粘贴几行日志作为样本…';
      area.addEventListener('input', function () { state.trialSample = area.value; });
      body.appendChild(area);
      var actions = el('div', 'rule-node-actions');
      var run = el('button', 'r6-button is-small is-primary', '运行试跑');
      run.type = 'button';
      run.disabled = !state.admin || state.busy;
      run.addEventListener('click', function () { runTrial(); });
      actions.appendChild(run);
      if (state.trial) {
        actions.appendChild(el('span', 'r6-meta', '分帧 ' + state.trial.framed_records +
          ' 条 · 展示 ' + state.trial.returned + ' 条' +
          (state.trial.rules_failed ? ' · 规则执行报错，记录已保留' : '')));
      }
      body.appendChild(actions);
      if (!state.trial) return;
      state.trial.records.forEach(function (record) {
        var card = el('div', 'rule-trial');
        var head = el('div', 'rule-trial-head');
        head.append(el('strong', null, '#' + record.ordinal + ' ' + (record.level || '')),
          el('span', 'r6-meta mono', record.ts || '无时间'));
        card.appendChild(head);
        var message = el('div', 'rule-trial-msg mono');
        message.textContent = record.message || '';
        card.appendChild(message);
        if (record.rules_failed) {
          card.appendChild(el('div', 'r6-cell-warn', '规则失败：' + record.rules_failed));
        }
        if (record.fields && record.fields.length) {
          var fields = el('div', 'r6-badges');
          record.fields.forEach(function (pair) {
            fields.appendChild(el('span', 'r6-badge', pair[0] + '=' + pair[1]));
          });
          card.appendChild(fields);
        }
        if (record.issues && record.issues.length) {
          record.issues.forEach(function (issue) {
            var line = el('div', 'rule-evidence');
            line.append(el('strong', null, '异常 · ' + issue.name + '（' + issue.severity + '）'),
              el('div', 'r6-meta', issue.condition || ''));
            (issue.evidence || []).forEach(function (item) {
              line.appendChild(el('div', 'r6-meta mono',
                '第 ' + item.line + ' 行 ' + item.field + ' = ' + item.value));
            });
            card.appendChild(line);
          });
        }
        if (record.nodes && record.nodes.length) {
          record.nodes.forEach(function (node) {
            var line = el('div', 'rule-evidence');
            line.append(el('strong', null, '节点 · ' + node.name + '（' + node.type + '）'));
            (node.evidence || []).forEach(function (item) {
              line.appendChild(el('div', 'r6-meta mono',
                '第 ' + item.line + ' 行 ' + item.field + ' = ' + item.value));
            });
            card.appendChild(line);
          });
        }
        body.appendChild(card);
      });
    }

    function runTrial() {
      if (!state.trialSample.trim()) {
        setNotice('请先粘贴样本行。', 'warn');
        return;
      }
      state.busy = true;
      renderBar();
      api.post('/api/v2/rules/trial', { profile: state.draft, sample: state.trialSample, limit: 20 })
        .then(function (value) {
          state.busy = false;
          state.trial = value;
          setNotice('试跑完成：分帧 ' + value.framed_records + ' 条，命中证据见下方。', 'ok');
          renderMain();
          renderBar();
        }, function (error) {
          state.busy = false;
          setNotice('试跑失败：' + (error.message || error), 'error');
          renderBar();
        });
    }

    /* ------------------------------------------------------------------ impact */
    function renderImpact(body) {
      var preview = state.preview;
      var button = el('button', 'r6-button is-small', preview ? '重新预览当前草稿' : '预览当前草稿的影响');
      button.type = 'button';
      button.disabled = !state.admin || state.busy;
      button.addEventListener('click', function () { preview_(); });
      var actions = el('div', 'rule-node-actions');
      actions.appendChild(button);
      if (state.signatureError) {
        actions.appendChild(el('span', 'r6-cell-warn',
          '语义签名不可用：' + state.signatureError.message));
      }
      body.appendChild(actions);
      if (!preview) {
        body.appendChild(el('p', 'r6-meta', '尚未预览。预览不会写库，也不会排队任务。'));
        return;
      }
      var totals = preview.totals;
      var summary = el('div', 'rt-stats');
      [
        ['可见文件', totals.visible],
        ['已建索引', totals.indexed],
        ['受影响', totals.affected],
        ['规则层', totals.rules_affected],
        ['检索层', totals.search_affected],
        ['未建索引', totals.not_indexed]
      ].forEach(function (pair) {
        var cell = el('div', 'rt-stat');
        cell.append(el('span', 'rt-stat-label', pair[0]),
          el('strong', 'rt-stat-value', String(pair[1])));
        summary.appendChild(cell);
      });
      body.appendChild(summary);
      body.appendChild(el('p', 'r6-hint', preview.layers.structure.reason));
      (preview.notes || []).forEach(function (note) {
        body.appendChild(el('p', 'r6-hint', note));
      });
      if (preview.truncated) {
        body.appendChild(el('p', 'r6-cell-warn',
          '影响扫描达到 ' + preview.scan_limit + ' 个文件上限，实际范围可能更大；结果按已扫描文件计数。'));
      }
      var table = el('table', 'r6-table rule-table');
      var head = el('thead');
      var headRow = el('tr');
      ['文件', '范围', '需要重建的层'].forEach(function (title) {
        headRow.appendChild(el('th', null, title));
      });
      head.appendChild(headRow);
      var tbody = el('tbody');
      (preview.files || []).forEach(function (file) {
        var tr = el('tr');
        var nameCell = el('td', 'mono');
        nameCell.textContent = file.displayName;
        var scopeCell = el('td', 'r6-meta');
        scopeCell.textContent = (file.rootLabel || '') + ' · ' + (file.scope === 'private' ? '私有' : '共享');
        var layersCell = el('td');
        if (file.state === 'not_indexed') {
          layersCell.appendChild(el('span', 'r6-badge is-quiet', '未建索引'));
        } else if (!file.layers.length) {
          layersCell.appendChild(el('span', 'r6-badge is-good', '无需重建'));
        } else {
          file.layers.forEach(function (layer) {
            layersCell.appendChild(el('span', 'r6-badge is-warn',
              LAYER_LABEL[layer] || layer));
          });
        }
        tr.append(nameCell, scopeCell, layersCell);
        tbody.appendChild(tr);
      });
      table.append(head, tbody);
      body.appendChild(table);
      if (preview.truncated) {
        body.appendChild(el('p', 'r6-meta', '列表只显示前 200 个文件。'));
      }
    }

    function preview_() {
      state.busy = true;
      renderBar();
      var digest = hash(state.draft);
      api.post('/api/v2/rules/impact', { profile: state.draft }).then(function (value) {
        state.busy = false;
        state.preview = value;
        state.previewDigest = digest;
        state.tab = 'impact';
        setNotice('影响预览：' + value.totals.affected + ' 个文件需要重建（共 ' +
          value.totals.visible + ' 个可见文件）。', value.totals.affected ? 'warn' : 'ok');
        render();
      }, function (error) {
        state.busy = false;
        setNotice('预览失败：' + (error.message || error), 'error');
        renderBar();
      });
    }

    function apply() {
      if (!state.preview || state.previewDigest !== hash(state.draft)) {
        setNotice('请先预览当前草稿的影响，再保存并应用。', 'warn');
        return;
      }
      state.busy = true;
      renderBar();
      api.post('/api/v2/rules/' + encodeURIComponent(state.ruleId) + '/apply', {
        profile: state.draft,
        name: state.name,
        expectedRevision: state.revision,
        actionId: 'rule-apply:' + state.ruleId + ':' + Date.now().toString(36)
      }).then(function (value) {
        state.busy = false;
        state.applyResult = value;
        state.revision = value.rule.revision;
        state.saved = clone(state.draft);
        setNotice('已保存修订 ' + value.rule.revision + '，排队重建 ' + value.queued +
          ' 个文件。旧版本在新版本完成前继续可读。', 'ok');
        onStatus('规则已应用（修订 ' + value.rule.revision + '）');
        return load(true);
      }, function (error) {
        state.busy = false;
        if (error.code === 'revision_conflict') {
          setNotice('规则已被其他管理员修改：你的草稿保留，已重新读取最新修订。请再次预览后保存。', 'warn');
          state.preview = null;
          state.previewDigest = null;
          return load(false);
        }
        setNotice('应用失败：' + (error.message || error), 'error');
        renderBar();
        return null;
      });
    }

    /* ------------------------------------------------------------------ helpers */
    function touch(rerender) {
      if (rerender || !state.preview || state.previewDigest !== hash(state.draft)) {
        state.preview = null;
        state.previewDigest = null;
      }
      renderBar();
      if (rerender) renderMain();
    }

    function cell(node) {
      var td = el('td');
      td.appendChild(node);
      return td;
    }

    function opsCell(onRemove) {
      var td = el('td');
      var button = el('button', 'r6-icon is-danger', '删除');
      button.type = 'button';
      button.addEventListener('click', onRemove);
      td.appendChild(button);
      return td;
    }

    function labelled(text, node) {
      var wrap = el('div', 'r6-field');
      wrap.append(el('label', 'r6-meta', text), node);
      return wrap;
    }

    function checkbox(checked, onChange, label) {
      var wrap = el('label', 'r6-check');
      var box = el('input');
      box.type = 'checkbox';
      box.checked = !!checked;
      box.setAttribute('aria-label', label);
      box.addEventListener('change', function () { onChange(box.checked); });
      wrap.append(box, el('span', null, label));
      return wrap;
    }

    function colourPicker(value, onChange) {
      var wrap = el('div', 'rule-colours');
      COLOURS.forEach(function (colour) {
        var button = el('button', 'rule-colour' + (colour === value ? ' is-active' : ''));
        button.type = 'button';
        button.style.background = colour;
        button.setAttribute('aria-label', '颜色 ' + colour);
        button.addEventListener('click', function () {
          onChange(colour);
        });
        wrap.appendChild(button);
      });
      var custom = el('input', 'r6-input mono rule-colour-input');
      custom.type = 'text';
      custom.value = value;
      custom.maxLength = 7;
      custom.setAttribute('aria-label', '颜色十六进制');
      custom.addEventListener('change', function () {
        if (/^#[0-9a-fA-F]{6}$/.test(custom.value)) onChange(custom.value);
      });
      wrap.appendChild(custom);
      return wrap;
    }

    function addButton(text, onClick) {
      var button = el('button', 'r6-button is-small', text);
      button.type = 'button';
      button.disabled = !state.admin;
      button.addEventListener('click', onClick);
      var wrap = el('div', 'rule-add');
      wrap.appendChild(button);
      return wrap;
    }

    return {
      mount: mount,
      show: function () { return load(!state.draft); },
      load: load,
      state: state,
      parts: parts
    };
  }

  return { create: create, FIELDS: FIELDS, OPS: OPS };
});
