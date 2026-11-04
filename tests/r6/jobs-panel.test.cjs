'use strict';
/*
 * R6-021 acceptance. The panel is a view over the queue; the tests check that it
 * never becomes the owner: closing does not cancel, actions reach the queue, and
 * announcements happen on transitions rather than on every tick.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const J = require(path.join(ROOT, 'src', 'ui', 'jobs.js'));

function fakeQueue(tasks) {
  const calls = [];
  const state = { tasks: tasks.map(t => Object.assign({ bytes_done: 0, records: 0, blocks: 0, error: null }, t)) };
  return {
    calls,
    state,
    snapshot() {
      const bytesTotal = state.tasks.reduce((sum, t) => sum + t.size, 0);
      const bytesDone = state.tasks.reduce((sum, t) => sum + t.bytes_done, 0);
      return {
        bytes_total: bytesTotal, bytes_done: bytesDone,
        files_total: state.tasks.length,
        files_ready: state.tasks.filter(t => t.phase === 'indexed').length,
        files_failed: state.tasks.filter(t => t.phase === 'failed').length,
        ratio: bytesTotal ? bytesDone / bytesTotal : null,
        tasks: state.tasks
      };
    },
    tasks() { return state.tasks; },
    pause(id) { calls.push(['pause', id]); return true; },
    resume(id) { calls.push(['resume', id]); return true; },
    cancel(id) { calls.push(['cancel', id]); return true; },
    retry(id) { calls.push(['retry', id]); return true; },
    raisePriority(id) { calls.push(['focus', id]); return true; }
  };
}

test('a multi-file batch opens the panel; a single file does not', () => {
  const queue = fakeQueue([{ id: 't1', name: 'a.log', size: 100, phase: 'queued' }]);
  const panel = J.createJobPanel({ queue });
  panel.onFilesChosen(1);
  assert.equal(panel.state().open, false);
  panel.onFilesChosen(3);
  assert.equal(panel.state().open, true);
});

test('closing the panel does not cancel anything', () => {
  const queue = fakeQueue([
    { id: 't1', name: 'a.log', size: 100, phase: 'reading' },
    { id: 't2', name: 'b.log', size: 100, phase: 'queued' }
  ]);
  const panel = J.createJobPanel({ queue });
  panel.onFilesChosen(2);
  const result = panel.close();
  assert.equal(result.cancelled, false);
  assert.deepEqual(queue.calls, [], 'no queue action was triggered by closing');
  assert.equal(result.running, 1, 'the running task kept running');
  assert.equal(panel.state().open, false);
});

test('buttons reach the queue, and unknown actions do not pretend to work', () => {
  const queue = fakeQueue([{ id: 't1', name: 'a.log', size: 100, phase: 'paused' }]);
  const panel = J.createJobPanel({ queue });
  assert.equal(panel.action('t1', 'resume').ok, true);
  assert.equal(panel.action('t1', 'cancel').ok, true);
  assert.deepEqual(queue.calls, [['resume', 't1'], ['cancel', 't1']]);
  assert.equal(panel.action('t1', 'explode').ok, false);
  assert.equal(panel.action('missing', 'pause').reason, 'unknown_task');
});

test('progress has no percentage when the denominator is unknown', () => {
  const queue = fakeQueue([{ id: 't1', name: 'a.log', size: 0, phase: 'reading' }]);
  const panel = J.createJobPanel({ queue });
  panel.onFilesChosen(1);
  const snapshot = panel.snapshot();
  assert.equal(snapshot.summary.percent, null, 'no invented 0% / 100%');
});

test('the live announcement changes on phase transitions only', () => {
  const queue = fakeQueue([{ id: 't1', name: 'a.log', size: 100, phase: 'queued' }]);
  const panel = J.createJobPanel({ queue });
  panel.onFilesChosen(2);
  const first = panel.snapshot().announcement;
  queue.state.tasks[0].bytes_done = 40;
  panel.refresh(true);
  assert.equal(panel.snapshot().announcement, first, 'a byte tick is not an announcement');

  queue.state.tasks[0].phase = 'indexed';
  panel.refresh(true);
  assert.ok(/已可阅读/.test(panel.snapshot().announcement), panel.snapshot().announcement);
});

test('the summary counts ready, waiting and failed separately', () => {
  const queue = fakeQueue([
    { id: 't1', name: 'a.log', size: 100, phase: 'indexed', bytes_done: 100 },
    { id: 't2', name: 'b.log', size: 100, phase: 'paused', bytes_done: 50 },
    { id: 't3', name: 'c.log', size: 100, phase: 'failed', bytes_done: 10 }
  ]);
  const panel = J.createJobPanel({ queue });
  panel.onFilesChosen(3);
  const summary = panel.snapshot().summary;
  assert.equal(summary.files_ready, 1);
  assert.equal(summary.waiting, 1);
  assert.equal(summary.files_failed, 1);
  assert.equal(summary.percent, Math.round((160 / 300) * 100));
});

test('restoring focus is the page’s job, but the panel reports what to restore', () => {
  const queue = fakeQueue([{ id: 't1', name: 'a.log', size: 100, phase: 'reading' }]);
  let rendered = 0;
  const panel = J.createJobPanel({ queue, render: () => { rendered += 1; } });
  panel.onFilesChosen(2);
  const before = rendered;
  panel.close();
  assert.ok(rendered > before, 'closing re-renders so the page can restore focus');
});
