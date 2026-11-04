"""R6-055: layered rule resolution and its explanation.

The chain is personal file → personal directory → shared file → shared directory
→ mapping default → service default. Two properties are easy to get wrong and are
therefore pinned here: a binding must never leak across roots that happen to share
a relative path, and a binding whose rule was deleted must be reported as skipped
instead of quietly becoming the next layer.
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

LINE = '2026-01-01 10:00:00.000 [INFO] boot\n'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = make_app(self.base / 'deploy')
        self.ip = '127.0.0.1'
        self.actor = self.app.actor(self.ip)
        self.data = self.app.config.data_dir
        for path in ('psg-a/sw', 'psg-a/algorithm'):
            (self.data / path).mkdir(parents=True, exist_ok=True)
        (self.data / 'psg-a' / 'sw' / 'app.log').write_text(LINE, 'utf-8')
        self.rules = {}
        for name in ('A', 'B', 'C', 'D'):
            item = self.request('POST', '/api/v1/rules', {
                'name': 'Rule ' + name,
                'profile': {'headerPattern': '^(?<message>.*)$', 'moduleRules': [],
                            'stepRules': [], 'errorRules': []}}).value['item']
            self.rules[name] = item['id']
        self.archive = self.base / 'archive'
        (self.archive / 'psg-a' / 'sw').mkdir(parents=True)
        (self.archive / 'psg-a' / 'sw' / 'app.log').write_text(LINE, 'utf-8')
        self.archive_root = self.app.storage.roots.create(
            self.actor, {'label': '归档', 'path': str(self.archive)})['item']['root_id']

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    def request(self, method, target, payload=None, ip=None):
        ip = ip or self.ip
        headers = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                   'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, headers, body, ip)

    def bind(self, scope, path, rule, *, root='legacy', personal=False):
        target = '/api/v2/bindings/personal' if personal else '/api/v2/bindings'
        return self.request('PUT', target, {'scope': scope, 'rootId': root, 'path': path,
                                           'ruleId': rule, 'expectedRevision': 0})

    def effective(self, path, root='legacy'):
        return self.request('GET', '/api/v2/bindings/effective?rootId=%s&path=%s'
                            % (root, path)).value

    # ----------------------------------------------------------- precedence
    def test_chain_priority_from_personal_file_down_to_service_default(self):
        self.bind('directory', 'psg-a', self.rules['A'])
        self.bind('directory', 'psg-a/sw', self.rules['B'])
        self.bind('file', 'psg-a/sw/app.log', self.rules['C'])
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['C'])
        self.bind('file', 'psg-a/sw/app.log', self.rules['D'], personal=True)
        result = self.effective('psg-a/sw/app.log')
        self.assertEqual(result['ruleId'], self.rules['D'])
        self.assertEqual(result['layer'], 'personal_file')
        self.assertIn('personal_directory', [row['layer'] for row in result['chain']])
        self.assertIn('service_default', [row['layer'] for row in result['chain']])
        # The whole chain is ordered, which is what the UI prints.
        layers = [row['layer'] for row in result['chain']]
        self.assertEqual(layers, ['personal_file', 'personal_directory', 'shared_file',
                                  'shared_directory', 'mapping_default', 'service_default'])

    def test_directory_binding_applies_to_descendants_only(self):
        self.bind('directory', 'psg-a/sw', self.rules['B'])
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['B'])
        self.assertEqual(self.effective('psg-a/algorithm/x.log')['layer'], 'service_default')

    def test_mapping_default_sits_between_directory_and_service(self):
        self.bind('mapping', 'psg-a/sw', self.rules['A'])
        self.assertEqual(self.effective('psg-a/sw/app.log')['layer'], 'mapping_default')
        self.bind('directory', 'psg-a', self.rules['B'])
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['B'])

    def test_same_path_in_another_root_does_not_inherit(self):
        self.bind('directory', 'psg-a', self.rules['A'])
        archive = self.effective('psg-a/sw/app.log', root=self.archive_root)
        self.assertEqual(archive['layer'], 'service_default')
        self.assertNotEqual(archive['ruleId'], self.rules['A'])

    # --------------------------------------------------------- personal layer
    def test_clearing_a_personal_override_restores_the_shared_rule(self):
        self.bind('directory', 'psg-a', self.rules['A'])
        self.bind('directory', 'psg-a', self.rules['B'], personal=True)
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['B'])
        removed = self.request('DELETE', '/api/v2/bindings/personal',
                               {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                                'expectedRevision': 1})
        self.assertEqual(removed.status, 200, removed.value)
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['A'])

    def test_personal_layer_cannot_be_written_by_scope_mapping(self):
        response = self.request('PUT', '/api/v2/bindings/personal',
                                {'scope': 'mapping', 'rootId': 'legacy', 'path': 'psg-a',
                                 'ruleId': self.rules['A'], 'expectedRevision': 0})
        self.assertEqual(response.status, 400)

    def test_only_admins_write_shared_bindings(self):
        viewer_ip = '10.5.5.5'
        response = self.request('PUT', '/api/v2/bindings',
                                {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                                 'ruleId': self.rules['A'], 'expectedRevision': 0},
                                ip=viewer_ip)
        self.assertEqual(response.status, 403)
        personal = self.request('PUT', '/api/v2/bindings/personal',
                                {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                                 'ruleId': self.rules['A'], 'expectedRevision': 0},
                                ip=viewer_ip)
        self.assertEqual(personal.status, 200, personal.value)

    # ------------------------------------------------------------- conflicts
    def test_stale_revision_is_a_conflict_and_the_value_is_not_written(self):
        first = self.bind('directory', 'psg-a', self.rules['A'])
        self.assertEqual(first.status, 200, first.value)
        stale = self.request('PUT', '/api/v2/bindings',
                             {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                              'ruleId': self.rules['B'], 'expectedRevision': 0})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.value['error']['code'], 'REVISION_CONFLICT')
        self.assertEqual(self.effective('psg-a/sw/app.log')['ruleId'], self.rules['A'])

    def test_binding_to_a_missing_rule_is_refused(self):
        response = self.bind('directory', 'psg-a', 'no-such-rule')
        self.assertEqual(response.status, 404)
        self.assertEqual(response.value['error']['code'], 'rule_not_found')

    def test_deleted_rule_is_reported_as_skipped_not_silently_defaulted(self):
        self.bind('directory', 'psg-a', self.rules['A'])
        rule = self.request('GET', '/api/v1/rules/' + self.rules['A']).value['item']
        # Detach first so the delete is allowed, then re-point the binding by SQL is
        # not an option: instead delete the rule after removing its binding and
        # create a binding row that still names it (the state an upgrade can leave).
        self.request('DELETE', '/api/v2/bindings',
                     {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                      'expectedRevision': 1})
        deleted = self.request('DELETE', '/api/v1/rules/' + self.rules['A'],
                               {'expectedRevision': rule['revision']})
        self.assertEqual(deleted.status, 200, deleted.value)
        with self.app.store.transaction() as db:
            db.execute("INSERT INTO bindings VALUES('directory','psg-a',?,1,datetime('now'))",
                       (self.rules['A'],))
        result = self.effective('psg-a/sw/app.log')
        self.assertEqual(result['layer'], 'service_default')
        skipped = [row for row in result['chain'] if row['reason'] == 'rule_missing']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['ruleId'], self.rules['A'])
        self.assertIn('已删除', result['explanation'])

    def test_rule_delete_is_blocked_by_root_and_personal_bindings(self):
        rule = self.request('GET', '/api/v1/rules/' + self.rules['B']).value['item']
        self.bind('directory', 'psg-a', self.rules['B'], personal=True)
        blocked = self.request('DELETE', '/api/v1/rules/' + self.rules['B'],
                               {'expectedRevision': rule['revision']})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.value['error']['code'], 'RULE_IN_USE')
        self.bind('mapping', 'psg-a/sw', self.rules['B'], root=self.archive_root)
        self.request('DELETE', '/api/v2/bindings/personal',
                     {'scope': 'directory', 'rootId': 'legacy', 'path': 'psg-a',
                      'expectedRevision': 1})
        still = self.request('DELETE', '/api/v1/rules/' + self.rules['B'],
                             {'expectedRevision': rule['revision']})
        self.assertEqual(still.status, 409)

class FrozenBindingFixtureTests(unittest.TestCase):
    """Replay tests/fixtures/r6/bindings/compat-cases.json through the real resolver.

    The same file is replayed by `tests/r6/bindings-compat.test.cjs`, so the
    explanation the browser shows cannot drift from the rule the server applies.
    """

    FIXTURE = (Path(__file__).resolve().parent / 'fixtures' / 'r6' / 'bindings'
               / 'compat-cases.json')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = make_app(self.base / 'deploy')
        self.actor = self.app.actor('127.0.0.1')
        self.spec = json.loads(self.FIXTURE.read_text('utf-8'))
        with self.app.store.transaction() as db:
            for rule_id, name in self.spec['rules'].items():
                db.execute('INSERT INTO rules VALUES(?,?,?,?,1,NULL,datetime(\'now\'),?)',
                           (rule_id, name, '', json.dumps({'headerPattern': '^(?<message>.*)$',
                                                           'moduleRules': [], 'stepRules': [],
                                                           'errorRules': []}), 'test'))

    def tearDown(self):
        self.app.index_jobs.close()
        self.app.queries.close()
        self.temp.cleanup()

    def replay(self, case):
        with self.app.store.transaction() as db:
            db.execute('DELETE FROM root_bindings')
            db.execute('DELETE FROM personal_bindings')
            db.execute("DELETE FROM bindings WHERE path<>''")
            db.execute('UPDATE rules SET deleted_at=NULL')
            has_default = any(row['scope'] == 'default' for row in case.get('shared', []))
            if not has_default:
                # The fixture's "no binding" case means the service default is absent
                # too; a fresh deployment always has one, so it is removed here.
                db.execute("DELETE FROM bindings WHERE scope='default'")
            for row in case.get('shared', []):
                if row['scope'] == 'default':
                    db.execute(
                        "INSERT INTO bindings VALUES('default','',?,1,datetime('now')) "
                        'ON CONFLICT(scope,path) DO UPDATE SET rule_id=excluded.rule_id, '
                        'revision=bindings.revision+1, updated_at=excluded.updated_at',
                        (row['ruleId'],))
                    continue
                db.execute('INSERT INTO root_bindings VALUES(?,?,?,?,1,datetime(\'now\'),?)',
                           (row['scope'], row['rootId'], row['path'], row['ruleId'], 'test'))
            for row in case.get('personal', []):
                db.execute('INSERT INTO personal_bindings VALUES(?,?,?,?,?,1,datetime(\'now\'))',
                           (self.actor['id'], row['scope'], row['rootId'], row['path'],
                            row['ruleId']))
            for rule_id in case.get('deleted', []):
                db.execute('UPDATE rules SET deleted_at=datetime(\'now\') WHERE id=?',
                           (rule_id,))
        return self.app.storage.bindings.resolve(self.actor, root_id=case['rootId'],
                                                 rel_path=case['path'])

    def test_fixture_layer_order_matches_the_printing_order(self):
        from server.storage.bindings import LAYERS
        self.assertEqual(list(LAYERS), self.spec['layerOrder'])

    def test_every_frozen_case_resolves_as_recorded(self):
        for case in self.spec['cases']:
            with self.subTest(case=case['name']):
                result = self.replay(case)
                expected = case['expected']
                self.assertEqual([row['layer'] for row in result['chain']],
                                 self.spec['layerOrder'])
                if expected['ruleId'] is None:
                    self.assertIsNone(result['ruleId'])
                    self.assertEqual([row for row in result['chain'] if row['matched']], [])
                    continue
                self.assertEqual(result['ruleId'], expected['ruleId'])
                self.assertEqual(result['layer'], expected['layer'])
                if expected.get('skipped'):
                    skipped = [row for row in result['chain']
                               if row.get('reason') == 'rule_missing']
                    self.assertEqual([row['ruleId'] for row in skipped],
                                     [expected['skipped']])

    def test_skipped_rule_is_named_in_the_explanation(self):
        case = [item for item in self.spec['cases']
                if item['name'].startswith('a deleted rule')][0]
        result = self.replay(case)
        self.assertIn('已删除', result['explanation'])


if __name__ == '__main__':
    unittest.main()
