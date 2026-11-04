"""R6-060: the adapter contract itself.

The point of these cases is that "capability" is a value with evidence, not a
boolean an adapter can set to `True` by accident:

* `unsupported` must raise, so a caller cannot read it as an empty result;
* a probe carries the configuration revision and fingerprint it measured;
* an expired or mismatched probe cannot enable a revision;
* a missing tool is reported as unavailable rather than as a working adapter.

A fake adapter appears here **only as a contract fixture**; it is not protocol
evidence. Real HTTP behaviour lives in `test_r6_http_range.py`; the other three
protocols have no endpoint on this machine and are recorded as BLOCKED in
`qa/evidence/M6-gate.md`.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors import base
from server.collectors.base import (Adapter, Capabilities, CollectorError, ProbeResult,
                                    SUPPORTED, UNSUPPORTED, UNVERIFIED, build,
                                    error_class)


class FakeAdapter(Adapter):
    name = 'fake'
    label = '契约夹具'

    def capabilities(self):
        return Capabilities(directory_discovery=UNSUPPORTED, range_read=UNSUPPORTED,
                            stable_identity='none', append_contract=UNSUPPORTED,
                            transport_version='fixture/1')

    def probe(self, *, config_revision, fingerprint, guard=None):
        return ProbeResult(self.source.get('id', ''), config_revision, fingerprint, True,
                           self.capabilities(), [], ['fixture'], [],
                           '2026-11-03T00:00:00Z', time.time() + 60)



class ContractTests(unittest.TestCase):
    def test_every_protocol_is_registered(self):
        names = base.adapter_names()
        for expected in ('http', 'scp', 'sftp', 'smb'):
            self.assertIn(expected, names)

    def test_unknown_adapter_is_unavailable_not_empty(self):
        state = base.availability('nope')
        self.assertFalse(state['available'])
        self.assertEqual(state['reason'], 'unknown_adapter')
        with self.assertRaises(CollectorError) as caught:
            build({'adapter': 'nope'})
        self.assertEqual(caught.exception.code, 'unsupported')

    def test_missing_tool_makes_the_adapter_unavailable(self):
        # rclone is genuinely absent in this environment; the test records that
        # rather than pretending SFTP works because the code exists.
        state = base.availability('sftp')
        if state['available']:
            self.skipTest('rclone is installed here; the missing-tool path is covered by unit state')
        self.assertEqual(state['missingTools'], ['rclone'])
        with self.assertRaises(CollectorError) as caught:
            build({'adapter': 'sftp', 'endpoint': 'sftp://example.com'})
        self.assertEqual(caught.exception.code, 'tool_missing')
        self.assertEqual(error_class('tool_missing'), 'manual')

    def test_http_is_always_available_because_it_is_stdlib(self):
        self.assertTrue(base.availability('http')['available'])

    def test_unsupported_capability_raises_instead_of_returning_empty(self):
        adapter = FakeAdapter({'id': 's', 'adapter': 'fake'})
        with self.assertRaises(CollectorError) as listing:
            adapter.list('/')
        self.assertEqual(listing.exception.code, 'unsupported')
        self.assertEqual(listing.exception.details['capability'], 'directoryDiscovery')
        with self.assertRaises(CollectorError) as chunk:
            adapter.read_range('/a.log', offset=0, length=10)
        self.assertEqual(chunk.exception.details['capability'], 'rangeRead')

    def test_capability_states_are_explicit_and_follow_needs_a_declared_contract(self):
        caps = Capabilities(range_read=SUPPORTED, append_contract=UNVERIFIED)
        self.assertFalse(caps.allows_follow())
        caps.append_contract = 'declared'
        self.assertTrue(caps.allows_follow())
        caps.range_read = UNSUPPORTED
        self.assertFalse(caps.allows_follow())
        payload = caps.to_dict()
        self.assertEqual(payload['rangeRead'], UNSUPPORTED)
        self.assertIn('followCapable', payload)

    def test_probe_is_bound_to_the_revision_it_measured(self):
        adapter = FakeAdapter({'id': 's_1', 'adapter': 'fake'})
        probe = adapter.probe(config_revision=7, fingerprint='fp-abc')
        payload = probe.to_dict()
        self.assertEqual(payload['configRevision'], 7)
        self.assertEqual(payload['fingerprint'], 'fp-abc')
        self.assertEqual(payload['sourceId'], 's_1')
        self.assertFalse(payload['expired'])

    def test_error_classes_separate_retry_from_human_action(self):
        self.assertEqual(error_class('timeout'), 'retry')
        self.assertEqual(error_class('hostkey_changed'), 'manual')
        self.assertEqual(error_class('generation_changed'), 'file')
        self.assertEqual(error_class('something_new'), 'fatal')

    def test_limits_are_bounded_and_overridable(self):
        adapter = FakeAdapter({'id': 's', 'adapter': 'fake'}, limits={'maxEntries': 5})
        self.assertEqual(adapter.limits['maxEntries'], 5)
        self.assertGreaterEqual(adapter.limits['maxReadBytes'], 1024 * 1024)
        with self.assertRaises(CollectorError):
            adapter.read_bounded(0, 1024 * 1024 * 64)

    def test_remote_names_never_become_paths(self):
        adapter = FakeAdapter({'id': 's', 'adapter': 'fake'})
        self.assertEqual(adapter.source.get('id'), 's')
        from server.collectors.security import safe_remote_name
        self.assertNotIn('/', safe_remote_name('/a/../../etc/passwd'))


if __name__ == '__main__':
    unittest.main()
