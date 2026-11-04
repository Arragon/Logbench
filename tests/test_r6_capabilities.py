"""R6-007 capability probe tests: degradation, journal safety, no secret leakage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import capabilities as cap  # noqa: E402
from server.app import Application, Config  # noqa: E402

STATES = {'enabled', 'degraded', 'disabled', 'unavailable'}


class ProbeShape(unittest.TestCase):
    def test_report_is_self_describing(self):
        payload = cap.collect(ROOT)
        self.assertEqual(payload['schema'], 'logbench.capabilities/1')
        self.assertEqual(set(payload['features']),
                         {'local_reading', 'browser_search', 'browser_storage', 'server_index',
                          'private_upload', 'remote_collect', 'js_engine'})
        for name, probe in payload['probes'].items():
            self.assertIn(probe['state'], STATES, name)
        for name, feature in payload['features'].items():
            self.assertIn(feature['state'], STATES, name)

    def test_report_serialises(self):
        json.dumps(cap.collect(ROOT))

    def test_python_probe_reports_real_interpreter(self):
        probe = cap.probe_python()
        self.assertEqual(probe['version'], '%d.%d.%d' % sys.version_info[:3])
        self.assertIn('meets_target', probe)

    def test_sqlite_probe_creates_real_tables(self):
        probe = cap.probe_sqlite(tempfile.mkdtemp(prefix='lb-cap-'))
        self.assertTrue(probe['version'])
        self.assertIsInstance(probe['fts5'], bool)
        self.assertIn(probe['journal_policy'], ('wal', 'delete'))

    def test_probe_leaves_no_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap.probe_sqlite(tmp)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [],
                             'a capability probe must not leave database files behind')


class JournalSafety(unittest.TestCase):
    def test_old_sqlite_falls_back_to_conservative_journal(self):
        policy = cap._sqlite_journal_policy('3.38.1')
        self.assertFalse(policy['safe_to_use_wal'])
        self.assertEqual(policy['journal_policy'], 'delete')
        self.assertIn('3.44.0', policy['wal_reason'])

    def test_modern_sqlite_may_use_wal(self):
        policy = cap._sqlite_journal_policy('3.46.0')
        self.assertTrue(policy['safe_to_use_wal'])
        self.assertEqual(policy['journal_policy'], 'wal')

    def test_unknown_build_is_not_optimistic(self):
        policy = cap._sqlite_journal_policy('0.0.0')
        self.assertFalse(policy['safe_to_use_wal'])


class Degradation(unittest.TestCase):
    def _probes(self, **overrides):
        base = {
            'sqlite': {'state': 'enabled', 'fts5': True, 'journal_policy': 'wal', 'reason': None},
            'disk': {'state': 'enabled', 'writable': True, 'reason': None},
            'node': {'state': 'enabled', 'reason': None},
            'rclone': {'state': 'enabled', 'reason': None},
            'ssh': {'state': 'enabled', 'reason': None},
        }
        base.update(overrides)
        return base

    def test_missing_tools_never_disable_local_reading(self):
        features = cap.derive_features(self._probes(
            node={'state': 'unavailable', 'reason': 'node 未安装'},
            rclone={'state': 'unavailable', 'reason': 'rclone 未安装'},
            ssh={'state': 'unavailable', 'reason': 'ssh 未安装'},
        ))
        self.assertEqual(features['local_reading']['state'], 'enabled')
        self.assertEqual(features['remote_collect']['state'], 'disabled')
        self.assertTrue(features['remote_collect']['reason'])

    def test_missing_fts5_degrades_to_scan(self):
        features = cap.derive_features(self._probes(
            sqlite={'state': 'degraded', 'fts5': False, 'journal_policy': 'delete',
                    'reason': 'FTS5 不可用'}))
        self.assertEqual(features['server_index']['state'], 'degraded')
        self.assertIn('扫描', features['server_index']['reason'])

    def test_broken_sqlite_disables_server_index_only(self):
        features = cap.derive_features(self._probes(
            sqlite={'state': 'disabled', 'fts5': False, 'journal_policy': 'delete',
                    'reason': 'SQLite 探测失败'}))
        self.assertEqual(features['server_index']['state'], 'disabled')
        self.assertEqual(features['local_reading']['state'], 'enabled')
        self.assertEqual(features['private_upload']['state'], 'enabled')

    def test_read_only_disk_blocks_private_upload(self):
        features = cap.derive_features(self._probes(disk={'state': 'disabled', 'writable': False}))
        self.assertEqual(features['private_upload']['state'], 'disabled')

    def test_node_alone_degrade_is_not_fatal(self):
        features = cap.derive_features(self._probes(node={'state': 'unavailable'}))
        self.assertEqual(features['js_engine']['state'], 'degraded')
        self.assertEqual(features['browser_search']['state'], 'enabled')

    def test_only_ssh_means_degraded_collection(self):
        features = cap.derive_features(self._probes(rclone={'state': 'unavailable'}))
        self.assertEqual(features['remote_collect']['state'], 'degraded')
        self.assertEqual(features['remote_collect']['channels'], ['ssh'])


class NoSecrets(unittest.TestCase):
    def test_environment_values_are_not_echoed(self):
        marker = 'capability-secret-8f3a1c'
        os.environ['LOGBENCH_CAP_TEST_SECRET'] = marker
        os.environ['AWS_SECRET_ACCESS_KEY'] = marker
        try:
            text = json.dumps(cap.collect(ROOT), ensure_ascii=False)
        finally:
            os.environ.pop('LOGBENCH_CAP_TEST_SECRET', None)
            os.environ.pop('AWS_SECRET_ACCESS_KEY', None)
        self.assertNotIn(marker, text)
        self.assertNotIn('LOGBENCH_CAP_TEST_SECRET', text)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', text)

    def test_report_has_no_environment_block(self):
        payload = cap.collect(ROOT)
        self.assertNotIn('env', payload)
        self.assertNotIn('environ', json.dumps(payload))


class CapabilityEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.admin = Application(Config(self.root, code_root=ROOT, bootstrap_admins=['127.0.0.1']))

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_reads_capabilities(self):
        result = self.admin.handle('GET', '/api/v2/capabilities', {'Host': 'localhost'}, peer='127.0.0.1')
        try:
            self.assertEqual(result.status, 200)
            body = result.json() if hasattr(result, 'json') else result.value
            self.assertEqual(body['schema'], 'logbench.capabilities/1')
            self.assertEqual(body['features']['local_reading']['state'], 'enabled')
        finally:
            result.close()

    def test_visitor_is_rejected(self):
        result = self.admin.handle('GET', '/api/v2/capabilities', {'Host': 'localhost'}, peer='203.0.113.9')
        try:
            self.assertEqual(result.status, 403)
        finally:
            result.close()

    def test_health_still_public(self):
        result = self.admin.handle('GET', '/healthz', {'Host': 'localhost'})
        try:
            self.assertEqual(result.status, 200)
        finally:
            result.close()


if __name__ == '__main__':
    unittest.main()
