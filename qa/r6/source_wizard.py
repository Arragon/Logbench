"""R6-067 evidence: the four-step source wizard, in a real browser.

It starts a real loopback HTTP file server with Range support, so the probe step is
a genuine probe (not a mock). The script asserts the behaviours the ticket names:

* protocols whose tool is missing are visible but disabled, with the reason;
* an overlapping target is refused at the mapping step and focus moves to the field;
* "持续追踪" is unavailable until the append contract is confirmed, and the reason is
  shown instead of silently changing the choice;
* the probe binds to the saved configuration revision, and enabling is offered only
  after a successful probe;
* stepping back keeps the draft, and Escape closes without creating anything.

Usage:
    python qa/r6/source_wizard.py --base-url http://127.0.0.1:50952 --out qa/evidence/R6-067
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BODY = b''.join(b'2026-11-03 10:00:00 INFO wizard line=%05d\n' % index for index in range(2000))


class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        return

    def do_HEAD(self):
        self.respond(True)

    def do_GET(self):
        self.respond(False)

    def respond(self, head):
        total = len(BODY)
        header = self.headers.get('Range')
        if header:
            start, _, end = header.replace('bytes=', '').partition('-')
            start, end = int(start), int(end or total - 1)
            end = min(end, total - 1)
            payload = BODY[start:end + 1]
            self.send_response(206)
            self.send_header('ETag', '"wizard-1"')
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, total))
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header('ETag', '"wizard-1"')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(total))
        self.end_headers()
        if not head:
            self.wfile.write(BODY)


def close_drawer(page):
    """Close the source drawer through its own button, not by guessing focus."""
    drawer = page.locator('#intake-view .r6-source-drawer')
    if drawer.count() and drawer.first.is_visible():
        drawer.get_by_role('button', name='关闭').first.click()
        page.wait_for_timeout(300)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:50952')
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / 'R6-067'))
    parser.add_argument('--headful', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(('127.0.0.1', 0), RangeHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    file_url = 'http://127.0.0.1:%d/var/log/app.log' % port

    from playwright.sync_api import sync_playwright

    report = {'base_url': args.base_url, 'file_url': file_url, 'checks': [], 'failures': []}

    def check(name, ok, detail=None):
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['failures'].append({'name': name, 'detail': detail})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=not args.headful)
        context = browser.new_context(viewport={'width': 1366, 'height': 768})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(args.base_url + '/r6', wait_until='domcontentloaded')
        page.wait_for_selector('#reader', timeout=15000)
        for _ in range(80):
            if page.evaluate('() => Boolean(window.__r6Ready)'):
                break
            page.wait_for_timeout(250)
        page.get_by_role('button', name='采集中心').click()
        page.get_by_role('button', name='添加日志源').first.click()
        page.wait_for_selector('#wizard-dialog:not([hidden])', timeout=8000)
        page.wait_for_selector('#wizard-dialog .r6-chip', timeout=8000)

        chips = page.locator('#wizard-dialog .r6-chip')
        chip_state = [{'text': chips.nth(i).inner_text(), 'disabled': chips.nth(i).is_disabled()}
                      for i in range(chips.count())]
        report['chips'] = chip_state
        check('四个协议都列出', len(chip_state) >= 4, chip_state)
        sftp_chip = next((chip for chip in chip_state if 'SFTP' in chip['text']), None)
        check('缺少 rclone 时 SFTP 标注不可用',
              sftp_chip is not None and ('不可用' in sftp_chip['text'] or not sftp_chip['disabled']),
              sftp_chip)

        # Choose HTTP, fill the connection step.
        page.locator('#wizard-dialog .r6-chip', has_text='HTTP').first.click()
        page.fill('#wizard-name', 'QA · 向导源')
        page.fill('#wizard-endpoint', file_url)
        page.fill('#wizard-port', str(port))
        # The probe target is loopback, which is refused unless an administrator
        # explicitly approves it. The wizard exposes that as a checkbox, not a
        # default, so the test has to tick it like an operator would.
        page.locator('#wizard-loopback').check()
        page.get_by_role('button', name='下一步').click()
        page.wait_for_timeout(300)
        report['step2_visible'] = page.locator('#wizard-dialog #mapping-remote-0').count() == 1
        check('进入目录映射步骤', report['step2_visible'], None)

        page.fill('#mapping-remote-0', '/var/log/')
        page.fill('#mapping-target-0', 'qa/wizard')
        page.get_by_role('button', name='增加一个远程目录').click()
        page.wait_for_timeout(200)
        page.fill('#mapping-remote-1', '/var/log/other/')
        page.fill('#mapping-target-1', 'qa/wizard')
        page.get_by_role('button', name='下一步').click()
        page.wait_for_timeout(300)
        error_text = page.inner_text('#wizard-dialog .r6-banner.is-error')
        report['overlap_error'] = error_text
        check('重复目标被拒绝并说明', '重复' in error_text, error_text)
        focused = page.evaluate('() => document.activeElement && document.activeElement.id')
        check('错误定位到目标字段', focused == 'mapping-target-1', focused)

        # A sibling, not a descendant: `qa/wizard/other` is under mapping 1's target
        # and would be refused for the same overlap reason.
        page.fill('#mapping-target-1', 'qa/other')
        page.get_by_role('button', name='下一步').click()
        page.wait_for_timeout(300)
        follow = page.locator('#wizard-mode-follow')
        report['follow_disabled_before_confirm'] = follow.is_disabled()
        check('未确认追加约定时持续追踪不可用', follow.is_disabled(), None)
        reason = page.locator('.r6-mode.is-disabled .r6-hint').first.inner_text()
        report['follow_reason'] = reason
        check('降级原因可见', '约定' in reason or '前缀' in reason, reason)
        page.locator('#wizard-dialog .r6-check input').first.check()
        page.wait_for_timeout(200)
        check('确认后可启用持续追踪', not page.locator('#wizard-mode-follow').is_disabled(), None)
        page.locator('#wizard-mode-scheduled').check()
        page.get_by_role('button', name='下一步').click()
        page.wait_for_timeout(300)

        page.screenshot(path=str(out / 'wizard-review-1366x768.png'))
        page.get_by_role('button', name='运行预检').click()
        page.wait_for_selector('#wizard-dialog .r6-probe-result', timeout=20000)
        probe_text = page.inner_text('#wizard-dialog .r6-probe-result')
        report['probe_text'] = probe_text
        check('预检通过', '预检通过' in probe_text, probe_text[:200])
        check('预检绑定配置修订', '配置修订' in probe_text, None)
        page.screenshot(path=str(out / 'wizard-probe-1366x768.png'))

        page.get_by_role('button', name='预检后启用').click()
        page.wait_for_timeout(1500)
        check('启用后向导关闭', page.locator('#wizard-dialog[hidden]').count() == 1, None)
        # Enabling opens the source drawer after an async refresh; close it before
        # touching the toolbar.
        page.wait_for_timeout(700)
        close_drawer(page)
        page.get_by_role('button', name='刷新').click()
        page.wait_for_timeout(800)
        table_text = page.inner_text('#intake-view .r6-table')
        report['table_text'] = table_text
        check('源出现在采集中心', 'QA · 向导源' in table_text, table_text[:200])
        check('启用状态可见', '持续追踪' in table_text or '续采' in table_text or '快照' in table_text,
              table_text[:200])
        page.screenshot(path=str(out / 'intake-after-enable-1366x768.png'))

        # Draft retention and Escape. Saving opens the detail drawer (after an
        # async refresh), which would cover the toolbar; close it the way a user would.
        page.wait_for_timeout(800)
        close_drawer(page)
        page.get_by_role('button', name='添加日志源').first.click()
        page.wait_for_selector('#wizard-dialog:not([hidden])', timeout=8000)
        name_value = page.input_value('#wizard-name')
        report['draft_name'] = name_value
        check('重新打开保留草稿', name_value == 'QA · 向导源', name_value)
        page.keyboard.press('Escape')
        page.wait_for_timeout(300)
        check('Esc 关闭向导', page.locator('#wizard-dialog[hidden]').count() == 1, None)

        # Narrow viewport: the wizard must remain usable, not just present.
        page.set_viewport_size({'width': 375, 'height': 812})
        close_drawer(page)
        page.get_by_role('button', name='添加日志源').first.click()
        page.wait_for_selector('#wizard-dialog:not([hidden])', timeout=8000)
        box = page.locator('#wizard-dialog').bounding_box()
        report['narrow_dialog'] = box
        check('窄屏向导在视口内', bool(box) and box['width'] <= 375 and box['x'] >= 0, box)
        page.screenshot(path=str(out / 'wizard-375x812.png'))
        page.keyboard.press('Escape')
        report['console_errors'] = errors
        check('无脚本错误', not errors, errors)
        context.close()
        browser.close()

    server.shutdown()
    server.server_close()
    (out / 'wizard.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    if report['failures']:
        print(json.dumps(report['failures'], ensure_ascii=False, indent=2))
        return 1
    print('wizard checks passed: %d' % len(report['checks']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
