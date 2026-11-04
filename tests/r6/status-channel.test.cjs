'use strict';
/*
 * The header status line is a reading-context channel, not a results channel.
 *
 * It says which file or page is open and what a search/ingest is doing. Its
 * right-hand neighbour is the cache meter, and at 375 px the status is clipped
 * to 80 px, so an operation result written there is both duplicated and
 * truncated. Every surface already has an inline notice; some of them reload
 * their list after a write, which is why their result is carried through
 * state rather than painted directly.
 *
 * These are source contracts, not behaviour tests. The browser gate in
 * qa/r6/ is what proves the messages actually appear.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const UI = path.join(ROOT, 'src', 'ui');
const read = (...parts) => fs.readFileSync(path.join(...parts), 'utf8');
const ui = (name) => read(UI, name);

// A call with a message argument. JSDoc like "onStatus(text)" must not count.
const RESULT_CALL = /\bonStatus\s*\(\s*(?:['"]|options\.onStatus)/;

test('only the reader context may write to the header status', () => {
  const offenders = fs.readdirSync(UI)
    .filter((name) => name.endsWith('.js') && name !== 'library.js')
    .filter((name) => RESULT_CALL.test(ui(name)));
  assert.deepEqual(offenders, [],
    'operation results belong in the surface inline notice, not #status');

  // "打开 <file>" is a context switch: it changes what the reader is looking at.
  const library = ui('library.js');
  const calls = library.match(/\bonStatus\s*\(/g) || [];
  assert.equal(calls.length, 1, 'library uses onStatus exactly once');
  assert.match(library, /onStatus\('打开 ' \+ file\.displayName\)/);
});

test('r6-app injects the status callback into the library only', () => {
  const app = read(ROOT, 'src', 'r6-app.js');
  const injections = app.match(/onStatus:\s*setStatus/g) || [];
  assert.equal(injections.length, 1,
    'every other surface must own its own feedback');
  assert.match(app, /LBLibrary\.create\(\{[\s\S]{0,400}?onStatus:\s*setStatus/,
    'the surviving injection must be the library');
});

test('list reloads cannot erase a batch result', () => {
  // load() clears the notice area. A result written before the reload would be
  // gone by the time the list comes back, so both modules queue it in state.
  for (const name of ['cache-manager.js', 'rule-impact.js']) {
    const source = ui(name);
    assert.match(source, /pendingNotice:\s*null/, name + ' must declare pendingNotice');
    assert.match(source, /state\.pendingNotice\s*\?\s*setNotice\(/,
      name + ' load() must publish the queued result after the refresh');
    assert.match(source, /state\.pendingNotice\s*=\s*\{[\s\S]{0,250}?tone:\s*'ok'/,
      name + ' must queue a success result');
    assert.match(source, /state\.pendingNotice\s*=\s*null/,
      name + ' must clear the queue on failure');
  }
});

test('surfaces without a reload still own an inline result', () => {
  const meter = ui('cache-meter.js');
  assert.match(meter, /showSaved\([\s\S]{0,120}?缓存限额已保存/);
  assert.match(meter, /parts\.savedNote/);
  assert.match(meter, /savedMessage/);
  assert.doesNotMatch(meter, /onStatus\s*\(/);

  const time = ui('global-time.js');
  assert.match(time, /showResult\('已保存视角/);
  assert.match(time, /showResult\('已载入视角/);
  assert.match(time, /parts\.status/);
  assert.doesNotMatch(time, /options\.onStatus/);

  // The budget page has a dedicated applied-result panel instead of a notice,
  // because load() clears its notice area too.
  const runtime = ui('runtime.js');
  assert.match(runtime, /预算已保存（修订/);
  assert.doesNotMatch(runtime, /onStatus\s*\(/);
});

test('the inline result slots are styled and shrink instead of clipping', () => {
  const css = read(ROOT, 'src', 'r6.css');
  assert.match(css, /\.gt-status\s*\{[^}]*text-overflow:\s*ellipsis/s);
  assert.match(css, /\.cache-quota-saved\s*\{\s*flex:\s*1 1 100%/);
});
