"""Optional native HTTP + persistent Chromium profile test. Never overrides policy.
Run where browser navigation to localhost is allowed. Requires Playwright/Chromium
only on the test machine, not on the deployment server. Exit 77 means environment
blocked, not pass. Creates a temporary isolated service/database/browser profile.
"""
from pathlib import Path
import argparse,json,os,shutil,sys,tempfile,threading,traceback
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server.app import Application,Config
from server.httpd import Server

def main():
 from playwright.sync_api import sync_playwright
 ap=argparse.ArgumentParser();ap.add_argument('--output',default=str(ROOT/'qa/native-persistence-results.json'));a=ap.parse_args();checks=[];report={'status':'not-run','checks':checks}
 with tempfile.TemporaryDirectory() as tmp:
  home=Path(tmp);app=Application(Config(home/'service',code_root=ROOT,bootstrap_admins=['127.0.0.1']));http=Server(('127.0.0.1',0),app);threading.Thread(target=http.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{http.server_port}'
  try:
   with sync_playwright() as p:
    def launch():return p.chromium.launch_persistent_context(str(home/'browser-profile'),executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
    def ready(page):page.goto(url,wait_until='domcontentloaded');page.wait_for_function('window.LogBench?.state.restoreReady');page.evaluate('async()=>await LogBench.ready')
    ctx=launch();pg=ctx.new_page();ready(pg)
    text='[2026-10-26 11:39:15.12345678] [EVENT] [APP-A] [1] [2] [test.py:3] [100] [0:0:0] read() value: 19\n'
    pg.locator('#file-input').set_input_files({'name':'durable.log','mimeType':'text/plain','buffer':text.encode()});pg.wait_for_function('LogBench.storage.fileSavedAt!==null && LogBench.storage.unsaved.size===0')
    id=pg.evaluate('LogBench.state.file');ns=pg.evaluate('LB_SESSION.user.id');pg.locator('#display-btn').click();pg.locator('[data-visible-column="logger"]').uncheck();pg.locator('[data-act="close-pop"]').last.click();pg.evaluate('LogBench.flushState()');pg.wait_for_timeout(300)
    pg.reload();pg.wait_for_function('window.LogBench?.state.restoreReady');pg.evaluate('async()=>await LogBench.ready')
    assert pg.evaluate('(id)=>LogBench.getFiles().some(f=>f.id===id&&f.text.includes("value: 19"))',id)
    assert pg.evaluate('LogBench.state.columns.logger===false');checks.append('refresh restores native IndexedDB file and hidden column')
    ctx.close();ctx=launch();pg=ctx.new_page();ready(pg)
    assert pg.evaluate('(id)=>LogBench.getFiles().some(f=>f.id===id)',id);assert pg.evaluate('LB_SESSION.user.id')==ns;assert pg.evaluate('LogBench.state.columns.logger===false');checks.append('browser profile restart restores file, namespace and settings');ctx.close();report['status']='passed'
  except Exception as e:
   report['status']='blocked' if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(e) else 'failed';report['error']=str(e);traceback.print_exc()
  finally:http.shutdown();http.server_close()
 Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));return 0 if report['status']=='passed' else 77 if report['status']=='blocked' else 1
if __name__=='__main__':raise SystemExit(main())
