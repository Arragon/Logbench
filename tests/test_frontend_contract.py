import unittest, tempfile
from pathlib import Path
from server.app import Application, Config

ROOT=Path(__file__).resolve().parents[1]
class FrontendContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Application(Config(Path(self.tmp.name),code_root=ROOT))
    def tearDown(self): self.tmp.cleanup()
    def get(self,path): return self.app.handle('GET',path,{'Host':'localhost'})
    def test_entry_bootstraps_identity_before_workbench(self):
        r=self.get('/'); self.assertEqual(r.status,200)
        text=r.stream.read().decode();r.close()
        self.assertIn('/assets/boot.js',text)
        self.assertNotIn('合成演示',text)
    def test_workbench_bundle_connected_and_keeps_r5(self):
        r=self.get('/assets/workbench.js');self.assertEqual(r.status,200)
        text=r.stream.read().decode();r.close()
        self.assertIn('startConnectedService',text)
        self.assertIn('parseTraceback',text)
        self.assertNotIn('@@SERVICE@@',text)
        self.assertIn('window.LB_SESSION.user.id',text)
    def test_only_allowlisted_static_files(self):
        for path in ('/src/app.js','/server/app.py','/config.json','/rules/cpqpsgx-default.json'):
            self.assertEqual(self.get(path).status,404)
