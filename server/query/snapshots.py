"""Query snapshots and signed cursors (R6-042).

A snapshot freezes *what the answer is about*: file, generation, index version,
rule version and the query signature. A cursor freezes *where the reader is* in
that answer. Both are signed, because the alternative — trusting the client's
`offset` — is how a page request turns into a scan of another file.

Design points that are easy to get wrong:

* **a cursor is not an offset.** It names the snapshot, and the position within it.
  Re-running the query to reach page 900 is not allowed; the executor resumes from
  the stored position.
* **the signature covers the principal and the ACL version.** A copied cursor used
  by another identity, or after permissions changed, must fail rather than serve
  rows the caller can no longer see.
* **`cursor_expired` is distinct from `unknown_query`.** The UI has to restart from
  the head when a snapshot expired, and must not silently jump back to page one.
* **snapshot creation does not scan.** It validates and records intent; the scan is
  the executor's job, off the request thread.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

DEFAULT_TTL = 900.0


class QueryError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _unb64(text: str) -> bytes:
    padding = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class SnapshotStore:
    """Persistent snapshots plus their result positions."""

    def __init__(self, store, secret: bytes, *, clock=time.time):
        self.store = store
        self.secret = secret
        self.clock = clock

    # ------------------------------------------------------------------ signing
    def _sign(self, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return _b64(body) + '.' + _b64(hmac.new(self.secret, body, hashlib.sha256).digest()[:16])

    def _verify(self, token: str) -> dict:
        if not isinstance(token, str) or token.count('.') != 1:
            raise QueryError('invalid_cursor', '游标格式无效。')
        body, mac = token.split('.', 1)
        try:
            raw = _unb64(body)
            given = _unb64(mac)
        except (ValueError, TypeError):
            raise QueryError('invalid_cursor', '游标格式无效。')
        expected = hmac.new(self.secret, raw, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(given, expected):
            raise QueryError('invalid_cursor', '游标签名不匹配。')
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError:
            raise QueryError('invalid_cursor', '游标内容不可解析。')

    # ------------------------------------------------------------------ create
    def create(self, *, principal: str, file_id: str, generation: int, signature: str,
               spec: dict, plan: dict, versions: dict, acl_version: int,
               ttl: float = DEFAULT_TTL) -> dict:
        query_id = 'q-' + secrets.token_hex(8)
        now = self.clock()
        payload = {
            'spec': spec,
            'plan': plan,
            'versions': versions,
            'acl_version': int(acl_version),
            'cursor': {'seq': -1, 'ordinal': -1, 'scanned': 0},
            'count': {'matched': 0, 'returned': 0, 'relation': 'unknown'},
            'coverage': {'scanned_bytes': '0', 'total_bytes': '0', 'complete': False},
            'resume_token': None,
            'error': None,
            'started_at': now,
            'elapsed_ms': 0,
            'budget_ms': int(spec.get('budget_ms') or 0),
        }
        with self.store.transaction() as db:
            db.execute('INSERT INTO query_snapshots(id,principal,file_id,generation,signature,state,'
                       'payload,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (query_id, principal, file_id, generation, signature, 'queued',
                        json.dumps(payload, ensure_ascii=False), _stamp(), now + ttl))
        return {'query_id': query_id, 'principal': principal, 'file_id': file_id,
                'generation': generation, 'signature': signature, 'state': 'queued',
                'created_at': now, 'expires_at': now + ttl, 'payload': payload}

    # -------------------------------------------------------------------- read
    def get(self, query_id: str, principal: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM query_snapshots WHERE id=?', (query_id,)).fetchone()
        if not row or row['principal'] != principal:
            # Same answer for "does not exist" and "someone else's": existence is
            # not something a query id should leak.
            raise QueryError('unknown_query', '查询不存在或已过期。')
        if row['expires_at'] <= self.clock() and row['state'] not in ('done', 'failed', 'cancelled'):
            raise QueryError('cursor_expired', '查询快照已过期，请重新发起。',
                             query_id=query_id)
        return {**dict(row), 'payload': json.loads(row['payload'])}

    def update(self, query_id: str, *, state: Optional[str] = None, payload: Optional[dict] = None,
               expires_at: Optional[float] = None) -> None:
        sets, params = [], []
        if state is not None:
            sets.append('state=?')
            params.append(state)
        if payload is not None:
            sets.append('payload=?')
            params.append(json.dumps(payload, ensure_ascii=False))
        if expires_at is not None:
            sets.append('expires_at=?')
            params.append(expires_at)
        if not sets:
            return
        params.append(query_id)
        with self.store.transaction() as db:
            db.execute('UPDATE query_snapshots SET %s WHERE id=?' % ','.join(sets), tuple(params))

    # ------------------------------------------------------------------- hits
    def append_hits(self, query_id: str, ordinals: list, *, start_seq: int) -> int:
        if not ordinals:
            return start_seq
        with self.store.transaction() as db:
            db.executemany(
                'INSERT OR REPLACE INTO query_hits(snapshot_id,seq,ordinal) VALUES(?,?,?)',
                [(query_id, start_seq + index, ordinal) for index, ordinal in enumerate(ordinals)])
        return start_seq + len(ordinals)

    def page(self, query_id: str, *, cursor: Optional[str], limit: int, principal: str,
             acl_version: int) -> dict:
        row = self.get(query_id, principal)
        payload = row['payload']
        position = self._position(cursor, row, principal, acl_version)
        with self.store.connect() as db:
            total = db.execute('SELECT COUNT(*) AS n FROM query_hits WHERE snapshot_id=?',
                               (query_id,)).fetchone()['n']
            rows = db.execute(
                'SELECT seq,ordinal FROM query_hits WHERE snapshot_id=? AND seq>=? '
                'ORDER BY seq LIMIT ?', (query_id, position, limit)).fetchall()
            matched = db.execute('SELECT COUNT(*) AS n FROM query_hits WHERE snapshot_id=?',
                                 (query_id,)).fetchone()['n']
        items = [{'seq': item['seq'], 'ordinal': item['ordinal']} for item in rows]
        next_position = items[-1]['seq'] + 1 if items else None
        complete = row['state'] in ('done', 'cancelled')
        relation = ('exact' if complete and row['state'] == 'done'
                    else ('unknown' if row['state'] == 'cancelled' else 'at_least'))
        return {
            'items': items,
            'next_cursor': (self.issue_cursor(row, next_position, principal, acl_version)
                            if next_position is not None and (next_position < total or not complete)
                            else None),
            'count': {'matched': matched if relation != 'unknown' else None,
                      'returned': len(items), 'relation': relation},
            'state': row['state'],
            'coverage': payload.get('coverage') or {},
            'total_hits': total,
        }

    def _position(self, cursor: Optional[str], row: dict, principal: str,
                  acl_version: int) -> int:
        if not cursor:
            return 0
        claims = self._verify(cursor)
        if claims.get('q') != row['id']:
            raise QueryError('invalid_cursor', '游标不属于该查询。')
        if claims.get('p') != principal:
            raise QueryError('invalid_cursor', '游标不属于当前身份。')
        if int(claims.get('a', -1)) != int(row['payload'].get('acl_version', -1)):
            raise QueryError('cursor_expired', '权限已变化，请重新发起查询。')
        if int(claims.get('v', -1)) != int(row['generation']):
            raise QueryError('cursor_expired', '索引代次已变化，请重新发起查询。')
        if claims.get('sig') != row['signature']:
            raise QueryError('cursor_expired', '查询条件已变化，请重新发起查询。')
        return int(claims.get('o', 0))

    def issue_cursor(self, row: dict, position: int, principal: str, acl_version: int) -> str:
        return self._sign({'q': row['id'], 'o': position, 'p': principal,
                           'a': int(acl_version), 'v': int(row['generation']),
                           'sig': row['signature'], 't': int(self.clock())})

    def resume_token(self, row: dict, principal: str, position: int, ordinal: int) -> str:
        return self._sign({'q': row['id'], 'p': principal, 'r': 1, 'o': position,
                           'ord': int(ordinal), 'v': int(row['generation']),
                           't': int(self.clock())})

    def read_resume(self, token: str, row: dict, principal: str) -> dict:
        claims = self._verify(token)
        if claims.get('q') != row['id'] or claims.get('p') != principal or not claims.get('r'):
            raise QueryError('invalid_cursor', '续查凭据不属于该查询。')
        if int(claims.get('v', -1)) != int(row['generation']):
            raise QueryError('cursor_expired', '索引代次已变化，请重新发起查询。')
        return {'position': int(claims.get('o', 0)), 'ordinal': int(claims.get('ord', -1))}


def _stamp() -> str:
    from ..app import now
    return now()
