"""R6-071: what an observation of a remote file actually means.

Pure decision table plus persistence. The cases come from real log rotation
behaviour: append, rename rotation, in-place truncate, same-size replacement,
`copytruncate`, deletion, and evidence too weak to decide.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.collectors.archive import Archive
from server.collectors.generations import (Decision, Observed, Previous, apply_decision,
                                           decide, is_compressed)

ROOT = Path(__file__).resolve().parents[1]


def observed(data: bytes, *, mtime=1000, identity=None, exists=True, name='app.log') -> Observed:
    return Observed(size=len(data), mtime_ns=mtime, identity=dict(identity or {}),
                    head=data[:65536], exists=exists, name=name)


def previous_from(data: bytes, *, mtime=1000, identity=None, generation=1, sealed=False) -> Previous:
    return Previous(size=len(data), mtime_ns=mtime, identity=dict(identity or {}),
                    head_digest=hashlib.sha256(data[:65536]).hexdigest()[:32],
                    generation=generation, sealed=sealed, prefix_len=len(data))


class DecisionTests(unittest.TestCase):
    def test_first_sight_is_a_new_generation(self):
        decision = decide(None, observed(b'hello'))
        self.assertEqual(decision.action, 'new')
        self.assertEqual(decision.generation, 1)

    def test_growth_with_an_unchanged_prefix_is_an_append(self):
        old = b'a' * 1000
        decision = decide(previous_from(old), observed(old + b'b' * 500))
        self.assertEqual(decision.action, 'append')
        self.assertEqual(decision.confidence, 'prefix-only', '抽样前缀不能证明中间未变')
        self.assertIsNone(decision.gap)

    def test_growth_with_a_changed_prefix_is_a_rewrite(self):
        old = b'a' * 1000
        decision = decide(previous_from(old), observed(b'z' * 1000 + b'b' * 500))
        self.assertEqual(decision.action, 'rewritten')
        self.assertEqual(decision.generation, 2)
        self.assertEqual(decision.gap['state'], 'unknown_length')

    def test_truncation_starts_a_new_generation_with_a_gap(self):
        decision = decide(previous_from(b'a' * 1000), observed(b'a' * 200))
        self.assertEqual(decision.action, 'truncated')
        self.assertEqual(decision.reason, 'copytruncate')
        self.assertEqual(decision.gap['lostFrom'], 1000)
        self.assertIsNone(decision.gap['lostTo'], '丢失长度未知，不能编造')

    def test_same_size_different_prefix_is_a_replacement(self):
        decision = decide(previous_from(b'a' * 500), observed(b'b' * 500))
        self.assertEqual(decision.action, 'replaced')

    def test_unchanged_file_is_not_rearchived(self):
        decision = decide(previous_from(b'a' * 500), observed(b'a' * 500))
        self.assertEqual(decision.action, 'unchanged')

    def test_identity_change_wins_over_size(self):
        decision = decide(previous_from(b'a' * 500, identity={'id': 'inode-1'}),
                          observed(b'a' * 900, identity={'id': 'inode-2'}))
        self.assertEqual(decision.action, 'rewritten')
        self.assertEqual(decision.reason, 'identity-changed')

    def test_mtime_going_backwards_with_the_same_content_is_undecidable(self):
        decision = decide(previous_from(b'a' * 500, mtime=2000), observed(b'a' * 500, mtime=1000))
        self.assertEqual(decision.action, 'unknown')
        self.assertEqual(decision.confidence, 'weak')

    def test_missing_prefix_evidence_never_becomes_an_append(self):
        previous = Previous(size=500, generation=1, head_digest='')
        decision = decide(previous, observed(b'a' * 900))
        self.assertEqual(decision.action, 'unknown')

    def test_sealed_generation_means_the_next_bytes_are_a_new_generation(self):
        decision = decide(previous_from(b'a' * 500, sealed=True), observed(b'a' * 900))
        self.assertEqual(decision.action, 'rewritten')
        self.assertEqual(decision.reason, 'previous-generation-sealed')

    def test_renamed_away_with_a_successor_is_a_rotation(self):
        decision = decide(previous_from(b'a' * 500), observed(b'', exists=False),
                          rotation_candidate=True)
        self.assertEqual(decision.action, 'rotated')

    def test_deleted_source_keeps_the_archive_and_says_so(self):
        decision = decide(previous_from(b'a' * 500), observed(b'', exists=False))
        self.assertEqual(decision.action, 'disappeared')
        self.assertEqual(decision.details['note'], '远端删除不传播为归档删除')

    def test_compressed_rotation_is_recognised(self):
        self.assertTrue(is_compressed('app.log.1.gz'))
        self.assertFalse(is_compressed('app.log'))


class ApplyDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'rules').mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                    self.root / 'rules' / 'cpqpsgx-default.json')
        self.app = Application(Config(self.root, bootstrap_admins=['127.0.0.1']))
        self.app.index_jobs.executor = None
        self.archive = Archive(self.app)
        self.plan = self.archive.plan(source={'id': 's', 'label': 'PSG-A'},
                                      mapping={'id': 'm', 'root_id': 'legacy',
                                               'target_subdir': 'psg-a'},
                                      remote_path='/var/log/app.log', name='app.log')
        self.archive.register(self.plan, source_id='s', mapping_id='m', size=1000,
                              evidence={'identity': {'id': 'inode-1'}, 'mtimeNs': 1})
        self.archive.append(self.plan['file_id'], 1, offset=0, data=b'a' * 1000)

    def tearDown(self):
        self.temp.cleanup()

    def row(self):
        with self.app.store.connect() as db:
            return db.execute('SELECT * FROM source_files').fetchone()

    def test_truncation_resets_the_watermarks_and_records_the_gap(self):
        row = self.row()
        decision = decide(previous_from(b'a' * 1000, generation=1), observed(b'a' * 200))
        with self.app.store.transaction() as db:
            apply_decision(db, row, decision, observed=observed(b'a' * 200), source_id='s')
        fresh = self.row()
        self.assertEqual(fresh['generation'], 2)
        self.assertEqual(fresh['contiguous_bytes'], 0)
        self.assertEqual(fresh['gap_state'], 'unknown_length')
        self.assertEqual(fresh['state'], 'gap')
        # The previous generation's intervals are untouched evidence.
        self.assertEqual(self.archive.watermark(self.plan['file_id'], 1)['archived'], 1000)

    def test_append_keeps_the_watermark_and_updates_the_observation(self):
        row = self.row()
        data = b'a' * 1000 + b'b' * 40
        decision = decide(previous_from(b'a' * 1000, generation=1), observed(data, mtime=1200))
        with self.app.store.transaction() as db:
            apply_decision(db, row, decision, observed=observed(data, mtime=1200), source_id='s')
        fresh = self.row()
        self.assertEqual(fresh['generation'], 1)
        self.assertEqual(fresh['contiguous_bytes'], 1000)
        self.assertEqual(fresh['gap_state'], 'none')
        evidence = json.loads(fresh['evidence'])
        self.assertEqual(evidence['mtimeNs'], 1200)
        # The stored digest covers what the observation actually contained; the next
        # comparison limits itself to the bytes we can vouch for.
        self.assertEqual(evidence['headDigest'], hashlib.sha256(b'a' * 1000 + b'b' * 40).hexdigest()[:32])


if __name__ == '__main__':
    unittest.main()
