"""Remote sources, directory mappings, and revision-bound probes (R6-066).

Three invariants hold this module together:

1. **A probe is evidence for one configuration.** `config_fingerprint()` hashes the
   adapter, endpoint, credential fingerprint, host pin, policy and the full mapping
   set. Editing any of them changes the fingerprint, so the previous probe cannot
   enable the new revision. A probe also expires.
2. **Enabling queues work; it does not claim work happened.** `enable` may create a
   `collect` job in `queued`; nothing here sets a state to `running` or a watermark
   to a number it did not read.
3. **Deleting a source deletes configuration, never archived bytes.** The archive
   rows and `source_files` survive so the collected logs remain readable; only the
   `remote_sources` row and its mappings go.

Members see summaries. Endpoints, policies, paths and credentials are administrator
material, and a member's visibility is decided per request, not by hiding buttons.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import base as adapter_base
from .credentials import CredentialError, CredentialStore
from .security import Admission, Endpoint, Guard, SecurityError

POLICY_KEYS = {
    'user', 'domain', 'share', 'directory', 'allowedHosts', 'allowedNetworks',
    'allowedPorts', 'allowedPaths', 'allowLoopback', 'knownHostsFile', 'hostKey',
    'appendContract', 'generationId', 'manifestUrl', 'manifestName', 'listingUrl',
    'explicitUrl', 'directoryMode', 'caFile', 'readTimeout', 'connectTimeout',
    'snapshotTimeout', 'probeTtl', 'maxDepth', 'maxResponseBytes', 'include',
    'exclude', 'tools', 'sessionDir', 'keyUseAgent', 'signing', 'encryption',
    'expectedSha256', 'expectedAddresses', 'credentialKind', 'disableSigning',
    # Scheduling intervals are policy, not a second source of truth: R6-072 reads
    # them and clamps them to its own bounds.
    'activeInterval', 'discoveryInterval', 'maxDepth', 'maxReadBytes',
}
MODES = ('manual', 'scheduled', 'follow')
ACTIONS = ('enable', 'disable', 'pause', 'resume', 'scan', 'retry')
MAX_POLICY_BYTES = 32 * 1024


class SourceError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _loads(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MappingRules:
    """Validation for one mapping. Kept separate because the wizard previews it."""

    @staticmethod
    def remote_dir(value) -> str:
        text = str(value or '').strip()
        if not text.startswith('/'):
            raise SourceError('invalid_mapping', '远端目录必须是绝对路径。')
        if len(text) > 2048 or any(ord(ch) < 32 for ch in text):
            raise SourceError('invalid_mapping', '远端目录包含非法字符。')
        parts = [part for part in text.split('/') if part]
        if any(part in ('.', '..') for part in parts):
            raise SourceError('invalid_mapping', '远端目录不能包含上级目录。')
        return text if text.endswith('/') else text + '/'

    @staticmethod
    def target(value) -> str:
        text = str(value or '').strip().strip('/')
        if not text:
            raise SourceError('invalid_mapping', '目标子目录不能为空。')
        if len(text) > 255 or '\\' in text or ':' in text:
            raise SourceError('invalid_mapping', '目标子目录包含非法字符。')
        parts = text.split('/')
        if any(part in ('', '.', '..') or part.startswith('.') or any(ord(ch) < 32 for ch in part)
               for part in parts):
            raise SourceError('invalid_mapping', '目标子目录不能包含隐藏目录或上级目录。')
        return '/'.join(parts)

    @staticmethod
    def patterns(values, label: str) -> list:
        if values in (None, ''):
            return []
        if not isinstance(values, list) or len(values) > 32:
            raise SourceError('invalid_mapping', '%s必须是至多 32 条的字符串数组。' % label)
        result = []
        for value in values:
            text = str(value).strip()
            if not text or len(text) > 128 or any(ch in text for ch in '\x00\n\r'):
                raise SourceError('invalid_mapping', '%s包含非法模式。' % label)
            result.append(text)
        return result

    @staticmethod
    def overlap(a: dict, b: dict) -> bool:
        """Two mappings may not write into the same subtree of the same root."""
        if a['root_id'] != b['root_id']:
            return False
        left, right = a['target_subdir'], b['target_subdir']
        return left == right or left.startswith(right + '/') or right.startswith(left + '/')


class SourceService:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.credentials = CredentialStore(app.config.state_dir)
        self.session_dir = Path(app.config.state_dir) / 'sessions'
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row(row) -> dict:
        return {key: row[key] for key in row.keys()}

    def _rows(self, sql: str, params: tuple = ()) -> list:
        with self.store.connect() as db:
            return [self._row(row) for row in db.execute(sql, params).fetchall()]

    @staticmethod
    def _public(source: dict) -> dict:
        return {
            'id': source['id'], 'adapter': source['adapter'], 'label': source['label'],
            'endpoint': source['endpoint'], 'credentialRef': source['credential_ref'],
            'acl': source['acl'], 'revision': source['revision'],
            'enabled': bool(source['enabled']), 'paused': bool(source['paused']),
            'policy': _loads(source['policy'], {}),
            'capabilities': _loads(source['capabilities'], {}),
            'hostPin': source['host_pin'],
            'createdAt': source['created_at'], 'updatedAt': source['updated_at'],
        }

    @staticmethod
    def _summary(source: dict, mappings: list, probe: Optional[dict]) -> dict:
        return {
            'id': source['id'], 'adapter': source['adapter'], 'label': source['label'],
            'revision': source['revision'], 'enabled': bool(source['enabled']),
            'paused': bool(source['paused']), 'mappingCount': len(mappings),
            'probe': {'ok': bool(probe and probe['ok']),
                      'checkedAt': probe['created_at'] if probe else None,
                      'revision': probe['revision'] if probe else None} if probe else None,
            'updatedAt': source['updated_at'],
        }

    def mappings(self, source_id: str) -> list:
        rows = self._rows('SELECT * FROM directory_mappings WHERE source_id=? '
                          'ORDER BY remote_dir, target_subdir', (source_id,))
        return [{'id': row['id'], 'sourceId': row['source_id'], 'remoteDir': row['remote_dir'],
                 'rootId': row['root_id'], 'targetSubdir': row['target_subdir'],
                 'include': _loads(row['include'], []), 'exclude': _loads(row['exclude'], []),
                 'recursive': bool(row['recursive']), 'ruleRevision': row['rule_revision'],
                 'mode': row['mode'], 'enabled': bool(row['enabled'])} for row in rows]

    def probe_row(self, source_id: str, revision: Optional[int] = None) -> Optional[dict]:
        if revision is None:
            rows = self._rows('SELECT * FROM source_probes WHERE source_id=? '
                              'ORDER BY revision DESC LIMIT 1', (source_id,))
        else:
            rows = self._rows('SELECT * FROM source_probes WHERE source_id=? AND revision=?',
                              (source_id, revision))
        if not rows:
            return None
        row = rows[0]
        return {'revision': row['revision'], 'fingerprint': row['fingerprint'],
                'ok': bool(row['ok']), 'result': _loads(row['result'], {}),
                'createdAt': row['created_at'], 'expiresAt': row['expires_at'],
                'expired': row['expires_at'] < time.time(),
                'matches': row['fingerprint'] == self.fingerprint(source_id)}

    # -------------------------------------------------------------- fingerprint
    def fingerprint(self, source_id: str) -> str:
        rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        source = rows[0]
        mappings = self.mappings(source_id)
        credential_state = ''
        if source['credential_ref']:
            credential_state = self._credential_fingerprint(source['credential_ref'])
        payload = {
            'adapter': source['adapter'], 'endpoint': source['endpoint'],
            'credential': credential_state, 'hostPin': source['host_pin'],
            'policy': _loads(source['policy'], {}),
            'mappings': [{key: mapping[key] for key in
                          ('remoteDir', 'rootId', 'targetSubdir', 'include', 'exclude',
                           'recursive', 'mode', 'enabled', 'ruleRevision')}
                         for mapping in sorted(mappings, key=lambda item: item['id'])],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                              .encode('utf-8')).hexdigest()[:32]

    def _credential_fingerprint(self, ref: str) -> str:
        rows = self._rows('SELECT kind, fingerprint FROM collector_credentials WHERE id=?', (ref,))
        if not rows:
            return 'missing'
        return '%s:%s:%s' % (ref, rows[0]['kind'],
                             self.credentials.metadata(ref)['state'])

    # ------------------------------------------------------------------- read
    def adapters(self) -> list:
        return [adapter_base.availability(name) for name in adapter_base.adapter_names()]

    def list(self, actor: dict, *, limit: int = 100) -> list:
        admin = actor.get('role') == 'admin'
        sql = 'SELECT * FROM remote_sources' + ('' if admin else " WHERE acl='members'")
        rows = self._rows(sql + ' ORDER BY created_at DESC LIMIT ?', (min(int(limit), 200),))
        result = []
        for row in rows:
            mappings = self.mappings(row['id'])
            probe = self.probe_row(row['id'])
            if admin:
                item = self._public(row)
                item['mappingCount'] = len(mappings)
                item['probe'] = probe
            else:
                item = self._summary(row, mappings, probe)
                item.pop('probe', None)
            result.append(item)
        return result

    def get(self, actor: dict, source_id: str) -> dict:
        rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        source = rows[0]
        if actor.get('role') != 'admin' and source['acl'] != 'members':
            raise SourceError('unknown_source', '远程源不存在。')
        mappings = self.mappings(source_id)
        probe = self.probe_row(source_id)
        detail = self._public(source) if actor.get('role') == 'admin' else self._summary(
            source, mappings, probe)
        detail['mappings'] = mappings if actor.get('role') == 'admin' else len(mappings)
        detail['probe'] = probe
        detail['fingerprint'] = self.fingerprint(source_id)
        detail['files'] = self.file_summary(source_id)
        detail['actions'] = self.action_log(source_id, limit=10) if actor.get('role') == 'admin' else []
        return detail

    def file_summary(self, source_id: str) -> dict:
        rows = self._rows(
            'SELECT COUNT(*) AS files, SUM(size) AS size, SUM(archived_bytes) AS archived, '
            'SUM(contiguous_bytes) AS contiguous, MAX(last_seen_at) AS seen, '
            "SUM(CASE WHEN gap_state<>'none' THEN 1 ELSE 0 END) AS gaps FROM source_files "
            'WHERE source_id=?', (source_id,))
        row = rows[0] if rows else {}
        return {'files': int(row.get('files') or 0), 'bytes': int(row.get('size') or 0),
                'archivedBytes': int(row.get('archived') or 0),
                'contiguousBytes': int(row.get('contiguous') or 0),
                'lastSeenAt': row.get('seen'), 'gaps': int(row.get('gaps') or 0)}

    def action_log(self, source_id: str, *, limit: int = 20) -> list:
        rows = self._rows('SELECT * FROM source_actions WHERE source_id=? '
                          'ORDER BY created_at DESC LIMIT ?', (source_id, min(int(limit), 100)))
        return [{'actionId': row['action_id'], 'action': row['action'],
                 'result': _loads(row['result'], {}), 'createdAt': row['created_at']}
                for row in rows]

    # ------------------------------------------------------------------ create
    def create(self, actor: dict, payload: dict) -> dict:
        self.app.require_admin(actor)
        adapter = str(payload.get('adapter') or '').strip()
        if not adapter_base.adapter_class(adapter):
            raise SourceError('unsupported', '未知的传输协议：' + adapter)
        label = str(payload.get('label') or '').strip()[:120]
        if not label:
            raise SourceError('invalid_source', '请填写源名称。')
        endpoint = str(payload.get('endpoint') or '').strip()
        if not endpoint or len(endpoint) > 512:
            raise SourceError('invalid_endpoint', '请填写有效的连接地址。')
        try:
            Endpoint.parse(endpoint)
        except SecurityError as exc:
            raise SourceError(exc.code, exc.message, **exc.details)
        policy = self._policy(payload.get('policy'))
        credential_ref = self._credential(payload.get('credentialRef'))
        acl = 'members' if str(payload.get('acl') or 'admin') == 'members' else 'admin'
        source_id = 'src_' + hashlib.sha256(
            ('%s|%s|%s' % (label, adapter, endpoint)).encode('utf-8')).hexdigest()[:12]
        stamp = _now()
        with self.store.transaction() as db:
            existing = db.execute('SELECT id FROM remote_sources WHERE id=?', (source_id,)).fetchone()
            if existing:
                raise SourceError('duplicate_source', '同名同地址的远程源已存在。')
            db.execute('INSERT INTO remote_sources(id,adapter,label,endpoint,credential_ref,'
                       'host_pin,acl,revision,enabled,paused,policy,capabilities,last_probe,'
                       'created_by,created_at,updated_at) VALUES(?,?,?,?,?,NULL,?,1,0,0,?,?,NULL,?,?,?)',
                       (source_id, adapter, label, endpoint, credential_ref, acl,
                        json.dumps(policy, ensure_ascii=False), '{}', actor['ip'], stamp, stamp))
            mappings = payload.get('mappings') or []
            if mappings:
                self._write_mappings(db, source_id, mappings, replace=True)
            self.store.audit(db, actor['ip'], 'source.create', source_id,
                             {'adapter': adapter, 'mappings': len(mappings)})
        return self.get(actor, source_id)

    def update(self, actor: dict, source_id: str, payload: dict) -> dict:
        self.app.require_admin(actor)
        rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        current = rows[0]
        expected = payload.get('expectedRevision')
        if expected is not None and int(expected) != current['revision']:
            raise SourceError('revision_conflict', '源配置已被其他管理员修改，请刷新后重试。',
                              currentRevision=current['revision'])
        fields, values = [], []
        if 'label' in payload:
            label = str(payload.get('label') or '').strip()[:120]
            if not label:
                raise SourceError('invalid_source', '请填写源名称。')
            fields.append('label=?')
            values.append(label)
        if 'endpoint' in payload:
            endpoint = str(payload.get('endpoint') or '').strip()
            try:
                Endpoint.parse(endpoint)
            except SecurityError as exc:
                raise SourceError(exc.code, exc.message, **exc.details)
            fields.append('endpoint=?')
            values.append(endpoint)
        if 'policy' in payload:
            fields.append('policy=?')
            values.append(json.dumps(self._policy(payload.get('policy')), ensure_ascii=False))
        if 'credentialRef' in payload:
            fields.append('credential_ref=?')
            values.append(self._credential(payload.get('credentialRef')))
        if 'acl' in payload:
            fields.append('acl=?')
            values.append('members' if str(payload.get('acl')) == 'members' else 'admin')
        if 'hostPin' in payload:
            fields.append('host_pin=?')
            values.append(str(payload.get('hostPin') or '') or None)
        if not fields:
            raise SourceError('invalid_source', '没有可更新的字段。')
        fields.append('revision=revision+1')
        fields.append('updated_at=?')
        values.append(_now())
        values.append(source_id)
        with self.store.transaction() as db:
            db.execute('UPDATE remote_sources SET %s WHERE id=?' % ', '.join(fields), values)
            if 'mappings' in payload:
                self._write_mappings(db, source_id, payload.get('mappings') or [], replace=True)
            self.store.audit(db, actor['ip'], 'source.update', source_id,
                             {'fields': [field.split('=')[0] for field in fields]})
        return self.get(actor, source_id)

    def delete(self, actor: dict, source_id: str, payload: dict) -> dict:
        self.app.require_admin(actor)
        rows = self._rows('SELECT id FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        with self.store.transaction() as db:
            db.execute('DELETE FROM directory_mappings WHERE source_id=?', (source_id,))
            db.execute('DELETE FROM source_probes WHERE source_id=?', (source_id,))
            db.execute('DELETE FROM remote_sources WHERE id=?', (source_id,))
            self.store.audit(db, actor['ip'], 'source.delete', source_id,
                             {'keptArchive': True})
        # `source_files` and the archived bytes stay: deleting a source is not a
        # request to delete the logs it already collected.
        return {'deleted': source_id, 'archiveRetained': True}

    # ---------------------------------------------------------------- mappings
    def _write_mappings(self, db, source_id: str, mappings: list, *, replace: bool):
        if not isinstance(mappings, list) or len(mappings) > 32:
            raise SourceError('invalid_mapping', '目录映射必须是至多 32 项的数组。')
        prepared = [self._mapping_payload(source_id, item) for item in mappings]
        for index, first in enumerate(prepared):
            for second in prepared[index + 1:]:
                if MappingRules.overlap(first, second):
                    raise SourceError('target_overlap', '两个映射写入同一目标子目录。',
                                      target=first['target_subdir'], other=second['target_subdir'])
        if replace:
            db.execute('DELETE FROM directory_mappings WHERE source_id=?', (source_id,))
        stamp = _now()
        for item in prepared:
            db.execute('INSERT INTO directory_mappings(id,source_id,remote_dir,root_id,'
                       'target_subdir,include,exclude,recursive,rule_revision,mode,enabled,'
                       'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)',
                       (item['id'], source_id, item['remote_dir'], item['root_id'],
                        item['target_subdir'], json.dumps(item['include'], ensure_ascii=False),
                        json.dumps(item['exclude'], ensure_ascii=False), 1 if item['recursive'] else 0,
                        item['rule_revision'], item['mode'], stamp, stamp))

    def _mapping_payload(self, source_id: str, item: dict) -> dict:
        if not isinstance(item, dict):
            raise SourceError('invalid_mapping', '映射必须是对象。')
        remote_dir = MappingRules.remote_dir(item.get('remoteDir') or item.get('remote_dir'))
        target = MappingRules.target(item.get('targetSubdir') or item.get('target_subdir'))
        root_id = str(item.get('rootId') or item.get('root_id') or 'legacy').strip()
        self._root(root_id)
        include = MappingRules.patterns(item.get('include'), '包含模式')
        exclude = MappingRules.patterns(item.get('exclude'), '排除模式')
        mode = str(item.get('mode') or 'scheduled')
        if mode not in MODES:
            raise SourceError('invalid_mapping', '采集模式无效。')
        rule_revision = item.get('ruleRevision') or item.get('rule_revision')
        if rule_revision is not None:
            rule_revision = str(rule_revision)[:64]
        digest = hashlib.sha256(('%s|%s|%s|%s' % (source_id, remote_dir, root_id, target))
                                .encode('utf-8')).hexdigest()[:16]
        return {'id': 'map_' + digest, 'remote_dir': remote_dir, 'root_id': root_id,
                'target_subdir': target, 'include': include, 'exclude': exclude,
                'recursive': bool(item.get('recursive', True)), 'mode': mode,
                'rule_revision': rule_revision}

    def _root(self, root_id: str) -> dict:
        """Resolve a target root through the storage registry (R6-052), not by path.

        The M5 registry owns root identity, kind and online state; duplicating that
        lookup here would let a source target a root the library itself considers
        offline or private.
        """
        storage = getattr(self.app, 'storage', None)
        if storage is not None:
            try:
                root = storage.roots.get(root_id)
            except Exception:  # noqa: BLE001 - unknown/malformed root falls through
                root = None
            if root:
                kind = root.get('kind') if hasattr(root, 'get') else root['kind']
                enabled = root.get('enabled') if hasattr(root, 'get') else root['enabled']
                if kind == 'private':
                    raise SourceError('invalid_root', '私有区不能作为共享采集目标。', rootId=root_id)
                if not enabled:
                    raise SourceError('root_disabled', '目标存储根已停用。', rootId=root_id)
                return root
        rows = self._rows('SELECT * FROM storage_roots WHERE id=?', (root_id,))
        if rows:
            root = rows[0]
            if root['kind'] == 'private':
                raise SourceError('invalid_root', '私有区不能作为共享采集目标。', rootId=root_id)
            if not root['enabled']:
                raise SourceError('root_disabled', '目标存储根已停用。', rootId=root_id)
            return root
        if root_id in ('legacy', 'default'):
            return {'id': root_id, 'kind': 'local', 'path': str(self.app.config.data_dir),
                    'enabled': 1}
        raise SourceError('unknown_root', '目标存储根不存在。', rootId=root_id)

    def add_mapping(self, actor: dict, source_id: str, payload: dict) -> dict:
        self.app.require_admin(actor)
        rows = self._rows('SELECT id FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        item = self._mapping_payload(source_id, payload)
        existing = self.mappings(source_id)
        for mapping in existing:
            if MappingRules.overlap({'root_id': mapping['rootId'],
                                     'target_subdir': mapping['targetSubdir']},
                                    {'root_id': item['root_id'],
                                     'target_subdir': item['target_subdir']}):
                raise SourceError('target_overlap', '目标子目录与现有映射重叠。',
                                  target=item['target_subdir'])
        stamp = _now()
        with self.store.transaction() as db:
            db.execute('INSERT INTO directory_mappings(id,source_id,remote_dir,root_id,target_subdir,'
                       'include,exclude,recursive,rule_revision,mode,enabled,created_at,updated_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)',
                       (item['id'], source_id, item['remote_dir'], item['root_id'],
                        item['target_subdir'], json.dumps(item['include'], ensure_ascii=False),
                        json.dumps(item['exclude'], ensure_ascii=False),
                        1 if item['recursive'] else 0, item['rule_revision'], item['mode'],
                        stamp, stamp))
            db.execute('UPDATE remote_sources SET revision=revision+1, updated_at=? WHERE id=?',
                       (stamp, source_id))
            self.store.audit(db, actor['ip'], 'mapping.create', source_id,
                             {'remoteDir': item['remote_dir']})
        return self.get(actor, source_id)

    def delete_mapping(self, actor: dict, source_id: str, mapping_id: str) -> dict:
        self.app.require_admin(actor)
        with self.store.transaction() as db:
            cursor = db.execute('DELETE FROM directory_mappings WHERE source_id=? AND id=?',
                                (source_id, mapping_id))
            if cursor.rowcount == 0:
                raise SourceError('unknown_mapping', '目录映射不存在。')
            db.execute('UPDATE remote_sources SET revision=revision+1, updated_at=? WHERE id=?',
                       (_now(), source_id))
            self.store.audit(db, actor['ip'], 'mapping.delete', source_id, {'mapping': mapping_id})
        return self.get(actor, source_id)

    # ------------------------------------------------------------------- probe
    def probe(self, actor: dict, source_id: str, payload: dict) -> dict:
        self.app.require_admin(actor)
        rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        source = rows[0]
        revision = int(source['revision'])
        fingerprint = self.fingerprint(source_id)
        adapter = self._adapter(source)
        try:
            result = adapter.probe(config_revision=revision, fingerprint=fingerprint,
                                   guard=Guard(self._admission(source)))
        except (CredentialError, SecurityError) as exc:
            raise SourceError(getattr(exc, 'code', 'probe_failed'),
                              getattr(exc, 'message', str(exc)))
        finally:
            adapter.close()
        payload_out = result.to_dict()
        with self.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO source_probes(source_id,revision,fingerprint,ok,'
                       'result,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                       (source_id, revision, fingerprint, 1 if result.ok else 0,
                        json.dumps(payload_out, ensure_ascii=False), _now(), result.expires_at))
            db.execute('UPDATE remote_sources SET capabilities=?, last_probe=? WHERE id=?',
                       (json.dumps(result.capabilities.to_dict(), ensure_ascii=False),
                        _now(), source_id))
            self.store.audit(db, actor['ip'], 'source.probe', source_id,
                             {'ok': result.ok, 'revision': revision,
                              'fingerprint': fingerprint})
        return {'probe': payload_out, 'fingerprint': fingerprint, 'revision': revision}

    def _adapter(self, source: dict):
        policy = _loads(source['policy'], {})
        credential_kind = None
        if source['credential_ref']:
            rows = self._rows('SELECT kind FROM collector_credentials WHERE id=?',
                              (source['credential_ref'],))
            credential_kind = rows[0]['kind'] if rows else None
        config = {'id': source['id'], 'adapter': source['adapter'],
                  'endpoint': source['endpoint'], 'credential_ref': source['credential_ref'],
                  'credential_kind': credential_kind, 'host_pin': source['host_pin'],
                  'path': policy.get('directory') or '/',
                  'policy': dict(policy, sessionDir=str(self.session_dir),
                                 tools=policy.get('tools') or {})}
        return adapter_base.build(config, credentials=self.credentials,
                                  guard=Guard(self._admission(source)), limits=policy.get('limits'))

    @staticmethod
    def _admission(source: dict) -> Admission:
        policy = _loads(source['policy'], {})
        try:
            endpoint = Endpoint.parse(source['endpoint'],
                                      default_port=adapter_base.adapter_class(
                                          source['adapter']).default_port
                                      if adapter_base.adapter_class(source['adapter']) else 22)
        except SecurityError:
            endpoint = None
        admission = Admission.from_policy(policy)
        if endpoint and not admission.allowed_hosts:
            # A source is admitted for its own host by default; the operator still
            # decides whether loopback is acceptable.
            admission.allowed_hosts = [endpoint.host.lower()]
        if endpoint and not admission.allowed_ports:
            admission.allowed_ports = [endpoint.port]
        return admission

    # ----------------------------------------------------------------- actions
    def action(self, actor: dict, source_id: str, action: str, payload: dict) -> dict:
        self.app.require_admin(actor)
        action = str(action or '').strip()
        if action not in ACTIONS:
            raise SourceError('unknown_action', '不支持的源动作：' + action)
        action_id = str(payload.get('actionId') or '').strip()
        if not action_id or len(action_id) > 128:
            raise SourceError('missing_action_id', '源动作必须携带 actionId 以便幂等。')
        cached = self._cached_action(action_id)
        if cached is not None:
            return cached
        rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        if not rows:
            raise SourceError('unknown_source', '远程源不存在。')
        source = rows[0]
        expected = payload.get('expectedRevision')
        if expected is not None and int(expected) != source['revision']:
            raise SourceError('revision_conflict', '源配置已变化，请刷新后重试。',
                              currentRevision=source['revision'])
        result = self._apply_action(source, action)
        with self.store.transaction() as db:
            db.execute('INSERT INTO source_actions(action_id,source_id,action,result,created_at) '
                       'VALUES(?,?,?,?,?)', (action_id, source_id, action,
                                             json.dumps(result, ensure_ascii=False), _now()))
            db.execute('UPDATE remote_sources SET revision=revision+1, updated_at=? WHERE id=?',
                       (_now(), source_id))
            self.store.audit(db, actor['ip'], 'source.action', source_id,
                             {'action': action, 'actionId': action_id})
        return result

    def _policy_with(self, source: dict, **changes) -> str:
        """Merge pause-ownership markers into the stored policy, keeping other keys."""
        rows = self._rows('SELECT policy FROM remote_sources WHERE id=?', (source['id'],))
        try:
            policy = json.loads(rows[0]['policy'] or '{}') if rows else {}
        except (TypeError, ValueError):
            policy = {}
        for key, value in changes.items():
            if value is None:
                policy.pop(key, None)
            else:
                policy[key] = value
        if changes.get('pausedBy') != 'storage':
            # An explicit pause/resume from an operator overrides any storage wait, so the
            # stale marker must not survive to confuse a later recovery.
            policy.pop('waitingStorage', None)
            policy.pop('waitingReason', None)
        return json.dumps(policy, ensure_ascii=False)

    def _apply_action(self, source: dict, action: str) -> dict:
        source_id = source['id']
        if action in ('pause', 'resume', 'scan', 'retry') and not source['enabled']:
            # Pausing a source that never started is not a state change; saying so is
            # better than recording a pause that means nothing.
            raise SourceError('source_disabled', '请先启用远程源。')
        if action == 'enable':
            probe = self.probe_row(source_id)
            if not probe or not probe['ok']:
                raise SourceError('probe_required', '启用前必须完成一次成功的预检。')
            if probe['expired']:
                raise SourceError('probe_expired', '预检已过期，请重新预检。')
            if not probe['matches']:
                raise SourceError('probe_stale', '配置已修改，旧预检不再适用。')
            with self.store.transaction() as db:
                db.execute('UPDATE remote_sources SET enabled=1, paused=0, policy=? WHERE id=?',
                           (self._policy_with(source, pausedBy=None), source_id))
            job = self._queue(source, 'scan')
            return {'action': 'enable', 'enabled': True, 'queued': job}
        if action == 'disable':
            with self.store.transaction() as db:
                db.execute('UPDATE remote_sources SET enabled=0, paused=1 WHERE id=?', (source_id,))
            return {'action': 'disable', 'enabled': False, 'archiveRetained': True}
        if action == 'pause':
            with self.store.transaction() as db:
                # `pausedBy` records who owns the pause: a storage-pressure pause is
                # cleared automatically when the disk recovers, an operator pause is not.
                db.execute('UPDATE remote_sources SET paused=1, policy=? WHERE id=?',
                           (self._policy_with(source, pausedBy='operator'), source_id))
            return {'action': 'pause', 'paused': True, 'archiveRetained': True}
        if action == 'resume':
            with self.store.transaction() as db:
                db.execute('UPDATE remote_sources SET paused=0, policy=? WHERE id=?',
                           (self._policy_with(source, pausedBy=None), source_id))
            job = self._queue(source, 'resume')
            return {'action': 'resume', 'paused': False, 'queued': job}
        job = self._queue(source, action)
        return {'action': action, 'queued': job}

    def _queue(self, source: dict, action: str) -> dict:
        """Queue a collect job. Never claims the job started."""
        repo = getattr(self.app, 'collect_jobs', None)
        if repo is None:
            from ..jobs.repository import JobRepository
            repo = JobRepository(self.store)
        job = repo.create('collect', 'system:collector',
                          {'source_id': source['id'], 'action': action},
                          action_key='collect:%s:%s:%d' % (source['id'], action,
                                                           int(time.time() // 60)))
        return {'jobId': job.id, 'state': job.state}

    def _cached_action(self, action_id: str) -> Optional[dict]:
        rows = self._rows('SELECT result FROM source_actions WHERE action_id=?', (action_id,))
        return _loads(rows[0]['result'], {}) if rows else None

    # ------------------------------------------------------------------- state
    def state(self, source_id: str) -> dict:
        rows = self._rows('SELECT * FROM source_files WHERE source_id=?', (source_id,))
        files = [{'id': row['id'], 'remotePath': row['remote_path'], 'fileId': row['file_id'],
                  'generation': row['generation'], 'size': row['size'],
                  'archivedBytes': row['archived_bytes'],
                  'contiguousBytes': row['contiguous_bytes'],
                  'gapState': row['gap_state'], 'sealed': bool(row['sealed']),
                  'state': row['state'], 'lastSeenAt': row['last_seen_at'],
                  'lastError': _loads(row['last_error']), 'retryAt': row['retry_at']}
                 for row in rows]
        archived = sum(item['archivedBytes'] for item in files)
        contiguous = sum(item['contiguousBytes'] for item in files)
        source_rows = self._rows('SELECT * FROM remote_sources WHERE id=?', (source_id,))
        source = source_rows[0] if source_rows else None
        lag = sum(max(0, item['size'] - item['contiguousBytes']) for item in files)
        return {'sourceId': source_id, 'files': files,
                'archivedBytes': archived, 'contiguousBytes': contiguous,
                'lagBytes': lag,
                'gaps': sum(1 for item in files if item['gapState'] != 'none'),
                'provisional': sum(1 for item in files if not item['sealed']),
                'enabled': bool(source['enabled']) if source else False,
                'paused': bool(source['paused']) if source else False,
                'lastProbe': self.probe_row(source_id) if source else None}

    def intake(self, actor: dict) -> dict:
        """The采集中心 table: per-source real numbers, no timers, no guesses."""
        rows = self._rows('SELECT * FROM remote_sources ORDER BY label')
        items = []
        for row in rows:
            if actor.get('role') != 'admin' and row['acl'] != 'members':
                continue
            state = self.state(row['id'])
            summary = {
                'id': row['id'], 'label': row['label'], 'adapter': row['adapter'],
                'enabled': bool(row['enabled']), 'paused': bool(row['paused']),
                'mappingCount': len(self.mappings(row['id'])),
                'files': len(state['files']),
                'archivedBytes': state['archivedBytes'],
                'contiguousBytes': state['contiguousBytes'],
                'lagBytes': state['lagBytes'],
                'gaps': state['gaps'],
                'provisional': state['provisional'],
                'lastSeenAt': max([item['lastSeenAt'] or '' for item in state['files']] or ['']) or None,
                'lastError': next((item['lastError'] for item in state['files']
                                   if item['lastError']), None),
                'retryAt': min([item['retryAt'] for item in state['files']
                                if item['retryAt']] or [None]) if state['files'] else None,
                'capabilities': _loads(row['capabilities'], {}),
            }
            if actor.get('role') == 'admin':
                summary['endpoint'] = row['endpoint']
                summary['revision'] = row['revision']
            items.append(summary)
        return {'items': items, 'adapters': self.adapters(),
                'generatedAt': _now()}

    # -------------------------------------------------------------- validation
    def _policy(self, value) -> dict:
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise SourceError('invalid_policy', '策略必须是对象。')
        if len(json.dumps(value)) > MAX_POLICY_BYTES:
            raise SourceError('invalid_policy', '策略内容过大。')
        unknown = sorted(set(value) - POLICY_KEYS)
        if unknown:
            raise SourceError('invalid_policy', '策略包含未知字段。', fields=unknown[:8])
        policy = dict(value)
        for key in ('allowedPaths',):
            if key in policy:
                policy[key] = [str(item)[:2048] for item in (policy[key] or [])][:16]
        for key in ('tools',):
            if key in policy and not isinstance(policy[key], dict):
                raise SourceError('invalid_policy', 'tools 必须是对象。')
        return policy

    def _credential(self, value) -> Optional[str]:
        if value in (None, ''):
            return None
        text = str(value)
        state = self.credentials.metadata(text)
        if state['state'] != 'ready':
            raise SourceError('credential_unusable', '凭据不可用，请重新录入。',
                              credentialRef=text, code=state.get('code'))
        return text
