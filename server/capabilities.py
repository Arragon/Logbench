"""Capability probing for R6.

Design rule: probe, never assume, and never let one missing tool disable the
local reading path. Every probe returns ``{state, ...}`` where ``state`` is one
of ``enabled`` / ``degraded`` / ``disabled`` / ``unavailable``; features are
derived from probes so the UI can show a reason instead of a dead button.

Hard constraints (R6-007):
* no installation, no ``sudo``, no mount, no downloads;
* the result never carries credentials or the process environment;
* an unknown SQLite build falls back to a conservative journal mode instead of
  claiming WAL support.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .runtime.procmetrics import (cgroup_cpu_quota, cgroup_memory_limit,
                                 cpu_affinity_count, physical_memory_bytes)

SCHEMA = 'logbench.capabilities/1'
TARGET_PYTHON = (3, 9, 11)
SAFE_WAL_SINCE = (3, 44, 0)
PROBE_TIMEOUT = 4
STATES = ('enabled', 'degraded', 'disabled', 'unavailable')


def _version_tuple(text: str) -> tuple:
    parts = []
    for chunk in str(text or '').strip().split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _probe_command(argv, version_flag='--version'):
    """Run one short, side-effect free command. Never raises."""
    executable = shutil.which(argv[0])
    if not executable:
        return {'state': 'unavailable', 'reason': '%s 未安装或不在 PATH 中' % argv[0], 'path': None, 'version': None}
    try:
        done = subprocess.run([executable] + list(argv[1:]) + ([version_flag] if version_flag else []),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {'state': 'disabled', 'reason': '%s 调用失败：%s' % (argv[0], exc.__class__.__name__), 'path': executable, 'version': None}
    text = done.stdout.decode('utf-8', 'replace').strip().splitlines()
    first = text[0].strip() if text else ''
    if done.returncode != 0:
        return {'state': 'disabled', 'reason': '%s 退出码 %d' % (argv[0], done.returncode), 'path': executable, 'version': first or None}
    return {'state': 'enabled', 'reason': None, 'path': executable, 'version': first or None}


def probe_python() -> dict:
    """Real interpreter check. A syntax-only pass on another version is not evidence."""
    version = '%d.%d.%d' % sys.version_info[:3]
    meets = _version_tuple(version) >= TARGET_PYTHON
    return {
        'state': 'enabled' if meets else 'degraded',
        'version': version,
        'implementation': sys.implementation.name,
        'target': '%d.%d.%d' % TARGET_PYTHON,
        'meets_target': meets,
        'reason': None if meets else '解释器低于目标 %d.%d.%d，仅作辅助证据' % TARGET_PYTHON,
        'executable': sys.executable,
    }


def _sqlite_journal_policy(version_text: str) -> dict:
    version = _version_tuple(version_text)
    safe = version >= SAFE_WAL_SINCE
    return {
        'safe_to_use_wal': safe,
        'journal_policy': 'wal' if safe else 'delete',
        'wal_reason': None if safe else 'SQLite %s 低于 %s，WAL 并发修复未确认，使用保守 journal' % (
            version_text, '%d.%d.%d' % SAFE_WAL_SINCE),
    }


def _usable_dir(directory) -> Path:
    """Nearest existing directory, so a missing data/ still yields a real report."""
    candidate = Path(directory) if directory else Path(tempfile.gettempdir())
    for parent in [candidate] + list(candidate.parents):
        if parent.is_dir():
            return parent
    return Path(tempfile.gettempdir())


def probe_sqlite(directory=None) -> dict:
    """Probe by doing: create the virtual tables instead of trusting compile flags."""
    result = {
        'state': 'disabled', 'reason': None, 'version': None,
        'fts5': False, 'fts3': False, 'trigram': False, 'json1': False,
        'journal_default': None,
    }
    tmp = None
    probe_path = None
    try:
        if directory is None or not Path(directory).is_dir():
            tmp = tempfile.mkdtemp(prefix='logbench-cap-')
            path = str(Path(tmp) / 'probe.db')
        else:
            # Probe on the real volume so journal behaviour is representative,
            # then remove the file: a capability check must not leave artifacts.
            probe_path = Path(directory) / ('.capability-probe-%d.db' % os.getpid())
            path = str(probe_path)
        connection = sqlite3.connect(path)
        try:
            result['version'] = connection.execute('select sqlite_version()').fetchone()[0]
            result['journal_default'] = connection.execute('pragma journal_mode').fetchone()[0]
            for name, sql in (
                ('fts5', "create virtual table probe_fts5 using fts5(x)"),
                ('fts3', "create virtual table probe_fts3 using fts3(x)"),
                ('trigram', "create virtual table probe_trigram using fts5(x, tokenize='trigram')"),
            ):
                try:
                    connection.execute(sql)
                    result[name] = True
                except sqlite3.Error:
                    result[name] = False
            try:
                result['json1'] = connection.execute("select json_valid('{\"a\":1}')").fetchone()[0] == 1
            except sqlite3.Error:
                result['json1'] = False
        finally:
            connection.close()
        result.update(_sqlite_journal_policy(result['version']))
        if not result['fts5']:
            result['state'] = 'degraded'
            result['reason'] = 'FTS5 不可用，索引查询回退为顺序扫描'
        else:
            result['state'] = 'enabled'
    except (sqlite3.Error, OSError) as exc:
        result['state'] = 'disabled'
        result['reason'] = 'SQLite 探测失败：%s' % exc.__class__.__name__
        result.update(_sqlite_journal_policy('0.0.0'))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        if probe_path is not None:
            for suffix in ('', '-journal', '-wal', '-shm'):
                try:
                    Path(str(probe_path) + suffix).unlink()
                except Exception:
                    # Some sandboxes/hooks deny unlink with a non-OSError.
                    # A leftover probe file is harmless; failing the whole
                    # capability report (and dropping the HTTP connection) is not.
                    pass
    return result


def probe_disk(directory) -> dict:
    requested = Path(directory) if directory else Path(tempfile.gettempdir())
    target = _usable_dir(requested)
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {'state': 'disabled', 'reason': '无法读取磁盘信息：%s' % exc.__class__.__name__,
                'free_bytes': None, 'total_bytes': None, 'writable': False, 'path': str(target)}
    writable = os.access(str(target), os.W_OK)
    exists = requested.is_dir()
    reasons = []
    if not exists:
        reasons.append('数据目录尚不存在，按最近的上级目录估算')
    if not writable:
        reasons.append('目录不可写，私有上传与索引写入会被拒绝')
    state = 'enabled' if writable and exists else ('degraded' if writable else 'disabled')
    return {
        'state': state,
        'reason': '；'.join(reasons) or None,
        'free_bytes': usage.free,
        'total_bytes': usage.total,
        'writable': writable,
        'path': str(target),
    }


def probe_node() -> dict:
    probe = _probe_command(['node'])
    if probe['state'] != 'enabled':
        return probe
    fixture = (
        'const out={ok:true,json:JSON.parse(\'{"a":[1,2]}\').a.length,'
        'bigint:(typeof 9007199254740993n==="bigint"),'
        'textdecoder:(typeof TextDecoder==="function"),'
        'platform:process.platform};'
        'process.stdout.write(JSON.stringify(out));'
    )
    try:
        done = subprocess.run([probe['path'], '-e', fixture], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=PROBE_TIMEOUT, check=False)
        payload = json.loads(done.stdout.decode('utf-8', 'replace') or '{}')
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        probe.update({'state': 'disabled', 'reason': 'Node 夹具执行失败：%s' % exc.__class__.__name__, 'fixture': None})
        return probe
    probe['fixture'] = payload
    if not payload.get('ok'):
        probe.update({'state': 'degraded', 'reason': 'Node 无法执行公共 JS 夹具'})
    elif payload.get('platform') != 'linux':
        # glibc compatibility only matters for the Linux采集机.
        probe['glibc'] = {'applicable': False, 'reason': '非 Linux 平台，跳过 glibc 兼容判定'}
    else:
        probe['glibc'] = {'applicable': True, 'reason': '由目标采集机安装时确认，本机不做推断'}
    return probe


def probe_rclone() -> dict:
    probe = _probe_command(['rclone'])
    if probe['state'] == 'enabled':
        probe['reason'] = None
    else:
        probe['reason'] = probe.get('reason') or 'rclone 不可用，远程对象存储采集不可用'
    return probe


def probe_ssh() -> dict:
    probe = _probe_command(['ssh'], version_flag='-V')
    if probe['state'] != 'enabled':
        probe['reason'] = probe.get('reason') or 'OpenSSH 不可用，SFTP 采集不可用'
    return probe


def probe_cpu() -> dict:
    """CPU and memory as the kernel reports them; `None` where it does not.

    `sched_getaffinity` is a Linux cgroup hint and `sysconf`/`/proc/meminfo` are
    POSIX, so the numbers come from the cross-platform probe and the source is
    named instead of assumed.
    """
    info = {'state': 'enabled', 'reason': None, 'affinity_count': None, 'cpu_count': os.cpu_count(),
            'affinity_source': 'cgroup', 'memory_bytes': None,
            'cgroup_memory_max': cgroup_memory_limit(), 'cgroup_cpu_quota': cgroup_cpu_quota()}
    affinity = cpu_affinity_count()
    if affinity is None:
        info['affinity_count'] = info['cpu_count']
        info['affinity_source'] = 'logical_cores'
    else:
        info['affinity_count'] = affinity
    info['memory_bytes'] = physical_memory_bytes()
    return info


def _feature(state, reason=None, **extra) -> dict:
    if state not in STATES:
        raise ValueError('unknown capability state: %s' % state)
    payload = {'state': state, 'reason': reason}
    payload.update(extra)
    return payload


def derive_features(probes: dict) -> dict:
    """Map probes to user-facing features. A missing tool never disables reading."""
    sqlite = probes.get('sqlite') or {}
    disk = probes.get('disk') or {}
    node = probes.get('node') or {}
    rclone = probes.get('rclone') or {}
    ssh = probes.get('ssh') or {}

    server_index_reason = None
    if sqlite.get('state') == 'disabled':
        server_index_state = 'disabled'
        server_index_reason = sqlite.get('reason') or 'SQLite 不可用，服务端索引无法启用'
    elif not sqlite.get('fts5'):
        server_index_state = 'degraded'
        server_index_reason = 'FTS5 不可用，索引查询退化为顺序扫描（结果正确但更慢）'
    else:
        server_index_state = 'enabled'

    collect_state, collect_reason = 'disabled', '未检测到 rclone 或 OpenSSH，远程采集不可用'
    if rclone.get('state') == 'enabled' or ssh.get('state') == 'enabled':
        collect_state = 'enabled'
        collect_reason = None
        if rclone.get('state') != 'enabled':
            collect_state = 'degraded'
            collect_reason = '仅 OpenSSH 可用，SMB/HTTP 采集需要其他通道'

    return {
        'local_reading': _feature('enabled', '本机阅读只依赖浏览器，与服务器工具链无关'),
        'browser_search': _feature('enabled', '由页面内 Worker 执行', probe='client'),
        'browser_storage': _feature('enabled', 'IndexedDB 由客户端探测', probe='client'),
        'server_index': _feature(server_index_state, server_index_reason,
                                 journal_policy=sqlite.get('journal_policy')),
        'private_upload': _feature('enabled' if disk.get('writable') else 'disabled',
                                   None if disk.get('writable') else '数据目录不可写'),
        'remote_collect': _feature(collect_state, collect_reason,
                                   channels=[name for name, probe in (('rclone', rclone), ('ssh', ssh))
                                             if probe.get('state') == 'enabled']),
        'js_engine': _feature('enabled' if node.get('state') == 'enabled' else 'degraded',
                              None if node.get('state') == 'enabled' else '本机 Node 不可用，仅影响夹具与诊断脚本，不影响页面'),
    }


def collect(directory=None) -> dict:
    directory = Path(directory or tempfile.gettempdir())
    probes = {
        'python': probe_python(),
        'sqlite': probe_sqlite(directory),
        'disk': probe_disk(directory),
        'node': probe_node(),
        'rclone': probe_rclone(),
        'ssh': probe_ssh(),
        'cpu': probe_cpu(),
    }
    return {
        'schema': SCHEMA,
        'probes': probes,
        'features': derive_features(probes),
        'notes': [
            '本结果不包含凭据、环境变量或用户目录内容。',
            '探测过程不安装任何软件，也不修改系统库。',
        ],
    }


def summary(payload: dict) -> str:
    lines = ['capabilities %s' % payload.get('schema')]
    for name, probe in sorted(payload.get('probes', {}).items()):
        lines.append('  probe %-8s %-11s %s' % (name, probe.get('state'), probe.get('reason') or probe.get('version') or ''))
    for name, feature in sorted(payload.get('features', {}).items()):
        lines.append('  feature %-15s %-11s %s' % (name, feature.get('state'), feature.get('reason') or ''))
    return '\n'.join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    target = None
    if '--out' in argv:
        target = argv[argv.index('--out') + 1]
    directory = None
    if '--dir' in argv:
        directory = argv[argv.index('--dir') + 1]
    payload = collect(directory)
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if target:
        Path(target).write_text(text + '\n', 'utf-8')
        print(summary(payload))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
