#!/usr/bin/env python3
"""R6-001 baseline manifest generator.

Reads the delivered SHA256SUMS, recomputes every source hash, derives a stable
tree digest, and folds in the machine-readable test evidence that the R6 baseline
commands already wrote to qa/r6/baseline/. Standard library only; Python 3.9.11
compatible.

Usage:
  python3.9 tools/r6_manifest.py --out docs/reference/source-manifest.json
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMS = ROOT / 'SHA256SUMS'
EVIDENCE = ROOT / 'qa/r6/baseline'
EXPECTED_ZIP = '07aa5d88de44f7794233fc1b69d66dacf41512520f5e176fcb88cc598b2e2d54'
VERSION_RE = re.compile(r"VERSION = '([^']+)'")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_sums() -> dict:
    rows = {}
    for line in SUMS.read_text('utf-8').splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition('  ')
        rows[name.strip()] = digest.strip()
    return rows


def tree_digest(rows: dict) -> str:
    """Same shape as run.py: sorted path + NUL + raw file digest."""
    digest = hashlib.sha256()
    for name in sorted(rows):
        digest.update(name.encode('utf-8'))
        digest.update(b'\x00')
        digest.update(bytes.fromhex(rows[name]))
    return digest.hexdigest()


def product_version() -> str:
    text = (ROOT / 'server/app.py').read_text('utf-8')
    found = VERSION_RE.search(text)
    return found.group(1) if found else 'unknown'


def frontend_bundle() -> dict:
    sys.path.insert(0, str(ROOT))
    from server.frontend import bundle
    raw = bundle(ROOT)
    return {'path': '/assets/workbench.js', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def git(*args: str) -> str:
    try:
        out = subprocess.run(['git', *args], cwd=str(ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=20, text=True)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def parse_unittest(path: Path) -> dict:
    if not path.is_file():
        return {'status': 'missing'}
    text = path.read_text('utf-8', errors='replace')
    ran = re.search(r'^Ran (\d+) tests? in ([0-9.]+)s', text, re.M)
    failed = re.search(r'^FAILED \(([^)]*)\)', text, re.M)
    ok = re.search(r'^OK', text, re.M)
    return {'status': 'pass' if ok else ('fail' if failed else 'unknown'),
            'tests': int(ran.group(1)) if ran else None,
            'seconds': float(ran.group(2)) if ran else None,
            'detail': failed.group(1) if failed else ''}


def parse_node(path: Path) -> dict:
    if not path.is_file():
        return {'status': 'missing'}
    text = path.read_text('utf-8', errors='replace')
    out = {'status': 'unknown', 'tests': None, 'pass': None, 'fail': None}
    for key in ('tests', 'pass', 'fail'):
        found = re.search(r'^# %s (\d+)' % key, text, re.M)
        if found:
            out[key] = int(found.group(1))
    out['status'] = 'pass' if out['fail'] == 0 and out['tests'] else ('fail' if out['fail'] else 'unknown')
    return out


def parse_exit(path: Path) -> dict:
    if not path.is_file():
        return {'status': 'missing'}
    text = path.read_text('utf-8', errors='replace').strip()
    return {'status': 'pass' if not text else 'fail', 'stdout_bytes': len(text)}


def python_probe(exe: str) -> dict:
    try:
        out = subprocess.run([exe, '-c', 'import sys,sqlite3;print(sys.version.split()[0]);print(sqlite3.sqlite_version)'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, text=True)
        if out.returncode != 0:
            return {'executable': exe, 'available': False}
        version, sqlite = (out.stdout.strip().splitlines() + ['', ''])[:2]
        return {'executable': exe, 'available': True, 'version': version, 'sqlite': sqlite}
    except (OSError, subprocess.SubprocessError):
        return {'executable': exe, 'available': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Generate the R6 source manifest')
    parser.add_argument('--out', default='docs/reference/source-manifest.json')
    args = parser.parse_args(argv)

    declared = read_sums()
    files = {}
    mismatched = []
    for name, expected in sorted(declared.items()):
        path = ROOT / name
        if not path.is_file():
            mismatched.append({'path': name, 'reason': 'missing'})
            continue
        actual = sha256_file(path)
        files[name] = {'sha256': actual, 'bytes': path.stat().st_size}
        if actual != expected:
            mismatched.append({'path': name, 'reason': 'hash-mismatch', 'declared': expected, 'actual': actual})

    zip_present = (ROOT / 'LogBench_R5_Server_v3.0.3.zip').is_file()
    manifest = {
        'schema': 'logbench.r6.source-manifest/1',
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'baseline': {
            'product': 'LogBench R5 Server',
            'version': product_version(),
            'profile': 'Floating Details / Python 3.9 compatible',
            'git_commit': git('rev-parse', 'HEAD'),
            'git_subject': git('log', '-1', '--pretty=%s'),
            'git_dirty': bool(git('status', '--porcelain', '--untracked-files=no')),
        },
        'package': {
            'declared_zip_sha256': EXPECTED_ZIP,
            'zip_present_in_workspace': zip_present,
            'zip_verification': 'verified' if zip_present else 'unavailable: workspace holds the unpacked tree, not the delivery ZIP',
            'tree_digest_sha256': tree_digest(declared),
            'tree_digest_note': 'sha256 over sorted "<path>\\0<file sha256>" pairs; equals the identity run.py computes for a release',
            'file_count': len(declared),
            'mismatched': mismatched,
        },
        'frontend_bundle': frontend_bundle(),
        'environment': {
            'os': platform.platform(),
            'machine': platform.machine(),
            'python_current': python_probe(sys.executable),
            'python_39': python_probe(os.path.expanduser('~/.local/bin/python3.9')),
            'node': git_free_version('node', '--version'),
            'chrome_present': Path('/Applications/Google Chrome.app').is_dir(),
        },
        'tests': {
            'python_39_unittest': parse_unittest(EVIDENCE / 'py39-tests.txt'),
            'python_current_unittest': parse_unittest(EVIDENCE / 'py313-tests.txt'),
            'node_test': parse_node(EVIDENCE / 'node-tests.txt'),
            'worker_self_test_py39': parse_exit(EVIDENCE / 'worker-self-test-py39.txt'),
            'sha256sums_check': parse_sums_check(EVIDENCE / 'sha256sums-check.txt'),
        },
        'evidence_dir': 'qa/r6/baseline',
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('wrote %s (%d files, %d mismatched)' % (out, len(files), len(mismatched)))
    return 0


def git_free_version(exe: str, flag: str) -> dict:
    try:
        out = subprocess.run([exe, flag], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=20, text=True)
        return {'executable': exe, 'available': out.returncode == 0, 'version': out.stdout.strip()}
    except (OSError, subprocess.SubprocessError):
        return {'executable': exe, 'available': False}


def parse_sums_check(path: Path) -> dict:
    if not path.is_file():
        return {'status': 'missing'}
    text = path.read_text('utf-8', errors='replace')
    ok = len(re.findall(r': OK$', text, re.M))
    bad = len(re.findall(r': FAILED', text, re.M))
    return {'status': 'pass' if bad == 0 and ok else 'fail', 'ok': ok, 'failed': bad}


if __name__ == '__main__':
    raise SystemExit(main())
