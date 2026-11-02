import importlib, json, sqlite3, tempfile, unittest, zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
class OperationsTests(unittest.TestCase):
 def module(self,name):
  self.assertTrue((ROOT/'tools'/f'{name}.py').exists(),f'{name} tool missing')
  return importlib.import_module('tools.'+name)
 def test_backup_uses_sqlite_snapshot_and_preserves_secret(self):
  backup=self.module('backup')
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);(h/'var').mkdir();(h/'data').mkdir();db=sqlite3.connect(h/'var/logbench.sqlite3');db.execute('pragma journal_mode=wal');db.execute('create table settings(k,v)');db.execute('insert into settings values(?,?)',('secret','stable'));db.commit();(h/'data/a.log').write_text('original');(h/'data/.logbench-trash').mkdir();(h/'data/.logbench-trash/deleted').write_text('recoverable');(h/'config.json').write_text('{}')
   z=h/'saved.zip';backup.create_backup(h,h/'data',h/'var',z,include_data=True)
   with zipfile.ZipFile(z) as archive:archive.extractall(h/'restore')
   with closing(sqlite3.connect(h/'restore/state/logbench.sqlite3')) as copy:self.assertEqual(copy.execute('select v from settings').fetchone()[0],'stable')
   self.assertEqual((h/'restore/data/a.log').read_text(),'original');self.assertTrue((h/'restore/manifest.json').exists());self.assertEqual((h/'restore/data/.logbench-trash/deleted').read_text(),'recoverable');db.close()
 def test_metadata_only_backup_still_includes_recycle_contents(self):
  backup=self.module('backup')
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);(h/'var').mkdir();(h/'data/.logbench-trash').mkdir(parents=True);(h/'data/.logbench-trash/item').write_text('recovery');(h/'data/live.log').write_text('live')
   with closing(sqlite3.connect(h/'var/logbench.sqlite3')) as db:db.execute('create table t(x)')
   backup.create_backup(h,h/'data',h/'var',h/'b.zip')
   with zipfile.ZipFile(h/'b.zip') as z:self.assertIn('data/.logbench-trash/item',z.namelist());self.assertNotIn('data/live.log',z.namelist())
 def test_backup_closes_real_connections_before_return(self):
  backup=self.module('backup');connections=[];connect=sqlite3.connect
  class TrackedConnection(sqlite3.Connection):
   was_closed=False
   def close(self):
    self.was_closed=True
    super().close()
  def track(*args,**kwargs):
   kwargs['factory']=TrackedConnection
   connection=connect(*args,**kwargs);connections.append(connection);return connection
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);(h/'var').mkdir();(h/'data').mkdir()
   with closing(connect(h/'var/logbench.sqlite3')) as db:
    db.execute('create table t(x)');db.commit()
   try:
    with patch.object(backup.sqlite3,'connect',track):
     backup.create_backup(h,h/'data',h/'var',h/'b.zip')
    self.assertEqual(len(connections),2)
    self.assertTrue(all(c.was_closed for c in connections),'backup must release SQLite handles before returning')
   finally:
    for connection in connections:connection.close()
 def archive(self,h,invalid=False):
  z=h/'update.zip'
  with zipfile.ZipFile(z,'w',zipfile.ZIP_DEFLATED) as out:
   for folder in ('server','src','rules'):
    for p in (ROOT/folder).rglob('*'):
     if p.is_file() and '__pycache__' not in p.parts:
      text=p.read_bytes()
      if p.name=='app.py':text+=b'\nINVALID PYTHON!!!\n' if invalid else b'\n# operations upgrade fixture\n'
      out.writestr('LogBench/'+p.relative_to(ROOT).as_posix(),text)
   out.write(ROOT/'worker.py','LogBench/worker.py');out.writestr('LogBench/config.json','{"destroy":true}');out.writestr('LogBench/data/keep.log','WRONG');out.writestr('LogBench/run.py','CHANGED LAUNCHER')
  return z
 def test_update_preserves_data_configuration_launcher_and_uses_hold_marker(self):
  update=self.module('apply_update')
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);home=h/'installed';(home/'data').mkdir(parents=True);(home/'data/keep.log').write_text('safe');(home/'config.json').write_text('{}');(home/'run.py').write_text('original launcher')
   z=self.archive(h);result=update.apply_update(z,home)
   self.assertTrue((home/'server/app.py').read_text().endswith('# operations upgrade fixture\n'));self.assertEqual((home/'data/keep.log').read_text(),'safe');self.assertEqual((home/'config.json').read_text(),'{}');self.assertEqual((home/'run.py').read_text(),'original launcher');self.assertFalse((home/'.update-in-progress').exists());self.assertGreater(result['files'],5)
 def test_traversal_package_is_rejected_before_writes(self):
  update=self.module('apply_update')
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);z=h/'evil.zip'
   with zipfile.ZipFile(z,'w') as out:out.writestr('LogBench/../outside','bad')
   with self.assertRaises(ValueError):update.apply_update(z,h/'installed')
   self.assertFalse((h/'outside').exists())
 def test_invalid_update_does_not_overwrite_running_source(self):
  update=self.module('apply_update')
  with tempfile.TemporaryDirectory() as d:
   h=Path(d);home=h/'installed';(home/'server').mkdir(parents=True);(home/'server/app.py').write_text('original')
   with self.assertRaises(ValueError):update.apply_update(self.archive(h,True),home)
   self.assertEqual((home/'server/app.py').read_text(),'original');self.assertFalse((home/'.update-in-progress').exists())
if __name__=='__main__':unittest.main()
