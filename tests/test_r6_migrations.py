"""R6-032: migrations are expand-only, idempotent, and do not touch v1 rows."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import migrations
from server import models
from server.app import Application, Config, Store

ROOT = Path(__file__).resolve().parents[1]


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    profile = {'name': 'Basic', 'headerPattern': '^(?<message>.*)$', 'moduleRules': [], 'stepRules': [], 'errorRules': []}
    (root / 'rules' / 'cpqpsgx-default.json').write_text(json.dumps(profile), 'utf-8')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_database_reaches_expected_version(self):
        report = migrations.verify(self.app.store)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['version'], migrations.SCHEMA_VERSION)
        self.assertEqual(report['missing'], [])

    def test_reopening_does_not_reapply_or_duplicate(self):
        with self.app.store.connect() as db:
            before = db.execute('SELECT COUNT(*) AS n FROM schema_migrations').fetchone()['n']
        report = migrations.apply(self.app.store)
        self.assertTrue(report['reused'])
        self.assertEqual(report['applied'], [])
        with self.app.store.connect() as db:
            after = db.execute('SELECT COUNT(*) AS n FROM schema_migrations').fetchone()['n']
        self.assertEqual(before, after)

    def test_v1_rows_survive_the_migration(self):
        with self.app.store.transaction() as db:
            db.execute("INSERT INTO users(ip,name,role,enabled,revision,updated_at) VALUES('10.1.1.1','Zhang','member',1,1,'2026-01-01')")
            db.execute("INSERT INTO rules(id,name,description,profile,revision,deleted_at,updated_at,updated_by) "
                       "VALUES('r1','Rule','d','{}',1,NULL,'2026-01-01','tester')")
            db.execute("INSERT INTO bindings(scope,path,rule_id,revision,updated_at) VALUES('directory','machine-a','r1',1,'2026-01-01')")
            self.app.store.audit(db, '10.1.1.1', 'rule.create', 'r1', {'name': 'Rule'})

        def snapshot():
            with self.app.store.connect() as db:
                return {
                    'users': [dict(row) for row in db.execute('SELECT * FROM users ORDER BY ip')],
                    'rules': [dict(row) for row in db.execute('SELECT id,name,revision FROM rules ORDER BY id')],
                    'bindings': [dict(row) for row in db.execute('SELECT * FROM bindings ORDER BY scope,path')],
                    'audit': [dict(row) for row in db.execute('SELECT actor,action,target FROM audit ORDER BY id')],
                }

        before = snapshot()
        report = migrations.apply(self.app.store)
        self.assertTrue(report['reused'])
        self.assertEqual(before, snapshot(), 'expand migrations must not rewrite v1 rows')

    def test_a_changed_applied_step_is_refused(self):
        tampered = list(migrations.STEPS)
        version, name, statements = tampered[0]
        tampered[0] = (version, name, statements + ['CREATE TABLE IF NOT EXISTS extra (x INTEGER)'])
        with self.assertRaises(migrations.MigrationError) as caught:
            migrations.apply(self.app.store, steps=tampered)
        self.assertEqual(caught.exception.code, 'migration_checksum_changed')

    def test_a_database_from_a_newer_program_is_refused(self):
        with self.app.store.transaction() as db:
            db.execute("INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(99,'future','x','now')")
        with self.assertRaises(migrations.MigrationError) as caught:
            migrations.apply(self.app.store)
        self.assertEqual(caught.exception.code, 'schema_too_new')

    def test_a_gap_in_the_sequence_is_refused(self):
        # A dedicated empty database: the app's store is already at the current
        # version, where a "gap" would be reported as a checksum change instead.
        with tempfile.TemporaryDirectory() as empty:
            store = Store(Path(empty))
            gap = [(1, 'a', ['CREATE TABLE IF NOT EXISTS a (x INTEGER)']),
                   (3, 'c', ['CREATE TABLE IF NOT EXISTS c (x INTEGER)'])]
            with self.assertRaises(migrations.MigrationError) as caught:
                migrations.apply(store, steps=gap)
            self.assertEqual(caught.exception.code, 'migration_gap')

    def test_a_failing_statement_leaves_no_partial_step(self):
        with tempfile.TemporaryDirectory() as empty:
            store = Store(Path(empty))
            broken = [(1, 'broken', ['CREATE TABLE IF NOT EXISTS ok_table (x INTEGER)', 'NOT VALID SQL'])]
            with self.assertRaises(sqlite3.Error):
                migrations.apply(store, steps=broken)
            with store.connect() as db:
                tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                version = db.execute('SELECT MAX(version) AS v FROM schema_migrations').fetchone()['v']
            self.assertNotIn('ok_table', tables, 'the transaction must roll the whole step back')
            self.assertIsNone(version)
            # And the step can be retried from a clean state once it is fixed.
            fixed = [(1, 'fixed', ['CREATE TABLE IF NOT EXISTS ok_table (x INTEGER)'])]
            self.assertEqual(migrations.apply(store, steps=fixed)['version'], 1)

    def test_state_tables_match_the_frozen_contract(self):
        contract = json.loads((ROOT / 'contracts/r6/state-machine.json').read_text('utf-8'))
        self.assertEqual(models.TASK_TRANSITIONS, contract['task']['transitions'])
        self.assertEqual(models.QUERY_TRANSITIONS, contract['query']['transitions'])
        self.assertEqual(list(models.TASK_TERMINAL), contract['task']['terminal'])
        self.assertEqual(list(models.QUERY_TERMINAL), contract['query']['terminal'])


class ModelTests(unittest.TestCase):
    def test_file_ids_are_opaque_and_stable(self):
        first = models.file_id_for('root-1', 'a/b.log')
        second = models.file_id_for('root-1', 'a/b.log')
        other = models.file_id_for('root-1', 'a/c.log')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith('f_'))
        self.assertNotIn('a/b.log', first)
        self.assertNotIn('b.log', first)

    def test_content_signature_notices_a_rewrite_of_equal_size(self):
        base = models.content_signature(100, 111, b'head', b'tail')
        self.assertEqual(base, models.content_signature(100, 111, b'head', b'tail'))
        self.assertNotEqual(base, models.content_signature(100, 112, b'head', b'tail'), 'mtime change')
        self.assertNotEqual(base, models.content_signature(101, 111, b'head', b'tail'), 'size change')
        self.assertNotEqual(base, models.content_signature(100, 111, b'HEAD', b'tail'), 'head change')
        self.assertNotEqual(base, models.content_signature(100, 111, b'head', b'TAIL'), 'tail change')

    def test_transition_guard_rejects_unknown_edges(self):
        models.assert_transition('task', 'queued', 'reading')
        with self.assertRaises(models.TransitionError):
            # The deleted edge: "upload finished" must not imply "searchable".
            models.assert_transition('task', 'uploading', 'indexed')
        with self.assertRaises(models.TransitionError):
            models.assert_transition('query', 'done', 'running')
        self.assertTrue(models.is_terminal('task', 'indexed'))
        self.assertFalse(models.is_terminal('task', 'indexing'))


if __name__ == '__main__':
    unittest.main()
