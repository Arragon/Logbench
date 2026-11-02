"""Real-process update acceptance. No mocked server or in-process module reload."""
import unittest,tempfile,shutil,subprocess,sys,time,json,socket,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class HotUpdate(unittest.TestCase):
 def test_overwrite_validate_and_keep_state(self):
  with tempfile.TemporaryDirectory() as tmp:
   dest=Path(tmp)/'deploy';shutil.copytree(ROOT,dest,ignore=shutil.ignore_patterns('var','data','__pycache__','.git','qa'))
   self.assertTrue((dest/'run.py').is_file(),'the deployable supervisor entry is required')
   with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
   log=open(Path(tmp)/'runtime.log','w+')
   p=subprocess.Popen([sys.executable,'-B',str(dest/'run.py'),'--port',str(port),'--admin-ip','127.0.0.1','--settle','0.3'],stdout=log,stderr=subprocess.STDOUT)
   base=f'http://127.0.0.1:{port}'
   def get(path):return json.load(urllib.request.urlopen(base+path,timeout=1))
   def wait(predicate,seconds=20):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
     try:
      value=get('/healthz')
      if predicate(value):return value
     except Exception:pass
     time.sleep(.15)
    log.flush();log.seek(0);self.fail('service condition not met\n'+log.read())
   try:
    original=wait(lambda h:h['status']=='ok');me=get('/api/v1/me');first=get('/api/v1/rules')['items'][0]['id']
    source=dest/'server/app.py';text=source.read_text(encoding='utf-8');version_line="VERSION = %r" % original['version'];self.assertIn(version_line,text);source.write_text(text.replace(version_line,"VERSION = 'test-update-1'"),encoding='utf-8')
    h=wait(lambda h:h['version']=='test-update-1');self.assertNotEqual(h['pid'],original['pid'])
    self.assertEqual(get('/api/v1/me')['user']['id'],me['user']['id'])
    self.assertEqual(get('/api/v1/rules')['items'][0]['id'],first)
    source.write_text(source.read_text(encoding='utf-8')+'\nINVALID PYTHON !!!!\n',encoding='utf-8');time.sleep(2)
    self.assertEqual(get('/healthz')['version'],'test-update-1')
    source.write_text(text.replace(version_line,"VERSION = 'test-update-2'"),encoding='utf-8');wait(lambda h:h['version']=='test-update-2')
    # A runtime failure after successful preflight rolls back to a previous snapshot.
    worker=dest/'worker.py';wt=worker.read_text(encoding='utf-8');worker.write_text(wt.replace('return main(sys.argv[1:])',"raise RuntimeError('injected runtime startup failure')"),encoding='utf-8')
    time.sleep(3);wait(lambda h:h['version']=='test-update-2')
    self.assertEqual(get('/api/v1/me')['user']['role'],'admin')
    worker.write_text(wt,encoding='utf-8');wait(lambda h:h['version']=='test-update-2')
   finally:
    p.terminate()
    try:p.wait(12)
    except subprocess.TimeoutExpired:p.kill();p.wait()
    log.close()
