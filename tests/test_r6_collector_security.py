"""R6-059: credential references, host admission and subprocess bounds.

Everything here is real: real files with real modes, a real resolver table, real
subprocesses that really get killed. The cases are chosen from the failure modes
that would otherwise be invisible until an incident:

* a credential that is world-readable instead of refusing to load;
* a host that passed the probe and later resolves to the metadata address;
* a listing that prints until the process is out of memory;
* a path from a form that becomes an argument.

No case claims a protocol works. Protocol evidence is in `test_r6_http_range.py`
(real sockets) and `qa/evidence/M6-gate.md` (SFTP/SMB/SCP BLOCKED without endpoints).
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.collectors import security
from server.collectors.base import CollectorError, error_class
from server.collectors.credentials import CredentialError, CredentialStore, fingerprint
from server.collectors.process import Runner, WindowedReader, raise_for_result
from server.collectors.security import (Admission, Endpoint, Guard, SecurityError,
                                        guard_args, guard_argv, redact, safe_remote_name)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'var'
        self.store = CredentialStore(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def test_creates_private_files_and_never_returns_the_secret(self):
        meta = self.store.create(kind='password', label='psg-a', value='s3cr3t-value', actor='admin')
        self.assertTrue(meta['id'].startswith('cred_'))
        self.assertNotIn('s3cr3t-value', repr(meta))
        self.assertEqual(meta['fingerprint'], fingerprint('s3cr3t-value'))
        self.assertEqual(self.store.read(meta['id']), 's3cr3t-value')

        directory = stat.S_IMODE((self.state / 'credentials').stat().st_mode)
        self.assertEqual(directory, 0o700, '凭据目录必须 0700')
        for path in (self.state / 'credentials').iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, '凭据文件必须 0600')

    def test_reference_cannot_escape_the_credential_directory(self):
        for bad in ('cred_../../etc/passwd', 'cred_', 'cred_zzzz', '/etc/passwd', 'cred_' + 'a' * 64):
            with self.assertRaises(CredentialError):
                self.store.read(bad)

    def test_reading_a_widened_file_refuses_instead_of_leaking(self):
        meta = self.store.create(kind='key', label='k', value='-----BEGIN KEY-----', actor='admin')
        target = self.state / 'credentials' / meta['id'][5:]
        os.chmod(target, 0o644)
        with self.assertRaises(CredentialError) as caught:
            self.store.read(meta['id'])
        self.assertEqual(caught.exception.code, 'credential_permissions')
        self.assertEqual(self.store.metadata(meta['id'])['state'], 'unusable')

    def test_missing_file_is_a_recoverable_state_not_an_exception_in_metadata(self):
        meta = self.store.create(kind='token', label='t', value='token-value', actor='admin')
        (self.state / 'credentials' / meta['id'][5:]).unlink()
        self.assertEqual(self.store.metadata(meta['id']),
                         {'ref': meta['id'], 'state': 'unusable', 'code': 'credential_missing'})
        with self.assertRaises(CredentialError):
            self.store.read(meta['id'])

    def test_delete_is_idempotent(self):
        meta = self.store.create(kind='password', label='t', value='v', actor='admin')
        self.assertTrue(self.store.delete(meta['id']))
        self.assertFalse(self.store.delete(meta['id']))


class AdmissionTests(unittest.TestCase):
    def guard(self, **policy):
        return Guard(Admission.from_policy(policy), resolver=lambda host, port: self.answers)

    def setUp(self):
        self.answers = ['10.0.0.5']

    def test_loopback_is_refused_by_default(self):
        self.answers = ['127.0.0.1']
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedHosts=['db.local']).admit(Endpoint.parse('sftp://db.local'))
        self.assertEqual(caught.exception.code, 'loopback_denied')

    def test_loopback_allowed_only_when_approved(self):
        self.answers = ['127.0.0.1']
        admitted = self.guard(allowedHosts=['127.0.0.1'], allowLoopback=True).admit(
            Endpoint.parse('http://127.0.0.1:50999'))
        self.assertEqual(admitted, ['127.0.0.1'])

    def test_metadata_address_is_refused_even_inside_an_allowed_network(self):
        self.answers = ['169.254.169.254']
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedNetworks=['0.0.0.0/0'], allowLoopback=True).admit(
                Endpoint.parse('sftp://metadata.local'))
        self.assertEqual(caught.exception.code, 'metadata_address_denied')

    def test_host_must_be_approved_by_name_not_only_by_range(self):
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedHosts=['approved.local'], allowedNetworks=['10.0.0.0/8']).admit(
                Endpoint.parse('sftp://other.local'))
        self.assertEqual(caught.exception.code, 'host_not_allowed')

    def test_address_outside_the_approved_network_is_refused(self):
        self.answers = ['10.9.9.9']
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedHosts=['db.local'], allowedNetworks=['10.0.0.0/24']).admit(
                Endpoint.parse('sftp://db.local'))
        self.assertEqual(caught.exception.code, 'address_not_allowed')

    def test_port_must_be_approved(self):
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedHosts=['db.local'], allowedPorts=[22]).admit(
                Endpoint.parse('sftp://db.local:2222'))
        self.assertEqual(caught.exception.code, 'port_not_allowed')

    def test_resolution_that_changed_since_the_probe_stops_collection(self):
        with self.assertRaises(SecurityError) as caught:
            self.guard(allowedHosts=['db.local']).admit(
                Endpoint.parse('sftp://db.local'), expected_addresses=['10.0.0.4'])
        self.assertEqual(caught.exception.code, 'dns_changed')
        self.assertEqual(caught.exception.details['actual'], ['10.0.0.5'])

    def test_endpoint_with_credentials_in_the_url_is_refused(self):
        with self.assertRaises(SecurityError) as caught:
            Endpoint.parse('https://user:pass@example.com/logs')
        self.assertEqual(caught.exception.code, 'credentials_in_url')

    def test_endpoint_parses_ports_and_paths_without_losing_them(self):
        http = Endpoint.parse('https://logs.example.com:8443/var/log/app/')
        self.assertEqual((http.host, http.port, http.path), ('logs.example.com', 8443, '/var/log/app/'))
        bare = Endpoint.parse('sftp://psg-a', default_port=22)
        self.assertEqual((bare.host, bare.port), ('psg-a', 22))

    def test_remote_path_traversal_and_control_characters_are_refused(self):
        guard = self.guard(allowedHosts=['db.local'])
        for bad in ('relative/path', '/var/log/../../etc', '/var/log/\x00x', '/var/log/a;b'):
            with self.assertRaises(SecurityError):
                guard.check_path(bad)
        self.assertEqual(guard.check_path('/var/log/app'), '/var/log/app')

    def test_allowed_path_prefix_is_enforced(self):
        guard = self.guard(allowedHosts=['db.local'], allowedPaths=['/var/log'])
        with self.assertRaises(SecurityError) as caught:
            guard.check_path('/opt/other')
        self.assertEqual(caught.exception.code, 'path_not_allowed')
        self.assertEqual(guard.check_path('/var/log/debug'), '/var/log/debug')
        with self.assertRaises(SecurityError):
            guard.check_path('/var/logs/other')

    def test_only_numeric_literals_bypass_the_name_list(self):
        guard = self.guard(allowedHosts=['db.local'])
        self.answers = ['10.0.0.5']
        # A literal address is not a name, so it is admitted if the range allows it.
        self.assertEqual(guard.admit(Endpoint.parse('sftp://10.0.0.7')), ['10.0.0.7'])


class ArgvTests(unittest.TestCase):
    def test_command_options_are_refused(self):
        for value in ('ProxyCommand=nc evil 22', '-oProxyCommand=x', '--sftp-ssh=sh -c id',
                      '--config=/tmp/x'):
            with self.assertRaises(SecurityError):
                guard_args([value])

    def test_shell_metacharacters_are_refused_but_spaces_are_fine(self):
        with self.assertRaises(SecurityError):
            guard_args(['/var/log/a; rm -rf /'])
        self.assertEqual(guard_args(['/var/log/my app/日志 01.log']),
                         ['/var/log/my app/日志 01.log'])

    def test_empty_argv_is_refused(self):
        with self.assertRaises(SecurityError):
            guard_argv([])


class RedactionTests(unittest.TestCase):
    def test_secret_values_and_url_userinfo_never_survive(self):
        text = 'failed: pass=hunter2 url=sftp://bob:hunter2@host/x token abc123'
        cleaned = redact(text, ['hunter2'])
        self.assertNotIn('hunter2', cleaned)
        self.assertIn('***', cleaned)
        self.assertIn('pass=***', cleaned)

    def test_redaction_keeps_diagnostic_shape(self):
        cleaned = redact('rclone: exit 5 for psg-a:/var/log', ['whatever'])
        self.assertIn('psg-a:/var/log', cleaned)


class NameMappingTests(unittest.TestCase):
    def test_unsafe_characters_cannot_escape_the_target_directory(self):
        self.assertEqual(safe_remote_name('/var/log/../../etc/passwd'), 'passwd')
        self.assertEqual(safe_remote_name('/var/log/a:b*c?.log'), 'a_b_c_.log')
        self.assertNotIn('/', safe_remote_name('/var/log/dir/文件 01.log'))
        self.assertTrue(safe_remote_name('/var/log/.hidden').startswith('hidden'))

    def test_long_names_are_bounded_but_keep_their_suffix(self):
        name = safe_remote_name('/x/' + 'a' * 400 + '.log')
        self.assertLessEqual(len(name), 180)
        self.assertTrue(name.endswith('.log'))


class RunnerTests(unittest.TestCase):
    def test_spawns_are_counted_and_success_is_reported(self):
        runner = Runner()
        result = runner.run([sys.executable, '-c', 'print("ok")'], timeout=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), b'ok')
        self.assertEqual(runner.spawns, 1)

    def test_timeout_kills_the_process_group(self):
        runner = Runner()
        started = time.monotonic()
        result = runner.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.4)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)

    def test_output_cap_is_enforced_while_reading(self):
        runner = Runner(max_stdout=1024)
        result = runner.run([sys.executable, '-c', 'print("x" * 200000)'], timeout=10)
        self.assertTrue(result.overflow)
        self.assertLessEqual(len(result.stdout), 1024)
        with self.assertRaises(CollectorError) as caught:
            raise_for_result(result)
        self.assertEqual(caught.exception.code, 'output_limit')

    def test_failure_is_classified_and_the_secret_is_redacted(self):
        runner = Runner()
        result = runner.run([sys.executable, '-c',
                             'import sys; sys.stderr.write("pass=hunter2 boom"); sys.exit(5)'],
                            timeout=10)
        with self.assertRaises(CollectorError) as caught:
            raise_for_result(result, secrets=['hunter2'])
        error = caught.exception
        self.assertEqual(error.code, 'transport_error')
        self.assertEqual(error_class(error.code), 'retry')
        self.assertNotIn('hunter2', repr(error.to_dict()))

    def test_cancel_stops_an_in_flight_read(self):
        runner = Runner()
        import threading
        box = {}

        def work():
            box['result'] = runner.run([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=30)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        time.sleep(0.3)
        runner.cancel()
        thread.join(timeout=10)
        self.assertTrue(box['result'].cancelled or box['result'].returncode != 0)
        self.assertLess(box['result'].duration, 20)


class WindowedReaderTests(unittest.TestCase):
    def test_sequential_reads_do_not_spawn_once_per_block(self):
        total = 32 * 1024 * 1024
        calls = []

        def fetch(offset, length):
            calls.append((offset, length))
            return b'a' * length

        reader = WindowedReader(fetch, window=8 * 1024 * 1024, size=total)
        position = 0
        while position < total:
            data = reader.read(position, min(4 * 1024 * 1024, total - position))
            self.assertTrue(data)
            position += len(data)
        self.assertEqual(position, total)
        # 32 MiB in 4 MiB steps used to be 8 spawns; one window per 8 MiB is 4.
        self.assertEqual(len(calls), 4)
        self.assertEqual(reader.spawns, 4)

    def test_reading_past_the_known_size_returns_empty(self):
        reader = WindowedReader(lambda offset, length: b'x' * length, window=1024, size=10)
        self.assertEqual(reader.read(10, 5), b'')
        self.assertEqual(reader.spawns, 0)


if __name__ == '__main__':
    unittest.main()
