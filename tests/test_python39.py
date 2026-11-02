"""Compatibility regression checks; a restricted API test is not a 3.9 VM.

Run this entire suite with an actual 3.9.11 interpreter for native acceptance.
On newer interpreters the filesystem shim rejects the 3.10-only stat keyword
while still using real on-disk files and SQLite.
"""
from __future__ import annotations
import ast
import io
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import typing
import unittest
from unittest.mock import patch

import run
from server.app import Application, Config, Response

ROOT = Path(__file__).resolve().parents[1]


NATIVE_STAT = Path.stat


def stat39(path, *args, **kwargs):
    """Reject new keywords at application boundaries, not in host stdlib internals.

    Newer pathlib.is_dir/lstat may call stat with the added keyword internally.
    Let those host internals work; application calls must obey the 3.9 signature.
    This is a targeted API-surface regression, not interpreter emulation.
    """
    caller = sys._getframe(1).f_globals.get('__name__', '')
    if caller.startswith('server.') and (args or kwargs):
        raise TypeError('Python 3.9 Path.stat() accepts no keyword arguments')
    return NATIVE_STAT(path, *args, **kwargs)


def lstat39(path):
    return os.lstat(path)


class Python39Compatibility(unittest.TestCase):
    def test_target_version_passes_launcher_and_initializes_real_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = SimpleNamespace(version_info=(3, 9, 11), stderr=io.StringIO())
            with patch.object(run, 'sys', environment):
                result = run.main(['--init-only', '--admin-ip', '127.0.0.1',
                                   '--state-dir', str(root / 'var'),
                                   '--data-dir', str(root / 'data')])
            self.assertEqual(result, 0, environment.stderr.getvalue())
            self.assertTrue((root / 'var' / 'logbench.sqlite3').is_file())

    def test_older_interpreter_is_rejected_before_database_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in [(3, 8, 20), (3, 9, 10)]:
                with self.subTest(version=version):
                    environment = SimpleNamespace(version_info=version, stderr=io.StringIO())
                    with patch.object(run, 'sys', environment):
                        result = run.main(['--init-only', '--state-dir', str(root / 'var')])
                    self.assertEqual(result, 2)
                    self.assertIn('3.9.11', environment.stderr.getvalue())
                    self.assertFalse((root / 'var').exists())

    def test_no_python310_union_annotations(self):
        for path in [ROOT / 'run.py', ROOT / 'worker.py', *sorted((ROOT / 'server').glob('*.py'))]:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            annotations = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.AnnAssign, ast.arg)):
                    annotations.append(node.annotation)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    annotations.append(node.returns)
            for annotation in filter(None, annotations):
                self.assertFalse(any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                                     for n in ast.walk(annotation)),
                                 '%s:%s uses Python 3.10 union evaluation' % (path.name, annotation.lineno))

    def test_all_python_sources_parse_using_python39_grammar(self):
        paths = [ROOT / 'run.py', ROOT / 'worker.py']
        for directory in ['server', 'tools', 'tests', 'qa']:
            paths.extend((ROOT / directory).rglob('*.py'))
        for path in paths:
            with self.subTest(file=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 9))

    def test_public_dataclass_annotations_can_be_resolved(self):
        self.assertEqual(typing.get_type_hints(Config)['data_dir'], typing.Optional[Path])
        self.assertEqual(typing.get_type_hints(Response)['stream'], typing.Optional[typing.BinaryIO])

    def test_catalog_with_python39_pathlib_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Application(Config(root, code_root=ROOT))
            (root / 'data' / 'machine').mkdir()
            (root / 'data' / 'machine' / 'test.log').write_text('日志\n', encoding='utf-8')
            with patch.object(Path, 'stat', stat39), patch.object(Path, 'lstat', lstat39):
                response = app.handle('GET', '/api/v1/catalog', {'Host': 'localhost'})
            self.assertEqual(response.status, 200, response.value)
            self.assertTrue(any(x['path'] == 'machine/test.log' for x in response.value['items']))

    def test_directory_listing_with_python39_pathlib_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Application(Config(root, code_root=ROOT))
            (root / 'data' / 'test.log').write_text('log\n', encoding='utf-8')
            with patch.object(Path, 'stat', stat39), patch.object(Path, 'lstat', lstat39):
                response = app.handle('GET', '/api/v1/files', {'Host': 'localhost'})
            self.assertEqual(response.status, 200, response.value)
            self.assertTrue(any(x['path'] == 'test.log' for x in response.value['items']))

    def test_filesystem_listing_does_not_expose_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Application(Config(root, code_root=ROOT))
            (root / 'outside.log').write_text('private', encoding='utf-8')
            (root / 'data' / 'public.log').write_text('public', encoding='utf-8')
            try:
                (root / 'data' / 'link.log').symlink_to(root / 'outside.log')
            except (OSError, NotImplementedError):
                self.skipTest('This OS/account cannot create symlinks')
            with patch.object(Path, 'stat', stat39), patch.object(Path, 'lstat', lstat39):
                for url in ['/api/v1/catalog', '/api/v1/files']:
                    response = app.handle('GET', url, {'Host': 'localhost'})
                    self.assertEqual(response.status, 200, response.value)
                    paths = {x['path'] for x in response.value['items']}
                    self.assertIn('public.log', paths)
                    self.assertNotIn('link.log', paths)
                response = app.handle('GET', '/api/v1/files/content?path=link.log', {'Host': 'localhost'})
                self.assertIn(response.status, [400, 403])


if __name__ == '__main__':
    unittest.main()
