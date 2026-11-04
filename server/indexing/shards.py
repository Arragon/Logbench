"""Per-generation SQLite index shards (R6-037).

One shard per (file, generation, parser signature). A shard is written by exactly
one writer holding a lease; readers open it read-only and use short cursors so a
long page does not pin the WAL.

Decisions worth stating:

* **journal mode is conservative by default.** SQLite 3.38 (the target host) does
  not have the WAL safety fixes of 3.44, and `capabilities.py` already refuses to
  claim WAL is safe there. Callers may opt in; the default is `DELETE`.
* **the checkpoint commits with the batch.** A batch that writes records but not
  its watermark would re-read data on restart, and a watermark without records
  would skip them. They are one transaction or neither.
* **`ts_ns` is stored as text.** 19 digits do not fit a float; `ts_key` (ms) is
  the sortable column and `ts_ns` is the exact evidence.
* **duplicate batches are idempotent.** Records are keyed by ordinal and written
  with `INSERT OR REPLACE`, so replaying a batch after a crash converges instead
  of duplicating.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    ordinal INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    level TEXT,
    ts TEXT,
    ts_ns TEXT,
    ts_key INTEGER,
    line_first INTEGER,
    line_last INTEGER,
    byte_start TEXT,
    byte_end TEXT,
    has_trace INTEGER NOT NULL DEFAULT 0,
    logger TEXT,
    fn TEXT,
    pid TEXT,
    tid TEXT,
    src TEXT,
    src_line INTEGER,
    message TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '[]',
    fields TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS records_ts ON records(ts_key, ordinal);
CREATE INDEX IF NOT EXISTS records_level ON records(level, ordinal);
CREATE INDEX IF NOT EXISTS records_logger ON records(logger, ordinal);
CREATE INDEX IF NOT EXISTS records_fn ON records(fn, ordinal);
CREATE INDEX IF NOT EXISTS records_line ON records(line_first);
CREATE TABLE IF NOT EXISTS checkpoints (
    stage TEXT PRIMARY KEY,
    watermark TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline_nodes (
    ordinal INTEGER PRIMARY KEY,
    level TEXT,
    kind TEXT,
    label TEXT,
    rule_id TEXT,
    ts_key INTEGER
);
CREATE INDEX IF NOT EXISTS timeline_ts ON timeline_nodes(ts_key, ordinal);
-- Derived layers live in the same shard as the structure they came from, tagged
-- with the version that produced them. That is what lets an old pinned version
-- stay readable after a newer one is published.
CREATE TABLE IF NOT EXISTS layer_versions (
    layer TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    signature TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    digest TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_rows (
    ordinal INTEGER NOT NULL,
    version TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '[]',
    issue TEXT,
    issue_severity TEXT,
    node TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(version, ordinal)
);
CREATE TABLE IF NOT EXISTS rule_timeline (
    version TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    level TEXT,
    kind TEXT,
    label TEXT,
    rule_id TEXT,
    ts_key INTEGER,
    PRIMARY KEY(version, ordinal)
);
CREATE INDEX IF NOT EXISTS rule_timeline_ts ON rule_timeline(version, ts_key, ordinal);
CREATE TABLE IF NOT EXISTS search_postings (
    version TEXT NOT NULL,
    gram TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    field TEXT NOT NULL,
    PRIMARY KEY(version, gram, ordinal, field)
);
CREATE INDEX IF NOT EXISTS search_postings_gram ON search_postings(version, gram);
"""

NETWORK_FS = ('cifs', 'smbfs', 'nfs', 'nfs4', 'afpfs', 'webdav', 'fuse.sshfs')


class ShardError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def assert_local_fs(path: Path) -> None:
    """Index shards must not live on a network filesystem.

    SQLite locking over SMB/NFS is unreliable in exactly the way that corrupts an
    index, and the ticket calls it out. The check is best-effort: on platforms
    where the mount table is unavailable it does not block.
    """
    resolved = str(Path(path).resolve())
    if resolved.startswith('/Volumes/'):
        raise ShardError('network_filesystem', '索引分片不能放在网络挂载卷上。', path=resolved)
    mounts = Path('/proc/mounts')
    if mounts.is_file():
        try:
            for line in mounts.read_text('utf-8', errors='replace').splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fstype = parts[1], parts[2]
                if fstype in NETWORK_FS and (resolved == mount_point or resolved.startswith(mount_point.rstrip('/') + '/')):
                    raise ShardError('network_filesystem', '索引分片不能放在网络挂载点 %s（%s）。'
                                     % (mount_point, fstype), path=resolved)
        except OSError:
            return


def shard_path(root: Path, file_id: str, generation: int, signature: str = 'default') -> Path:
    safe_sig = ''.join(ch for ch in signature if ch.isalnum() or ch in '-_')[:24] or 'default'
    return Path(root) / file_id / ('g%d-%s.sqlite3' % (generation, safe_sig))


@dataclass
class Batch:
    records: list
    watermark: dict


class ShardWriter:
    def __init__(self, path: Path, *, file_id: str, generation: int, signature: str,
                 meta: Optional[dict] = None, allow_wal: bool = False):
        self.path = Path(path)
        self.file_id = file_id
        self.generation = generation
        self.signature = signature
        self.meta = dict(meta or {})
        self.allow_wal = allow_wal
        self.conn: Optional[sqlite3.Connection] = None

    def open(self) -> 'ShardWriter':
        assert_local_fs(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        # Conservative: no WAL unless the caller proved the SQLite build is safe.
        self.conn.execute('PRAGMA journal_mode=%s' % ('WAL' if self.allow_wal else 'DELETE'))
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript(SCHEMA)
        with self.conn:
            for key, value in {'file_id': self.file_id, 'generation': str(self.generation),
                               'signature': self.signature, 'schema': 'r6-shard/1',
                               'created_at': _stamp()}.items():
                self.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, value))
            for key, value in self.meta.items():
                self.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                                  (str(key), json.dumps(value, ensure_ascii=False)))
        return self

    # ------------------------------------------------------------------- writes
    def commit_batch(self, records: Iterable[dict], watermark: dict, stage: str = 'parsed') -> dict:
        """
        One transaction: records + checkpoint. A failure leaves neither, so a
        restart re-reads the same block instead of skipping it.
        """
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        rows = list(records)
        cursor = self.conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        try:
            for record in rows:
                self._insert(cursor, record)
            cursor.execute(
                'INSERT INTO checkpoints(stage,watermark,updated_at) VALUES(?,?,?) '
                'ON CONFLICT(stage) DO UPDATE SET watermark=excluded.watermark,'
                'updated_at=excluded.updated_at',
                (stage, json.dumps(watermark, ensure_ascii=False, sort_keys=True), _stamp()))
            cursor.execute('COMMIT')
        except BaseException:
            cursor.execute('ROLLBACK')
            raise
        return {'written': len(rows), 'stage': stage, 'watermark': watermark}

    def _insert(self, cursor, record: dict):
        ordinal = record.get('ordinal')
        if not isinstance(ordinal, int) or ordinal < 0:
            raise ShardError('bad_record', '记录缺少有效 ordinal。', record=str(record)[:120])
        lines = record.get('lines') or {}
        byte_range = record.get('bytes') or {}
        cursor.execute(
            'INSERT OR REPLACE INTO records(ordinal,record_id,version,level,ts,ts_ns,ts_key,'
            'line_first,line_last,byte_start,byte_end,has_trace,logger,fn,pid,tid,src,src_line,'
            'message,body,fields,payload) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                ordinal,
                record.get('record_id') or ('%s:%d' % (self.file_id, ordinal)),
                int(record.get('version') or 1),
                record.get('level'),
                record.get('ts'),
                None if record.get('ts_ns') is None else str(record.get('ts_ns')),
                _int_or_none(record.get('ts_key')),
                _int_or_none(lines.get('first')),
                _int_or_none(lines.get('last')),
                None if byte_range.get('start') is None else str(byte_range.get('start')),
                None if byte_range.get('end') is None else str(byte_range.get('end')),
                1 if record.get('has_trace') else 0,
                record.get('logger'),
                record.get('fn'),
                record.get('pid'),
                record.get('tid'),
                record.get('src'),
                _int_or_none(record.get('src_line')),
                record.get('message') or '',
                json.dumps(record.get('body') or [], ensure_ascii=False),
                json.dumps(record.get('fields') or [], ensure_ascii=False),
                json.dumps({key: record[key] for key in ('issue', 'issueSeverity', 'event', 'node')
                            if key in record}, ensure_ascii=False),
            ))
        if record.get('event') or record.get('node') or (record.get('level') or '').upper() in ('ERROR', 'FATAL'):
            marked = record.get('node') or record.get('event') or {}
            cursor.execute(
                'INSERT OR REPLACE INTO timeline_nodes(ordinal,level,kind,label,rule_id,ts_key) '
                'VALUES(?,?,?,?,?,?)',
                (ordinal, record.get('level'), marked.get('type') or 'error',
                 marked.get('label') or record.get('message'),
                 marked.get('ruleId'), _int_or_none(record.get('ts_key'))))

    def checkpoint(self, stage: str) -> Optional[dict]:
        if self.conn is None:
            return None
        row = self.conn.execute('SELECT watermark FROM checkpoints WHERE stage=?', (stage,)).fetchone()
        return json.loads(row['watermark']) if row else None

    # ------------------------------------------------------------------- layers
    def clear_ordinals(self, version: str, ordinals) -> int:
        """Drop derived rows for ordinals that are about to be re-derived.

        A record that was provisional can become clean. Re-inserting the new row is
        not enough: the old issue, timeline node and postings would survive, and the
        counts would only ever grow. That is exactly the "只增不减" failure R6-073
        names.
        """
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        values = sorted({int(ordinal) for ordinal in ordinals})
        if not values:
            return 0
        removed = 0
        cursor = self.conn.cursor()
        for offset in range(0, len(values), 400):
            chunk = values[offset:offset + 400]
            marks = ','.join('?' for _ in chunk)
            cursor.execute('BEGIN IMMEDIATE')
            try:
                removed += cursor.execute(
                    'DELETE FROM rule_rows WHERE version=? AND ordinal IN (%s)' % marks,
                    [version] + chunk).rowcount
                cursor.execute(
                    'DELETE FROM rule_timeline WHERE version=? AND ordinal IN (%s)' % marks,
                    [version] + chunk)
                cursor.execute(
                    'DELETE FROM search_postings WHERE version=? AND ordinal IN (%s)' % marks,
                    [version] + chunk)
                cursor.execute(
                    'DELETE FROM timeline_nodes WHERE ordinal IN (%s)' % marks, chunk)
                cursor.execute('COMMIT')
            except BaseException:
                cursor.execute('ROLLBACK')
                raise
        return removed

    def write_layer(self, layer: str, version: str, *, signature: str = '', rows: int = 0,
                    digest: Optional[str] = None) -> dict:
        """Record that `version` is the shard's content for `layer`.

        The version row is the coverage claim; `publish` in the registry refuses
        to flip the pointer unless this matches what was expected.
        """
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        with self.conn:
            self.conn.execute(
                'INSERT INTO layer_versions(layer,version,signature,rows,digest,created_at) '
                'VALUES(?,?,?,?,?,?) ON CONFLICT(layer) DO UPDATE SET version=excluded.version,'
                'signature=excluded.signature,rows=excluded.rows,digest=excluded.digest,'
                'created_at=excluded.created_at',
                (layer, version, signature, int(rows), digest, _stamp()))
        return {'layer': layer, 'version': version, 'rows': int(rows), 'digest': digest}

    def layer(self, layer: str) -> Optional[dict]:
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        row = self.conn.execute('SELECT * FROM layer_versions WHERE layer=?', (layer,)).fetchone()
        return dict(row) if row else None

    def write_rule_rows(self, version: str, rows: Iterable[dict], *, reset: bool = False) -> int:
        """Append this batch's rule rows for `version`.

        `reset=True` clears the version first; that must happen **once** per build.
        Clearing on every batch silently keeps only the last batch, which looks
        like a complete layer in the row counter and an empty one in the table.
        """
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        written = 0
        cursor = self.conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        try:
            if reset:
                cursor.execute('DELETE FROM rule_rows WHERE version=?', (version,))
                cursor.execute('DELETE FROM rule_timeline WHERE version=?', (version,))
            for record in rows:
                ordinal = record.get('ordinal')
                if not isinstance(ordinal, int):
                    raise ShardError('bad_record', '规则层记录缺少 ordinal。')
                cursor.execute(
                    'INSERT OR REPLACE INTO rule_rows(ordinal,version,fields,issue,issue_severity,node,payload) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (ordinal, version,
                     json.dumps(record.get('fields') or [], ensure_ascii=False),
                     record.get('issue'), record.get('issueSeverity'),
                     json.dumps(record.get('event') or record.get('node') or None, ensure_ascii=False),
                     json.dumps({key: record[key] for key in ('issue', 'issueSeverity', 'event', 'node')
                                 if key in record}, ensure_ascii=False)))
                written += 1
                if _is_timeline_node(record):
                    cursor.execute(
                        'INSERT OR REPLACE INTO rule_timeline(version,ordinal,level,kind,label,rule_id,ts_key) '
                        'VALUES(?,?,?,?,?,?,?)',
                        (version, ordinal, record.get('level'),
                         (record.get('event') or {}).get('type') or 'error',
                         (record.get('event') or {}).get('label') or record.get('message'),
                         (record.get('event') or {}).get('ruleId'),
                         _int_or_none(record.get('ts_key'))))
            cursor.execute('COMMIT')
        except BaseException:
            cursor.execute('ROLLBACK')
            raise
        return written

    def write_postings(self, version: str, postings: Iterable[tuple], *, reset: bool = False) -> int:
        """`postings` is (gram, ordinal, field) triples. Appends unless `reset`."""
        if self.conn is None:
            raise ShardError('not_open', '分片写入器尚未打开。')
        written = 0
        cursor = self.conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        try:
            if reset:
                cursor.execute('DELETE FROM search_postings WHERE version=?', (version,))
            for gram, ordinal, field in postings:
                cursor.execute(
                    'INSERT OR REPLACE INTO search_postings(version,gram,ordinal,field) VALUES(?,?,?,?)',
                    (version, gram, int(ordinal), field))
                written += 1
            cursor.execute('COMMIT')
        except BaseException:
            cursor.execute('ROLLBACK')
            raise
        return written

    def close(self):
        if self.conn is not None:
            try:
                self.conn.execute('PRAGMA optimize')
            except sqlite3.Error:
                pass
            self.conn.close()
            self.conn = None


class ShardReader:
    """Read-only view. Short cursors; nothing here holds a transaction open."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ShardError('no_shard', '索引分片不存在。', path=str(self.path))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect('file:%s?mode=ro' % self.path, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def meta(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute('SELECT key,value FROM meta').fetchall()
        return {row['key']: row['value'] for row in rows}

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n']

    def page(self, offset: int = 0, limit: int = 200) -> list[dict]:
        if limit > 2000:
            raise ShardError('page_too_large', '单页最多 2000 条。', requested=limit)
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM records ORDER BY ordinal LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, ordinal: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM records WHERE ordinal=?', (ordinal,)).fetchone()
        return self._decode(row) if row else None

    def get_by_id(self, record_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM records WHERE record_id=?', (record_id,)).fetchone()
        return self._decode(row) if row else None

    def seek_line(self, line: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM records WHERE line_first<=? AND line_last>=? ORDER BY ordinal LIMIT 1',
                (line, line)).fetchone()
        return self._decode(row) if row else None

    def seek_byte(self, offset: int) -> Optional[dict]:
        # byte_start is text because offsets exceed 2^53; compare as integers in
        # SQL so the index on line_first is not misused for a text range.
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM records WHERE CAST(byte_start AS INTEGER)<=? '
                'AND CAST(byte_end AS INTEGER)>? ORDER BY ordinal LIMIT 1',
                (offset, offset)).fetchone()
        return self._decode(row) if row else None

    def time_range(self, from_key: Optional[int], to_key: Optional[int], limit: int = 200) -> list[dict]:
        clauses, params = [], []
        if from_key is not None:
            clauses.append('ts_key>=?')
            params.append(from_key)
        if to_key is not None:
            clauses.append('ts_key<=?')
            params.append(to_key)
        sql = 'SELECT * FROM records'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        # Untimed records go last: they cannot be placed on a time axis, and
        # putting NULL first would make a time view start with undated rows.
        sql += ' ORDER BY (ts_key IS NULL), ts_key, ordinal LIMIT ?'
        params.append(min(2000, limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._decode(row) for row in rows]

    def timeline(self, page: int = 0, page_size: int = 50) -> dict:
        if page_size not in (20, 50, 100, 150):
            raise ShardError('invalid_page_size', '时间线每页只支持 20/50/100/150。')
        if page < 0:
            raise ShardError('invalid_cursor', '页码不能为负。')
        with self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS n FROM timeline_nodes').fetchone()['n']
            rows = conn.execute(
                'SELECT * FROM timeline_nodes ORDER BY ts_key, ordinal LIMIT ? OFFSET ?',
                (page_size, page * page_size)).fetchall()
        return {
            'nodes': [{'ordinal': row['ordinal'], 'level': row['level'], 'kind': row['kind'],
                       'label': row['label'], 'rule_id': row['rule_id'], 'ts_key': row['ts_key']}
                      for row in rows],
            'page': page, 'page_size': page_size, 'total': total,
            'has_prev': page > 0, 'has_next': (page + 1) * page_size < total,
            'count': {'relation': 'exact', 'matched': total},
            'coverage': {'complete': True},
        }

    def checkpoint(self, stage: str = 'parsed') -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute('SELECT watermark FROM checkpoints WHERE stage=?', (stage,)).fetchone()
        return json.loads(row['watermark']) if row else None

    # ------------------------------------------------------------------- layers
    def layers(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM layer_versions').fetchall()
        return {row['layer']: dict(row) for row in rows}

    def layer(self, layer: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM layer_versions WHERE layer=?', (layer,)).fetchone()
        return dict(row) if row else None

    def rule_rows(self, version: str, offset: int = 0, limit: int = 200) -> list[dict]:
        if limit > 2000:
            raise ShardError('page_too_large', '单页最多 2000 条。', requested=limit)
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM rule_rows WHERE version=? ORDER BY ordinal LIMIT ? OFFSET ?',
                (version, limit, offset)).fetchall()
        return [self._decode_rule(row) for row in rows]

    def records_for(self, ordinals: Iterable[int]) -> list[dict]:
        """Bulk fetch for query evaluation. Order follows the input list."""
        wanted = [int(item) for item in ordinals]
        if not wanted:
            return []
        if len(wanted) > 2000:
            raise ShardError('page_too_large', '一次最多取 2000 条记录。', requested=len(wanted))
        out = {}
        with self._connect() as conn:
            for index in range(0, len(wanted), 400):
                chunk = wanted[index:index + 400]
                rows = conn.execute(
                    'SELECT * FROM records WHERE ordinal IN (%s)' % ','.join('?' * len(chunk)),
                    tuple(chunk)).fetchall()
                for row in rows:
                    out[row['ordinal']] = self._decode(row)
        return [out[item] for item in wanted if item in out]

    def rule_row(self, version: str, ordinal: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM rule_rows WHERE version=? AND ordinal=?',
                               (version, ordinal)).fetchone()
        return self._decode_rule(row) if row else None

    def rule_count(self, version: str) -> dict:
        """Coverage check: rows actually stored for a version, not the claimed count."""
        with self._connect() as conn:
            rows = conn.execute('SELECT COUNT(*) AS n FROM rule_rows WHERE version=?',
                                (version,)).fetchone()['n']
            nodes = conn.execute('SELECT COUNT(*) AS n FROM rule_timeline WHERE version=?',
                                 (version,)).fetchone()['n']
        return {'rows': rows, 'timeline': nodes}

    def posting_count(self, version: str) -> int:
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*) AS n FROM search_postings WHERE version=?',
                                (version,)).fetchone()['n']

    def rule_timeline(self, version: str, page: int = 0, page_size: int = 50) -> dict:
        if page_size not in (20, 50, 100, 150):
            raise ShardError('invalid_page_size', '时间线每页只支持 20/50/100/150。')
        if page < 0:
            raise ShardError('invalid_cursor', '页码不能为负。')
        with self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS n FROM rule_timeline WHERE version=?',
                                 (version,)).fetchone()['n']
            rows = conn.execute(
                'SELECT * FROM rule_timeline WHERE version=? ORDER BY (ts_key IS NULL), ts_key, ordinal '
                'LIMIT ? OFFSET ?', (version, page_size, page * page_size)).fetchall()
        return {
            'nodes': [{'ordinal': row['ordinal'], 'level': row['level'], 'kind': row['kind'],
                       'label': row['label'], 'rule_id': row['rule_id'], 'ts_key': row['ts_key']}
                      for row in rows],
            'page': page, 'page_size': page_size, 'total': total,
            'has_prev': page > 0, 'has_next': (page + 1) * page_size < total,
            'count': {'relation': 'exact', 'matched': total},
            'coverage': {'complete': True},
        }

    def postings(self, grams: Iterable[str], version: str) -> dict[str, set]:
        grams = [gram for gram in grams]
        found = {gram: set() for gram in grams}
        if not grams:
            return found
        with self._connect() as conn:
            for start in range(0, len(grams), 400):
                chunk = grams[start:start + 400]
                rows = conn.execute(
                    'SELECT gram,ordinal FROM search_postings WHERE version=? AND gram IN (%s)'
                    % ','.join('?' * len(chunk)), (version, *chunk)).fetchall()
                for row in rows:
                    found[row['gram']].add(row['ordinal'])
        return found

    def query_plan(self, sql: str, params: tuple = ()) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
        return [row['detail'] for row in rows]

    @staticmethod
    def _decode_rule(row) -> dict:
        return {
            'ordinal': row['ordinal'],
            'version': row['version'],
            'fields': json.loads(row['fields']),
            'issue': row['issue'],
            'issueSeverity': row['issue_severity'],
            'node': json.loads(row['node']) if row['node'] else None,
            'payload': json.loads(row['payload']),
        }

    @staticmethod
    def _decode(row) -> dict:
        return {
            'ordinal': row['ordinal'],
            'record_id': row['record_id'],
            'version': row['version'],
            'level': row['level'],
            'ts': row['ts'],
            'ts_ns': row['ts_ns'],
            'ts_key': row['ts_key'],
            'lines': {'first': row['line_first'], 'last': row['line_last']},
            'bytes': {'start': row['byte_start'], 'end': row['byte_end']},
            'has_trace': bool(row['has_trace']),
            'logger': row['logger'],
            'fn': row['fn'],
            'pid': row['pid'],
            'tid': row['tid'],
            'src': row['src'],
            'src_line': row['src_line'],
            'message': row['message'],
            'body': json.loads(row['body']),
            'fields': json.loads(row['fields']),
            'payload': json.loads(row['payload']),
        }


def _is_timeline_node(record: dict) -> bool:
    event = record.get('event') or record.get('node')
    return bool(event) or (record.get('level') or '').upper() in ('ERROR', 'FATAL')


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stamp() -> str:
    from ..app import now
    return now()
