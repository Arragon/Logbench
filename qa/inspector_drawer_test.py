"""Floating detail drawer regression, using real Chromium + actual HTTP service through bridge.
Does not verify native HTTP navigation or browser persistence; can run in restricted CI.
"""
import base64
import http.client
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.app import Application, Config
from server.httpd import Server


def make_line(i):
    return f'[2026-10-26 11:43:{i % 60:02}.12345678] [EVENT] [APP-A] [1001] [2001] [rpc_client.py:208] [100] [0:0:0] invoke() <TAG_A> step {i} value={i}'


def test():
    temp = tempfile.TemporaryDirectory()
    home = Path(temp.name)
    (home / 'data').mkdir()
    (home / 'data' / 'sample.log').write_text('\n'.join(make_line(i) for i in range(400)), encoding='utf-8')
    app = Application(Config(home, code_root=ROOT, bootstrap_admins=['127.0.0.1']))
    srv = Server(('127.0.0.1', 0), app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    base = 'http://127.0.0.1:' + str(port)

    def request(path, method='GET', body=None, headers=None):
        h = dict(headers or {})
        h['Host'] = '127.0.0.1:' + str(port)
        if method not in ('GET', 'HEAD'):
            h['Origin'] = base
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        conn.request(method, path, body, h)
        r = conn.getresponse()
        data = r.read()
        result = {'status': r.status, 'headers': dict(r.getheaders()), 'base64': base64.b64encode(data).decode()}
        conn.close()
        return result

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.set_default_timeout(12000)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            html = base64.b64decode(request('/')['base64']).decode()
            html = html.replace('<script src="/assets/boot.js" defer></script>', '')
            html = html.replace('<link rel="stylesheet" href="/assets/style.css">', '')
            html = html.replace('<link rel="stylesheet" href="/assets/service.css">', '')
            page.set_content(html)
            for asset in ('style.css', 'service.css'):
                page.add_style_tag(content=base64.b64decode(request('/assets/' + asset)['base64']).decode())
            page.expose_function('actualHTTP', lambda data: request(data['path'], data.get('method', 'GET'), data.get('body'), data.get('headers')))
            page.evaluate("""()=>{window.fetch=async(path,o={})=>{const x=await actualHTTP({path:String(path),method:o.method||'GET',headers:o.headers||{},body:o.body});return new Response(Uint8Array.from(atob(x.base64),c=>c.charCodeAt(0)),{status:x.status,headers:x.headers})}}""")
            me = json.loads(base64.b64decode(request('/api/v1/me')['base64']))
            page.evaluate('(v) => window.LB_SESSION = v', me)
            page.add_script_tag(content=base64.b64decode(request('/assets/workbench.js')['base64']).decode())
            page.wait_for_function('window.LogBench?.state.restoreReady')
            page.evaluate('async() => await LogBench.ready')
            page.locator('[data-act="nav"][data-value="files"]').first.click()
            page.locator('[data-act="open-file"]').first.click()
            page.wait_for_function('LogBench.filtered().length===400')

            def stats():
                return page.evaluate("""()=>{
                    const log = document.querySelector('.log-column'), timeline=document.querySelector('.timeline'), sc=document.querySelector('#log-scroll');
                    const lg=log.getBoundingClientRect(), tl=timeline?.getBoundingClientRect();
                    return {x:lg.x,width:lg.width, tlWidth:tl?.width, tlVisible:!!tl?.height,scrollTop:sc.scrollTop};
                }""")

            page.locator('#log-scroll').evaluate('(e)=>e.scrollTop=1300')
            page.wait_for_timeout(150)
            before=stats()
            row_id = page.evaluate("""()=>{const sc=document.querySelector('#log-scroll'),b=sc.getBoundingClientRect();const rs=[...sc.querySelectorAll('[data-record]')];return rs.find(r=>{const x=r.getBoundingClientRect();return x.top>b.top+100&&x.bottom<b.bottom-100})?.dataset.record}""")
            assert row_id, 'no record fully visible in reader'
            row = page.locator(f'#log-scroll [data-record="{row_id}"]')
            rect = row.bounding_box()
            row_id = row.get_attribute('data-record')
            row.dblclick()
            page.wait_for_selector('.inspector')
            after=stats()
            drawer=page.locator('.inspector').bounding_box()
            print('before', before, 'after', after, 'drawer',drawer,flush=True)
            assert abs(after['width']-before['width']) <= 1, 'drawer shrunk log viewport'
            assert abs(after['x']-before['x']) <= 1, 'drawer shifted log viewport'
            assert before['tlVisible'] and after['tlVisible'], 'opening drawer hid timeline'
            assert abs(after['tlWidth']-before['tlWidth']) <= 1, 'timeline changed width'
            assert drawer['x'] > after['x'] + 100, 'drawer is not floating over log panel'
            selected = page.locator(f'#log-scroll [data-record="{row_id}"]').first
            assert selected.count() == 1, 'focused record lost on rerender'
            row_after = selected.bounding_box()
            assert abs(rect['y'] - row_after['y']) <= 3, f'record jumped vertically {rect["y"]} -> {row_after["y"]}'
            assert page.locator('.inspector [aria-label="关闭记录详情"]').count() == 1
            page.locator('.inspector [data-act="inspector-tab"][data-value="raw"]').click()
            assert page.locator('.inspector').count() == 1
            assert abs(stats()['width'] - before['width']) <= 1, 'raw tab moved viewport'
            page.locator('.inspector [data-act="inspector-tab"][data-value="fields"]').click()
            assert abs(stats()['width'] - before['width']) <= 1
            page.screenshot(path=str(ROOT/'qa/drawer-1440.png'))
            page.locator('.inspector [aria-label="关闭记录详情"]').click()
            assert page.locator('.inspector').count() == 0
            assert abs(stats()['width']-before['width']) <= 1
            # On a narrow screen the drawer must stay inside viewport, not change log table width.
            page.set_viewport_size({'width': 1024, 'height': 768})
            page.wait_for_timeout(100)
            log_before=stats()
            row_id = page.evaluate("""()=>{const sc=document.querySelector('#log-scroll'),b=sc.getBoundingClientRect();return [...sc.querySelectorAll('[data-record]')].find(r=>{const x=r.getBoundingClientRect();return x.top>b.top+60&&x.bottom<b.bottom-80})?.dataset.record}""")
            assert row_id, 'no visible row in narrow viewport'
            row=page.locator(f'#log-scroll [data-record="{row_id}"]')
            row.dblclick()
            narrow=stats(); bounds=page.locator('.inspector').bounding_box()
            assert abs(narrow['width']-log_before['width']) <= 1
            assert bounds['x'] + bounds['width'] <= 1024
            page.screenshot(path=str(ROOT/'qa/drawer-1024.png'))
            page.keyboard.press('Escape')
            assert page.locator('.inspector').count() == 0, 'Escape failed to close drawer'
            page.set_viewport_size({'width':390,'height':844})
            page.wait_for_timeout(100)
            mobile_before=stats()
            rid=page.evaluate("""()=>{const sc=document.querySelector('#log-scroll'),b=sc.getBoundingClientRect();return [...sc.querySelectorAll('[data-record]')].find(r=>{const x=r.getBoundingClientRect();return x.top>b.top+45&&x.bottom<b.bottom-60})?.dataset.record}""")
            assert rid, 'mobile record missing'
            page.locator(f'#log-scroll [data-record="{rid}"]').dblclick()
            mobile_drawer=page.locator('.inspector').bounding_box()
            assert mobile_drawer['x']>=0 and mobile_drawer['x']+mobile_drawer['width']<=390, 'mobile drawer exceeds viewport'
            assert abs(stats()['width']-mobile_before['width'])<=1, 'mobile drawer shifted viewport'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'page horizontal overflow'
            page.screenshot(path=str(ROOT/'qa/drawer-390.png'))
            page.locator('.inspector [aria-label="关闭记录详情"]').click()
            assert not errors, errors
            print('PASS: inspector overlay does not displace log/timeline; anchor and close behavior verified')
            browser.close()
    finally:
        srv.shutdown(); srv.server_close(); temp.cleanup()

if __name__ == '__main__':
    test()
