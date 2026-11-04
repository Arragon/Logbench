(function () {
  'use strict';

  var checks = [];
  function record(label, ok, detail) {
    checks.push({ label: label, ok: !!ok, detail: detail == null ? null : String(detail) });
    var li = document.createElement('li');
    li.className = ok ? 'ok' : 'bad';
    li.textContent = (ok ? 'PASS ' : 'FAIL ') + label + (detail == null ? '' : ' — ' + detail);
    document.getElementById('checks').appendChild(li);
  }

  /* Every real Worker is created here, so a leak would be visible in the count. */
  var created = 0;
  var terminated = 0;
  var RealWorker = window.Worker;

  function spawnExecutor() {
    var worker = new RealWorker('/assets/workers/regex-executor.js');
    created += 1;
    return {
      post: function (message) { worker.postMessage(message); },
      onMessage: function (fn) { worker.onmessage = function (event) { fn(event.data); }; },
      terminate: function () { terminated += 1; worker.terminate(); }
    };
  }

  /**
   * A catastrophic pattern over a hostile input: 30 'a' followed by '!', with
   * `(a+)+$`. It never finishes, which is exactly the point — no in-worker yield
   * can help, only `terminate()`.
   */
  function hostileTexts(count) {
    var texts = [];
    for (var i = 0; i < count; i++) texts.push('a'.repeat(28) + '!');
    return texts;
  }

  async function run() {
    var supervisor = LBRegexSupervisor.createSupervisor({
      spawn: spawnExecutor,
      sliceMs: 800,
      budgetMs: 10000,
      recordsPerSlice: 40
    });

    /* Main-thread liveness: a 20ms timer that must not be starved. */
    var ticks = 0;
    var maxGap = 0;
    var last = performance.now();
    var timer = setInterval(function () {
      var now = performance.now();
      maxGap = Math.max(maxGap, now - last);
      last = now;
      ticks += 1;
    }, 20);

    var started = performance.now();
    var result = await supervisor.scan({ pattern: '(a+)+$', texts: hostileTexts(60) });
    var elapsed = performance.now() - started;

    clearInterval(timer);
    record('危险正则被终止', result.state === 'partial' && result.killed === true,
      'state=' + result.state + ' killed=' + result.killed);
    record('终止发生在保护时限附近', elapsed < 4000, Math.round(elapsed) + 'ms');
    record('执行 Worker 真的被 terminate', terminated >= 1, 'terminate 调用 ' + terminated + ' 次');
    record('主线程未被冻结', maxGap < 400, '最大定时器间隔 ' + Math.round(maxGap) + 'ms，' + ticks + ' 次心跳');
    record('被终止的范围不算精确零', result.coverage.complete === false &&
      result.count === undefined && result.matched === 0,
      'coverage=' + JSON.stringify(result.coverage));

    /* A healthy pattern on the same executor still works afterwards. */
    var texts = [];
    for (var i = 0; i < 300; i++) texts.push(i % 5 === 0 ? 'energy spike ' + i : 'idle ' + i);
    var healthy = await supervisor.scan({ pattern: 'energy', flags: '', texts: texts });
    var expected = texts.filter(function (text) { return text.indexOf('energy') !== -1; }).length;
    record('正常正则结果与本地一致', healthy.state === 'done' && healthy.matched === expected,
      'matched=' + healthy.matched + ' expected=' + expected);
    record('正常扫描覆盖完整', healthy.coverage.complete === true, JSON.stringify(healthy.coverage));

    /* Zero-length matches are refused by the executor rather than counted. */
    var empty = await supervisor.scan({ pattern: 'a*', texts: ['aaa', 'bbb'] });
    record('零长匹配被拒绝', empty.state === 'failed' && empty.error.code === 'empty_match',
      'state=' + empty.state + ' code=' + (empty.error && empty.error.code));

    record('终止后为新任务创建新 Worker', created > 1, '创建 ' + created + ' 个，终止 ' + terminated + ' 个');

    var payload = {
      ok: checks.every(function (check) { return check.ok; }),
      checks: checks,
      workers_created: created,
      workers_terminated: terminated,
      max_timer_gap_ms: Math.round(maxGap),
      hostile_elapsed_ms: Math.round(elapsed),
      supervisor: supervisor.stats()
    };
    window.__result = payload;
    document.getElementById('status').textContent = payload.ok ? 'all checks passed' : 'FAILURES';
    document.getElementById('status').className = payload.ok ? 'ok' : 'bad';
    document.getElementById('detail').textContent = JSON.stringify(payload, null, 2);
    return payload;
  }

  window.__run = function () {
    return run().catch(function (error) {
      record('运行未抛异常', false, error && (error.code || error.message));
      window.__result = { ok: false, checks: checks, error: String(error && (error.stack || error.message)) };
      document.getElementById('status').textContent = 'ERROR';
      return window.__result;
    });
  };
})();
