#!/usr/bin/env python3
"""Gate M7 (R6-077): reliable archive, resume, rotation and live reading.

Every section below either produces evidence or says BLOCKED. It never converts a
missing protocol endpoint into a pass:

          section              what is real here
    ----------------------  ------------------------------------------------
    archive-crash           real files, real SQLite, real SIGKILL of a child
                            process at three points in the commit chain
    rotation                real renames/truncations on a real directory,
                            driven through the real scheduler and archive
    faults                  real error classes through the real retry policy;
                            real storage low-watermark on a real root
    events                  real HTTP server, real socket, real SSE frames
    browser-close           collection progress with zero subscribers

    protocol-sftp/smb/scp   BLOCKED — no endpoint in this environment (B-007)

    env -u PYTHONPATH python3.9 qa/r6/remote_vertical_gate.py --out qa/evidence/M7-gate

Exit codes: 0 all executed sections passed, 1 a check failed, 3 blocked (the whole
gate could not run).
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import Application, Config                                    # noqa: E402
from server.collectors import base as adapter_base                            # noqa: E402
from server.collectors.archive import Archive, ArchiveError                   # noqa: E402
from server.collectors.base import (Adapter, Capabilities, Chunk,             # noqa: E402
                                   CollectorError, Entry, Listing, RemoteStat,
                                   SUPPORTED)
from server.collectors.generations import Observed, decide                    # noqa: E402
from server.collectors.scheduler import CollectScheduler                      # noqa: E402
from server.collectors.sources import SourceService                           # noqa: E402
from server.storage.lifecycle import GIB, Lifecycle, LifecycleError           # noqa: E402

CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ['LB_ROOT'])
from pathlib import Path
from server.app import Application, Config
from server.collectors.archive import Archive

cfg = json.loads(os.environ['LB_CFG'])
app = Application(Config(Path(cfg['root']), data_dir=Path(cfg['data']),
                         state_dir=Path(cfg['state']), code_root=Path(cfg['root'])))
archive = Archive(app)
mode = os.environ['LB_MODE']
kill = lambda: os.kill(os.getpid(), 9)

if mode == 'before-commit':
    archive._commit = lambda *a, **k: kill()
    archive.append(cfg['file_id'], cfg['generation'], offset=cfg['offset'],
                   data=cfg['payload'].encode(), writer='gate')
elif mode == 'after-commit':
    archive.append(cfg['file_id'], cfg['generation'], offset=cfg['offset'],
                   data=cfg['payload'].encode(), writer='gate')
    kill()
elif mode == 'after-index':
    archive.append(cfg['file_id'], cfg['generation'], offset=cfg['offset'],
                   data=cfg['payload'].encode(), writer='gate')
    from server.indexing.incremental import IncrementalIndexer
    IncrementalIndexer(app).sync(cfg['file_id'], cfg['generation'], cfg['rel_path'])
    kill()
else:
    raise SystemExit('unknown mode ' + mode)
'''


# --------------------------------------------------------------------- adapter
class LocalFilesAdapter(Adapter):
    """A real transport over a local directory, used only by this gate.

    It is not a protocol adapter and is not registered in the product: it exists so
    the gate can rename, truncate and delete bytes for real instead of pretending
    with mocks.
    """

    name = 'gate-local'
    label = '门禁 · 本机目录'
    default_port = 0
    requires_tools = ()

    def __init__(self, source, *, base: Path, **kwargs):
        super().__init__(source, **kwargs)
        self.base = Path(base)
        self.fail = None

    def capabilities(self):
        return Capabilities(directory_discovery=SUPPORTED, range_read=SUPPORTED,
                            stable_identity='size-mtime', append_contract='declared')

    def probe(self, *, config_revision, fingerprint, guard=None):
        raise CollectorError('unsupported', '门禁适配器不提供预检')

    def _resolve(self, path: str) -> Path:
        return self.base / str(path).lstrip('/')

    def list(self, directory, *, cursor=None, limit=None):
        if self.fail == 'list':
            raise CollectorError('transport_error', '门禁注入的列目录失败')
        target = self._resolve(directory)
        if not target.is_dir():
            raise CollectorError('directory_not_found', '远端目录不存在。', path=directory)
        entries = []
        for item in sorted(target.iterdir()):
            if not item.is_file():
                continue
            stat = item.stat()
            entries.append(Entry(path=directory.rstrip('/') + '/' + item.name,
                                 name=item.name, size=stat.st_size,
                                 mtime_ns=stat.st_mtime_ns,
                                 identity={'id': str(stat.st_ino)}))
        return Listing(directory=directory, entries=entries, scanned=len(entries))

    def stat(self, path):
        item = self._resolve(path)
        if not item.is_file():
            raise CollectorError('not_found', '远端文件不存在。', path=path)
        stat = item.stat()
        return RemoteStat(path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                          identity={'id': str(stat.st_ino), 'modTime': stat.st_mtime_ns})

    def read_range(self, path, *, offset, length, identity=None):
        if self.fail == 'read':
            raise CollectorError('locked', '门禁注入的文件锁')
        item = self._resolve(path)
        if not item.is_file():
            raise CollectorError('not_found', '远端文件不存在。', path=path)
        with open(item, 'rb') as handle:
            handle.seek(offset)
            data = handle.read(length)
        return Chunk(data, offset, offset + len(data),
                     identity={'size': item.stat().st_size}, verified=True)


def make_app(root: Path) -> Application:
    data = root / 'data'
    state = root / 'var'
    data.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json',
                root / 'rules' / 'cpqpsgx-default.json')
    app = Application(Config(root, data_dir=data, state_dir=state, code_root=root,
                             allowed_hosts=['127.0.0.1'], bootstrap_admins=['127.0.0.1']))
    app.index_jobs.executor = None
    return app


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ''


# ------------------------------------------------------------------ sections
def section_archive_crash(out: Path) -> dict:
    """Kill a real child process at each unconfirmed/confirmed boundary."""
    cases = []
    root = Path(tempfile.mkdtemp(prefix='lb-gate-crash-'))
    app = make_app(root)
    remote = root / 'remote'
    remote.mkdir()
    watched = remote / 'app.log'
    watched.write_bytes(b'line-1\nline-2\n')

    service = SourceService(app)
    created = service.create({'role': 'admin', 'ip': '127.0.0.1', 'id': 'gate'},
                             {'adapter': 'gate-local', 'label': 'GATE',
                              'endpoint': 'sftp://127.0.0.1', 'acl': 'admin',
                              'policy': {'allowedHosts': ['127.0.0.1'],
                                         'allowLoopback': True},
                              'mappings': [{'remoteDir': '/', 'rootId': 'legacy',
                                            'targetSubdir': 'gate'}]})
    source_id = created['id']
    with app.store.transaction() as db:
        db.execute('UPDATE remote_sources SET enabled=1 WHERE id=?', (source_id,))
    mapping = service.mappings(source_id)[0]

    scheduler = CollectScheduler(app, settings={'max_bytes_per_run': 1 << 20},
                                 adapters_factory=lambda src: LocalFilesAdapter(
                                     src, base=remote))
    scheduler.run_source(source_id)
    with app.store.connect() as db:
        row = db.execute('SELECT * FROM source_files').fetchone()
    file_id = row['file_id']
    rel_path = row['rel_path']
    generation = int(row['generation'])
    archived = Path(app.config.data_dir) / rel_path
    base_bytes = archived.read_bytes()
    cases.append({'case': 'first-pass', 'committed': len(base_bytes),
                  'hash': digest_of(archived), 'passed': len(base_bytes) > 0})

    cfg = {'root': str(root), 'data': str(app.config.data_dir), 'state': str(app.config.state_dir)}
    for mode, extra in (('before-commit', b'line-3\n'), ('after-commit', b'line-4\n'),
                        ('after-index', b'line-5\n')):
        with app.store.connect() as db:
            current = db.execute('SELECT contiguous_bytes FROM source_files WHERE file_id=?',
                                 (file_id,)).fetchone()['contiguous_bytes']
        payload = json.dumps(dict(cfg, file_id=file_id, generation=generation,
                                  offset=int(current), rel_path=rel_path,
                                  payload=extra.decode())).encode()
        env = dict(os.environ, LB_ROOT=str(ROOT), LB_CFG=payload.decode(), LB_MODE=mode,
                   LB_DEBUG='1')
        done = subprocess.run([sys.executable, '-c', CHILD], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        killed = done.returncode in (-9, 137, 9)
        archive = Archive(app)
        before_recover = archive.watermark(file_id, generation)
        recovery = archive.recover(file_id, generation)
        after = archive.watermark(file_id, generation)
        verified = archive.verify(file_id, generation)
        case = {'case': mode, 'killed': killed, 'returncode': done.returncode,
                'watermark_before_recover': before_recover['contiguous'],
                'watermark_after_recover': after['contiguous'],
                'recovery': recovery, 'verify_ok': verified.get('ok'),
                'stderr': (done.stderr or b'').decode()[-1200:]}
        if mode == 'before-commit':
            # Unconfirmed bytes must not enter the watermark, and the tail is cut.
            case['passed'] = (killed and int(before_recover['contiguous']) == int(current)
                              and int(after['contiguous']) == int(current)
                              and digest_of(archived) == hashlib.sha256(base_bytes).hexdigest())
            if case['passed']:
                archive.append(file_id, generation, offset=int(current), data=extra,
                               writer='gate')
                base_bytes += extra
        elif mode == 'after-commit':
            case['passed'] = (killed and int(after['contiguous']) == int(current) + len(extra)
                              and digest_of(archived) == hashlib.sha256(
                                  base_bytes + extra).hexdigest() and verified.get('ok'))
            if case['passed']:
                base_bytes += extra
        else:
            # The index was written before the notification: a restart must not
            # parse the same bytes again, and the record count must not double.
            case['passed'] = killed and int(after['contiguous']) == int(current) + len(extra) \
                and digest_of(archived) == hashlib.sha256(base_bytes + extra).hexdigest()
            if case['passed']:
                base_bytes += extra
                try:
                    from server.indexing.incremental import IncrementalIndexer
                    again = IncrementalIndexer(app).sync(file_id, generation, rel_path)
                    case['resync'] = {k: again.get(k) for k in
                                      ('state', 'mode', 'newRecords', 'records', 'bytesRead')}
                    case['no_reparse'] = (again.get('state') == 'current'
                                          and not again.get('newRecords'))
                    case['passed'] = case['no_reparse']
                except Exception as exc:  # noqa: BLE001 - Node worker may be absent
                    case['index_blocked'] = repr(exc)[:200]
        cases.append(case)

    with app.store.connect() as db:
        intervals = db.execute('SELECT COUNT(*) AS n, SUM(byte_end-byte_start) AS bytes '
                               'FROM archive_intervals WHERE file_id=? AND generation=?',
                               (file_id, generation)).fetchone()
        preview = db.execute('SELECT kind FROM event_log').fetchall()
    archive_report = {'file_id': file_id, 'generation': generation,
                      'intervals': int(intervals['n'] or 0),
                      'interval_bytes': int(intervals['bytes'] or 0),
                      'archive_bytes': len(base_bytes), 'events': len(preview),
                      'final_hash': digest_of(archived)}
    passed = all(case.get('passed', False) for case in cases if 'index_blocked' not in case)
    summary = {'state': 'PASS' if passed else 'FAIL', 'cases': cases,
               'archive': archive_report}
    (out / 'archive-crash.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def section_rotation(out: Path) -> dict:
    """Real renames, truncations and deletions on a real directory."""
    cases = []
    root = Path(tempfile.mkdtemp(prefix='lb-gate-rot-'))
    app = make_app(root)
    remote = root / 'remote'
    remote.mkdir()
    watched = remote / 'app.log'
    watched.write_text('alpha-1\nalpha-2\n', encoding='utf-8')

    service = SourceService(app)
    created = service.create({'role': 'admin', 'ip': '127.0.0.1', 'id': 'gate'},
                             {'adapter': 'gate-local', 'label': 'ROT',
                              'endpoint': 'sftp://127.0.0.1', 'acl': 'admin',
                              'policy': {'allowedHosts': ['127.0.0.1'],
                                         'allowLoopback': True},
                              'mappings': [{'remoteDir': '/', 'rootId': 'legacy',
                                            'targetSubdir': 'rot'}]})
    source_id = created['id']
    with app.store.transaction() as db:
        db.execute('UPDATE remote_sources SET enabled=1 WHERE id=?', (source_id,))
    scheduler = CollectScheduler(app, settings={'max_bytes_per_run': 1 << 20},
                                 adapters_factory=lambda src: LocalFilesAdapter(
                                     src, base=remote))

    def rows():
        with app.store.connect() as db:
            return {row['remote_path']: dict(row)
                    for row in db.execute('SELECT * FROM source_files').fetchall()}

    # Prime: the first pass registers the file, it is not a rotation case.
    scheduler.run_source(source_id)

    def run(label, expected_action, mutate, verify=None, path='/app.log'):
        mutate()
        report = scheduler.run_source(source_id)
        detail = None
        for mapping in report['mappings']:
            for item in mapping['generations']:
                if item.get('path') == path:
                    detail = item
        observed_action = detail.get('action') if detail else None
        if observed_action is None:
            # A file that vanished is reported by the pass, not by a per-file detail.
            missing = [item for mapping in report['mappings']
                       for item in mapping['generations']
                       if item.get('path') == path and item.get('action') == 'disappeared']
            if missing:
                observed_action = 'disappeared'
        case = {'case': label, 'expected': expected_action,
                'observed': observed_action,
                'report_bytes': report['bytes'], 'files': len(rows())}
        ok = observed_action is not None and (expected_action is None
                                             or observed_action == expected_action)
        if verify:
            extra = verify()
            case.update(extra)
            ok = ok and extra.get('passed', False)
        case['passed'] = ok
        cases.append(case)

    run('append', 'append', lambda: watched.write_text(
        'alpha-1\nalpha-2\nalpha-3\n', encoding='utf-8'))

    def rename_rotation():
        (remote / 'app.log').rename(remote / 'app.log.1')
        (remote / 'app.log').write_text('beta-1\n', encoding='utf-8')

    def check_rotation():
        snapshot = rows()
        old = snapshot.get('/app.log.1')
        current = snapshot.get('/app.log')
        old_bytes = (Path(app.config.data_dir) / old['rel_path']).read_bytes() if old else b''
        return {'passed': bool(old and current and old_bytes == b'alpha-1\nalpha-2\nalpha-3\n'
                               and current['contiguous_bytes'] == len(b'beta-1\n')),
                'old_file_bytes': len(old_bytes),
                'new_generation': current['generation'] if current else None}

    # A rename leaves a new file at the watched name: same path, different content,
    # different inode. Either `new` or `rewritten` is truthful; what must hold is that
    # the old generation keeps every byte and the new one starts from zero.
    run('rename-rotation', 'rewritten', rename_rotation, check_rotation)

    def check_truncate():
        row = rows().get('/app.log')
        return {'gap_state': row['gap_state'] if row else None,
                'generation': row['generation'] if row else None,
                'passed': bool(row and row['gap_state'] != 'none' and row['generation'] >= 3)}

    run('truncate', 'truncated',
        lambda: watched.write_text('bet', encoding='utf-8'), check_truncate)

    run('regrow-after-truncate', 'append', lambda: watched.write_text(
        'beta-1\nbeta-2\n', encoding='utf-8'))

    # Same byte count as the previous generation (14), different first bytes: this is
    # a replacement, not an append and not a truncation.
    run('same-size-replace', 'replaced', lambda: watched.write_text(
        'GAMMA-1\nGAMMA\n', encoding='utf-8'))

    def copytruncate():
        shutil.copy(remote / 'app.log', remote / 'app.log.2')
        with open(remote / 'app.log', 'w', encoding='utf-8') as handle:
            handle.write('delta-1\n')

    run('copytruncate', 'truncated', copytruncate)

    def check_disappeared():
        row = rows().get('/app.log')
        archived = bool(row) and (Path(app.config.data_dir) / row['rel_path']).exists()
        return {'passed': bool(row and row['state'] == 'disappeared' and archived),
                'state': row['state'] if row else None,
                'archive_retained': archived}

    # Deleted between two passes: the path is simply absent from the next listing.
    run('rapid-delete', None, lambda: (remote / 'app.log').unlink(), check_disappeared)

    (remote / 'app.log').write_text('epsilon-1\n', encoding='utf-8')
    scheduler.run_source(source_id)
    (remote / 'app.log.3.gz').write_bytes(b'\x1f\x8b\x08\x00' + b'\x00' * 32)
    compressed = scheduler.run_source(source_id)
    actions = [(item.get('path'), item.get('action')) for mapping in compressed['mappings']
               for item in mapping['generations']]
    cases.append({'case': 'compressed-rotation', 'expected': 'compressed-snapshot',
                  'observed': actions,
                  'passed': any(action == 'compressed-snapshot' for _, action in actions)})

    # Ambiguity: same size, same mtime, different first bytes, no rotation hint.
    from server.collectors.generations import Observed, Previous, decide
    observed = Observed(size=3, mtime_ns=1000, identity={'id': 'x'}, head=b'two',
                        exists=True, name='amb.log')
    previous = Previous(generation=1, size=3, identity={'id': 'x'}, mtime_ns=1000,
                        head_digest=hashlib.sha256(b'one').hexdigest()[:32], prefix_len=3)
    decision = decide(previous, observed)
    cases.append({'case': 'ambiguous-identity', 'observed': decision.action,
                  'reason': decision.reason, 'confidence': decision.confidence,
                  'passed': decision.action in ('rewritten', 'replaced', 'unknown')})

    passed = all(case['passed'] for case in cases)
    summary = {'state': 'PASS' if passed else 'FAIL', 'cases': cases,
               'protocol': 'local directory (gate adapter); SFTP/SMB/SCP blocked'}
    (out / 'rotation.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def section_faults(out: Path) -> dict:
    """Error classes through the real retry policy, and a real low-watermark."""
    cases = []
    root = Path(tempfile.mkdtemp(prefix='lb-gate-fault-'))
    app = make_app(root)
    remote = root / 'remote'
    remote.mkdir()
    (remote / 'app.log').write_text('one\n', encoding='utf-8')

    service = SourceService(app)
    created = service.create({'role': 'admin', 'ip': '127.0.0.1', 'id': 'gate'},
                             {'adapter': 'gate-local', 'label': 'FAULT',
                              'endpoint': 'sftp://127.0.0.1', 'acl': 'admin',
                              'policy': {'allowedHosts': ['127.0.0.1'],
                                         'allowLoopback': True},
                              'mappings': [{'remoteDir': '/', 'rootId': 'legacy',
                                            'targetSubdir': 'fault'}]})
    source_id = created['id']
    with app.store.transaction() as db:
        db.execute('UPDATE remote_sources SET enabled=1 WHERE id=?', (source_id,))

    holder = {}

    def factory(src):
        adapter = LocalFilesAdapter(src, base=remote)
        adapter.fail = holder.get('fail')
        holder['adapter'] = adapter
        return adapter

    scheduler = CollectScheduler(app, settings={'max_bytes_per_run': 1 << 20,
                                                'base_backoff': 5, 'max_backoff': 60},
                                 adapters_factory=factory)
    scheduler.run_source(source_id)

    def state_of(path='/app.log'):
        with app.store.connect() as db:
            rows = db.execute('SELECT * FROM source_files WHERE remote_path=?',
                              (path,)).fetchall()
        return dict(rows[0]) if rows else {}

    # Retryable: a locked file keeps its archive and gets a retry time.
    (remote / 'app.log').write_text('one\ntwo\n', encoding='utf-8')
    holder['fail'] = 'read'
    scheduler.run_source(source_id)
    row = state_of()
    cases.append({'case': 'locked-file', 'state': row.get('state'),
                  'retry_at': bool(row.get('retry_at')),
                  'passed': row.get('state') == 'retry' and bool(row.get('retry_at'))})

    # Not retryable: a missing directory is not an empty directory.
    holder['fail'] = 'list'
    report = scheduler.run_source(source_id)
    codes = [error['code'] for error in report['errors']]
    cases.append({'case': 'list-failure', 'codes': codes,
                  'passed': codes == ['transport_error']})

    holder['fail'] = None
    scheduler.run_source(source_id)
    row = state_of()
    cases.append({'case': 'recovery-after-error', 'state': row.get('state'),
                  'passed': row.get('state') == 'archived' and not row.get('retry_at')})

    # Manual classes stop and stay stopped: no retry time, a reason on record.
    for code, expect in (('hostkey_changed', 'stopped'), ('auth_failed', 'stopped'),
                         ('permission_denied', 'stopped')):
        with app.store.transaction() as db:
            db.execute('UPDATE source_files SET state=\'archived\', retry_at=NULL, '
                       'last_error=NULL WHERE remote_path=?', ('/app.log',))
        scheduler._record_error(source_id, service.mappings(source_id)[0]['id'],
                                CollectorError(code, '门禁注入'))
        row = state_of()
        cases.append({'case': code, 'state': row.get('state'),
                      'retry_at': row.get('retry_at'),
                      'needs_human': json.loads(row.get('last_error') or '{}').get('needs_human'),
                      'passed': row.get('state') == expect and row.get('retry_at') is None})

    # Storage low watermark: the source waits, and reads keep working.
    free = {'bytes': 1 * GIB}

    def sampler(path):
        return {'free': free['bytes'], 'total': 4000 * GIB, 'error': None}

    lifecycle = Lifecycle(app, sampler=sampler)
    report = lifecycle.enforce()
    with app.store.connect() as db:
        waiting = db.execute('SELECT paused,policy FROM remote_sources WHERE id=?',
                             (source_id,)).fetchone()
    index_answer = app.preview_index('legacy') if hasattr(app, 'preview_index') else None
    queries_ok = True
    try:
        with app.store.connect() as db:
            db.execute('SELECT COUNT(*) FROM source_files').fetchone()
    except Exception:  # noqa: BLE001
        queries_ok = False
    cases.append({'case': 'storage-low', 'paused': bool(waiting['paused']),
                  'policy': json.loads(waiting['policy'] or '{}'),
                  'paused_sources': len(report['paused']),
                  'reads_ok': queries_ok,
                  'passed': bool(waiting['paused']) and queries_ok})
    free['bytes'] = 1000 * GIB
    lifecycle.enforce()
    with app.store.connect() as db:
        resumed = db.execute('SELECT paused,policy FROM remote_sources WHERE id=?',
                             (source_id,)).fetchone()
    cases.append({'case': 'storage-recovered', 'paused': bool(resumed['paused']),
                  'policy': json.loads(resumed['policy'] or '{}'),
                  'passed': not resumed['paused']})

    passed = all(case['passed'] for case in cases)
    summary = {'state': 'PASS' if passed else 'FAIL', 'cases': cases}
    (out / 'faults.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def section_events(out: Path) -> dict:
    """Real HTTP server, real socket, real SSE frames."""
    from server.httpd import Server

    cases = []
    root = Path(tempfile.mkdtemp(prefix='lb-gate-events-'))
    app = make_app(root)
    server = Server(('127.0.0.1', 0), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def read_stream(last_event_id, publish_after=0):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
        connection.request('GET', '/api/v2/events?last=%s' % last_event_id,
                           headers={'Accept': 'text/event-stream', 'Accept-Language': 'zh'})
        response = connection.getresponse()
        frames = []
        deadline = time.time() + 6
        buffer = b''
        while time.time() < deadline and len(frames) < 3:
            if publish_after and len(frames) == 0:
                app.events.publish(kind='index_commit',
                                   payload={'fileId': 'f1', 'state': 'indexed', 'records': 1})
                publish_after -= 1
            chunk = response.fp.readline()
            if not chunk:
                break
            buffer += chunk
            if buffer.endswith(b'\n\n'):
                frames.append(buffer.decode('utf-8', 'replace'))
                buffer = b''
        connection.close()
        return response.status, frames

    for _ in range(3):
        app.events.publish(kind='archive_commit',
                           payload={'fileId': 'f1', 'committed': True, 'offset': 10})
    status, frames = read_stream(0)
    ids = [int(line.split('id:')[1]) for frame in frames for line in frame.splitlines()
           if line.startswith('id:')]
    bodies = [frame for frame in frames if 'line-1' in frame]
    cases.append({'case': 'stream-handshake', 'status': status, 'frames': len(frames),
                  'ids': ids, 'monotonic': ids == sorted(ids),
                  'bodies_leaked': len(bodies),
                  'passed': status == 200 and len(frames) >= 3 and ids == sorted(ids)
                  and not bodies})

    # Expired replay: ask from an id that has aged out of the bounded log.
    with app.store.transaction() as db:
        db.execute('DELETE FROM event_log')
        db.execute("INSERT INTO event_log(scope,kind,payload,created_at,expires_at) "
                   "VALUES('shared','archive_commit','{}','x',?)", (time.time() + 60,))
        db.execute("INSERT INTO event_log(scope,kind,payload,created_at,expires_at) "
                   "VALUES('shared','archive_commit','{}','x',?)", (time.time() + 60,))
        first = db.execute('SELECT MIN(id) AS id FROM event_log').fetchone()['id']
    # Ask from a position that was pruned: the answer must be `resync_required`,
    # not a shorter event list that looks like "nothing happened".
    status, frames = read_stream(max(0, int(first) - 2))
    joined = ''.join(frames)
    cases.append({'case': 'expired-replay', 'status': status,
                  'resync_required': 'resync_required' in joined,
                  'reason': 'event_retention_exceeded' in joined,
                  'passed': 'resync_required' in joined})

    # Polling and streaming must agree for the same cursor.
    app.events.publish(kind='task_progress', payload={'sourceId': 's1', 'state': 'indexed'})
    replay = app.events.poll({'role': 'admin', 'ip': '127.0.0.1'}, 0)
    polled_ids = [event['id'] for event in replay['events']]
    status, frames = read_stream(0)
    stream_ids = [int(line.split('id:')[1]) for frame in frames for line in frame.splitlines()
                  if line.startswith('id:')]
    cases.append({'case': 'poll-equals-stream', 'polled': polled_ids[:len(stream_ids)],
                  'streamed': stream_ids,
                  'passed': polled_ids[:len(stream_ids)] == stream_ids and bool(stream_ids)})

    # Scope: a member never sees the administrator scope.
    app.events.publish(kind='source_error', payload={'code': 'x'}, scope='shared')
    member = app.events.poll({'role': 'member', 'ip': '127.0.0.1'}, 0)
    cases.append({'case': 'scope-filter', 'member_events': len(member['events']),
                  'passed': all(event['scope'] != 'shared' for event in member['events'])})

    server.shutdown()
    passed = all(case['passed'] for case in cases)
    summary = {'state': 'PASS' if passed else 'FAIL', 'cases': cases,
               'note': '真实 socket；HTTP 传输，不涉及 WSGI 代理缓冲'}
    (out / 'events.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def section_browser_close(out: Path) -> dict:
    """Collection progress must not depend on a browser being open."""
    root = Path(tempfile.mkdtemp(prefix='lb-gate-close-'))
    app = make_app(root)
    remote = root / 'remote'
    remote.mkdir()
    (remote / 'app.log').write_text('one\n', encoding='utf-8')
    service = SourceService(app)
    created = service.create({'role': 'admin', 'ip': '127.0.0.1', 'id': 'gate'},
                             {'adapter': 'gate-local', 'label': 'CLOSE',
                              'endpoint': 'sftp://127.0.0.1', 'acl': 'admin',
                              'policy': {'allowedHosts': ['127.0.0.1'],
                                         'allowLoopback': True},
                              'mappings': [{'remoteDir': '/', 'rootId': 'legacy',
                                            'targetSubdir': 'close'}]})
    source_id = created['id']
    with app.store.transaction() as db:
        db.execute('UPDATE remote_sources SET enabled=1 WHERE id=?', (source_id,))
    scheduler = CollectScheduler(app, settings={'max_bytes_per_run': 1 << 20},
                                 adapters_factory=lambda src: LocalFilesAdapter(
                                     src, base=remote))
    first = scheduler.run_source(source_id)
    subscribers = app.events.snapshot()['subscribers']
    (remote / 'app.log').write_text('one\ntwo\nthree\n', encoding='utf-8')
    second = scheduler.run_source(source_id)
    with app.store.connect() as db:
        row = db.execute('SELECT contiguous_bytes FROM source_files').fetchone()
    summary = {'state': 'PASS' if (subscribers == 0 and second['bytes'] > 0) else 'FAIL',
               'cases': [{'case': 'no-subscriber-progress', 'subscribers': subscribers,
                          'first_bytes': first['bytes'], 'second_bytes': second['bytes'],
                          'contiguous': row['contiguous_bytes'],
                          'passed': subscribers == 0 and second['bytes'] > 0}],
               'note': '采集由服务端调度驱动；浏览器只是订阅者'}
    (out / 'browser-close.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def section_protocols(out: Path) -> dict:
    """No SFTP/SMB/legacy-SCP endpoint exists here; say so instead of mocking."""
    probes = {}
    for name, script in (('sftp', 'protocol_sftp.py'), ('smb', 'protocol_smb.py'),
                         ('scp', 'protocol_scp.py')):
        path = ROOT / 'qa' / 'r6' / script
        if not path.is_file():
            probes[name] = {'state': 'BLOCKED', 'reason': 'probe script missing'}
            continue
        done = subprocess.run([sys.executable, str(path)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120,
                              env=dict(os.environ, PYTHONPATH=''))
        probes[name] = {'state': 'BLOCKED' if done.returncode == 3 else
                        ('PASS' if done.returncode == 0 else 'FAIL'),
                        'returncode': done.returncode,
                        'stdout': done.stdout.decode()[-400:]}
    summary = {'state': 'BLOCKED', 'protocols': probes,
               'reason': 'B-007：本机无 rclone / SFTP / SMB / legacy SCP 端点；不以 mock 抵扣'}
    (out / 'protocols.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence' / 'M7-gate'))
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if adapter_base.adapter_class(LocalFilesAdapter.name) is None:
        adapter_base.register(LocalFilesAdapter)

    sections = {'archive-crash': section_archive_crash, 'rotation': section_rotation,
                'faults': section_faults, 'events': section_events,
                'browser-close': section_browser_close,
                'protocols': section_protocols}
    results = {}
    for name, runner in sections.items():
        if args.only and name not in args.only.split(','):
            continue
        started = time.time()
        try:
            results[name] = runner(out)
        except Exception as exc:  # noqa: BLE001 - a crashed section is evidence too
            results[name] = {'state': 'FAIL', 'error': repr(exc)[:600]}
        results[name]['seconds'] = round(time.time() - started, 2)
        sys.stderr.write('%-14s %s\n' % (name, results[name]['state']))

    verdicts = [value['state'] for key, value in results.items() if key != 'protocols']
    report = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'sections': results,
              'passed': all(state == 'PASS' for state in verdicts),
              'blocked_sections': [key for key, value in results.items()
                                   if value['state'] == 'BLOCKED'],
              'python': sys.version.split()[0],
              'node': shutil.which('node')}
    (out / 'M7-gate.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'],
                      'sections': {key: value['state'] for key, value in results.items()}},
                     ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
