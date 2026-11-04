#!/usr/bin/env python3
"""R6-075 evidence: real events over a real socket, in a real browser.

What this script proves, and what it refuses to claim:

* the stream the page opens is a real HTTP SSE connection to a real server process,
  not a synthetic `applyEvent` call;
* an event published by the server reaches the DOM and the page reports how long
  it took, measured on both sides of the same clock;
* scrolling up does not pull the view to the bottom, and the badge counts;
* a reconnect that replays the same ids does not apply anything twice;
* a browser without `EventSource` falls back to polling and says so in words;
* closing the tab drops the subscription while the server keeps its work.

Collection itself is simulated: there is no SFTP/SMB endpoint in this environment
(blocker B-007), so the writer here is `app.events.publish`, the same broker the
scheduler calls. Everything downstream of the broker is real.

    env -u PYTHONPATH python3.9 qa/r6/live_follow.py --out qa/evidence/R6-075

Exit codes: 0 pass, 1 failed check, 3 blocked (no browser or no playwright).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LINES = 4000

JS_IMPORT = r"""
async (path) => {
  const res = await fetch(path);
  const buf = await res.arrayBuffer();
  const file = new File([buf], 'live-follow.log', {lastModified: 1767225600000});
  const tasks = window.__r6.queue.add([file]);
  window.__r6.panel.onFilesChosen(tasks.length);
  return {tasks: tasks.length};
}
"""

JS_OPEN_FIRST = r"""
async () => {
  const value = await window.__r6.provider.listFiles();
  const file = (value.files || [])[0];
  if (!file) return null;
  await window.__r6.openFile(file);
  return {fileId: window.__r6.state.fileId, total: window.__r6.state.total};
}
"""

JS_LIVE_STATE = "() => window.__r6.live() ? window.__r6.live().state() : null"


def start_server():
    from server.app import Application, Config
    from server.httpd import Server

    tmp = tempfile.mkdtemp(prefix='lb-live-')
    data = Path(tmp) / 'data'
    state = Path(tmp) / 'var'
    data.mkdir()
    state.mkdir()
    config = Config(ROOT, data_dir=data, state_dir=state, code_root=ROOT,
                    allowed_hosts=['127.0.0.1', 'localhost'],
                    bootstrap_admins=['127.0.0.1'])
    app = Application(config)
    app.index_jobs.executor = None
    server = Server(('127.0.0.1', 0), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return app, server, server.server_address[1], tmp


def write_fixture(directory: Path) -> Path:
    path = directory / 'live-follow.log'
    stamp = 1767225600
    rows = []
    for index in range(LINES):
        seconds = stamp + index
        clock = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(seconds))
        rows.append('%s.000 INFO [qa] worker: record %d ok\n' % (clock, index))
    path.write_text(''.join(rows), encoding='utf-8')
    return path


def wait_applied(page, target, timeout=5.0):
    """Poll the client's applied counter; returns elapsed milliseconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = page.evaluate(JS_LIVE_STATE)
        if state and state['applied'] >= target:
            return True
        time.sleep(0.005)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / 'R6-075'))
    parser.add_argument('--headful', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('BLOCKED: playwright is not installed')
        return 3

    os.environ['LOGBENCH_DEV_ASSETS'] = '1'
    app, server, port, tmp = start_server()
    base = 'http://127.0.0.1:%d' % port
    fixture_dir = Path(tmp) / 'fixtures'
    fixture_dir.mkdir()
    fixture = write_fixture(fixture_dir)

    report = {'url': base, 'fixture': {'lines': LINES, 'bytes': fixture.stat().st_size},
              'checks': [], 'failures': [], 'latency_ms': [], 'started_at':
              time.strftime('%Y-%m-%dT%H:%M:%S')}

    def check(name, ok, detail=None):
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['failures'].append({'name': name, 'detail': detail})
        return ok

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=not args.headful,
                                                 args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 1366, 'height': 768})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/fixtures/*', lambda route: route.fulfill(
                path=str(fixture_dir / route.request.url.split('/')[-1])))
            page.goto(base + '/r6', wait_until='load', timeout=30000)
            deadline = time.time() + 30
            while time.time() < deadline:
                if page.evaluate('() => Boolean(window.__r6Ready)'):
                    break
                time.sleep(0.05)

            # ---------------------------------------------------------- stream up
            connected_at = time.time()
            state = page.evaluate(JS_LIVE_STATE)
            for _ in range(200):
                state = page.evaluate(JS_LIVE_STATE)
                if state and state['mode'] == 'live':
                    break
                time.sleep(0.01)
            report['stream'] = {'mode': state['mode'] if state else None,
                                'label': state['label'] if state else None,
                                'connect_ms': round((time.time() - connected_at) * 1000, 1)}
            check('SSE 建流成功，界面写明“实时”', bool(state) and state['mode'] == 'live',
                  report['stream'])

            # -------------------------------------------------------- a real file
            page.evaluate(JS_IMPORT, '/fixtures/live-follow.log')
            page.wait_for_function(
                "() => window.__r6.queue.tasks().every(t => ['indexed','failed','cancelled']"
                ".includes(t.phase))", timeout=180000)
            opened = page.evaluate(JS_OPEN_FIRST)
            check('本机文件已打开并有可滚动记录', bool(opened) and opened['total'] >= 1000, opened)
            file_id = opened['fileId'] if opened else 'unknown'

            # ------------------------------------------- event reaches the reader
            page.evaluate("() => { const r = document.getElementById('reader'); r.scrollTop = r.scrollHeight; }")
            page.wait_for_timeout(200)
            before = page.evaluate(JS_LIVE_STATE)['applied']
            published = time.time()
            app.events.publish(kind='archive_commit',
                              payload={'fileId': file_id, 'sourceId': 'qa', 'generation': 1,
                                       'offset': 10, 'committed': True})
            arrived = wait_applied(page, before + 1)
            elapsed = round((time.time() - published) * 1000, 1)
            report['latency_ms'].append(elapsed)
            check('服务端事件经真实 SSE 到达页面', arrived, {'ms': elapsed})
            check('跟随模式打开时不出常驻横幅',
                  page.evaluate("() => document.getElementById('new-records').hidden"))

            # --------------------------------------------------- scroll up pauses
            page.evaluate("() => { const r = document.getElementById('reader'); r.scrollTop = 0; "
                          "r.dispatchEvent(new Event('scroll')); }")
            page.wait_for_timeout(150)
            anchor_before = page.evaluate("() => document.getElementById('reader').scrollTop")
            for _ in range(3):
                app.events.publish(kind='index_commit',
                                   payload={'fileId': file_id, 'sourceId': 'qa', 'generation': 1,
                                            'state': 'indexed', 'records': 12})
            wait_applied(page, before + 4)
            page.wait_for_timeout(120)
            badge = page.evaluate("""() => {
                const el = document.getElementById('new-records');
                return {hidden: el.hidden, text: el.textContent.trim(),
                        count: document.getElementById('new-count').textContent};
            }""")
            anchor_after = page.evaluate("() => document.getElementById('reader').scrollTop")
            report['badge'] = badge
            report['anchor'] = {'before': anchor_before, 'after': anchor_after}
            check('上滚后新记录进徽标而不是拉回底部',
                  badge['hidden'] is False and badge['count'] == '3', badge)
            check('上滚期间阅读位置不变', abs(anchor_after - anchor_before) <= 2, report['anchor'])

            # ------------------------------------------------- click seek to tail
            try:
                page.click('#new-records', timeout=5000)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                report.setdefault('click_error', repr(exc)[:300])
            page.wait_for_timeout(400)
            tail = page.evaluate("""() => {
                const r = document.getElementById('reader');
                return {hidden: document.getElementById('new-records').hidden,
                        atBottom: (r.scrollHeight - r.scrollTop - r.clientHeight) < 40};
            }""")
            report['seek'] = tail
            check('点击徽标回到最新已索引位置', tail['hidden'] and tail['atBottom'], tail)

            # ------------------------------------------------ reconnect: no dupes
            applied_before = page.evaluate(JS_LIVE_STATE)['applied']
            page.evaluate("() => { window.__r6.live().stop('qa'); window.__r6.live().start(); }")
            time.sleep(0.6)
            after_reconnect = page.evaluate(JS_LIVE_STATE)
            report['reconnect'] = {'applied_before': applied_before,
                                   'applied_after': after_reconnect['applied'],
                                   'duplicates': after_reconnect['duplicates'],
                                   'mode': after_reconnect['mode']}
            check('重连回放不重复应用同一事件',
                  after_reconnect['applied'] == applied_before, report['reconnect'])
            check('重连后仍在实时模式', after_reconnect['mode'] == 'live',
                  report['reconnect']['mode'])

            # -------------------------------------------------------- screenshots
            page.screenshot(path=str(out / 'live-follow-1366.png'))
            page.set_viewport_size({'width': 375, 'height': 812})
            page.wait_for_timeout(200)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            page.screenshot(path=str(out / 'live-follow-375.png'))
            report['narrow'] = {'overflow_px': overflow}
            check('375px 不出现横向溢出', overflow <= 1, report['narrow'])
            page.set_viewport_size({'width': 1366, 'height': 768})

            # --------------------------------------------- closing the tab only
            # A reconnect can leave the previous generator waiting for its next
            # heartbeat, so settle first and then measure the release.
            settle_deadline = time.time() + 25
            while time.time() < settle_deadline and app.events.snapshot()['subscribers'] > 1:
                time.sleep(0.25)
            report['subscribers_while_open'] = app.events.snapshot()['subscribers']
            page.close()
            closed_at = time.time()
            release_deadline = closed_at + 25
            while time.time() < release_deadline and app.events.snapshot()['subscribers'] > 0:
                time.sleep(0.25)
            report['subscribers_after_close'] = app.events.snapshot()['subscribers']
            report['release_seconds'] = round(time.time() - closed_at, 2)
            check('关闭页面后订阅被释放（最长一个心跳周期）',
                  report['subscribers_after_close'] == 0,
                  {'while_open': report['subscribers_while_open'],
                   'after_close': report['subscribers_after_close'],
                   'release_seconds': report['release_seconds']})
            published_after_close = app.events.publish(
                kind='task_progress', payload={'sourceId': 'qa', 'state': 'indexed'})
            check('关闭页面后服务端仍可继续发布（采集不受浏览器影响）',
                  published_after_close['id'] > 0, published_after_close['id'])

            # ------------------------------------------------- polling fallback
            context2 = browser.new_context(viewport={'width': 1366, 'height': 768})
            context2.add_init_script("Object.defineProperty(window, 'EventSource', "
                                     "{value: undefined, configurable: true});")
            page2 = context2.new_page()
            page2.goto(base + '/r6', wait_until='load', timeout=30000)
            deadline = time.time() + 30
            while time.time() < deadline:
                if page2.evaluate('() => Boolean(window.__r6Ready)'):
                    break
                time.sleep(0.05)
            for _ in range(300):
                state2 = page2.evaluate(JS_LIVE_STATE)
                if state2 and state2['mode'] in ('polling', 'offline'):
                    break
                time.sleep(0.01)
            report['polling'] = {'mode': state2['mode'] if state2 else None,
                                 'label': state2['label'] if state2 else None}
            check('没有 EventSource 时降级为“轮询”并写明', bool(state2) and
                  state2['mode'] == 'polling' and state2['label'] == '轮询',
                  report['polling'])
            before2 = page2.evaluate(JS_LIVE_STATE)['applied'] if state2 else 0
            app.events.publish(kind='source_error',
                               payload={'sourceId': 'qa', 'code': 'auth_failed', 'mapping': 'm'})
            polled = wait_applied(page2, before2 + 1, timeout=12.0)
            report['polling']['delivered'] = polled
            check('轮询同样收到事件（同一应用路径）', polled, report['polling'])
            page2.screenshot(path=str(out / 'live-follow-polling.png'))
            context2.close()
            browser.close()

            report['page_errors'] = errors
            check('页面无未捕获异常', not errors, errors[:3])
    except Exception as exc:  # noqa: BLE001 - evidence is written even on a crash
        report['failures'].append({'name': 'unhandled', 'detail': repr(exc)[:500]})
    finally:
        server.shutdown()

    latencies = report['latency_ms']
    report['latency_summary'] = {
        'n': len(latencies),
        'p50': round(statistics.median(latencies), 1) if latencies else None,
        'max': round(max(latencies), 1) if latencies else None,
        'note': '事件发布到客户端应用的时间，含浏览器事件循环与一次 IPC；不是目标值，是实测值',
    }
    report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    report['passed'] = not report['failures']
    (out / 'live-follow.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']),
                      'failures': report['failures'],
                      'latency': report['latency_summary']}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
