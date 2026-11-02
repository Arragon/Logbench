"""Port defaults, CLI overrides and user-friendly persistent configuration."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server.app import Config
from server.httpd import load_config

ROOT = Path(__file__).resolve().parents[1]


class PortConfigurationTests(unittest.TestCase):
    def test_default_port_is_50004_without_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(Config(root).port, 50004)
            config, _ = load_config(['--config', str(ROOT / 'config.example.json'),
                                     '--state-dir', str(root / 'state'),
                                     '--data-dir', str(root / 'data')])
            self.assertEqual(config.port, 50004)
            self.assertEqual(json.loads((ROOT / 'config.example.json').read_text('utf-8'))['port'], 50004)

    def test_cli_port_overrides_config_port_without_writing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            path.write_text(json.dumps({'port': 44333}), 'utf-8')
            original = path.read_bytes()
            config, _ = load_config(['--config', str(path), '--port', '50007',
                                     '--state-dir', str(root / 'state'),
                                     '--data-dir', str(root / 'data')])
            self.assertEqual(config.port, 50007)
            self.assertEqual(path.read_bytes(), original)

    def test_set_port_preserves_existing_configuration_and_extra_fields(self):
        from tools.set_port import set_port
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = {'host': '0.0.0.0', 'port': 8765, 'data_dir': 'different-data',
                        'allowed_hosts': ['example.internal', '127.0.0.1'],
                        'trusted_proxies': ['127.0.0.1/32'], 'public_origin': ''}
            path = root / 'config.json'
            path.write_text(json.dumps(original), 'utf-8')
            before, after = set_port(50005, root)
            self.assertEqual((before, after), (8765, 50005))
            self.assertEqual(json.loads(path.read_text('utf-8')),
                             {**original, 'port': 50005})

    def test_set_port_generates_config_from_template_if_missing(self):
        from tools.set_port import set_port
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
            old, new = set_port(51000, root)
            self.assertEqual((old, new), (50004, 51000))
            config = json.loads((root / 'config.json').read_text('utf-8'))
            self.assertEqual(config['port'], 51000)
            self.assertEqual(config['allowed_hosts'], ['localhost', '127.0.0.1', '::1'])

    def test_invalid_port_never_modifies_config(self):
        from tools.set_port import set_port
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            path.write_text('{"port": 50004, "host": "127.0.0.1"}\n', 'utf-8')
            old = path.read_bytes()
            for bad in [0, 65536, -3, '123abc', '3.0', ' ', '0x4', True]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    set_port(bad, root)
                self.assertEqual(path.read_bytes(), old)

    def test_corrupted_configuration_is_not_replaced(self):
        from tools.set_port import set_port
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            path.write_text('something secret', 'utf-8')
            with self.assertRaises(ValueError):
                set_port(50005, root)
            self.assertEqual(path.read_text('utf-8'), 'something secret')

    def test_interactive_command_port_prompt_saves_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/set_port.py'),
                                     '--home', str(root)], input='50009\n',
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((root / 'config.json').read_text('utf-8'))['port'], 50009)

    def test_set_port_rejects_symlink_config_without_touching_target(self):
        from tools.set_port import set_port
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / 'external.json'
            outside.write_text('{"port": 8080}', 'utf-8')
            try:
                (root / 'config.json').symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest('Environment forbids symlinks')
            with self.assertRaises(ValueError):
                set_port(50005, root)
            self.assertEqual(json.loads(outside.read_text('utf-8'))['port'], 8080)

    def test_command_line_port_setter_and_batch_shortcut_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/set_port.py'), '50005',
                                     '--home', str(root)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('50005', result.stdout)
            self.assertIn('重启', result.stdout)
            self.assertEqual(json.loads((root / 'config.json').read_text('utf-8'))['port'], 50005)
            self.assertTrue((ROOT / 'change-port.bat').exists())
            self.assertTrue((ROOT / 'change-port.sh').exists())


if __name__ == '__main__':
    unittest.main()
