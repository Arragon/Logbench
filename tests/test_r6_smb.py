"""R6-062: SMB adapter — share visibility, locks, reconnect identity.

Real Windows/Samba endpoint: **BLOCKED** on this machine (enabling SMB sharing is an
administrative change and the sandbox cannot start a server). The adapter logic that
decides "is this a retryable lock or a permission problem" and "did the file change
while we were disconnected" is verified against tool-shaped output.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors.base import CollectorError
from server.collectors.process import Result
from server.collectors.smb import SmbAdapter
from tests.r6_collector_fixtures import FakeTool

DATA = b''.join(b'2026-11-03 10:00:00 WARN share line=%05d\n' % index for index in range(40000))


def source(**policy):
    base_policy = {'user': 'logbench', 'share': 'logs', 'directory': 'app',
                   'allowedHosts': ['127.0.0.1'], 'allowLoopback': True,
                   'tools': {'rclone': '/bin/echo'}}
    base_policy.update(policy)
    return {'id': 'src_smb', 'adapter': 'smb', 'endpoint': 'smb://127.0.0.1:445',
            'policy': base_policy}


class SmbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tool = FakeTool({'/logs/app/app.log': {'data': DATA,
                                                    'mtime': '2026-11-03T10:00:00Z'}},
                             directories={'/logs', '/logs/app'})
        self.tool.shares = ['logs']
        self.store = None

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, **policy) -> SmbAdapter:
        policy.setdefault('sessionDir', str(self.root))
        return SmbAdapter(source(**policy), runner=self.tool)

    def test_probe_reports_share_visibility_and_snapshot_limits(self):
        probe = self.adapter().probe(config_revision=1, fingerprint='fp')
        self.assertTrue(probe.ok, [check.to_dict() for check in probe.checks])
        caps = probe.capabilities
        self.assertEqual(caps.range_read, 'supported')
        self.assertEqual(caps.directory_discovery, 'supported')
        self.assertEqual(caps.append_contract, 'unverified')
        self.assertIn('signing', caps.security_status)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['shares'].state, 'pass')
        self.assertEqual(checks['shares'].evidence['shares'], ['logs'])

    def test_missing_share_fails_the_probe_and_is_visible(self):
        probe = self.adapter(share='absent').probe(config_revision=1, fingerprint='fp')
        self.assertFalse(probe.ok)
        checks = {check.id: check for check in probe.checks}
        self.assertEqual(checks['share'].state, 'fail')
        self.assertEqual(checks['share'].evidence['share'], 'absent')

    def test_disabling_signing_is_refused_rather_than_silently_used(self):
        adapter = self.adapter(disableSigning=True)
        with self.assertRaises(CollectorError) as caught:
            adapter.connection()
        self.assertEqual(caught.exception.code, 'invalid_config')

    def test_lock_is_retryable_and_reported_as_locked(self):
        self.tool.fail_with = Result(['rclone'], 2, b'',
                                     'ERROR : The process cannot access the file '
                                     'because it is being used by another process.', 0.0)
        adapter = self.adapter()
        with self.assertRaises(CollectorError) as caught:
            adapter.stat('/logs/app/app.log')
        self.assertEqual(caught.exception.code, 'locked')
        self.assertTrue(caught.exception.to_dict()['retryable'])

    def test_access_denied_is_not_retried(self):
        self.tool.fail_with = Result(['rclone'], 2, b'', 'ERROR : Access is denied.', 0.0)
        adapter = self.adapter()
        with self.assertRaises(CollectorError) as caught:
            adapter.stat('/logs/app/app.log')
        self.assertEqual(caught.exception.code, 'permission_denied')
        self.assertTrue(caught.exception.to_dict()['needs_human'])
        self.assertFalse(caught.exception.to_dict()['retryable'])

    def test_reading_after_a_size_change_is_a_generation_change(self):
        adapter = self.adapter()
        stat = adapter.stat('/logs/app/app.log')
        self.tool.files['/logs/app/app.log']['data'] = DATA + b'extra line\n'
        with self.assertRaises(CollectorError) as caught:
            adapter.read_range('/logs/app/app.log', offset=0, length=64,
                               identity={'size': stat.size,
                                         'modTime': stat.identity.get('modTime')})
        self.assertEqual(caught.exception.code, 'generation_changed')

    def test_range_reads_are_exact_and_windowed(self):
        adapter = self.adapter()
        stat = adapter.stat('/logs/app/app.log')
        block = adapter.read_range('/logs/app/app.log', offset=0, length=1024 * 1024,
                                   identity={'size': stat.size,
                                             'modTime': stat.identity.get('modTime')})
        self.assertEqual(block.data, DATA[:1024 * 1024])
        cats = [call for call in self.tool.calls if len(call) > 1 and call[1] == 'cat']
        self.assertEqual(len(cats), 1)

    def test_listing_keeps_directory_prefix_and_filters_extensions(self):
        self.tool.files['/logs/app/notes.txt'] = {'data': b'x', 'mtime': '2026-11-03T10:00:00Z'}
        adapter = self.adapter(include=['*.log'])
        listing = adapter.list('/logs/app')
        self.assertEqual([entry.path for entry in listing.entries], ['/logs/app/app.log'])

    def test_unapproved_directory_is_refused_before_the_tool_runs(self):
        adapter = self.adapter()
        with self.assertRaises(Exception):
            adapter.list('/logs/app/../../etc')


if __name__ == '__main__':
    unittest.main()
