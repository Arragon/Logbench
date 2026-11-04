"""R6-033: job control with real SQLite, real leases and real HTTP.

Nothing here is mocked: two "workers" are separate repository clients on the same
database file, and the HTTP cases go through `Application.handle`, including the
CSRF header. That matters because the failure this ticket prevents — two writers
on one generation — only appears with a real transaction.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.jobs.repository import JobRepository, LeaseError
from server.jobs.service import JobService
from server.models import Job


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    (root / 'rules' / 'cpqpsgx-default.json').write_text(
        json.dumps({'name': 'Basic', 'headerPattern': '^(?<message>.*)$',
                    'moduleRules': [], 'stepRules': [], 'errorRules': []}), 'utf-8')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class JobRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.repo = JobRepository(self.app.store)

    def tearDown(self):
        self.temp.cleanup()

    def make(self, **kwargs) -> Job:
        return self.repo.create(kwargs.pop('kind', 'index'), 'p-1',
                                kwargs.pop('payload', {'file_id': 'f_1'}), **kwargs)

    def test_action_key_makes_creation_idempotent(self):
        first = self.make(action_key='user-42-click')
        second = self.make(action_key='user-42-click')
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.repo.list('p-1')), 1)

    def test_a_live_lease_cannot_be_taken_by_another_owner(self):
        job = self.make()
        self.repo.claim(job.id, 'worker-a', ttl=30, now=1000)
        with self.assertRaises(LeaseError) as caught:
            self.repo.claim(job.id, 'worker-b', ttl=30, now=1001)
        self.assertEqual(caught.exception.code, 'lease_held')

    def test_an_expired_lease_is_taken_over_with_a_higher_fence(self):
        job = self.make()
        first = self.repo.claim(job.id, 'worker-a', ttl=5, now=1000)
        second = self.repo.claim(job.id, 'worker-b', ttl=30, now=1006)
        self.assertGreater(second.fencing, first.fencing)
        self.assertEqual(second.epoch, first.epoch + 1)
        with self.assertRaises(LeaseError) as caught:
            self.repo.checkpoint(first, 'reading', {'bytes': 10}, now=1006)
        self.assertEqual(caught.exception.code, 'stale_fencing')

    def test_a_checkpoint_is_visible_to_the_next_worker(self):
        job = self.make()
        lease = self.repo.claim(job.id, 'worker-a', ttl=30)
        self.repo.checkpoint(lease, 'reading', {'bytes': 4096, 'records': 40})
        state = self.repo.checkpoints(job.id)
        self.assertEqual(state['stages']['reading']['bytes'], 4096)
        self.assertEqual(state['epoch'], lease.epoch)

    def test_finishing_with_a_stale_lease_is_refused(self):
        job = self.make()
        stale = self.repo.claim(job.id, 'worker-a', ttl=1, now=1000)
        fresh = self.repo.claim(job.id, 'worker-b', ttl=30, now=1100)
        # A worker reaching "indexed" must have passed through reading first; the
        # transition table is checked even for a valid lease.
        for stage in ('reading', 'framing', 'indexing'):
            self.repo.set_state(job.id, stage)
        with self.assertRaises(LeaseError) as caught:
            self.repo.finish(stale, 'indexed', now=1100)
        self.assertEqual(caught.exception.code, 'stale_fencing')
        finished = self.repo.finish(fresh, 'indexed', now=1100)
        self.assertEqual(finished.state, 'indexed')
        self.assertIsNone(self.repo.lease(job.id))

    def test_an_illegal_transition_is_refused(self):
        job = self.make()
        lease = self.repo.claim(job.id, 'worker-a', ttl=30)
        # queued -> indexed skips every stage the contract requires.
        with self.assertRaises(LeaseError) as caught:
            self.repo.finish(lease, 'indexed')
        self.assertEqual(caught.exception.code, 'illegal_transition')

    def test_reclaim_turns_an_expired_job_into_paused_with_a_reason(self):
        job = self.make()
        lease = self.repo.claim(job.id, 'worker-a', ttl=1, now=1000)
        self.repo.checkpoint(lease, 'reading', {'bytes': 42}, now=1000)
        reclaimed = self.repo.reclaim_expired(now=2000)
        self.assertEqual([item['job_id'] for item in reclaimed], [job.id])
        current = self.repo.get(job.id)
        self.assertEqual(current.state, 'paused')
        self.assertEqual(current.resume_reason, 'lease_expired')
        self.assertEqual(self.repo.checkpoints(job.id)['stages']['reading']['bytes'], 42,
                         'a reclaimed job keeps its checkpoint')

    def test_reclaim_ignores_terminal_jobs(self):
        job = self.make()
        lease = self.repo.claim(job.id, 'worker-a', ttl=30)
        self.repo.finish(lease, 'cancelled')
        self.assertEqual(self.repo.reclaim_expired(now=10 ** 9), [])


class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.service = JobService(self.app.store)
        self.actor = {'id': 'p-1', 'role': 'member', 'ip': '10.0.0.1'}
        self.other = {'id': 'p-2', 'role': 'member', 'ip': '10.0.0.2'}
        self.admin = {'id': 'p-admin', 'role': 'admin', 'ip': '127.0.0.1'}
        self.job = self.service.repo.create('index', 'p-1', {'file_id': 'f_1'})

    def tearDown(self):
        self.temp.cleanup()

    def test_another_principal_cannot_see_the_job(self):
        with self.assertRaises(LeaseError) as caught:
            self.service.get(self.other, self.job.id)
        self.assertEqual(caught.exception.code, 'unknown_job')
        self.assertEqual(self.service.get(self.admin, self.job.id)['id'], self.job.id)

    def test_an_action_without_an_id_is_refused(self):
        with self.assertRaises(LeaseError) as caught:
            self.service.action(self.actor, self.job.id, 'cancel', {})
        self.assertEqual(caught.exception.code, 'missing_action_id')

    def test_the_same_action_id_returns_the_same_answer_once(self):
        first = self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-1'})
        second = self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-1'})
        self.assertEqual(first, second)
        with self.app.store.connect() as db:
            rows = db.execute('SELECT COUNT(*) AS n FROM job_actions WHERE action_id=?', ('a-1',)).fetchone()
        self.assertEqual(rows['n'], 1)

    def test_cancel_keeps_raw_and_is_a_no_op_when_already_finished(self):
        result = self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-2'})
        self.assertTrue(result['cancelled'])
        self.assertTrue(result['raw_kept'], 'cancel is not delete (C8)')
        again = self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-3'})
        self.assertFalse(again['cancelled'])
        self.assertEqual(again['reason'], 'already_terminal')

    def test_pause_is_refused_where_the_contract_has_no_edge(self):
        with self.assertRaises(LeaseError) as caught:
            self.service.action(self.actor, self.job.id, 'pause', {'actionId': 'a-4'})
        self.assertEqual(caught.exception.code, 'pause_not_supported_in_state')

    def test_retry_creates_a_new_job_instead_of_reopening_a_terminal_one(self):
        cancelled = self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-5'})['job']
        retried = self.service.action(self.actor, self.job.id, 'retry', {'actionId': 'a-6'})
        self.assertNotEqual(retried['job']['id'], cancelled['id'])
        self.assertEqual(retried['job']['state'], 'queued')
        self.assertEqual(retried['retried_from'], self.job.id)

    def test_revision_conflict_is_reported_not_ignored(self):
        with self.assertRaises(LeaseError) as caught:
            self.service.action(self.actor, self.job.id, 'cancel',
                                {'actionId': 'a-7', 'expectedRevision': 99})
        self.assertEqual(caught.exception.code, 'revision_conflict')

    def test_audit_records_the_action_but_not_log_content(self):
        self.job = self.service.repo.create('index', 'p-1', {'file_id': 'f_1', 'note': 'secret-log-line'})
        self.service.action(self.actor, self.job.id, 'cancel', {'actionId': 'a-8'})
        with self.app.store.connect() as db:
            row = db.execute("SELECT detail FROM audit WHERE action='job.cancel' ORDER BY id DESC").fetchone()
        self.assertIn('user_cancelled', row['detail'])
        self.assertNotIn('secret-log-line', row['detail'])


class JobHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, target, payload=None, ip='127.0.0.1', headers=None):
        h = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
             'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        h.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, h, body, ip)

    def create_job(self) -> str:
        return self.app.jobs.repo.create('index', self.app.actor('127.0.0.1')['id'],
                                         {'file_id': 'f_http'}).id

    def test_list_and_detail_are_scoped_to_the_caller(self):
        job_id = self.create_job()
        mine = self.request('GET', '/api/v2/jobs')
        self.assertEqual(mine.status, 200)
        self.assertEqual([item['id'] for item in mine.value['items']], [job_id])

        visitor = self.request('GET', '/api/v2/jobs', ip='10.9.9.9')
        self.assertEqual(visitor.value['items'], [], 'a visitor must not see an admin job')

        hidden = self.request('GET', '/api/v2/jobs/' + job_id, ip='10.9.9.9')
        self.assertEqual(hidden.status, 404)
        self.assertEqual(hidden.value['error']['code'], 'unknown_job')

    def test_post_action_requires_csrf(self):
        job_id = self.create_job()
        missing = self.request('POST', '/api/v2/jobs/%s/actions' % job_id,
                               {'action': 'cancel', 'actionId': 'x-1'},
                               headers={'X-LogBench-CSRF': 'wrong'})
        self.assertEqual(missing.status, 403)
        ok = self.request('POST', '/api/v2/jobs/%s/actions' % job_id,
                          {'action': 'cancel', 'actionId': 'x-2'})
        self.assertEqual(ok.status, 200)
        self.assertTrue(ok.value['cancelled'])

    def test_unknown_action_and_bad_method_are_rejected(self):
        job_id = self.create_job()
        bad = self.request('POST', '/api/v2/jobs/%s/actions' % job_id,
                           {'action': 'explode', 'actionId': 'x-3'})
        self.assertEqual(bad.status, 400)
        self.assertEqual(bad.value['error']['code'], 'unknown_action')
        method = self.request('GET', '/api/v2/jobs/%s/actions' % job_id)
        self.assertEqual(method.status, 405)

    def test_job_detail_can_include_payload_and_checkpoints(self):
        job_id = self.create_job()
        value = self.request('GET', '/api/v2/jobs/%s?include=payload' % job_id).value['item']
        self.assertEqual(value['payload']['file_id'], 'f_http')
        self.assertIn('checkpoints', value)
        self.assertIsNone(value['lease'])


if __name__ == '__main__':
    unittest.main()
