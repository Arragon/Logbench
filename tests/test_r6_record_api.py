"""R6-040: authorized record, raw-range, timeline and index-status API.

Everything here goes through `Application.handle`, so Host validation, the CSRF
rule for writes, the file cage and the error envelope are all exercised as they
would be in production. The raw-range cases compare against the actual bytes on
disk, including ranges that begin or end in the middle of a UTF-8 character,
because that is the case a "just read the slice" implementation gets wrong.
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
from server.indexing.cache_versions import CacheRegistry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


class RecordApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.data = self.app.config.data_dir
        (self.data / 'logs').mkdir(parents=True, exist_ok=True)
        self.log = self.data / 'logs' / 'edge.log'
        shutil.copy(GOLDEN / 'edge_cases.log', self.log)
        self.ip = '127.0.0.1'
        self.admin = self.app.actor(self.ip)

    def tearDown(self):
        self.app.index_jobs.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def request(self, method, target, payload=None, ip=None, headers=None):
        ip = ip or self.ip
        base = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
                'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': self.app.csrf(ip)}
        base.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.handle(method, target, base, body, ip)

    def index(self, path='logs/edge.log'):
        created = self.request('POST', '/api/v2/index-jobs', {'path': path})
        self.assertEqual(created.status, 200, created.value)
        item = created.value['item']
        self.app.index_jobs.run_now(item['id'])
        return item['payload']['file_id']

    def record_id(self, file_id, ordinal=0):
        return '%s:%d' % (file_id, ordinal)

    # ------------------------------------------------------------- index status
    def test_status_is_pending_until_the_index_is_built(self):
        item = self.request('POST', '/api/v2/index-jobs', {'path': 'logs/edge.log'}).value['item']
        file_id = item['payload']['file_id']
        pending = self.request('GET', '/api/v2/files/' + file_id)
        self.assertEqual(pending.status, 200)
        self.assertEqual(pending.value['coverage']['state'], 'pending')
        self.assertEqual(pending.value['coverage']['relation'], 'unknown')
        self.assertEqual({row['state'] for row in pending.value['layers'].values()}, {'pending'})

        self.app.index_jobs.run_now(item['id'])
        ready = self.request('GET', '/api/v2/files/' + file_id).value
        self.assertEqual(ready['coverage']['state'], 'complete')
        self.assertEqual(ready['coverage']['relation'], 'exact')
        self.assertGreater(ready['coverage']['record_count'], 0)
        self.assertEqual({row['state'] for row in ready['layers'].values()}, {'complete'})
        self.assertEqual(ready['display_name'], 'edge.log')

    def test_generations_are_listed_for_the_authorised_file_only(self):
        file_id = self.index()
        listed = self.request('GET', '/api/v2/files/%s/generations' % file_id)
        self.assertEqual(listed.status, 200)
        self.assertEqual(listed.value['items'][0]['generation'], 1)
        self.assertEqual(listed.value['current'], 1)
        foreign = self.request('GET', '/api/v2/files/%s/generations' % file_id, ip='10.9.9.9')
        self.assertEqual(foreign.status, 404)

    # ------------------------------------------------------------------ records
    def test_record_detail_carries_structure_rules_and_version_identity(self):
        file_id = self.index()
        response = self.request('GET', '/api/v2/records/' + self.record_id(file_id))
        self.assertEqual(response.status, 200)
        value = response.value
        record = value['record']
        self.assertEqual(record['ordinal'], 0)
        self.assertEqual(record['record_id'], self.record_id(file_id))
        self.assertIn('message', record)
        self.assertIn('lines', record)
        self.assertIn('bytes', record)
        self.assertEqual(value['generation'], 1)
        self.assertTrue(value['version']['structure'])
        self.assertTrue(value['version']['rules'])
        self.assertFalse(record['provisional'])
        self.assertFalse(value['body']['truncated'])

    def test_an_unknown_or_tampered_record_id_is_indistinguishable_from_absent(self):
        file_id = self.index()
        cases = ['%s:999999' % file_id, 'not-a-record-id', '%s:0/../../etc/passwd' % file_id,
                 'f_deadbeefdeadbeef:0']
        for case in cases:
            response = self.request('GET', '/api/v2/records/' + case)
            self.assertEqual(response.status, 404, case)
            self.assertEqual(response.value['error']['code'], 'NOT_FOUND', case)
            self.assertNotIn('count', response.value['error']['details'], case)

    def test_another_principal_cannot_read_the_record_or_learn_it_exists(self):
        file_id = self.index()
        own = self.request('GET', '/api/v2/records/' + self.record_id(file_id))
        self.assertEqual(own.status, 200)
        foreign = self.request('GET', '/api/v2/records/' + self.record_id(file_id), ip='10.9.9.9')
        missing = self.request('GET', '/api/v2/records/f_ffffffffffffffff:0', ip='10.9.9.9')
        self.assertEqual(foreign.status, missing.status)
        self.assertEqual(foreign.value['error'], missing.value['error'])

    def test_revocation_terminates_reads(self):
        file_id = self.index()
        self.assertEqual(self.request('GET', '/api/v2/files/' + file_id).status, 200)
        CacheRegistry(self.app.store).revoke(self.admin['id'])
        for target in ('/api/v2/files/' + file_id,
                       '/api/v2/records/' + self.record_id(file_id),
                       '/api/v2/files/%s/timeline' % file_id):
            response = self.request('GET', target)
            self.assertEqual(response.status, 404, target)
            self.assertEqual(response.value['error']['code'], 'NOT_FOUND', target)

    def test_a_stale_or_unknown_rules_version_is_refused_instead_of_substituted(self):
        file_id = self.index()
        response = self.request('GET', '/api/v2/records/%s?version=ver-00000000deadbeef'
                                % self.record_id(file_id))
        self.assertEqual(response.status, 404)
        self.assertEqual(response.value['error']['code'], 'NOT_FOUND')
        rules = CacheRegistry(self.app.store).pointer(file_id, 'rules')
        pinned = self.request('GET', '/api/v2/records/%s?version=%s'
                              % (self.record_id(file_id), rules['version_id']))
        self.assertEqual(pinned.status, 200)
        self.assertEqual(pinned.value['version']['rules'], rules['version_id'])

    def test_huge_bodies_are_clipped_with_a_cursor(self):
        big = self.data / 'logs' / 'big.log'
        lines = ['[2024-01-01 00:00:00.000] [ERROR] [app] [1] [2] [main.py:10] [run] [u] f() boom',
                 'Traceback (most recent call last):']
        for index in range(4000):
            lines.append('  File "/srv/app/m%d.py", line %d, in handler' % (index, index))
        lines.append('RuntimeError: boom')
        big.write_text('\n'.join(lines) + '\n', 'utf-8')
        file_id = self.index('logs/big.log')

        detail = self.request('GET', '/api/v2/records/' + self.record_id(file_id)).value
        self.assertTrue(detail['body']['truncated'])
        self.assertIsNotNone(detail['body']['next'])
        self.assertLessEqual(detail['body']['bytes'], 64 * 1024)
        self.assertGreater(detail['body']['total'], detail['body']['next'])

        # The rest of the body, and the raw text, are still readable in pieces.
        raw = self.request('GET', '/api/v2/records/%s/raw?max=4096' % self.record_id(file_id))
        self.assertEqual(raw.status, 200)
        self.assertTrue(raw.value['truncated'])
        self.assertIsNotNone(raw.value['next'])

    # ---------------------------------------------------------------- raw range
    def test_raw_ranges_match_the_original_bytes_exactly(self):
        file_id = self.index()
        blob = self.log.read_bytes()
        detail = self.request('GET', '/api/v2/records/' + self.record_id(file_id)).value
        start = int(detail['record']['bytes']['start'])
        end = int(detail['record']['bytes']['end'])
        for offset, length in ((0, 40), (7, 33), (start + 5, 17)):
            response = self.request('GET', '/api/v2/records/%s/raw?start=%d&end=%d'
                                    % (self.record_id(file_id), start + offset, start + offset + length))
            self.assertEqual(response.status, 200)
            expected = blob[start + offset:start + offset + length].decode('utf-8')
            self.assertEqual(response.value['text'], expected)
            self.assertTrue(response.value['complete'])
            self.assertEqual(response.value['range']['start'], start + offset)
        # A whole-record read must report exactly the record's line span.
        whole = self.request('GET', '/api/v2/records/%s/raw' % self.record_id(file_id)).value
        self.assertEqual(whole['lines']['first'], detail['record']['lines']['first'])
        self.assertEqual(whole['lines']['last'], detail['record']['lines']['last'])
        self.assertEqual(end, int(detail['record']['bytes']['end']))

    def test_a_range_inside_a_multibyte_character_reports_the_real_boundary(self):
        cjk = self.data / 'logs' / 'cjk.log'
        cjk.write_text('[2024-01-01 00:00:00.000] [INFO] [app] [1] [2] [m.py:1] [run] [u] f() 中文日志测试\n',
                       'utf-8')
        file_id = self.index('logs/cjk.log')
        detail = self.request('GET', '/api/v2/records/' + self.record_id(file_id)).value
        start = int(detail['record']['bytes']['start'])
        end = int(detail['record']['bytes']['end'])
        window = cjk.read_bytes()[start:end]
        target = next(index for index in range(len(window) - 3) if 0xE0 <= window[index] <= 0xEF)
        width = 3
        # Starting one byte into a three-byte character: start moves to its end.
        head = self.request('GET', '/api/v2/records/%s/raw?start=%d&end=%d'
                            % (self.record_id(file_id), start + target + 1, start + target + 9))
        self.assertEqual(head.status, 200)
        self.assertEqual(head.value['range']['adjusted_start'], start + target + width)
        self.assertNotIn('\ufffd', head.value['text'])
        self.assertEqual(head.value['text'], window[target + width:target + 9].decode('utf-8'))

        # Ending inside a character: the tail is dropped, the cursor continues from
        # the last complete boundary, and the next call returns the whole character.
        tail = self.request('GET', '/api/v2/records/%s/raw?start=%d&end=%d'
                            % (self.record_id(file_id), start + target, start + target + 2))
        self.assertEqual(tail.value['text'], '')
        self.assertEqual(tail.value['next'], start + target)
        again = self.request('GET', '/api/v2/records/%s/raw?start=%d&end=%d'
                             % (self.record_id(file_id), tail.value['next'], start + target + width))
        self.assertEqual(again.value['text'], window[target:target + width].decode('utf-8'))

    def test_clipped_raw_reads_continue_from_an_exact_offset(self):
        file_id = self.index()
        detail = self.request('GET', '/api/v2/records/' + self.record_id(file_id)).value
        start = int(detail['record']['bytes']['start'])
        pieces, cursor, guard = [], start, 0
        while True:
            response = self.request('GET', '/api/v2/records/%s/raw?start=%d&max=64'
                                    % (self.record_id(file_id), cursor))
            value = response.value
            pieces.append(value['text'])
            if not value['truncated']:
                break
            self.assertGreater(value['next'], cursor)
            cursor = value['next']
            guard += 1
            self.assertLess(guard, 200, 'the cursor must make progress')
        expected = self.log.read_bytes()[start:int(detail['record']['bytes']['end'])].decode('utf-8')
        self.assertEqual(''.join(pieces), expected)

    # ----------------------------------------------------------------- timeline
    def test_timeline_pages_with_coverage_and_count_relation(self):
        file_id = self.index()
        for page_size in (20, 50, 100, 150):
            response = self.request('GET', '/api/v2/files/%s/timeline?pageSize=%d'
                                    % (file_id, page_size))
            self.assertEqual(response.status, 200)
            value = response.value
            self.assertEqual(value['coverage']['state'], 'complete')
            self.assertEqual(value['count']['relation'], 'exact')
            self.assertEqual(value['page_size'], page_size)
            self.assertFalse(value['has_prev'])
        bad = self.request('GET', '/api/v2/files/%s/timeline?pageSize=33' % file_id)
        self.assertEqual(bad.status, 400)
        self.assertEqual(bad.value['error']['code'], 'INVALID_PAGE_SIZE')

    def test_timeline_without_a_rules_layer_is_pending_not_empty(self):
        file_id = self.index()
        CacheRegistry(self.app.store).invalidate(file_id, 'rules', reason='rule edit')
        value = self.request('GET', '/api/v2/files/%s/timeline' % file_id).value
        self.assertEqual(value['coverage']['state'], 'pending')
        self.assertEqual(value['count'], {'relation': 'unknown', 'matched': None})
        self.assertEqual(value['nodes'], [])

    # --------------------------------------------------------------------- seek
    def test_seek_by_line_and_byte_finds_the_same_record(self):
        file_id = self.index()
        detail = self.request('GET', '/api/v2/records/' + self.record_id(file_id)).value
        line = detail['record']['lines']['first']
        byte = int(detail['record']['bytes']['start'])
        by_line = self.request('GET', '/api/v2/files/%s/seek?line=%d' % (file_id, line)).value
        by_byte = self.request('GET', '/api/v2/files/%s/seek?byte=%d' % (file_id, byte)).value
        self.assertTrue(by_line['found'])
        self.assertEqual(by_line['record_id'], by_byte['record_id'])
        self.assertEqual(by_line['record_id'], detail['record']['record_id'])
        self.assertEqual(by_line['lines']['first'], by_byte['lines']['first'])
        both = self.request('GET', '/api/v2/files/%s/seek?line=1&byte=0' % file_id)
        self.assertEqual(both.status, 400)

    def test_seek_before_indexing_is_an_explicit_pending(self):
        item = self.request('POST', '/api/v2/index-jobs', {'path': 'logs/edge.log'}).value['item']
        file_id = item['payload']['file_id']
        response = self.request('GET', '/api/v2/files/%s/seek?line=1' % file_id)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.value['error']['code'], 'INDEX_PENDING')

    # ------------------------------------------------------------------- headers
    def test_reads_do_not_require_csrf_but_do_require_a_valid_host(self):
        file_id = self.index()
        ok = self.request('GET', '/api/v2/files/' + file_id,
                          headers={'X-LogBench-CSRF': 'wrong'})
        self.assertEqual(ok.status, 200)
        bad_host = self.request('GET', '/api/v2/files/' + file_id,
                                headers={'Host': 'evil.example.com'})
        self.assertEqual(bad_host.status, 421)
        self.assertEqual(bad_host.value['error']['code'], 'HOST_NOT_ALLOWED')


if __name__ == '__main__':
    unittest.main()
