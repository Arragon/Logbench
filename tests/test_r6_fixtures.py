"""R6-005 fixture generator contract: determinism, manifest accuracy, streaming scale."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'generate_log_fixture.py'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def run_generator(*args):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, str(TOOL), *args], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, text=True)
    if result.returncode != 0:
        raise AssertionError('generator failed: ' + result.stdout)
    return result.stdout


def digest_dir(path):
    out = {}
    for item in sorted(Path(path).iterdir()):
        if item.is_file():
            out[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    return out


class FixtureGenerator(unittest.TestCase):
    def test_golden_preset_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            run_generator('--preset', 'golden', '--out', a)
            run_generator('--preset', 'golden', '--out', b, '--seed', '20261102')
            self.assertEqual(digest_dir(a), digest_dir(b))

    def test_committed_golden_matches_generator_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_generator('--preset', 'golden', '--out', tmp)
            generated = digest_dir(tmp)
            committed = digest_dir(GOLDEN)
            for name, digest in generated.items():
                self.assertIn(name, committed, name + ' missing from the repository')
                self.assertEqual(committed[name], digest, name + ' is stale; regenerate it')
            for name in committed:
                if name == 'expected.json':
                    continue
                self.assertIn(name, generated, name + ' is not produced by the generator')

    def test_manifest_reports_real_sizes_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_generator('--preset', 'scale', '--files', '2', '--mib', '1', '--out', tmp)
            manifest = json.loads((Path(tmp) / 'manifest.json').read_text('utf-8'))
            self.assertEqual(manifest['schema'], 'logbench.r6.fixture-manifest/1')
            self.assertEqual(len(manifest['files']), 2)
            for entry in manifest['files']:
                raw = (Path(tmp) / entry['name']).read_bytes()
                self.assertEqual(entry['bytes'], len(raw))
                self.assertEqual(entry['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertGreaterEqual(entry['bytes'], 1024 * 1024)
                self.assertGreater(entry['records'], 0)
                self.assertGreater(entry['physical_lines'], entry['records'] - 1)
                self.assertLessEqual(entry['first_key'], entry['last_key'])
            self.assertEqual(manifest['totals']['files'], 2)
            self.assertGreaterEqual(manifest['totals']['bytes'], 2 * 1024 * 1024)

    def test_scale_output_is_reproducible(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            run_generator('--preset', 'scale', '--files', '1', '--mib', '1', '--out', a)
            run_generator('--preset', 'scale', '--files', '1', '--mib', '1', '--out', b)
            self.assertEqual(digest_dir(a), digest_dir(b))


if __name__ == '__main__':
    unittest.main()
