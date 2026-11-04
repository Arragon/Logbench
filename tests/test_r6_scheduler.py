"""R6-034: budget derivation and scheduler fairness, deterministically."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.jobs.budget import Budget, Pressure, derive, slot_for, total_cpu_of
from server.jobs.scheduler import PRIORITY, Scheduler


class BudgetTests(unittest.TestCase):
    def test_defaults_are_valid_and_consistent(self):
        budget = Budget().validate()
        self.assertLessEqual(budget.parse_slots + budget.query_slots, budget.total_cpu)
        self.assertLessEqual(budget.history_slots, budget.parse_slots)
        self.assertLessEqual(budget.hot_cache_bytes, budget.mem_soft_bytes)
        self.assertEqual(total_cpu_of(budget), budget.parse_slots + budget.query_slots)

    def test_invalid_combinations_are_rejected(self):
        with self.assertRaises(ValueError):
            Budget(parse_slots=6, query_slots=4, total_cpu=8).validate()
        with self.assertRaises(ValueError):
            Budget(history_slots=9).validate()
        with self.assertRaises(ValueError):
            Budget(hot_cache_bytes=32 * 1024 ** 3).validate()

    def test_a_small_cgroup_shrinks_and_never_grows(self):
        base = Budget()
        shrunk, notes = derive(base, Pressure(cpu_limit=2))
        self.assertLessEqual(shrunk.total_cpu, base.total_cpu)
        self.assertLessEqual(shrunk.parse_slots + shrunk.query_slots, 2)
        self.assertEqual(shrunk.history_slots, 1)
        self.assertIn('small_cgroup', notes)

        bigger, _ = derive(base, Pressure(cpu_limit=64))
        self.assertEqual(bigger, base, 'a large host must not raise the limits')

    def test_memory_pressure_pauses_history_and_halves_the_cache(self):
        base = Budget()
        shrunk, notes = derive(base, Pressure(rss_bytes=int(base.mem_soft_bytes * 0.9)))
        self.assertEqual(shrunk.history_slots, 0)
        self.assertEqual(shrunk.hot_cache_bytes, base.hot_cache_bytes // 2)
        self.assertIn('memory_high', notes)

    def test_slow_disk_reduces_writers(self):
        shrunk, notes = derive(Budget(), Pressure(disk_latency_ms=80))
        self.assertLessEqual(shrunk.writer_slots, 2)
        self.assertIn('disk_slow', notes)

    def test_slot_mapping(self):
        self.assertEqual(slot_for(Budget(), 'index'), 'parse')
        self.assertEqual(slot_for(Budget(), 'query'), 'query')
        self.assertEqual(slot_for(Budget(), 'upload'), 'upload')
        with self.assertRaises(ValueError):
            slot_for(Budget(), 'teleport')


class SchedulerTests(unittest.TestCase):
    def make(self, **overrides) -> Scheduler:
        return Scheduler(Budget(**overrides))

    def test_pools_never_exceed_the_cpu_budget(self):
        scheduler = self.make()
        for index in range(20):
            scheduler.submit('p%d' % index, 'index', 'u%d' % (index % 3), PRIORITY['realtime'])
        for index in range(10):
            scheduler.submit('q%d' % index, 'query', 'u%d' % (index % 3))
        started = scheduler.tick(now=0)
        self.assertLessEqual(len(started), scheduler.budget.total_cpu)
        parse = [job for job in started if job.kind == 'index']
        query = [job for job in started if job.kind == 'query']
        self.assertLessEqual(len(parse), scheduler.budget.parse_slots)
        self.assertLessEqual(len(query), scheduler.budget.query_slots)

    def test_one_principal_cannot_take_every_query_slot(self):
        scheduler = self.make()
        for index in range(6):
            scheduler.submit('q%d' % index, 'query', 'noisy-user')
        started = scheduler.tick(now=0)
        self.assertEqual(len(started), 1, 'per-principal query quota is 1')
        waiting = [item.reason for item in scheduler.waiting]
        self.assertIn('principal_query_quota', waiting)

    def test_history_cannot_consume_the_interactive_reserve(self):
        # history_slots == parse_slots, so the reserve — not the history cap — is
        # the constraint under test.
        scheduler = self.make(parse_slots=4, query_slots=2, total_cpu=8,
                             history_slots=4, interactive_reserve=1)
        for index in range(6):
            scheduler.submit('h%d' % index, 'index', 'ops', PRIORITY['history'])
        started = scheduler.tick(now=0)
        self.assertEqual(len(started), scheduler.budget.parse_slots - 1)
        self.assertIn('interactive_reserve', [item.reason for item in scheduler.waiting])

        # An interactive job still gets a slot despite the history backlog.
        scheduler.submit('now', 'index', 'user', PRIORITY['interactive'])
        more = scheduler.tick(now=1)
        self.assertIn('now', [job.job_id for job in more])

    def test_memory_pressure_stops_scheduling_history(self):
        scheduler = self.make()
        scheduler.submit('h1', 'index', 'ops', PRIORITY['history'])
        shrunk, notes = derive(scheduler.budget, Pressure(rss_bytes=int(17 * 1024 ** 3)))
        paused = scheduler.apply_budget(shrunk, notes)
        self.assertEqual(scheduler.budget.history_slots, 0)
        self.assertEqual(paused, [], 'nothing was running yet')
        scheduled = scheduler.tick(now=0)
        self.assertEqual(scheduled, [], 'history must not start with history_slots=0')
        self.assertEqual(scheduler.waiting[0].job_id, 'h1')
        self.assertEqual(scheduler.waiting[0].reason, 'history_paused_memory')

    def test_shrinking_parks_the_lowest_priority_running_job(self):
        scheduler = self.make()
        scheduler.submit('a', 'index', 'u1', PRIORITY['interactive'])
        scheduler.submit('b', 'index', 'u2', PRIORITY['history'])
        scheduler.tick(now=0)
        self.assertEqual(len(scheduler.running), 2)
        shrunk = Budget(parse_slots=1, query_slots=1, total_cpu=2, history_slots=1,
                        interactive_reserve=0, writer_slots=2)
        paused = scheduler.apply_budget(shrunk, ['cpu_limit=2'])
        self.assertEqual([item['job_id'] for item in paused], ['b'])
        self.assertIn('b', [job.job_id for job in scheduler.waiting])
        self.assertIn('a', scheduler.running)

    def test_the_same_inputs_produce_the_same_schedule(self):
        def run():
            scheduler = Scheduler(Budget())
            for index in range(8):
                scheduler.submit('j%d' % index, 'index', 'u%d' % (index % 2),
                                 PRIORITY['realtime'] if index % 2 else PRIORITY['current_file'],
                                 now=float(index))
            return [job.job_id for job in scheduler.tick(now=10)]

        self.assertEqual(run(), run())

    def test_round_robin_between_principals(self):
        scheduler = self.make(parse_slots=2, query_slots=1, total_cpu=3)
        scheduler.submit('a1', 'index', 'alpha', PRIORITY['realtime'], now=0)
        scheduler.submit('b1', 'index', 'beta', PRIORITY['realtime'], now=1)
        first = [job.job_id for job in scheduler.tick(now=2)]
        self.assertEqual(len(first), 2)
        scheduler.submit('a2', 'index', 'alpha', PRIORITY['realtime'], now=3)
        scheduler.submit('b2', 'index', 'beta', PRIORITY['realtime'], now=4)
        scheduler.finish(first[0])
        second = [job.job_id for job in scheduler.tick(now=5)]
        # beta still has b1 running, alpha has nothing: alpha goes first. Fairness
        # is "who is idle", not "who queued first".
        self.assertEqual(second, ['a2'])

        # With both principals idle the rotation counter decides, so neither can
        # monopolise the head of the queue.
        scheduler.finish('b1')
        scheduler.finish('a2')
        third = [job.job_id for job in scheduler.tick(now=6)]
        self.assertEqual(third, ['b2'])

    def test_duplicate_submission_is_rejected(self):
        scheduler = self.make()
        scheduler.submit('x', 'index', 'u')
        with self.assertRaises(ValueError):
            scheduler.submit('x', 'index', 'u')

    def test_snapshot_reports_why_a_job_is_waiting(self):
        scheduler = self.make()
        for index in range(8):
            scheduler.submit('q%d' % index, 'query', 'boom')
        scheduler.tick(now=0)
        snapshot = scheduler.snapshot()
        self.assertIn('budget', snapshot)
        self.assertTrue(any(item['reason'] in ('principal_query_quota', 'query_slots_full')
                            for item in snapshot['waiting']))


if __name__ == '__main__':
    unittest.main()
