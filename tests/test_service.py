import json, tempfile, unittest
from pathlib import Path
from server.app import Application, Config

PROFILE={'id':'p','name':'R5规则','format':'bracket','version':'1.0','extractors':[{'id':'kv','name':'键值','enabled':True,'source':'both','pattern':r'(?<key>\w+):\s*(?<value>.+)','key':'','group':'value','type':'auto','unit':''}], 'anomalies':[{'id':'code','name':'非零错误码','enabled':True,'field':'fields.err_code','op':'nonzero','value':'','severity':'error'}], 'nodes':[{'id':'n','name':'异常点','enabled':True,'type':'custom','combine':'all','conditions':[{'field':'fields.err_code','op':'nonzero','value':''}],'shape':'diamond','color':'#b91c1c'}]}
class ServiceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  self.app=Application(Config(self.root,bootstrap_admins=['127.0.0.1']))
 def tearDown(self):self.tmp.cleanup()
 def req(self,m,path,p=None,ip='127.0.0.1'):
  return self.app.handle(m,path,{'Host':'localhost','Content-Type':'application/json','X-LogBench-CSRF':self.app.csrf(ip)},json.dumps(p).encode() if p is not None else b'',ip)
 def rule(self):
  r=self.req('POST','/api/v1/rules',{'name':'R5','profile':PROFILE});self.assertEqual(r.status,200,r.value);return r.value['item']
 def test_r5_schema_roundtrip(self):
  a=self.rule(); self.assertEqual(a['profile']['nodes'][0]['shape'],'diamond')
 def test_invalid_node_rejected(self):
  p=json.loads(json.dumps(PROFILE));p['nodes'][0]['color']='url(javascript:x)'
  self.assertEqual(self.req('POST','/api/v1/rules',{'name':'bad','profile':p}).status,400)
 def test_catalog_no_raw_content_and_empty_directories(self):
  (self.root/'data/a/empty').mkdir(parents=True);(self.root/'data/a/x.log').write_text('secret log body')
  r=self.req('GET','/api/v1/catalog');self.assertEqual(r.status,200,r.value)
  self.assertTrue(any(x['path']=='a/empty' for x in r.value['items']));self.assertNotIn('secret log body',json.dumps(r.value))
 def test_cannot_delete_bound_rule(self):
  r=self.rule(); rid=r['id'];self.req('PUT','/api/v1/bindings',{'scope':'default','path':'','ruleId':rid,'expectedRevision':0})
  self.assertEqual(self.req('DELETE','/api/v1/rules/'+rid,{'expectedRevision':1}).status,409)
 def test_content_route_rechecks_disabled_actor(self):
  (self.root/'data/x.log').write_text('secret')
  self.req('PUT','/api/v1/admin/users',{'ip':'10.1.1.1','name':'x','role':'member','enabled':False,'expectedRevision':0})
  self.assertEqual(self.req('GET','/api/v1/files/content?path=x.log',ip='10.1.1.1').status,403)
 def test_server_rule_is_not_changed_by_member(self):
  r=self.rule();self.assertEqual(self.req('PUT','/api/v1/rules/'+r['id'],{'name':'hack','profile':PROFILE,'expectedRevision':1},'10.1.1.2').status,403)
 def test_compare_and_swap(self):
  r=self.rule();p={'name':'saved','profile':PROFILE,'expectedRevision':1}
  self.assertEqual(self.req('PUT','/api/v1/rules/'+r['id'],p).status,200)
  self.assertEqual(self.req('PUT','/api/v1/rules/'+r['id'],p).status,409)
 def test_health_has_release_id(self):
  r=self.req('GET','/api/v1/health');self.assertIn('release',r.value)
if __name__=='__main__':unittest.main()
