"""R6-061: SFTP adapter logic against deterministic tool output.

Real endpoint: **BLOCKED** (no SFTP server, no rclone on this machine). What is
verified here is the part that is ours: host-key policy, credential materialisation,
listing filters, exact byte ranges, windowed spawn accounting and refusing to append
to a changed generation. The fixture is an rclone-shaped binary, and the evidence
file says so.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors.base import CollectorError
from server.collectors.credentials import CredentialStore
from server.collectors.sftp import SftpAdapter
from tests.r6_collector_fixtures import FakeTool

DATA = b''.join(b'2026-11-03 10:00:00 INFO line=%05d\n' % index for index in range(100000))


def source(path='/var/log/app', **policy):
    base_policy = {'user': 'logbench', 'allowedHosts': ['127.0.0.1'], 'allowLoopback': True,
                   'tools': {'rclone': '/bin/echo'}}
    base_policy.update(policy)
    return {'id': 'src_sftp', 'adapter': 'sftp', 'endpoint': 'sftp://127.0.0.1:22',
            'path': path, 'policy': base_policy}


class SftpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.known_hosts = self.root / 'known_hosts'
        self.known_hosts.write_text('127.0.0.1 ssh-ed25519 AAAAC3Nza\n', 'utf-8')
        self.tool = FakeTool({'/var/log/app/app.log': {'data': DATA,
                                                       'mtime': '2026-11-03T10:00:00Z'}},
                             directories={'/var/log', '/var/log/app'})
        self.store = CredentialStore(self.root / 'var')

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, **policy) -> SftpAdapter:
        policy.setdefault('knownHostsFile', str(self.known_hosts))
        policy.setdefault('sessionDir', str(self.root / 'sessions'))
        return SftpAdapter(source(**policy), credentials=self.store, runner=self.tool)

    def test_probe_without_host_key_evidence_fails_instead_of_trusting(self):
        adapter = SftpAdapter(source(), credentials=self.store, runner=self.tool)
        probe = adapter.probe(config_revision=1, fingerprint='fp')
        self.assertFalse(probe.ok)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['hostkey'].state, 'fail')
        self.assertIn('known_hosts', checks['hostkey'].detail)

    def test_probe_reports_capabilities_and_a_bound_revision(self):
        probe = self.adapter().probe(config_revision=4, fingerprint='fp-4')
        self.assertTrue(probe.ok, [check.to_dict() for check in probe.checks])
        caps = probe.capabilities
        self.assertEqual(caps.range_read, 'supported')
        self.assertEqual(caps.directory_discovery, 'supported')
        self.assertEqual(caps.stable_identity, 'size-mtime')
        self.assertEqual(caps.append_contract, 'unverified')
        self.assertFalse(caps.allows_follow())
        self.assertEqual(probe.config_revision, 4)
        self.assertEqual(probe.fingerprint, 'fp-4')
        self.assertTrue(any('inode' in warning for warning in probe.warnings))

    def test_host_key_pin_change_stops_the_source(self):
        adapter = self.adapter()
        adapter.source['host_pin'] = 'SHA256:old'
        adapter.policy['hostKey'] = 'SHA256:new'
        probe = adapter.probe(config_revision=2, fingerprint='fp')
        self.assertFalse(probe.ok)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['hostkey'].state, 'fail')

    def test_listing_filters_and_paginates(self):
        self.tool.files['/var/log/app/other.txt'] = {'data': b'x', 'mtime': '2026-11-03T10:00:00Z'}
        self.tool.files['/var/log/app/dump.bin'] = {'data': b'y', 'mtime': '2026-11-03T10:00:00Z'}
        adapter = self.adapter(include=['*.log'], exclude=['dump*'])
        listing = adapter.list('/var/log/app', limit=1)
        self.assertEqual([entry.name for entry in listing.entries], ['app.log'])
        self.assertIsNone(listing.next_cursor)
        self.assertEqual(listing.scanned, 1)

    def test_unknown_directory_is_an_error_not_an_empty_list(self):
        adapter = self.adapter()
        with self.assertRaises(CollectorError) as caught:
            adapter.list('/var/log/absent')
        self.assertIn(caught.exception.code, ('not_found', 'directory_not_found'))

    def test_reads_are_exact_and_windowed(self):
        adapter = self.adapter()
        stat = adapter.stat('/var/log/app/app.log')
        self.assertEqual(stat.size, len(DATA))
        first = adapter.read_range('/var/log/app/app.log', offset=0, length=1024 * 1024,
                                   identity={'size': stat.size, 'modTime': stat.identity['modTime']})
        self.assertEqual(first.data, DATA[:1024 * 1024])
        second = adapter.read_range('/var/log/app/app.log', offset=1024 * 1024, length=1024 * 1024,
                                    identity={'size': stat.size, 'modTime': stat.identity['modTime']})
        self.assertEqual(second.data, DATA[1024 * 1024:2 * 1024 * 1024])
        cats = [call for call in self.tool.calls if len(call) > 1 and call[1] == 'cat']
        self.assertEqual(len(cats), 1, '两个 1MiB 块应共用一次 8MiB 窗口读取')
        self.assertEqual(self.tool.spawns, 4,
                         'version + 两次 stat（每次读取都复核代次）+ 一次窗口读取')

    def test_size_change_between_reads_is_a_generation_change(self):
        adapter = self.adapter()
        stat = adapter.stat('/var/log/app/app.log')
        self.tool.files['/var/log/app/app.log']['data'] = DATA + b'appended\n'
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/var/log/app/app.log', offset=0, length=16,
                               identity={'size': stat.size, 'modTime': stat.identity['modTime']})
        self.assertEqual(caught.exception.code, 'generation_changed')

    def test_private_key_is_materialised_private_and_removed(self):
        meta = self.store.create(kind='key', label='k', value='-----BEGIN KEY-----\nabc\n',
                                 actor='admin')
        self.tool.files['/var/log/app/app.log'] = {'data': b'abc', 'mtime': '2026-11-03T10:00:00Z'}
        adapter = SftpAdapter(dict(source(), credential_ref=meta['id'],
                                   credential_kind='key'),
                              credentials=self.store, runner=self.tool)
        adapter._load_credential()
        key_file = adapter._key_file
        self.assertTrue(key_file and os.path.isfile(key_file))
        self.assertEqual(stat.S_IMODE(os.stat(key_file).st_mode), 0o600)
        adapter.close()
        self.assertFalse(os.path.exists(key_file), '关闭连接后私钥临时文件必须删除')

    def test_missing_credential_is_reported_as_manual_recovery(self):
        adapter = self.adapter()
        adapter.source['credential_ref'] = 'cred_deadbeef'
        probe = adapter.probe(config_revision=1, fingerprint='fp')
        self.assertFalse(probe.ok)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['admission'].state, 'fail')
        self.assertEqual(checks['admission'].evidence.get('code'), 'credential_missing')

    def test_shell_metacharacters_in_a_path_are_refused(self):
        adapter = self.adapter()
        with self.assertRaises(Exception):
            adapter.list('/var/log/a;rm -rf /')


if __name__ == '__main__':
    unittest.main()
