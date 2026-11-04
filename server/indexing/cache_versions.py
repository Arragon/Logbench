"""Layered cache versions with staging and a single publication pointer (R6-038).

The problem this solves: a log view has five derivations — the raw bytes, the
framed structure, the rule results, the search projection and cached query
results — and they change for different reasons. Treating them as one blob means
recolouring a rule rebuilds an index, and re-opening a file re-parses it. So each
layer gets its own version with its own signature, its own staging object and its
own publication pointer.

Rules that are enforced here rather than trusted to callers:

* **a signature is declared, not inferred.** `deps_for()` refuses a layer whose
  required dependencies are missing, and refuses file `mtime`/`size` as an
  identity key. Those two shortcuts are exactly how a cache starts lying: mtime
  changes when a file is touched and not when it changes, and a growing file has
  a stale tail.
* **style is not semantics.** The rule signatures come from
  `src/shared/rules.js` (`semanticSignature`), which hashes conditions and
  extractors but not colours, shapes or names. One implementation, both sides.
* **a version is invisible until it is complete.** `ensure()` creates a staging
  row; `publish()` re-checks coverage and digest, then flips one pointer row in
  one transaction. A failed or cancelled rebuild leaves the previous pointer
  untouched, so readers keep the old version.
* **old versions stay readable while pinned.** A reader pins the version it is
  paging through; publishing a newer version does not invalidate the pin, and GC
  will not reclaim a pinned version.
* **one principal cannot see another's cache.** Versions are keyed by owner and
  a read by a different principal returns the same `not_found` as a file that
  does not exist — no "forbidden", because that would leak existence, and no
  counts, because counts are also existence.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Iterable, Optional

LAYERS = ('raw', 'structure', 'rules', 'search', 'query')

#: `raw` is a reference to authorized source bytes. It is never rebuilt and never
#: collected: deleting it would make an index unrecoverable rather than merely
#: stale.
REBUILDABLE = ('structure', 'rules', 'search', 'query')

#: Identities that are not identities. A file can be touched without changing,
#: and can grow without its prefix changing.
FORBIDDEN_KEYS = ('mtime', 'mtime_ns', 'ctime', 'size', 'growth', 'length')

REQUIRED = {
    'raw': ('content_sig',),
    'structure': ('content_sig', 'generation', 'framing', 'runtime', 'schema', 'encoding'),
    'rules': ('structure_signature', 'rule_structure', 'rule_extract', 'engine'),
    'search': ('rules_signature', 'projection', 'gram_size'),
    'query': ('search_signature', 'query_hash', 'sort', 'generation'),
}

TERMINAL = ('complete', 'superseded', 'stale', 'failed', 'cancelled', 'revoked')

#: `rule_structure` / `rule_extract` are the `signatures.structure` and
#: `signatures.extract` buckets from src/shared/rules.js: conditions/anomalies/
#: nodes, and extractors. Colours, shapes and names are outside both, which is why
#: recolouring a rule rebuilds nothing.


class CacheError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def canonical(deps: dict) -> str:
    return json.dumps(deps, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def signature(layer: str, deps: dict) -> str:
    digest = hashlib.sha256()
    digest.update(('logbench.layer/1|%s|' % layer).encode('utf-8'))
    digest.update(canonical(deps).encode('utf-8'))
    return 'sig1-' + digest.hexdigest()[:32]


def deps_for(layer: str, **deps) -> dict:
    """Validate and normalise a layer's dependency record.

    Missing dependencies are a bug, not a default: a structure version that does
    not name its framing/parser/runtime/schema/encoding cannot be invalidated
    correctly when one of them changes.
    """
    if layer not in LAYERS:
        raise CacheError('unknown_layer', '未知缓存层：%s' % layer, layer=layer)
    wanted = REQUIRED[layer]
    given = {key: value for key, value in deps.items() if value is not None}
    missing = [key for key in wanted if key not in given]
    if missing:
        raise CacheError('missing_dependency', '缓存层 %s 缺少依赖：%s' % (layer, ', '.join(missing)),
                         layer=layer, missing=missing)
    bad = [key for key in given if key in FORBIDDEN_KEYS]
    if bad:
        raise CacheError('unstable_dependency',
                         '缓存签名不得使用 %s 这类易变文件属性作为身份。' % ', '.join(bad),
                         layer=layer, forbidden=bad)
    return {key: given[key] for key in sorted(given)}


def rows_digest(rows: Iterable[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical(row).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()


class CacheRegistry:
    """Control-plane bookkeeping for layered cache versions."""

    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        self.counters = {'ensure': 0, 'reused': 0, 'created': 0, 'published': 0,
                         'failed': 0, 'collected': 0, 'reads': 0, 'leaks_blocked': 0}

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        from ..app import now as stamp
        return stamp()

    def _event(self, db, version_id: str, event: str, detail: str = ''):
        db.execute('INSERT INTO layer_events(version_id,event,detail,created_at) VALUES(?,?,?,?)',
                   (version_id, event, detail, self._now()))

    def _row(self, version_id: str) -> Optional[dict]:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM layer_versions WHERE version_id=?', (version_id,)).fetchone()
        return _decode(row) if row else None

    # -------------------------------------------------------------------- ensure
    def ensure(self, file_id: str, layer: str, *, owner: str, generation: int,
               deps: dict, scope: str = 'shared') -> dict:
        """Return a version for (file, generation, layer, scope, signature).

        `reused` is the whole point: the second open of the same file finds a
        complete version and does not parse anything.
        """
        if layer not in LAYERS:
            raise CacheError('unknown_layer', '未知缓存层：%s' % layer, layer=layer)
        normalised = deps_for(layer, **deps)
        sig = signature(layer, normalised)
        self.counters['ensure'] += 1
        with self.store.transaction() as db:
            row = db.execute(
                'SELECT * FROM layer_versions WHERE file_id=? AND generation=? AND layer=? '
                'AND scope=? AND signature=? AND owner=?',
                (file_id, generation, layer, scope, sig, owner)).fetchone()
            if row and row['state'] == 'complete':
                self.counters['reused'] += 1
                pointer = db.execute(
                    'SELECT version_id FROM layer_pointers WHERE file_id=? AND layer=? AND scope=?',
                    (file_id, layer, scope)).fetchone()
                return {'version_id': row['version_id'], 'signature': sig, 'reused': True,
                        'published': bool(pointer and pointer['version_id'] == row['version_id']),
                        'layer': layer, 'generation': generation, 'state': row['state'],
                        'rows': row['rows'], 'stats': json.loads(row['stats'])}
            if row:
                # A previous attempt at this exact signature: retry it rather than
                # minting a parallel version for the same content.
                db.execute("UPDATE layer_versions SET state='building' WHERE version_id=?",
                           (row['version_id'],))
                self._event(db, row['version_id'], 'retry')
                return {'version_id': row['version_id'], 'signature': sig, 'reused': False,
                        'published': False, 'layer': layer, 'generation': generation,
                        'state': 'building', 'rows': 0, 'stats': {}}
            version_id = 'ver-' + secrets.token_hex(8)
            pointer = db.execute(
                'SELECT version_id FROM layer_pointers WHERE file_id=? AND layer=? AND scope=?',
                (file_id, layer, scope)).fetchone()
            db.execute(
                'INSERT INTO layer_versions(version_id,file_id,generation,layer,scope,owner,signature,'
                'deps,state,rows,digest,bytes,stats,supersedes,created_at) '
                "VALUES(?,?,?,?,?,?,?,?,?,0,NULL,0,'{}',?,?)",
                (version_id, file_id, generation, layer, scope, owner, sig, canonical(normalised),
                 'building', pointer['version_id'] if pointer else None, self._now()))
            self._event(db, version_id, 'staged', sig)
            self.counters['created'] += 1
            return {'version_id': version_id, 'signature': sig, 'reused': False, 'published': False,
                    'layer': layer, 'generation': generation, 'state': 'building',
                    'rows': 0, 'stats': {}}

    # ------------------------------------------------------------------- publish
    def publish(self, version_id: str, *, rows: int, digest: Optional[str],
                stats: Optional[dict] = None, expected_rows: Optional[int] = None) -> dict:
        """Verify the staged layer, then move the pointer — one transaction.

        Nothing here reads the source file: a layer that is already complete is
        never rebuilt, and a layer that fails verification never becomes visible.
        """
        if not digest or len(digest) != 64:
            self.fail(version_id, 'missing_digest', '发布前必须提供内容摘要。')
            raise CacheError('missing_digest', '发布前必须提供内容摘要。', version=version_id)
        if expected_rows is not None and int(rows) != int(expected_rows):
            self.fail(version_id, 'coverage_mismatch',
                      '覆盖不完整：期望 %s 行，实际 %s 行。' % (expected_rows, rows))
            raise CacheError('coverage_mismatch', '覆盖不完整，未发布。',
                             expected=expected_rows, actual=rows, version=version_id)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM layer_versions WHERE version_id=?', (version_id,)).fetchone()
            if not row:
                raise CacheError('unknown_version', '缓存版本不存在。', version=version_id)
            if row['state'] == 'complete' and row['digest'] == digest:
                return _decode(row)
            if row['state'] not in ('building', 'failed'):
                raise CacheError('not_staged', '只有构建中的版本可以发布。', state=row['state'])
            previous = db.execute(
                'SELECT version_id FROM layer_pointers WHERE file_id=? AND layer=? AND scope=?',
                (row['file_id'], row['layer'], row['scope'])).fetchone()
            payload = json.dumps(stats or {}, sort_keys=True)
            db.execute(
                "UPDATE layer_versions SET state='complete',rows=?,digest=?,bytes=?,stats=?,"
                "published_at=? WHERE version_id=?",
                (int(rows), digest, int((stats or {}).get('bytes', 0)), payload, self._now(), version_id))
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 AS s FROM layer_pointers').fetchone()['s']
            db.execute(
                'INSERT INTO layer_pointers(file_id,layer,scope,version_id,seq,updated_at) '
                'VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,layer,scope) DO UPDATE SET '
                'version_id=excluded.version_id,seq=excluded.seq,updated_at=excluded.updated_at',
                (row['file_id'], row['layer'], row['scope'], version_id, seq, self._now()))
            if previous and previous['version_id'] != version_id:
                # Superseded, not deleted: a pinned reader continues on it.
                db.execute("UPDATE layer_versions SET state='superseded' WHERE version_id=? "
                           "AND state='complete'", (previous['version_id'],))
            db.execute(
                'INSERT INTO cache_versions(file_id,generation,layer,signature,state,bytes,created_at) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_id,generation,layer) DO UPDATE SET '
                'signature=excluded.signature,state=excluded.state,bytes=excluded.bytes',
                (row['file_id'], row['generation'], row['layer'], row['signature'], 'complete',
                 int((stats or {}).get('bytes', 0)), self._now()))
            self._event(db, version_id, 'published', 'rows=%d seq=%d' % (int(rows), seq))
            self.counters['published'] += 1
            published = dict(row)
        published.update({'state': 'complete', 'rows': int(rows), 'digest': digest,
                          'stats': stats or {}, 'seq': seq})
        return published

    def fail(self, version_id: str, code: str, message: str) -> dict:
        with self.store.transaction() as db:
            row = db.execute('SELECT state FROM layer_versions WHERE version_id=?', (version_id,)).fetchone()
            if not row:
                raise CacheError('unknown_version', '缓存版本不存在。', version=version_id)
            db.execute("UPDATE layer_versions SET state='failed',stats=? WHERE version_id=?",
                       (json.dumps({'error': code}, ensure_ascii=False), version_id))
            self._event(db, version_id, 'failed', '%s: %s' % (code, message))
            self.counters['failed'] += 1
        return {'version': version_id, 'state': 'failed', 'code': code}

    def cancel(self, version_id: str) -> dict:
        with self.store.transaction() as db:
            db.execute("UPDATE layer_versions SET state='cancelled' WHERE version_id=? "
                       "AND state='building'", (version_id,))
            self._event(db, version_id, 'cancelled')
        return {'version': version_id, 'state': 'cancelled'}

    # --------------------------------------------------------------------- read
    def pointer(self, file_id: str, layer: str, *, scope: str = 'shared') -> Optional[dict]:
        with self.store.connect() as db:
            row = db.execute(
                'SELECT v.* FROM layer_pointers p JOIN layer_versions v ON v.version_id=p.version_id '
                'WHERE p.file_id=? AND p.layer=? AND p.scope=?', (file_id, layer, scope)).fetchone()
        return _decode(row) if row else None

    def read(self, file_id: str, layer: str, *, owner: str, scope: str = 'shared',
             version_id: Optional[str] = None) -> dict:
        """Resolve the version a read should use, or raise `not_found`.

        `not_found` covers all of: no such file, no such layer, and a version
        owned by someone else. Distinguishing them would leak existence.
        """
        self.counters['reads'] += 1
        with self.store.connect() as db:
            if version_id:
                row = db.execute('SELECT * FROM layer_versions WHERE version_id=?',
                                 (version_id,)).fetchone()
            else:
                row = db.execute(
                    'SELECT v.* FROM layer_pointers p JOIN layer_versions v ON v.version_id=p.version_id '
                    'WHERE p.file_id=? AND p.layer=? AND p.scope=?',
                    (file_id, layer, scope)).fetchone()
        if not row or row['state'] not in ('complete', 'superseded', 'stale'):
            raise CacheError('not_found', '没有可用的缓存版本。', file_id=file_id, layer=layer)
        version = _decode(row)
        if version['owner'] != owner:
            self.counters['leaks_blocked'] += 1
            raise CacheError('not_found', '没有可用的缓存版本。', file_id=file_id, layer=layer)
        if version['state'] == 'stale' and not self._pinned(version['version_id']):
            # A stale version is only readable while someone is still paging it.
            raise CacheError('stale_version', '该版本已失效，请重新打开。',
                             version=version['version_id'])
        return version

    # ---------------------------------------------------------------------- pins
    def pin(self, version_id: str, *, owner: str, token: Optional[str] = None,
            ttl: float = 900.0) -> dict:
        token = token or secrets.token_hex(8)
        row = self._row(version_id)
        if not row:
            raise CacheError('not_found', '没有可用的缓存版本。')
        if row['owner'] != owner:
            raise CacheError('not_found', '没有可用的缓存版本。')
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO layer_pins(version_id,token,owner,created_at,expires_at) VALUES(?,?,?,?,?) '
                'ON CONFLICT(version_id,token) DO UPDATE SET expires_at=excluded.expires_at',
                (version_id, token, owner, self._now(), self.clock() + ttl))
        return {'version': version_id, 'token': token, 'expires_at': self.clock() + ttl}

    def unpin(self, version_id: str, token: str) -> dict:
        with self.store.transaction() as db:
            cursor = db.execute('DELETE FROM layer_pins WHERE version_id=? AND token=?',
                                (version_id, token))
        return {'version': version_id, 'released': cursor.rowcount > 0}

    def _pinned(self, version_id: str) -> bool:
        with self.store.connect() as db:
            row = db.execute('SELECT COUNT(*) AS n FROM layer_pins WHERE version_id=? AND expires_at>?',
                             (version_id, self.clock())).fetchone()
        return bool(row['n'])

    def revoke(self, owner: str) -> dict:
        """Terminate every read this principal could still perform.

        Pins are dropped and the owner's versions become `revoked`, which the read
        path treats as absent. A later re-authorization stages a new version; it
        does not resurrect the old one, because the old one may already have been
        read under permissions that no longer hold.
        """
        with self.store.transaction() as db:
            pins = db.execute('DELETE FROM layer_pins WHERE owner=?', (owner,)).rowcount
            versions = db.execute('SELECT version_id FROM layer_versions WHERE owner=?',
                                  (owner,)).fetchall()
            for row in versions:
                db.execute("UPDATE layer_versions SET state='revoked' WHERE version_id=? "
                           "AND state<>'revoked'", (row['version_id'],))
                self._event(db, row['version_id'], 'revoked', owner)
            db.execute('DELETE FROM layer_pointers WHERE version_id IN '
                       '(SELECT version_id FROM layer_versions WHERE owner=? AND state=? )',
                       (owner, 'revoked'))
        return {'owner': owner, 'pins': pins, 'versions': len(versions)}

    def invalidate(self, file_id: str, layer: str, *, scope: str = 'shared',
                   reason: str = '') -> dict:
        """Mark a published version stale and drop the pointer.

        The shard rows stay: a stale version is still readable by a pinned
        cursor, which is what "editing a rule must not blank the reader" means.
        """
        with self.store.transaction() as db:
            pointer = db.execute(
                'SELECT version_id FROM layer_pointers WHERE file_id=? AND layer=? AND scope=?',
                (file_id, layer, scope)).fetchone()
            if not pointer:
                return {'invalidated': False, 'file_id': file_id, 'layer': layer}
            db.execute("UPDATE layer_versions SET state='stale' WHERE version_id=? "
                       "AND state='complete'", (pointer['version_id'],))
            db.execute('DELETE FROM layer_pointers WHERE file_id=? AND layer=? AND scope=?',
                       (file_id, layer, scope))
            self._event(db, pointer['version_id'], 'invalidated', reason)
        return {'invalidated': True, 'version': pointer['version_id'], 'reason': reason}

    # ------------------------------------------------------------------------ gc
    def gc(self, *, now: Optional[float] = None) -> dict:
        """Collect unpinned, unreferenced, rebuildable versions.

        `raw` is not in the candidate set and checkpoints are not in this table at
        all: neither is regenerable, so neither is collectable.
        """
        now = self.clock() if now is None else now
        collected, kept = [], []
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM layer_versions').fetchall()
            pointed = {row['version_id'] for row in db.execute('SELECT version_id FROM layer_pointers')}
            pinned = {row['version_id'] for row in db.execute(
                'SELECT DISTINCT version_id FROM layer_pins WHERE expires_at>?', (now,))}
            for row in rows:
                version_id = row['version_id']
                if row['layer'] not in REBUILDABLE:
                    kept.append({'version': version_id, 'reason': 'raw'})
                    continue
                if version_id in pointed:
                    kept.append({'version': version_id, 'reason': 'published'})
                    continue
                if version_id in pinned:
                    kept.append({'version': version_id, 'reason': 'pinned'})
                    continue
                if row['state'] == 'building':
                    kept.append({'version': version_id, 'reason': 'building'})
                    continue
                db.execute('DELETE FROM layer_events WHERE version_id=?', (version_id,))
                db.execute('DELETE FROM layer_versions WHERE version_id=?', (version_id,))
                collected.append(version_id)
            self.counters['collected'] += len(collected)
        return {'collected': collected, 'kept': kept}

    # ------------------------------------------------------------------- views
    def versions(self, *, file_id: Optional[str] = None, layer: Optional[str] = None,
                 owner: Optional[str] = None) -> list:
        sql = 'SELECT * FROM layer_versions'
        clauses, params = [], []
        for column, value in (('file_id', file_id), ('layer', layer), ('owner', owner)):
            if value is not None:
                clauses.append('%s=?' % column)
                params.append(value)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at, version_id'
        with self.store.connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()
        return [_decode(row) for row in rows]

    def events(self, version_id: str) -> list:
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM layer_events WHERE version_id=? ORDER BY id',
                              (version_id,)).fetchall()
        return [{'event': row['event'], 'detail': row['detail'], 'at': row['created_at']}
                for row in rows]

    def metrics(self) -> dict:
        """Counters plus per-layer totals, so "no re-parse" is a number, not a claim."""
        with self.store.connect() as db:
            rows = db.execute('SELECT layer,state,COUNT(*) AS n FROM layer_versions '
                              'GROUP BY layer,state').fetchall()
        layers: dict = {}
        for row in rows:
            bucket = layers.setdefault(row['layer'], {'versions': 0, 'states': {}})
            bucket['versions'] += row['n']
            bucket['states'][row['state']] = row['n']
        return {'counters': dict(self.counters), 'layers': layers}


def _decode(row) -> dict:
    return {
        'version_id': row['version_id'],
        'file_id': row['file_id'],
        'generation': row['generation'],
        'layer': row['layer'],
        'scope': row['scope'],
        'owner': row['owner'],
        'signature': row['signature'],
        'deps': json.loads(row['deps']),
        'state': row['state'],
        'rows': row['rows'],
        'digest': row['digest'],
        'bytes': row['bytes'],
        'stats': json.loads(row['stats'] or '{}'),
        'supersedes': row['supersedes'],
        'created_at': row['created_at'],
        'published_at': row['published_at'],
    }
