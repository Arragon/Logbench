"""R6-065: legacy SCP snapshot semantics.

Real server with SFTP disabled: **BLOCKED** (no controlled SSH host here). What is
verified: strict host-key requirement, refusal of path characters that cannot be
quoted safely, whole-file snapshot with a hash, and the refusal to seal a download
whose digest does not match the declared one.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors.base import CollectorError
from server.collectors.process import Result
from server.collectors.scp_snapshot import ScpSnapshotAdapter
from server.collectors.security import SecurityError

DATA = b''.join(b'2026-11-03 10:00:00 ERROR legacy=%05d\n' % index for index in range(5000))


class FakeScp:
    """Behaves like scp: copies `files[remote]` to the local path in argv."""

    def __init__(self, data: bytes = DATA, *, version='OpenSSH_9.0p1'):
        self.data = data
        self.version_line = version
        self.spawns = 0
        self.secrets = ()
        self.calls = []
        self.fail = False

    def run(self, argv, *, timeout=None, max_stdout=None, max_stderr=None, env=None,
            input=None) -> Result:
        values = [str(value) for value in argv]
        self.spawns += 1
        self.calls.append(values)
        if '-V' in values or '-v' in values:
            return Result(values, 0, b'', self.version_line, 0.0)
        if self.fail:
            return Result(values, 1, b'', 'scp: permission denied', 0.0)
        local = values[-1]
        Path(local).write_bytes(self.data)
        return Result(values, 0, b'', '', 0.0)

    def cancel(self):
        return None


def source(**policy):
    base_policy = {'user': 'logbench', 'allowedHosts': ['127.0.0.1'], 'allowLoopback': True,
                   'tools': {'scp': '/bin/echo'}, 'sessionDir': None}
    base_policy.update(policy)
    return {'id': 'src_scp', 'adapter': 'scp', 'endpoint': 'scp://127.0.0.1:22',
            'policy': base_policy}


class ScpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.known = self.root / 'known_hosts'
        self.known.write_text('127.0.0.1 ssh-ed25519 AAAAC3Nza\n', 'utf-8')
        self.scp = FakeScp()
        self.staging = self.root / 'staging'

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, **policy) -> ScpSnapshotAdapter:
        policy.setdefault('knownHostsFile', str(self.known))
        policy.setdefault('sessionDir', str(self.root / 'sessions'))
        return ScpSnapshotAdapter(source(**policy), runner=self.scp)

    def test_probe_requires_a_known_hosts_file(self):
        adapter = ScpSnapshotAdapter(source(), runner=self.scp)
        probe = adapter.probe(config_revision=1, fingerprint='fp')
        self.assertFalse(probe.ok)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['hostkey'].state, 'fail')

    def test_probe_states_the_downgrade_instead_of_hiding_it(self):
        probe = self.adapter().probe(config_revision=1, fingerprint='fp')
        self.assertTrue(probe.ok, [check.to_dict() for check in probe.checks])
        caps = probe.capabilities
        self.assertEqual(caps.range_read, 'unsupported')
        self.assertEqual(caps.directory_discovery, 'unsupported')
        self.assertEqual(caps.append_contract, 'unsupported')
        self.assertEqual(caps.stable_identity, 'snapshot-hash')
        self.assertTrue(any('快照' in warning for warning in probe.warnings))
        self.assertFalse(caps.allows_follow())

    def test_snapshot_is_hashed_and_lands_in_private_staging(self):
        adapter = self.adapter()
        result = adapter.open_snapshot('/var/log/app/app.log', self.staging)
        self.assertEqual(result['bytes'], len(DATA))
        self.assertEqual(result['sha256'], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(Path(result['path']).read_bytes(), DATA)
        self.assertEqual(stat.S_IMODE(self.staging.stat().st_mode), 0o700)
        self.assertNotIn('--config', result['path'])
        argv = self.scp.calls[-1]
        self.assertIn('StrictHostKeyChecking=yes', ' '.join(argv))

    def test_paths_that_cannot_be_quoted_are_refused(self):
        adapter = self.adapter()
        for bad in ("/var/log/a'b.log", '/var/log/a$b.log', '/var/log/a`id`.log',
                    '/var/log/a\nb.log'):
            with self.assertRaises((CollectorError, SecurityError)):
                adapter.open_snapshot(bad, self.staging)
        self.assertEqual(self.scp.spawns, 0, '校验失败时不得启动 scp')

    def test_digest_mismatch_never_publishes_a_snapshot(self):
        adapter = self.adapter(expectedSha256='0' * 64)
        with self.assertRaises(CollectorError) as caught:
            adapter.open_snapshot('/var/log/app/app.log', self.staging)
        self.assertEqual(caught.exception.code, 'snapshot_unstable')
        leftovers = list(self.staging.iterdir()) if self.staging.exists() else []
        self.assertEqual(leftovers, [], '摘要不符的暂存文件必须删除')

    def test_failed_transfer_reports_the_tool_error(self):
        self.scp.fail = True
        adapter = self.adapter()
        with self.assertRaises(CollectorError) as caught:
            adapter.open_snapshot('/var/log/app/app.log', self.staging)
        self.assertEqual(caught.exception.code, 'transport_error')

    def test_private_key_is_removed_on_close(self):
        from server.collectors.credentials import CredentialStore
        store = CredentialStore(self.root / 'var')
        meta = store.create(kind='key', label='k', value='-----BEGIN KEY-----\nabc\n',
                            actor='admin')
        adapter = ScpSnapshotAdapter(dict(source(knownHostsFile=str(self.known),
                                                 sessionDir=str(self.root / 'sessions')),
                                          credential_ref=meta['id'], credential_kind='key'),
                                     credentials=store, runner=self.scp)
        adapter._load_credential()
        key_file = adapter._key_file
        self.assertEqual(stat.S_IMODE(os.stat(key_file).st_mode), 0o600)
        adapter.close()
        self.assertFalse(os.path.exists(key_file))


if __name__ == '__main__':
    unittest.main()
