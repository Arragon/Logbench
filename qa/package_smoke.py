"""Unpack a trusted deployment ZIP and check the actual launched service.

Standard library only. Run with Python 3.9.11 to obtain native-target evidence.
A successful run on a newer interpreter is not reported as a native 3.9.11 pass.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


def check_package(archive):
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({'name': name, 'passed': True})
    with tempfile.TemporaryDirectory(prefix='logbench-py39-package-') as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.namelist()
            for name in members:
                dest = (root / name).resolve()
                if root != dest and root not in dest.parents:
                    raise ValueError('Unsafe archive member: ' + name)
            bundle.extractall(root)
        home = root / 'LogBench'
        for row in (home / 'SHA256SUMS').read_text('utf-8').splitlines():
            digest, name = row.split('  ', 1)
            check('checksum:' + name, hashlib.sha256((home / name).read_bytes()).hexdigest() == digest)
        env = os.environ.copy()
        for key in ('LOGBENCH_HOME', 'LOGBENCH_CONFIG', 'LOGBENCH_RELEASE', 'LOGBENCH_SUPERVISED'):
            env.pop(key, None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        def run(*args):
            result = subprocess.run([sys.executable, '-B', *args], cwd=home, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', timeout=30)
            check('command:' + ' '.join(args), result.returncode == 0)
            return result
        check('default port in example',
              json.loads((home / 'config.example.json').read_text('utf-8'))['port'] == 50004)
        run('worker.py', '--self-test')
        run('tools/set_port.py', '51005')
        check('quick port change persisted',
              json.loads((home / 'config.json').read_text('utf-8'))['port'] == 51005)
        run('run.py', '--admin-ip', '127.0.0.1', '--init-only')
        sample = '[2026-10-31 12:00:00.12345678] [EVENT] [APP-A] [1001] [1001] [demo.py:7] [100] [0:0:0] run() <DEMO> 中文日志\nvalue: 42\n'
        logpath = home / 'data' / 'demo' / 'run.log'
        logpath.parent.mkdir(parents=True, exist_ok=True)
        logpath.write_text(sample, encoding='utf-8')
        with socket.socket() as socket_:
            socket_.bind(('127.0.0.1', 0))
            port = socket_.getsockname()[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            with opener.open('http://127.0.0.1:%d%s' % (port, path), timeout=2) as response:
                return response.read()
        with (root / 'service.log').open('w', encoding='utf-8') as logfile:
            process = subprocess.Popen([sys.executable, '-B', str(home / 'run.py'), '--port', str(port)],
                                       cwd=home, env=env, stdout=logfile, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                health = None
                while time.monotonic() < deadline:
                    try:
                        health = json.loads(get('/healthz'))
                        break
                    except Exception:
                        if process.poll() is not None:
                            break
                        time.sleep(.1)
                check('supervised service started', bool(health and health['status'] == 'ok'))
                check('release version', health['version'] == '3.0.3')
                check('supervisor uses child worker', health['pid'] != process.pid and health['release'] != 'direct')
                check('R5 entry', b'/assets/boot.js' in get('/'))
                check('R5 real assembled bundle', b'parseTraceback' in get('/assets/workbench.js'))
                check('explicit administrator retained', json.loads(get('/api/v1/me'))['user']['role'] == 'admin')
                catalog = json.loads(get('/api/v1/catalog'))
                check('recursive catalog', any(item['path'] == 'demo/run.log' for item in catalog['items']))
                listing = json.loads(get('/api/v1/files?path=demo'))
                check('directory listing', any(item['path'] == 'demo/run.log' for item in listing['items']))
                check('UTF-8 log download', get('/api/v1/files/content?path=demo/run.log').decode('utf-8') == sample)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        check('no bootstrap data shipped', 'LogBench/var/logbench.sqlite3' not in members)
    return {'interpreter': sys.version, 'target': '3.9.11',
            'nativeTargetRuntime': tuple(sys.version_info[:3]) == (3, 9, 11),
            'passed': len(checks), 'checks': checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = check_package(args.archive)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
