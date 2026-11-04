"""HTTP surface for collection configuration (R6-066).

Every route here is administrator-only except the two read paths a member needs
(source summary and intake status), and those return a reduced shape rather than
the administrator object with fields removed by the UI.

The handler never accepts a filesystem path, a command or a secret from the wire:
`credentialRef` is an opaque id, `endpoint` is parsed and admitted, and a mapping's
target is a root id plus a relative subdirectory that the server resolves.
"""
from __future__ import annotations

import re

from .credentials import CredentialError
from .sources import SourceError, SourceService

STATUS = {
    'unknown_source': 404, 'unknown_mapping': 404,
    'revision_conflict': 409, 'duplicate_source': 409, 'target_overlap': 409,
    'probe_required': 409, 'probe_stale': 409, 'probe_expired': 409,
    'source_disabled': 409, 'root_disabled': 409, 'credential_in_use': 409,
    'unsupported': 400, 'invalid_source': 400, 'invalid_endpoint': 400,
    'invalid_policy': 400, 'invalid_mapping': 400, 'invalid_root': 400,
    'unknown_root': 400, 'unknown_action': 400, 'missing_action_id': 400,
    'credential_unusable': 400, 'credentials_in_url': 400,
}


class CollectorApi:
    def __init__(self, app):
        self.app = app
        self.service = SourceService(app)

    def route(self, method: str, path: str, q, payload: dict, actor: dict, headers: dict):
        from ..app import fail, integer, text
        try:
            return self._route(method, path, q, payload, actor, fail, integer, text)
        except SourceError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)
        except CredentialError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)

    # ------------------------------------------------------------------ routes
    def _route(self, method, path, q, payload, actor, fail, integer, text):
        if path == '/api/v2/collectors/adapters' and method in ('GET', 'HEAD'):
            self.app.require_admin(actor)
            return {'items': self.service.adapters()}
        if path == '/api/v2/credentials':
            self.app.require_admin(actor)
            if method in ('GET', 'HEAD'):
                return {'items': self._credentials()}
            if method == 'POST':
                return {'item': self._create_credential(actor, payload, fail, text)}
        match = re.fullmatch(r'/api/v2/credentials/(cred_[0-9a-f]{32})', path)
        if match and method == 'DELETE':
            self.app.require_admin(actor)
            return self._delete_credential(actor, match.group(1), fail)
        if path == '/api/v2/sources':
            if method in ('GET', 'HEAD'):
                return {'items': self.service.list(actor, limit=integer(q('limit', '100'), '数量', 1, 200))}
            if method == 'POST':
                return {'item': self.service.create(actor, payload)}
        if path == '/api/v2/intake' and method in ('GET', 'HEAD'):
            return self.service.intake(actor)
        match = re.fullmatch(r'/api/v2/sources/(src_[0-9a-f]{12})(?:/(probe|actions|mappings|state))?'
                             r'(?:/(map_[0-9a-f]{16}))?', path)
        if match:
            source_id, action, mapping_id = match.groups()
            if action is None:
                if method in ('GET', 'HEAD'):
                    return {'item': self.service.get(actor, source_id)}
                if method == 'PATCH':
                    return {'item': self.service.update(actor, source_id, payload)}
                if method == 'DELETE':
                    return self.service.delete(actor, source_id, payload)
            if action == 'probe' and method == 'POST':
                return self.service.probe(actor, source_id, payload)
            if action == 'actions' and method == 'POST':
                return {'result': self.service.action(actor, source_id,
                                                      payload.get('action'), payload)}
            if action == 'state' and method in ('GET', 'HEAD'):
                return self.service.state(source_id) if actor.get('role') == 'admin' else \
                    {'sourceId': source_id, 'archivedBytes': 0, 'note': '仅管理员可见详细状态。'}
            if action == 'mappings':
                if method in ('GET', 'HEAD'):
                    return {'items': self.service.mappings(source_id)}
                if method == 'POST':
                    return {'item': self.service.add_mapping(actor, source_id, payload)}
                if method == 'DELETE' and mapping_id:
                    return {'item': self.service.delete_mapping(actor, source_id, mapping_id)}
        fail(404, 'NOT_FOUND', '接口或资源不存在。')

    # ------------------------------------------------------------- credentials
    def _credentials(self) -> list:
        with self.app.store.connect() as db:
            rows = db.execute('SELECT id,kind,label,fingerprint,created_at,updated_at '
                              'FROM collector_credentials ORDER BY created_at DESC').fetchall()
        items = []
        for row in rows:
            items.append({'ref': row['id'], 'kind': row['kind'], 'label': row['label'],
                          'fingerprint': row['fingerprint'], 'createdAt': row['created_at'],
                          'state': self.service.credentials.metadata(row['id'])['state']})
        return items

    def _create_credential(self, actor, payload, fail, text):
        kind = str(payload.get('kind') or 'password')
        label = text(payload.get('label'), '备注', 120, empty=True)
        value = payload.get('value')
        if not isinstance(value, str):
            fail(400, 'invalid_secret', '凭据内容必须是字符串。')
        meta = self.service.credentials.create(kind=kind, label=label, value=value,
                                              actor=actor['ip'])
        stamp = meta.pop('actor', None)
        with self.app.store.transaction() as db:
            db.execute('INSERT INTO collector_credentials(id,kind,label,secret_path,fingerprint,'
                       'revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)',
                       (meta['id'], meta['kind'], meta['label'], 'credentials/' + meta['id'][5:],
                        meta['fingerprint'], actor['ip'], _now(), _now()))
            self.app.store.audit(db, actor['ip'], 'credential.create', meta['id'],
                                 {'kind': kind})
        return {'ref': meta['id'], 'kind': meta['kind'], 'label': meta['label'],
                'fingerprint': meta['fingerprint'], 'state': 'ready'}

    def _delete_credential(self, actor, ref, fail):
        with self.app.store.connect() as db:
            used = db.execute('SELECT id FROM remote_sources WHERE credential_ref=? LIMIT 1',
                              (ref,)).fetchone()
            row = db.execute('SELECT id FROM collector_credentials WHERE id=?', (ref,)).fetchone()
        if not row:
            fail(404, 'unknown_credential', '凭据不存在。')
        if used:
            fail(409, 'credential_in_use', '该凭据仍被远程源引用，请先更换凭据。',
                 sourceId=used['id'])
        self.service.credentials.delete(ref)
        with self.app.store.transaction() as db:
            db.execute('DELETE FROM collector_credentials WHERE id=?', (ref,))
            self.app.store.audit(db, actor['ip'], 'credential.delete', ref)
        return {'deleted': ref}


def _now() -> str:
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
