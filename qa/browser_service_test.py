"""Optional end-to-end QA. Runtime has no Playwright dependency.
Native mode visits the actual HTTP origin. --bridge uses set_content plus real TCP
requests for restricted automation environments; it does NOT verify native storage,
CSP, navigation, or downloads. No browser policy is altered.
"""
from pathlib import Path
import argparse,base64,http.client,json,os,shutil,sys,tempfile,threading,time,traceback
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server.app import Application,Config
from server.httpd import Server

def head(msg,n=0,level='EVENT'):
 return f'[2026-10-26 11:{39+n//60:02}:{n%60:02}.12345678] [{level}] [APP-A] [1001] [2001] [rpc_client.py:208] [100] [0:0:0] invoke() <TAG_A> {msg}'
TEXT='\n'.join([head('module start'),head('rpc_success: False, err_code: 0x8001',1),head('Traceback (most recent call last):',2,'ERROR'),'  File "/opt/app/rpc.py", line 25, in invoke','    raise TimeoutError("read timed out")','TimeoutError: read timed out']+[head(f'step={i}, value={i}',i) for i in range(3,95)]+[head('module end',96)])
RESULTS=[]
def check(ok,message):
 if not ok:raise AssertionError(message)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--bridge',action='store_true');ap.add_argument('--output',default=str(ROOT/'qa/browser-service-results.json'));args=ap.parse_args()
 from playwright.sync_api import sync_playwright
 tmp=tempfile.TemporaryDirectory();home=Path(tmp.name);(home/'data/MachineA').mkdir(parents=True);(home/'data/MachineA/A.log').write_text(TEXT,encoding='utf-8');(home/'data/MachineA/B.log').write_text(TEXT.replace('APP-A','APP-B'),encoding='utf-8')
 app=Application(Config(home,code_root=ROOT,bootstrap_admins=['127.0.0.1']))
 http_server=Server(('127.0.0.1',0),app);port=http_server.server_port;base=f'http://127.0.0.1:{port}';thread=threading.Thread(target=http_server.serve_forever,daemon=True);thread.start();traffic=[]
 def request(path,method='GET',body=None,ip='127.0.0.1',headers=None):
  h=dict(headers or {});h['Host']=f'127.0.0.1:{port}'
  if method not in ('GET','HEAD'):h['Origin']=base
  con=http.client.HTTPConnection('127.0.0.1',port,timeout=20,source_address=(ip,0));con.request(method,path,body.encode() if isinstance(body,str) else body,h);res=con.getresponse();data=res.read();traffic.append((method,path,res.status));out={'status':res.status,'headers':dict(res.getheaders()),'base64':base64.b64encode(data).decode()};con.close();return out
 def get(path,ip='127.0.0.1'):return json.loads(base64.b64decode(request(path,ip=ip)['base64']))
 def admin(path,method,data):return request('/api/v1'+path,method,json.dumps(data),headers={'Content-Type':'application/json','X-LogBench-CSRF':get('/api/v1/me')['csrfToken']})
 errors=[]
 try:
  with sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
   def boot(ip='127.0.0.1'):
    pg=browser.new_page(viewport={'width':1440,'height':900});pg.set_default_timeout(5000);pg.on('pageerror',lambda e:errors.append(str(e)))
    if args.bridge:
     html=base64.b64decode(request('/')['base64']).decode().replace('<script src="/assets/boot.js" defer></script>','').replace('<link rel="stylesheet" href="/assets/style.css">','').replace('<link rel="stylesheet" href="/assets/service.css">','')
     pg.set_content(html)
     for name in ['style.css','service.css']:pg.add_style_tag(content=base64.b64decode(request('/assets/'+name)['base64']).decode())
     pg.expose_function('actualHTTP',lambda data:request(data['path'],data.get('method','GET'),data.get('body'),ip,data.get('headers')))
     pg.evaluate("""()=>{window.fetch=async(path,o={})=>{const x=await actualHTTP({path:String(path),method:o.method||'GET',headers:o.headers||{},body:o.body});return new Response(Uint8Array.from(atob(x.base64),c=>c.charCodeAt(0)),{status:x.status,headers:x.headers})}}""")
     pg.evaluate('(v)=>window.LB_SESSION=v',get('/api/v1/me',ip));pg.add_script_tag(content=base64.b64decode(request('/assets/workbench.js')['base64']).decode())
    else:pg.goto(base,wait_until='domcontentloaded')
    pg.wait_for_function('window.LogBench?.state.restoreReady');pg.evaluate('async()=>await LogBench.ready');return pg
   pg=boot()
   def test(name,fn):
    try:fn();RESULTS.append({'name':name,'pass':True});print('PASS',name,flush=True)
    except Exception as e:RESULTS.append({'name':name,'pass':False,'error':str(e)});print('FAIL',name,repr(e),flush=True);raise
   def act(name,extra=''):pg.locator('[data-act="'+name+'"]'+extra).first.click()
   def close():pg.locator('#dialog [data-act="close-dialog"]').last.click()
   def nav(view):act('nav','[data-value="'+view+'"]');pg.wait_for_timeout(100)
   test('initial workspace is empty; metadata is real; no sample injection',lambda:check(pg.evaluate('LogBench.getFiles().length===2 && LogBench.getFiles().every(f=>!f.text) && LogBench.state.opened.length===0'),'metadata eagerly read or seeded demo'))
   test('connected identity comes from backend',lambda:check(pg.evaluate('LB_SESSION.user.role==="admin" && LB_SESSION.user.ip==="127.0.0.1"'),'wrong identity'))
   nav('files');ids=pg.evaluate('LogBench.getFiles().map(f=>f.id)');default_rule=get('/api/v1/rules')['items'][0]['id']
   def read_file():
    act('open-file','[data-id="'+ids[0]+'"]');pg.wait_for_function('LogBench.filtered().length>90');check(any(x[1].startswith('/api/v1/files/content') for x in traffic),'no content HTTP');check(pg.locator('.trace-card').count()==1,'traceback card missing')
   test('server file loads over HTTP and retains R5 traceback card',read_file)
   def columns():
    pg.locator('#display-btn').click();pg.locator('[data-visible-column="logger"]').uncheck();pg.locator('[data-visible-column="pid"]').check();check('软件' not in pg.locator('.virtual-head').inner_text(),'column not hidden');check(pg.evaluate('LogBench.workspaceBackup().workspace.state.columns.logger===false'),'snapshot missing');act('close-pop')
   test('column configuration and workspace serialization',columns)
   def create_rule():
    nav('rules');act('new-profile');pg.locator('#new-profile-name').fill('集成测试规则');act('create-profile');pg.wait_for_function('LogBench.state.profiles.some(p=>p.name==="集成测试规则")');check(any(r['name']=='集成测试规则' for r in get('/api/v1/rules')['items']),'not persisted')
   test('administrator creates actual shared rule from UI',create_rule)
   rid=pg.evaluate('LogBench.state.profile')
   def add_field():
    act('rule-section','[data-value="fields"]');act('edit-extractor','[data-index="-1"]');pg.locator('#ex-name').fill('提取温度');pg.locator('#ex-pattern').fill(r'temperature:\s*(?<value>[\d.]+)');pg.locator('#ex-key').fill('temperature');pg.locator('#ex-unit').fill('°C');act('commit-extractor');check(pg.evaluate('LogBench.state.dirty'),'not draft')
   test('custom extraction becomes draft, not automatic publish',add_field)
   def add_node():
    act('rule-section','[data-value="nodes"]');act('r5-edit-node','[data-index="-1"]');pg.locator('#nd-name').fill('高温关键点');pg.locator('[data-cond-field="0"]').fill('fields.temperature');pg.locator('[data-cond-op="0"]').select_option('gt');pg.locator('[data-cond-value="0"]').fill('80');act('r5-shape','[data-value="hexagon"]');act('r5-marker-color','[data-value="#be185d"]');act('r5-node-commit');check(pg.evaluate('LogBench.state.nodeRules.some(x=>x.name==="高温关键点")'),'node missing')
   test('R5 custom node shape, color and condition editor works',add_node)
   def trial():
    pg.locator('#rule-test-input').fill(head('temperature: 85',9));act('run-test');pg.wait_for_function('!LogBench.state.testBusy && LogBench.state.testResult!==null');check('高温关键点' in pg.locator('#test-output').inner_text(),'trial not matched');check(get('/api/v1/rules/'+rid)['item']['revision']==1,'trial modified server')
   test('real Worker trial matches node but does not write server',trial)
   def publish():
    act('save-profile');pg.wait_for_function('!LogBench.state.ruleBusy && !LogBench.state.dirty');r=get('/api/v1/rules/'+rid)['item'];check(r['revision']==2 and any(n['name']=='高温关键点' for n in r['profile']['nodes']),'shared save missing')
   test('publish persists full R5 profile and increments revision',publish)
   def bind_directory():
    nav('files');act('folder','[data-value="data/MachineA"]');act('directory-rule');pg.locator('input[name="bind-scope"][value="shared"]').check();pg.locator('#binding-rule').select_option(rid);act('commit-binding');pg.wait_for_function('!document.querySelector("#dialog").open');r=get('/api/v1/bindings/resolve?path=MachineA/A.log');check(r['rule']['id']==rid,'directory default not effective')
   test('directory-wide shared rule updates real backend binding',bind_directory)
   def personal():
    act('bind-file','[data-id="'+ids[0]+'"]');pg.locator('#binding-rule').select_option(default_rule);act('commit-binding');pg.wait_for_function('!document.querySelector("#dialog").open');check(pg.evaluate('(id)=>LogBench.state.fileBindings[id]',ids[0])==default_rule,'personal not saved');check(get('/api/v1/bindings/resolve?path=MachineA/A.log')['rule']['id']==rid,'personal leaked server')
   test('personal override is separate from shared directory rule',personal)
   def conflict():
    nav('rules');act('choose-profile','[data-id="'+rid+'"]');act('rule-section','[data-value="nodes"]');pg.locator('[data-enable-node="0"]').uncheck();server=get('/api/v1/rules/'+rid)['item'];check(admin('/rules/'+rid,'PUT',{'name':server['name'],'profile':server['profile'],'expectedRevision':server['revision']})['status']==200,'external edit failed');act('save-profile');pg.wait_for_function('!LogBench.state.ruleBusy');check(pg.evaluate('LogBench.state.dirty'),'conflicting draft lost');check(any(x[0]=='PUT' and x[2]==409 for x in traffic),'no409')
   test('stale revision fails and preserves the local draft',conflict)
   def discard():
    act('service-discard-draft');act('service-discard-confirm');pg.wait_for_function('!LogBench.state.dirty');check(pg.evaluate('(id)=>LogBench.state.profiles.find(x=>x.id===id)._serverRevision',rid)==3,'did not refresh')
   test('discard loads latest server revision without merging stale draft',discard)
   def global_time():
    act('global-open');pg.wait_for_selector('[data-pick-file]');
    for id in ids:pg.locator('[data-pick-file="'+id+'"]').check()
    act('picker-full-time');pg.wait_for_function('LogBench.getFiles().filter(f=>f.source==="server").every(f=>f.loaded)');act('apply-picker');pg.wait_for_function('LogBench.state.file==="global"');check(pg.evaluate('LogBench.filtered().length')>180,'not merged')
   test('global time selection reads selected actual server files',global_time)
   def compare():
    act('compare-open');pg.wait_for_selector('[data-pick-file]');
    for id in ids:pg.locator('[data-pick-file="'+id+'"]').check()
    act('picker-full-time');pg.wait_for_timeout(100);act('apply-picker');pg.wait_for_function('LogBench.state.view==="compare"');check(pg.evaluate('LogBench.getCompareAxis().length')>90,'axis empty');check(pg.locator('.compare-pane').count()==2,'two panes missing')
   test('two-file aligned comparison uses real server log records',compare)
   def local():
    before=len(traffic);pg.locator('#file-input').set_input_files({'name':'private.log','mimeType':'text/plain','buffer':head('temperature: 85',11).encode()});pg.wait_for_function('LogBench.state.file?.startsWith("local-")');check(not any(m=='POST' and ('upload' in p or 'files' in p) for m,p,s in traffic[before:]),'local text uploaded');check(not (home/'data/private.log').exists(),'private log in server')
   test('local import remains in browser, never uploaded automatically',local)
   def mkdir():
    nav('files');act('lib-source','[data-value="server"]');act('new-folder');pg.locator('#folder-name').fill('NewFolder');act('confirm-folder');pg.wait_for_function('!document.querySelector("#dialog").open');check((home/'data/MachineA/NewFolder').is_dir() or (home/'data/NewFolder').is_dir(),'no real directory')
   test('administrator creates directory on disk',mkdir)
   def delete_restore():
    act('delete-file','[data-id="'+ids[1]+'"]');act('confirm-delete-files');pg.wait_for_function('!document.querySelector("#dialog").open');check(not (home/'data/MachineA/B.log').exists(),'not removed');act('lib-source','[data-value="trash"]');pg.wait_for_selector('[data-act="restore-file"][data-id^="trash-"]');act('restore-file','[data-id^="trash-"]');pg.wait_for_timeout(250);check((home/'data/MachineA/B.log').read_text()==TEXT.replace('APP-A','APP-B'),'restore bytes changed')
   test('shared delete and restore changes disk and preserves exact text',delete_restore)
   def user():
    nav('admin');pg.wait_for_selector('[data-act="new-user"]');act('new-user');pg.locator('#service-user-ip').fill('127.0.0.2');pg.locator('#service-user-name').fill('只读测试');act('service-user-save');pg.wait_for_function('!document.querySelector("#dialog").open');check(get('/api/v1/me','127.0.0.2')['user']['role']=='member','not real user')
   test('administrator creates real IP member',user)
   def audit():
    act('service-admin-tab','[data-value="audit"]');check('127.0.0.1' in pg.locator('.service-admin-table').inner_text(),'audit absent');check(len(get('/api/v1/admin/audit?limit=100')['items'])>=6,'audit not persisted')
   test('management audit displays actual writes',audit)
   if args.bridge:
    member=boot('127.0.0.2')
    def readonly():
     member.locator('[data-act="nav"][data-value="rules"]').first.click();member.wait_for_timeout(150);check(member.locator('[data-act="save-profile"]').is_disabled(),'publish enabled');check(member.evaluate('LB_SESSION.user.id')!=pg.evaluate('LB_SESSION.user.id'),'namespace shared');check(member.evaluate('LogBench.getFiles().every(f=>f.source==="server")'),'privatefileleaked');result=member.evaluate('async()=>{try{await LogBenchService.api("/rules","POST",{name:"illegal",profile:LogBench.state.appliedProfiles[0]});return 200}catch(e){return e.status}}');check(result==403,'server allowed write')
    test('different socket IP has isolated namespace and read-only permissions',readonly);member.close()
   def visual():
    nav('logs');pg.evaluate('(id)=>LogBench.state.file=id',ids[0]);pg.evaluate('LogBench.render()');out=ROOT/'qa/previews';out.mkdir(exist_ok=True)
    for width in [1440,1366,1024,390]:
     pg.set_viewport_size({'width':width,'height':900});pg.wait_for_timeout(150);check(pg.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),f'page overflow {width}');pg.screenshot(path=str(out/f'service-{width}.png'))
   test('R5 layout remains usable at four widths without page overflow',visual)
   test('no unhandled page errors',lambda:check(not errors,str(errors)))
   browser.close()
 except Exception:
  traceback.print_exc()
 finally:
  http_server.shutdown();http_server.server_close();tmp.cleanup();report={'mode':'real-Chromium + real-HTTP bridge (no native navigation/persistence)' if args.bridge else 'native HTTP browser','passed':sum(r['pass'] for r in RESULTS),'checks':RESULTS,'browserErrors':errors,'trafficCount':len(traffic)};Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 return 0 if RESULTS and all(x['pass'] for x in RESULTS) and len(RESULTS)>=20 else 1
if __name__=='__main__':raise SystemExit(main())
