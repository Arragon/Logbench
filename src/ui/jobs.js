/*
 * jobs.js — the import panel's state, not its pixels (R6-021).
 *
 * The rule that shapes everything here: the panel is an observer, not the owner.
 * Closing it must not cancel anything, and the reading surface must stay usable
 * while a 100 MiB file is being read. So this controller only:
 *
 *   - decides when the panel opens (first multi-file batch) and that closing is
 *     not cancelling;
 *   - maps buttons to queue actions and reports what actually happened;
 *   - produces an announcement string that changes on phase transitions, not on
 *     every progress tick, so a screen reader is not flooded with percentages.
 *
 * The DOM is injected as `render(state)`. That keeps the geometry decisions in
 * the page and the behaviour decisions here, where they can be tested.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBJobPanel = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var PHASE_LABELS = {
    queued: '等待',
    reading: '读取',
    saving: '保存',
    framing: '解析',
    indexing: '建索引',
    indexed: '可阅读',
    paused: '已暂停',
    cancelled: '已取消',
    failed: '失败'
  };

  function createJobPanel(options) {
    options = options || {};
    var queue = options.queue;
    if (!queue) throw new Error('job panel needs an import queue');
    var render = options.render || function () {};
    var now = options.now || function () { return Date.now(); };
    var state = {
      open: false,
      minimized: false,
      last_opened_at: null,
      announcement: '',
      tasks: [],
      summary: null
    };
    var lastPhases = new Map();

    function snapshot() {
      var value = queue.snapshot();
      return {
        open: state.open,
        minimized: state.minimized,
        announcement: state.announcement,
        tasks: value.tasks.map(function (task) {
          return Object.assign({}, task, { phase_label: PHASE_LABELS[task.phase] || task.phase });
        }),
        summary: {
          files_total: value.files_total,
          files_ready: value.files_ready,
          files_failed: value.files_failed,
          bytes_done: value.bytes_done,
          bytes_total: value.bytes_total,
          // No percentage without a denominator. Every task carries a real size,
          // so this is null only for a genuinely empty batch.
          percent: value.bytes_total > 0 ? Math.round((value.bytes_done / value.bytes_total) * 100) : null,
          running: value.tasks.filter(function (task) {
            return ['reading', 'saving', 'framing', 'indexing'].indexOf(task.phase) !== -1;
          }).length,
          waiting: value.tasks.filter(function (task) { return task.phase === 'queued' || task.phase === 'paused'; }).length
        }
      };
    }

    function announce(text) {
      state.announcement = text;
    }

    function refresh(force) {
      var next = snapshot();
      state.summary = next.summary;
      state.tasks = next.tasks;
      // Announce only on a phase change: a live region that fires per byte is
      // noise, and users stop listening.
      next.tasks.forEach(function (task) {
        var previous = lastPhases.get(task.id);
        if (previous !== task.phase) {
          lastPhases.set(task.id, task.phase);
          if (task.phase === 'indexed') announce(task.name + ' 已可阅读');
          else if (task.phase === 'failed') announce(task.name + ' 导入失败');
          else if (task.phase === 'paused') announce(task.name + ' 已暂停');
        }
      });
      if (force) render(snapshot());
      else render(snapshot());
    }

    /** Called when files are chosen. A multi-file batch opens the panel. */
    function onFilesChosen(count) {
      if (count > 1 && !state.open) {
        state.open = true;
        state.minimized = false;
        state.last_opened_at = now();
      }
      refresh(true);
    }

    function open() { state.open = true; state.minimized = false; refresh(true); }
    function minimize() { state.minimized = true; refresh(true); }
    function close() {
      // Closing is not cancelling: the queue keeps running and this is asserted
      // by the tests.
      state.open = false;
      state.minimized = false;
      refresh(true);
      return { cancelled: false, running: state.summary ? state.summary.running : 0 };
    }

    function action(taskId, name) {
      var task = queue.tasks().find(function (item) { return item.id === taskId; });
      if (!task) return { ok: false, reason: 'unknown_task' };
      if (name === 'pause') return { ok: queue.pause(taskId), action: 'pause' };
      if (name === 'resume') return { ok: queue.resume(taskId), action: 'resume' };
      if (name === 'cancel') return { ok: queue.cancel(taskId), action: 'cancel' };
      if (name === 'retry') return { ok: queue.retry(taskId), action: 'retry' };
      if (name === 'focus') { queue.raisePriority(taskId); return { ok: true, action: 'focus' }; }
      return { ok: false, reason: 'unknown_action' };
    }

    function bind() {
      if (!options.emit) return function () {};
      options.emit('task', () => refresh(true));
      options.emit('progress', () => refresh(false));
      return function () { refresh(true); };
    }

    return {
      onFilesChosen: onFilesChosen,
      open: open,
      close: close,
      minimize: minimize,
      action: action,
      bind: bind,
      refresh: refresh,
      snapshot: snapshot,
      labels: PHASE_LABELS,
      state: function () { return { open: state.open, minimized: state.minimized }; }
    };
  }

  return { PHASE_LABELS: PHASE_LABELS, createJobPanel: createJobPanel };
});
