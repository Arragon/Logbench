import json
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent
HTML = OUT / 'r6-restored.html'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
errors, warns = [], []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True)
    page = browser.new_page(viewport={'width': 1366, 'height': 768})
    page.on('pageerror', lambda e: errors.append('PAGEERROR ' + str(e)))
    page.on('console', lambda m: (errors if m.type == 'error' else warns).append(m.type.upper() + ' ' + m.text[:220]) if m.type in ('error', 'warning') else None)
    page.goto(HTML.as_uri(), wait_until='load')
    page.wait_for_timeout(1200)

    def js(expr):
        return page.evaluate(expr)

    report = {}
    report['boot'] = js("({app:!!document.querySelector('#app'), children:document.querySelector('#app').children.length, lb:typeof window.LB, r6:typeof window.__r6, view:window.__r6&&window.__r6.state.view})")
    report['shell'] = js("""(() => {
      const q=s=>document.querySelector(s); const b=s=>{const e=q(s);return e?Math.round(e.getBoundingClientRect().height):null};
      const app=q('#app'); const cs=getComputedStyle(app);
      return {gridRows:cs.gridTemplateRows, gridCols:cs.gridTemplateColumns, display:cs.display,
        header:b('.lb-header'), sidebar:Math.round((q('.sidebar')||{getBoundingClientRect:()=>({width:0})}).getBoundingClientRect().width),
        r6head:b('.r6-head'), r6tabs:b('.r6-tabs'), r6toolbar:b('.r6-toolbar'), status:b('.app-status'),
        dataArea:Math.round((q('.r6-pane')||{getBoundingClientRect:()=>({height:0})}).getBoundingClientRect().height),
        hasFoot:!!q('.r6-foot'), hasTable:!!q('.r6-table'), hasStatusSelect:!!q('#lb-scenario'),
        statusText:(q('.app-status .row')||{}).textContent};})()""")

    # 四 tab
    tabs = {}
    for t in ['sources', 'storage', 'cache', 'events']:
        js("t => { const b=document.querySelector('[data-r6=\"tab\"][data-id=\"'+t+'\"]'); if(b) b.click(); }", t) if False else None
        page.evaluate("t => { const b=document.querySelector('[data-r6=\"tab\"][data-id=\"'+t+'\"]'); if(b) b.click(); }", t)
        page.wait_for_timeout(120)
        tabs[t] = js("({active:document.querySelector('.r6-tab.active')?.dataset.id, table:!!document.querySelector('.r6-table'), list:!!document.querySelector('.r6-log-list'), rows:document.querySelectorAll('.r6-table tbody tr').length})")
    report['tabs'] = tabs
    page.evaluate("() => document.querySelector('[data-r6=\"tab\"][data-id=\"sources\"]')?.click()")
    page.wait_for_timeout(120)

    # 六视图
    views = {}
    for v in ['sync', 'logs', 'files', 'rules', 'admin', 'compare']:
        page.evaluate("v => window.__r6.showView(v)", v)
        page.wait_for_timeout(160)
        views[v] = js("({view:window.__r6.state.view, appChildren:document.querySelector('#app').children.length, hasStatus:!!document.querySelector('.app-status'), hasToolbar:!!document.querySelector('.toolbar, .r6-toolbar'), textLen:document.querySelector('main.main')?.textContent.trim().length||0})")
    report['views'] = views

    # 回到 sync + 状态检视器
    page.evaluate("() => window.__r6.showView('sync')")
    page.wait_for_timeout(160)
    states = {}
    for s in ['loading', 'empty', 'no-result', 'error', 'forbidden', 'notfound', 'conflict', 'saving', 'saved', 'rebuilding', 'offline', 'ready']:
        page.evaluate("s => window.LB.setScenario(s)", s)
        page.wait_for_timeout(60)
        states[s] = js("({banner:!!document.querySelector('#lb-state'), len:(document.querySelector('#lb-state')||{}).textContent?.trim().length||0, status:document.querySelector('.app-status .row')?.textContent?.trim(), tableHidden:document.querySelector('.r6-table-scroll')?.hidden})")
    report['states'] = states

    # 对话框族
    dialogs = {}
    cases = [
        ('detail', "document.querySelector('.r6-table tbody [data-r6=\"source-detail\"]')?.click()"),
        ('wizard', "document.querySelector('[data-r6=\"new-source\"]')?.click()"),
        ('tuning', "document.querySelector('[data-r6=\"tuning\"]')?.click()"),
        ('datalink', "document.querySelector('[data-r6=\"datalink\"]')?.click()"),
        ('backup', "document.querySelector('[data-r6=\"backup\"]')?.click()"),
    ]
    for name, expr in cases:
        page.evaluate("() => { const d=document.getElementById('dialog'); if(d&&d.open)d.close(); }")
        page.evaluate("e => { (0,eval)(e); }", expr)
        page.wait_for_timeout(140)
        dialogs[name] = js("({open:document.getElementById('dialog')?.open, title:document.querySelector('#dialog .dialog-head h2')?.textContent, body:document.querySelector('#dialog .dialog-body')?.textContent.trim().length||0, drawer:!!document.querySelector('.r6-drawer')})")
    report['dialogs'] = dialogs
    page.evaluate("() => { const d=document.getElementById('dialog'); if(d&&d.open)d.close(); }")

    # 可见文案扫描
    report['copy'] = js("""(() => {
      const t=document.body.innerText; const bad=['合成','示例','演示','交互原型','设计原型','不连接远程服务','未连接后端'];
      return {len:t.length, hits:bad.filter(k=>t.includes(k))};})()""")

    # 截图（回到默认 sources tab）
    page.evaluate("""() => {
      window.__r6.showView('sync');
      document.querySelector('[data-r6="tab"][data-id="sources"]')?.click();
      window.LB.setScenario('ready');
    }""")
    page.wait_for_timeout(250)
    report['tableWidth'] = js("Math.round(document.querySelector('.r6-table-scroll').getBoundingClientRect().width)")
    page.screenshot(path=str(OUT / 'preview-1366.png'), full_page=False)
    page.evaluate("() => window.LB.setScenario('conflict')")
    page.wait_for_timeout(150)
    report['tableWidth409'] = js("Math.round(document.querySelector('.r6-table-scroll').getBoundingClientRect().width)")
    page.screenshot(path=str(OUT / 'preview-409.png'), full_page=False)
    page.evaluate("() => window.LB.setScenario('ready')")
    page.set_viewport_size({'width': 375, 'height': 812})
    page.wait_for_timeout(250)
    report['narrow'] = js("({main:Math.round(document.querySelector('main.main')?.getBoundingClientRect().height||0), scrollW:document.documentElement.scrollWidth, clientW:document.documentElement.clientWidth})")
    page.screenshot(path=str(OUT / 'preview-375.png'), full_page=False)

    browser.close()

print(json.dumps(report, ensure_ascii=False, indent=2))
print('\n=== ERRORS (%d) ===' % len(errors))
for e in errors[:20]:
    print(' ', e)
print('=== WARNINGS (%d) ===' % len(warns))
for w in warns[:10]:
    print(' ', w)
