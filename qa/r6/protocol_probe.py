"""Shared real-protocol probe for M6 Gate evidence.

The three protocol scripts (`protocol_sftp.py`, `protocol_smb.py`,
`protocol_scp.py`) are thin wrappers around this module so the evidence produced by
each one has the same shape.

Exit codes are the evidence contract:

* `0` — every step passed against a real endpoint, with a real tool;
* `1` — a step failed (output says which);
* `3` — **BLOCKED**: no endpoint supplied, the tool is missing, or the host cannot
  be reached. A blocked run is a legitimate record; it is never reported as a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.collectors import base as adapters  # noqa: E402
from server.collectors.base import CollectorError  # noqa: E402
from server.collectors.credentials import CredentialStore  # noqa: E402
from server.collectors.security import Admission, Guard  # noqa: E402


def build_source(kind: str, args) -> dict:
    policy = {'allowedHosts': [args.host], 'allowLoopback': bool(args.allow_loopback),
              'readTimeout': 30}
    if args.user:
        policy['user'] = args.user
    if args.known_hosts:
        policy['knownHostsFile'] = args.known_hosts
    if args.share:
        policy['share'] = args.share
    if args.tools:
        policy['tools'] = {'rclone': args.tools, 'scp': args.tools}
    return {'id': 'gate_' + kind, 'adapter': kind, 'endpoint': args.endpoint,
            'path': args.remote_path or '/', 'policy': policy,
            'credential_ref': None}


def main(kind: str) -> int:
    parser = argparse.ArgumentParser(description='%s 真实协议证据' % kind)
    parser.add_argument('--endpoint', help='例如 sftp://host:22 或 smb://host:445')
    parser.add_argument('--host')
    parser.add_argument('--user')
    parser.add_argument('--known-hosts')
    parser.add_argument('--share')
    parser.add_argument('--remote-path')
    parser.add_argument('--tools', help='工具路径（rclone / scp）')
    parser.add_argument('--allow-loopback', action='store_true')
    parser.add_argument('--out')
    args = parser.parse_args()

    report = {'kind': kind, 'startedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'steps': [], 'result': 'BLOCKED', 'blockedReason': None}

    state = adapters.availability(kind)
    report['adapter'] = state
    if not state['available']:
        report['blockedReason'] = '缺少运行组件：%s' % ', '.join(state.get('missingTools') or [])
        return finish(report, args, 3)

    if not args.endpoint:
        report['blockedReason'] = '未提供可实连端点（--endpoint）。本机没有受控 %s 服务。' % kind
        return finish(report, args, 3)

    if not args.host:
        from urllib.parse import urlsplit
        args.host = urlsplit(args.endpoint).hostname
    if not args.host:
        report['blockedReason'] = '端点缺少主机名'
        return finish(report, args, 3)

    source = build_source(kind, args)
    store = CredentialStore(ROOT / 'var' / 'gate-credentials')
    guard = Guard(Admission.from_policy(source['policy']))
    try:
        adapter = adapters.build(source, credentials=store, guard=guard)
    except CollectorError as exc:
        report['blockedReason'] = exc.message
        report['steps'].append({'step': 'build', 'state': 'blocked', 'detail': exc.to_dict()})
        return finish(report, args, 3)

    code = 1
    try:
        probe = adapter.probe(config_revision=1, fingerprint='gate', guard=guard)
        report['probe'] = probe.to_dict()
        report['steps'].append({'step': 'probe', 'state': 'pass' if probe.ok else 'fail'})
        listing = None
        if probe.capabilities.directory_discovery == 'supported':
            listing = adapter.list(args.remote_path or '/', limit=20)
            report['steps'].append({'step': 'list', 'state': 'pass',
                                    'entries': len(listing.entries)})
        sample = None
        if listing and listing.entries:
            sample = listing.entries[0]
        elif probe.capabilities.directory_discovery == 'unsupported':
            target = args.remote_path
            sample = adapter.stat(target) if target else None
            report['steps'].append({'step': 'stat', 'state': 'pass' if sample else 'skip'})
        if sample is not None and probe.capabilities.range_read == 'supported':
            chunk = adapter.read_range(sample.path, offset=0, length=min(1024, sample.size))
            digest = hashlib.sha256(chunk.data).hexdigest()
            report['steps'].append({'step': 'read', 'state': 'pass', 'bytes': len(chunk.data),
                                    'sha256': digest})
            report['sample'] = {'path': sample.path, 'size': sample.size, 'sha256': digest}
        elif probe.capabilities.range_read == 'unsupported':
            # scp: the only byte path is a full snapshot, which the gate runs against
            # a throw-away staging directory.
            staging = Path('/tmp') / ('lb-gate-' + kind)
            result = adapter.open_snapshot(args.remote_path, staging)
            report['steps'].append({'step': 'snapshot', 'state': 'pass',
                                    'bytes': result['bytes'], 'sha256': result['sha256']})
            report['sample'] = {'path': result['remotePath'], 'size': result['bytes'],
                                'sha256': result['sha256']}
        code = 0 if probe.ok else 1
        report['result'] = 'PASS' if code == 0 else 'FAIL'
    except CollectorError as exc:
        report['steps'].append({'step': 'run', 'state': 'fail', 'detail': exc.to_dict()})
        report['result'] = 'FAIL'
    except Exception as exc:  # noqa: BLE001 - a gate must record, not crash
        report['steps'].append({'step': 'run', 'state': 'error', 'detail': repr(exc)[:300]})
        report['result'] = 'FAIL'
    finally:
        adapter.close()
    return finish(report, args, code)


def finish(report: dict, args, code: int) -> int:
    report['finishedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, 'utf-8')
    print(text)
    if code == 3:
        print('BLOCKED: %s' % report['blockedReason'], file=sys.stderr)
    return code
