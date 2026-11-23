#!/usr/bin/env python3
"""Bounded browser acceptance for the wired R6 front end.

Prerequisite: a running server with an admin identity, e.g.
  python3 -B run.py --no-reload --admin-ip 127.0.0.1 --port 50950

Usage:
  python3 qa/r6/frontend_wiring_smoke.py --base-url http://127.0.0.1:50950

Writes qa/evidence/R6-frontend-wiring.json. Failures are recorded, not hidden;
exit code is 1 when any assertion fails. This is a UI wiring smoke test, not a
substitute for backend, permission or performance acceptance (Linear R6 project).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'qa' / 'evidence' / 'R6-frontend-wiring.json'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-url', default='http://127.0.0.1:50004')
    ap.add_argument('--chrome', default=CHROME)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    result = {
        'at': datetime.now(timezone.utc).isoformat(),
        'base_url': args.base_url,
        'checks': {},
        'failures': [],
        'page_errors': [],
        'notes': []
    }

    def check(name, ok, detail=None):
        result['checks'][name] = {'ok': bool(ok), 'detail': detail}
        if not ok:
            result['failures'].append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chrome, headless=True,
                                    args=['--no-proxy-server'])
        page = browser.new_page(viewport={'width': 1366, 'height': 768})
        page.on('pageerror', lambda e: result['page_errors'].append(str(e)[:300]))
        page.goto(args.base_url.rstrip('/') + '/r6', wait_until='domcontentloaded', timeout=30000)
        # Wait for the real boot to finish, not a fixed sleep: a slow machine
        # otherwise samples the pre-boot frame and reports fixture geometry.
        try:
            page.wait_for_function(
                "() => window.LB && window.LB.state.scenario !== 'loading'", timeout=20000)
        except Exception as exc:
            result['failures'].append('boot_timeout')
            result['notes'].append('boot did not leave loading state: %s' % exc)
        page.wait_for_timeout(250)

        boot = page.evaluate("""({
            lb: typeof window.LB, r6: typeof window.__r6,
            scenario: window.LB && window.LB.state.scenario,
            admin: window.LB && window.LB.state.admin,
            csrf: !!(window.LB && window.LB.state.csrfToken),
            endpoints: window.LB ? Object.keys(window.LB.ENDPOINTS).length : 0,
            sources: window.__r6 ? window.__r6.intake.sources.length : -1,
            roots: window.__r6 ? window.__r6.intake.roots.length : -1,
            rev2: !!document.getElementById('lb-rev2'),
            calls: window.LB ? window.LB.state.calls.slice(0,10).map(c=>c.method+' '+c.path) : []
        })""")
        result['boot'] = boot
        check('adapter_loaded', boot['lb'] == 'object' and boot['r6'] == 'object')
        check('identity_and_csrf', boot['admin'] is True and boot['csrf'] is True)
        check('real_endpoints', boot['endpoints'] >= 40, boot['endpoints'])
        check('boot_from_api', any('/api/v2/intake' in c for c in boot['calls']), boot['calls'][:3])
        check('rev2_entries', boot['rev2'])
        check('no_demo_copy', not page.evaluate(
            "['合成','示例','演示','交互原型','设计原型'].some(k=>document.body.innerText.includes(k))"))
        seed = page.evaluate("""({ids:window.__r6.intake.sources.map(s=>s.id),
            fixture:['psg-a','psg-b','build','legacy'].filter(k=>window.__r6.intake.sources.some(s=>s.id===k))})""")
        result['seed_check'] = seed
        check('no_fixture_sources', not seed['fixture'], seed)

        geo = page.evaluate("""(()=>{const q=s=>document.querySelector(s);
          const h=s=>Math.round(q(s)?.getBoundingClientRect().height||0);
          return {rows:getComputedStyle(q('#app')).gridTemplateRows,
            cols:getComputedStyle(q('#app')).gridTemplateColumns,
            header:h('.lb-header'),head:h('.r6-head'),tabs:h('.r6-tabs'),
            toolbar:h('.r6-toolbar'),status:h('.app-status'),pane:h('.r6-pane'),
            foot:!!q('.r6-foot')};})()""")
        result['geometry'] = geo
        check('design_baseline', geo['rows'].startswith('52px') and geo['cols'].startswith('208px')
              and geo['header'] == 52 and geo['head'] == 50 and geo['tabs'] == 39
              and geo['toolbar'] == 49 and geo['status'] == 22 and not geo['foot'], geo)
        check('data_area', geo['pane'] >= 550, geo['pane'])

        # Real state machine: pausing a source that has not been probed/enabled
        # must surface the backend's source_disabled precondition, not a fake 409.
        if page.query_selector('.r6-table tbody [data-r6="pause"]'):
            page.evaluate("()=>document.querySelector('.r6-table tbody [data-r6=\"pause\"]').click()")
            page.wait_for_timeout(1300)
            sm = page.evaluate("""({scenario:window.LB.state.scenario,
                code:window.LB.state.lastCode, msg:window.LB.state.lastError,
                actionSent:window.LB.state.calls.some(c=>c.path.includes('/actions'))})""")
            result['state_machine'] = sm
            # A real source that has not been probed+enabled returns 409
            # source_disabled => 'precondition'. A 404 would mean the UI sent a
            # prototype fixture id, which is the exact regression we guard.
            check('backend_error_code_mapped',
                  sm['actionSent'] and sm['scenario'] in ('precondition', 'conflict', 'ready')
                  and sm['code'] != 'NOT_FOUND', sm)
        else:
            result['notes'].append('No source row available; state-machine write path not exercised.')

        page.evaluate("()=>window.LB.refresh()")
        page.wait_for_timeout(1000)

        # Fault injection: 403 / 409 revision / network loss, all recoverable.
        faults = {}
        for label, code, err_code in [('403', 403, 'forbidden'),
                                      ('409_revision', 409, 'revision_conflict'),
                                      ('offline', 0, '')]:
            body = json.dumps({'error': {'code': err_code, 'message': '注入故障'}}, ensure_ascii=False)

            def make(c, data):
                def handler(route, request):
                    if c == 0:
                        route.abort('failed')
                    else:
                        route.fulfill(status=c, content_type='application/json', body=data)
                return handler

            page.route('**/api/v2/intake', make(code, body))
            page.evaluate("()=>window.LB.refresh()")
            page.wait_for_timeout(650)
            faults[label] = page.evaluate("""({scenario:window.LB.state.scenario,
                banner:!!document.querySelector('#lb-state'),
                status:document.querySelector('.app-status .row')?.textContent.trim()})""")
            page.unroute('**/api/v2/intake')
        result['faults'] = faults
        check('fault_403', faults['403']['scenario'] == 'forbidden' and faults['403']['banner'])
        check('fault_409', faults['409_revision']['scenario'] == 'conflict' and faults['409_revision']['banner'])
        check('fault_offline', faults['offline']['scenario'] == 'offline' and faults['offline']['banner'])

        page.evaluate("()=>window.LB.refresh()")
        page.wait_for_timeout(1100)
        check('recovery', page.evaluate("window.LB.state.scenario==='ready'"))

        # Real write round-trips. A 409 must never leave the UI claiming
        # "已保存"; both endpoints must actually advance their revision.
        pref_before = page.evaluate("window.LB.state.prefRevision")
        page.evaluate("()=>window.LB.savePref('wrap', true)")
        page.wait_for_timeout(700)
        pref = page.evaluate("({rev:window.LB.state.prefRevision,saved:window.LB.state.saved})")
        result['preferences_write'] = pref
        check('preferences_write', pref['rev'] > pref_before and pref['saved'] == '已保存', pref)

        page.evaluate("()=>document.querySelector('[data-r6=\"tuning\"]')?.click()")
        page.wait_for_timeout(200)
        page.evaluate("()=>document.querySelector('[data-r6=\"save-tuning\"]')?.click()")
        page.wait_for_timeout(900)
        budget = page.evaluate("""({saved:window.LB.state.saved,
            scenario:window.LB.state.scenario,
            err:document.querySelector('#r6-tune-error')?.textContent.trim()||''})""")
        result['budget_write'] = budget
        check('budget_write', budget['saved'] == '已保存' and
              budget['scenario'] == 'saved' and not budget['err'], budget)
        page.evaluate("()=>{const d=document.getElementById('dialog');if(d.open)d.close();}")
        page.evaluate("()=>window.LB.setScenario('ready')")

        views, tabs = {}, {}
        for v in ['sync', 'logs', 'files', 'rules', 'admin', 'compare']:
            page.evaluate("v=>window.__r6.showView(v)", v)
            page.wait_for_timeout(100)
            views[v] = page.evaluate("({view:window.__r6.state.view,len:document.querySelector('main.main')?.textContent.trim().length||0})")
        page.evaluate("()=>window.__r6.showView('sync')")
        page.wait_for_timeout(150)
        for t in ['sources', 'storage', 'cache', 'events']:
            page.evaluate("t=>document.querySelector('[data-r6=\"tab\"][data-id=\"'+t+'\"]')?.click()", t)
            page.wait_for_timeout(100)
            tabs[t] = page.evaluate("!!document.querySelector('.r6-table, .r6-log-list, .r6-empty')")
        result['views'], result['tabs'] = views, tabs
        check('six_views', all(views[v]['view'] == v and views[v]['len'] > 0 for v in views), views)
        check('four_tabs', all(tabs.values()), tabs)

        # rules/admin must be driven by real server data, not prototype locals.
        ra = page.evaluate("""(()=>{
            const a=window.LB.state.adminData||{};
            const serverId=a.rules&&a.rules.items&&a.rules.items[0]&&a.rules.items[0].ruleId;
            const profiles=window.__r6.state.profiles||[];
            return {serverId:serverId||null,
              profiles:profiles.length,
              firstId:profiles[0]&&profiles[0].id,
              users:(a.users&&a.users.items||[]).length,
              audit:(a.audit&&a.audit.items||[]).length};})()""")
        result['rules_admin_data'] = ra
        check('rules_from_api', bool(ra['serverId']) and ra['firstId'] == ra['serverId'], ra)
        page.evaluate("()=>window.__r6.showView('rules')")
        page.wait_for_timeout(150)
        check('rules_editor_renders',
              page.evaluate("document.querySelectorAll('.profile-item').length>0 && !!document.querySelector('.rule-work')"))
        page.evaluate("()=>window.__r6.showView('admin')")
        page.wait_for_timeout(150)
        check('admin_from_api',
              page.evaluate("document.querySelectorAll('.settings-table').length>=2 && document.body.innerText.includes('服务端已连接')"))
        page.evaluate("()=>window.__r6.showView('sync')")
        page.wait_for_timeout(150)

        # Real-time channel: an SSE stream (or an actual polling request) must
        # exist. A client that start()s without injected transports is a no-op.
        evt = page.evaluate("""(()=>{const s=window.LB.eventState&&window.LB.eventState();
            return {mode:s&&s.mode, polls:s&&s.polls, eventMode:window.LB.state.eventMode};})()""")
        result['events_channel'] = evt
        check('sse_or_poll_active',
              evt['mode'] in ('live', 'connecting', 'polling') and
              (evt['polls'] > 0 or evt['mode'] in ('live', 'connecting')), evt)

        page.evaluate("()=>document.querySelector('[data-r6=\"tab\"][data-id=\"sources\"]')?.click()")
        page.wait_for_timeout(200)
        out = EVIDENCE.parent
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ROOT / 'docs' / 'R6' / 'preview' / 'prod-1366.png'))
        page.set_viewport_size({'width': 375, 'height': 812})
        page.wait_for_timeout(300)
        narrow = page.evaluate("({scrollW:document.documentElement.scrollWidth,clientW:document.documentElement.clientWidth,main:Math.round(document.querySelector('main.main')?.getBoundingClientRect().height||0)})")
        result['narrow'] = narrow
        check('narrow_no_overflow', narrow['scrollW'] == narrow['clientW'] and narrow['main'] > 0, narrow)
        page.screenshot(path=str(ROOT / 'docs' / 'R6' / 'preview' / 'prod-375.png'))

        check('no_page_errors', not result['page_errors'], result['page_errors'][:3])
        browser.close()

    result['passed'] = len(result['failures']) == 0
    EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({k: result[k] for k in ('passed', 'failures', 'page_errors', 'boot', 'geometry')},
                     ensure_ascii=False, indent=2))
    print('evidence:', EVIDENCE.relative_to(ROOT))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
