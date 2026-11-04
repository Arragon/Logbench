"""R6-081: rule signatures, impact preview, trial run and apply.

What is asserted, and why each one matters:

* the *same* profile reports zero affected files — if the impact calculation were a
  guess, its own baseline would not be zero;
* a colour/shape/name edit also reports zero — style is not semantics, and a rebuild
  that produces identical output is worse than no rebuild;
* an anomaly or extractor edit names both derived layers — the rule layer and the
  search projection depend on it, the structure layer does not;
* applying a change keeps the structure pointer's signature — that is the machine
  check behind "改规则不重新分帧";
* the trial run reports the same evidence shape the reader uses, and it never reads a
  local file: the only input is the text the caller posted.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
SEED = ROOT / 'rules' / 'cpqpsgx-default.json'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(SEED, root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None          # run jobs inline, deterministically
    app.queries.executor_pool = None
    return app


class RuleImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        shutil.copy(GOLDEN / 'edge_cases.log', self.data / 'logs' / 'edge.log')
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.profile = json.loads(SEED.read_text('utf-8'))
        self.file_id = self.index('logs/edge.log')

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, ip=None):
        ip = ip or self.ip
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, headers, body, ip)

    def index(self, path: str) -> str:
        created = self.request('POST', '/api/v2/index-jobs', {'path': path})
        self.assertEqual(created.status, 200, created.value)
        item = created.value['item']
        self.app.index_jobs.run_now(item['id'])
        return item['payload']['file_id']

    def impact(self, profile: dict) -> dict:
        response = self.request('POST', '/api/v2/rules/impact', {'profile': profile})
        self.assertEqual(response.status, 200, response.value)
        return response.value

    def pointer(self, layer: str) -> dict:
        with self.app.store.connect() as db:
            row = db.execute('SELECT p.version_id, v.signature FROM layer_pointers p '
                             'JOIN layer_versions v ON v.version_id=p.version_id '
                             "WHERE p.file_id=? AND p.layer=? AND p.scope='shared'",
                             (self.file_id, layer)).fetchone()
        return {'version': row['version_id'], 'signature': row['signature']} if row else {}

    def variant(self, **edits) -> dict:
        profile = json.loads(json.dumps(self.profile))
        for key, value in edits.items():
            profile[key] = value
        return profile

    # -------------------------------------------------------------------- list
    def test_rule_list_carries_semantic_signatures(self):
        response = self.request('GET', '/api/v2/rules?signatures=1')
        self.assertEqual(response.status, 200, response.value)
        item = response.value['items'][0]
        self.assertEqual(response.value['semantic_source'], 'node')
        self.assertEqual(set(item['signatures']), {'structure', 'extract', 'search'})
        self.assertIsNone(item['signatureError'])
        self.assertGreaterEqual(item['semantic']['extractors'], 1)

    def test_rule_list_without_signatures_does_not_start_the_node_pool(self):
        before = len(self.app.index_jobs.pool.workers)
        response = self.request('GET', '/api/v2/rules')
        self.assertEqual(response.status, 200, response.value)
        self.assertIsNone(response.value['items'][0]['signatures'])
        self.assertEqual(len(self.app.index_jobs.pool.workers), before)

    # ------------------------------------------------------------------ impact
    def test_current_profile_affects_nothing(self):
        report = self.impact(self.profile)
        self.assertEqual(report['totals']['affected'], 0, report['totals'])
        self.assertEqual(report['totals']['already_current'], 1)
        self.assertEqual(report['totals']['indexed'], 1)
        self.assertEqual(report['layers']['rules']['affected'], 0)
        self.assertEqual(report['files'][0]['layers'], [])

    def test_style_only_edit_affects_nothing(self):
        profile = json.loads(json.dumps(self.profile))
        for node in profile.get('nodes') or []:
            node['color'] = '#123456'
            node['shape'] = 'hexagon'
            node['name'] = '改过的名字'
            node['priority'] = 9
        for extractor in profile.get('extractors') or []:
            # A name is presentation; `unit` is not (it belongs to the extracted
            # value's type), so this test deliberately leaves it alone.
            extractor['name'] = '改名后的提取规则'
        report = self.impact(profile)
        self.assertEqual(report['totals']['affected'], 0, report['layers'])
        self.assertEqual(report['signatures'], self.impact(self.profile)['signatures'])

    def test_anomaly_edit_marks_rules_and_search_only(self):
        profile = self.variant(anomalies=[{
            'id': 'extra-anomaly', 'name': '新增失败判定', 'field': 'message',
            'op': 'contains', 'value': 'NEVER-MATCHES-XYZZY', 'severity': 'warning',
            'enabled': True}])
        report = self.impact(profile)
        self.assertEqual(report['totals']['rules_affected'], 1)
        self.assertEqual(report['totals']['search_affected'], 1)
        self.assertEqual(report['layers']['structure']['affected'], 0)
        self.assertEqual(report['files'][0]['layers'], ['rules', 'search'])

    def test_extractor_edit_changes_extract_signature(self):
        profile = json.loads(json.dumps(self.profile))
        profile.setdefault('extractors', []).append({
            'id': 'extra-extract', 'name': '额外提取', 'pattern': 'iter=(\\d+)',
            'source': 'message', 'key': 'iteration', 'group': '1', 'type': 'number',
            'enabled': True})
        baseline = self.impact(self.profile)['signatures']
        report = self.impact(profile)
        self.assertNotEqual(report['signatures']['extract'], baseline['extract'])
        self.assertEqual(report['totals']['rules_affected'], 1)

    def test_unknown_file_ids_are_reported_not_counted(self):
        report = self.impact(self.profile)
        self.assertEqual(report['totals']['not_indexed'], 0)
        with self.app.store.connect() as db:
            db.execute("UPDATE layer_pointers SET layer='structure' WHERE layer='structure'")
        response = self.request('POST', '/api/v2/rules/impact',
                                {'profile': self.profile, 'fileIds': ['f_missing']})
        self.assertEqual(response.status, 200, response.value)
        self.assertEqual(response.value['totals']['visible'], 0)
        self.assertEqual(response.value['totals']['affected'], 0)

    def test_non_admin_cannot_preview_or_trial(self):
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf('10.0.0.9')}
        for target, payload in (('/api/v2/rules/impact', {'profile': self.profile}),
                                ('/api/v2/rules/trial', {'profile': self.profile, 'sample': 'x'})):
            response = self.app.handle('POST', target, headers,
                                       json.dumps(payload).encode(), '10.0.0.9')
            self.assertEqual(response.status, 403, response.value)

    def test_invalid_profile_is_rejected_with_a_reason(self):
        broken = self.variant(extractors=[{'name': '', 'pattern': '(', 'source': 'message'}])
        response = self.request('POST', '/api/v2/rules/impact', {'profile': broken})
        self.assertEqual(response.status, 400, response.value)
        self.assertEqual(response.value['error']['code'], 'invalid_profile')

    # ------------------------------------------------------------------- trial
    def test_trial_runs_the_real_framer_and_rule_layer(self):
        sample = (GOLDEN / 'edge_cases.log').read_text('utf-8')
        response = self.request('POST', '/api/v2/rules/trial',
                                {'profile': self.profile, 'sample': sample, 'limit': 5})
        self.assertEqual(response.status, 200, response.value)
        body = response.value
        self.assertGreater(body['framed_records'], 0)
        self.assertLessEqual(body['returned'], 5)
        record = body['records'][0]
        for key in ('ordinal', 'ts', 'level', 'message', 'fields', 'issues', 'nodes'):
            self.assertIn(key, record)
        self.assertTrue(any(record['fields']) or any(item['fields'] for item in body['records']),
                        'sample should produce at least one extracted field')

    def test_trial_rejects_an_empty_or_oversized_sample(self):
        empty = self.request('POST', '/api/v2/rules/trial', {'profile': self.profile, 'sample': '  '})
        self.assertEqual(empty.status, 400, empty.value)
        self.assertEqual(empty.value['error']['code'], 'no_sample')
        big = 'x' * (300 * 1024)
        oversized = self.request('POST', '/api/v2/rules/trial',
                                 {'profile': self.profile, 'sample': big})
        self.assertEqual(oversized.status, 413, oversized.value)

    def test_trial_reports_a_broken_rule_instead_of_losing_records(self):
        # Server-side validation is structural; a regex that does not compile is
        # caught where regexes actually run (the Node worker). The contract is that
        # framed records survive and the failure is attributed, not that the request
        # 500s or that the sample disappears.
        profile = self.variant(anomalies=[{
            'id': 'bad', 'name': '坏正则', 'field': 'message', 'op': 'regex',
            'value': '([', 'severity': 'error', 'enabled': True}])
        response = self.request('POST', '/api/v2/rules/trial',
                                {'profile': profile, 'sample': '[2026-01-01 00:00:00] [INFO] x\n',
                                 'limit': 3})
        self.assertEqual(response.status, 200, response.value)
        body = response.value
        self.assertTrue(body['rules_failed'])
        self.assertGreaterEqual(body['framed_records'], 1)
        self.assertTrue('正则' in body['records'][0]['rules_failed'])

    # ------------------------------------------------------------------- apply
    def test_apply_bumps_the_revision_and_rebuilds_only_derived_layers(self):
        with self.app.store.connect() as db:
            row = db.execute('SELECT revision FROM rules WHERE id=?', ('cpqpsgx-default',)).fetchone()
        revision = row['revision']
        profile = self.variant(anomalies=[{
            'id': 'extra-anomaly', 'name': '新增失败判定', 'field': 'message',
            'op': 'contains', 'value': 'NEVER-MATCHES-XYZZY', 'severity': 'warning',
            'enabled': True}])
        before_structure = self.pointer('structure')
        before_rules = self.pointer('rules')
        response = self.request('POST', '/api/v2/rules/cpqpsgx-default/apply',
                                {'profile': profile, 'expectedRevision': revision,
                                 'actionId': 'act-test-1'})
        self.assertEqual(response.status, 200, response.value)
        body = response.value
        self.assertEqual(body['rule']['revision'], revision + 1)
        self.assertGreaterEqual(body['queued'], 1)
        # Run the queued rebuild the way the background executor would.
        for job in body['jobs']:
            if job.get('jobId'):
                self.app.index_jobs.run_now(job['jobId'])
        after_structure = self.pointer('structure')
        after_rules = self.pointer('rules')
        self.assertEqual(before_structure['signature'], after_structure['signature'],
                         'a rule change must not re-frame the structure layer')
        self.assertNotEqual(before_rules['signature'], after_rules['signature'])
        # After the rebuild, the same profile reports nothing left to do.
        report = self.impact(profile)
        self.assertEqual(report['totals']['affected'], 0, report['totals'])

    def test_apply_refuses_a_stale_revision(self):
        response = self.request('POST', '/api/v2/rules/cpqpsgx-default/apply',
                                {'profile': self.profile, 'expectedRevision': 999})
        self.assertEqual(response.status, 409, response.value)
        self.assertEqual(response.value['error']['code'], 'revision_conflict')

    def test_apply_refuses_an_unknown_rule(self):
        response = self.request('POST', '/api/v2/rules/nope/apply',
                                {'profile': self.profile, 'expectedRevision': 1})
        self.assertEqual(response.status, 404, response.value)


if __name__ == '__main__':
    unittest.main()
