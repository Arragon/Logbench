"""Incremental index advance from confirmed archive bytes (R6-073).

The rule is that a growing file must not be re-parsed. The chain is:

    archive commits an interval → parsed watermark advances only over those bytes
    → rules are applied to the *new ordinals only* → search postings likewise
    → counts are recomputed from the tables, not accumulated

Four things this file refuses:

* **a gap is not crossed.** If the archive has no confirmed bytes beyond the parsed
  watermark, nothing is read; the file reports `behind` with accurate coverage.
* **a revision may reduce a count.** A provisional tail record that later becomes
  valid loses its issue, its timeline node and its postings. `clear_ordinals()` runs
  before the re-derivation, and `_recount()` recomputes instead of adding.
* **watermarks are separate.** `archive`, `parsed`, `rules` and `search` can be at
  four different offsets; a single "progress" number would hide that.
* **an empty index is not zero matches.** Coverage is reported from the watermarks,
  so a query over an unparsed range stays `partial` until the bytes are parsed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from ..models import content_signature, new_id
from .node_pool import PoolError
from .shards import ShardError, ShardReader, ShardWriter, shard_path

BLOCK = 1024 * 1024
MAX_BLOCKS = 64


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class IncrementalIndexer:
    def __init__(self, app, *, indexer=None):
        self.app = app
        self.store = app.store
        self.indexer = indexer

    # ------------------------------------------------------------------ helpers
    def _service(self):
        return self.indexer or self.app.index_jobs

    def watermark(self, file_id: str, generation: int, stage: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT watermark FROM index_watermarks WHERE file_id=? AND '
                             'generation=? AND stage=?', (file_id, generation, stage)).fetchone()
        return json.loads(row['watermark']) if row else {}

    def _put_watermark(self, file_id: str, generation: int, stage: str, value: dict) -> None:
        with self.store.transaction() as db:
            db.execute('INSERT INTO index_watermarks(file_id,generation,stage,watermark,updated_at) '
                       'VALUES(?,?,?,?,?) ON CONFLICT(file_id,generation,stage) DO UPDATE SET '
                       'watermark=excluded.watermark, updated_at=excluded.updated_at',
                       (file_id, generation, stage,
                        json.dumps(value, ensure_ascii=False, sort_keys=True), _now()))

    def shard(self, file_id: str, generation: int) -> Path:
        service = self._service()
        signatures = service.default_signatures()
        tag = 'r-%s-%s' % (signatures.get('structure'), signatures.get('extract'))
        return shard_path(service.index_root, file_id, generation, tag)

    # --------------------------------------------------------------------- sync
    def sync(self, file_id: str, generation: int, rel_path: str, *, lease=None) -> dict:
        from ..collectors.archive import Archive
        archive = Archive(self.app)
        watermark = archive.watermark(file_id, generation)
        target = int(watermark['contiguous'])
        shard = self.shard(file_id, generation)
        if not shard.exists():
            return self._first_index(file_id, generation, rel_path, target)
        return self._advance(file_id, generation, rel_path, shard, target)

    def _first_index(self, file_id: str, generation: int, rel_path: str, target: int) -> dict:
        """A shard that does not exist yet is built by the product index job."""
        service = self._service()
        from ..jobs.repository import JobRepository
        path = self.app.safe_path(rel_path)
        # The signature must be the one the pipeline will compute, otherwise its
        # post-read verification sees a different file and refuses the run.
        size, mtime_ns, head, tail = service.pipeline.sample(path)
        signature = content_signature(size, mtime_ns, head, tail)
        job = JobRepository(self.store).create(
            'index', 'system:collector',
            {'path': str(path), 'file_id': file_id, 'generation': generation,
             'content_sig': signature})
        result = service.run_now(job.id)
        for stage in ('parsed', 'rules', 'search'):
            self._put_watermark(file_id, generation, stage,
                                {'bytes': target, 'at': _now(), 'build': 'full'})
        self._put_watermark(file_id, generation, 'archive', {'bytes': target, 'at': _now()})
        return {'state': result.get('state', 'indexed'), 'mode': 'full',
                'records': result.get('layers', {}), 'bytes': target}

    def _advance(self, file_id: str, generation: int, rel_path: str, shard: Path,
                 target: int) -> dict:
        service = self._service()
        parsed = self.watermark(file_id, generation, 'parsed')
        start = int(parsed.get('bytes') or 0)
        self._put_watermark(file_id, generation, 'archive', {'bytes': target, 'at': _now()})
        if target <= start:
            return {'state': 'current', 'mode': 'incremental', 'parsed': start,
                    'archive': target, 'records': self._count(shard)}
        path = self.app.safe_path(rel_path)
        writer = ShardWriter(shard, file_id=file_id, generation=generation,
                             signature=shard.name.split('-', 2)[-1].replace('.sqlite3', '')).open()
        pool = service.pool
        job = 'inc:%s:%d:%s' % (file_id, generation, new_id('s')[-8:])
        read = 0
        try:
            checkpoint = writer.checkpoint('parsed') or {}
            framer = checkpoint.get('framer')
            ordinal_start = int(checkpoint.get('records') or 0)
            if framer:
                pool.restore(job, framer, start_ordinal=ordinal_start)
            else:
                pool.restore(job, None, start_ordinal=ordinal_start, start_position=start)
            offset = start
            new_ordinals = []
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                blocks = 0
                while offset < target and blocks < MAX_BLOCKS:
                    length = min(BLOCK, target - offset)
                    os.lseek(descriptor, offset, os.SEEK_SET)
                    data = os.read(descriptor, length)
                    if not data:
                        break
                    reply = pool.parse(job, 1, data, eof=offset + len(data) >= target,
                                       offset=offset + len(data))
                    consumed = int(reply.get('consumed') or len(data))
                    records = reply.get('records') or []
                    for record in records:
                        record['file_id'] = file_id
                        record['record_id'] = '%s:%d' % (file_id, record.get('ordinal', 0))
                        new_ordinals.append(int(record.get('ordinal', 0)))
                    offset += consumed
                    blocks += 1
                    read += consumed
                    if records:
                        writer.commit_batch(records, {
                            'bytes': offset, 'records': ordinal_start + len(new_ordinals),
                            'framer': reply.get('checkpoint'), 'eof': offset >= target},
                            stage='parsed')
                    if consumed <= 0:
                        break
            finally:
                os.close(descriptor)
            self._put_watermark(file_id, generation, 'parsed',
                                {'bytes': offset, 'at': _now(), 'build': 'incremental',
                                 'pending': bool((writer.checkpoint('parsed') or {}).get('framer', {})
                                                 .get('pending'))})
            if not new_ordinals:
                return {'state': 'current', 'mode': 'incremental', 'parsed': offset,
                        'archive': target, 'records': self._count(shard), 'bytesRead': read}
            applied = self._apply_rules(writer, shard, file_id, generation, new_ordinals,
                                        rel_path, pool, job)
            self._put_watermark(file_id, generation, 'rules',
                                {'bytes': offset, 'at': _now(), 'rows': applied['rows']})
            self._put_watermark(file_id, generation, 'search',
                                {'bytes': offset, 'at': _now(), 'postings': applied['postings']})
            return {'state': 'indexed', 'mode': 'incremental', 'parsed': offset,
                    'archive': target, 'bytesRead': read, 'records': self._count(shard),
                    'issues': applied['issues'], 'newRecords': len(new_ordinals)}
        finally:
            writer.close()
            try:
                pool.reset(job)
            except PoolError:
                pass

    # ------------------------------------------------------------------- layers
    def _apply_rules(self, writer, shard: Path, file_id: str, generation: int,
                     ordinals: list, rel_path: str, pool, job: str) -> dict:
        service = self._service()
        profile = service.default_profile()
        version = self._rules_version(shard, file_id, generation, service)
        reader = ShardReader(shard)
        records = reader.records_for(ordinals)
        with open(self.app.safe_path(rel_path), 'rb') as handle:
            for record in records:
                span = record.get('bytes') or {}
                if span.get('start') is not None:
                    handle.seek(int(span['start']))
                    # `raw` travels to the Node worker as JSON, so it is text, not bytes.
                    record['raw'] = handle.read(int(span['end']) - int(span['start'])) \
                        .decode('utf-8', 'replace')
        applied = pool.apply(job, records, profile).get('records') or []
        search_version = self._layer_version(shard, file_id, generation, service, 'search')
        # Remove the previous derivation first: a revised record must be allowed to
        # *lose* an issue, a timeline node and its postings.
        writer.clear_ordinals(version, ordinals)
        writer.clear_ordinals(search_version, ordinals)
        writer.write_rule_rows(version, applied, reset=False)
        postings = []
        from .layers import grams, search_text
        for record, ruled in zip(records, applied):
            seen = set()
            for field, text in search_text(record, ruled):
                for gram in grams(text):
                    if (gram, field) in seen:
                        continue
                    seen.add((gram, field))
                    postings.append((gram, int(record['ordinal']), field))
        if postings:
            # Postings belong to the search layer, not to the rules layer: the search
            # version is what a query pins.
            writer.write_postings(search_version, postings, reset=False)
        counts = self._recount(writer, version)
        writer.write_layer('rules', version, signature='', rows=counts['rows'],
                           digest=counts['digest'])
        writer.write_layer('search', search_version, signature='', rows=len(postings))
        return {'rows': counts['rows'], 'issues': counts['issues'], 'postings': len(postings),
                'searchVersion': search_version}

    def _layer_version(self, shard: Path, file_id: str, generation: int, service,
                       layer_name: str) -> str:
        reader = ShardReader(shard)
        layer = reader.layer(layer_name) or {}
        if layer.get('version'):
            return layer['version']
        from .cache_versions import CacheRegistry, deps_for
        registry = CacheRegistry(self.store)
        signatures = service.default_signatures()
        entry = registry.ensure(file_id, layer_name, owner='system:collector',
                                generation=generation,
                                deps=deps_for(layer_name,
                                              structure_signature='incremental' if layer_name == 'rules' else None,
                                              rule_structure=signatures.get('structure') if layer_name == 'rules' else None,
                                              rule_extract=signatures.get('extract') if layer_name == 'rules' else None,
                                              engine='rules/1' if layer_name == 'rules' else None,
                                              projection='grams/v2' if layer_name == 'search' else None,
                                              gram_size=3 if layer_name == 'search' else None))
        version = entry['version_id']
        writer = ShardWriter(shard, file_id=file_id, generation=generation,
                             signature=shard.name.split('-', 2)[-1].replace('.sqlite3', '')).open()
        try:
            writer.write_layer(layer_name, version, signature='', rows=0)
        finally:
            writer.close()
        return version

    def _rules_version(self, shard: Path, file_id: str, generation: int, service) -> str:
        reader = ShardReader(shard)
        layer = reader.layer('rules') or {}
        if layer.get('version'):
            return layer['version']
        from .cache_versions import CacheRegistry, deps_for
        registry = CacheRegistry(self.store)
        signatures = service.default_signatures()
        entry = registry.ensure(file_id, 'rules', owner='system:collector',
                                generation=generation,
                                deps=deps_for('rules', structure_signature='incremental',
                                              rule_structure=signatures.get('structure'),
                                              rule_extract=signatures.get('extract'),
                                              engine='rules/1'))
        version = entry['version_id']
        writer = ShardWriter(shard, file_id=file_id, generation=generation,
                             signature=shard.name.split('-', 2)[-1].replace('.sqlite3', '')).open()
        try:
            writer.write_layer('rules', version, signature='', rows=0)
        finally:
            writer.close()
        return version

    @staticmethod
    def _recount(writer, version: str) -> dict:
        cursor = writer.conn.cursor()
        rows = cursor.execute('SELECT COUNT(*) AS n FROM rule_rows WHERE version=?',
                              (version,)).fetchone()['n']
        issues = cursor.execute('SELECT COUNT(*) AS n FROM rule_rows WHERE version=? AND '
                                'issue IS NOT NULL AND issue<>?', (version, '')).fetchone()['n']
        nodes = cursor.execute('SELECT COUNT(*) AS n FROM rule_timeline WHERE version=?',
                               (version,)).fetchone()['n']
        payload = cursor.execute('SELECT ordinal,fields,issue,issue_severity,node,payload '
                                 'FROM rule_rows WHERE version=? ORDER BY ordinal',
                                 (version,)).fetchall()
        import hashlib
        digest = hashlib.sha256()
        for row in payload:
            digest.update(json.dumps([row['ordinal'], row['issue'], row['issue_severity']],
                                     sort_keys=True).encode('utf-8'))
        return {'rows': int(rows), 'issues': int(issues), 'nodes': int(nodes),
                'digest': digest.hexdigest()}

    @staticmethod
    def _count(shard: Path) -> int:
        try:
            return ShardReader(shard).count()
        except ShardError:
            return 0

    # ------------------------------------------------------------------ coverage
    def coverage(self, file_id: str, generation: int) -> dict:
        stages = {stage: int(self.watermark(file_id, generation, stage).get('bytes') or 0)
                  for stage in ('archive', 'parsed', 'rules', 'search')}
        # Read the live archive watermark: a new append must show up as `unparsed`
        # immediately, not after the next sync.
        from ..collectors.archive import Archive
        stages['archive'] = int(Archive(self.app).watermark(file_id, generation)['contiguous'])
        archive = stages['archive']
        parsed = stages['parsed']
        return {'archiveBytes': archive, 'parsedBytes': parsed,
                'unparsedBytes': max(0, archive - parsed),
                'complete': bool(archive) and parsed >= archive,
                'stages': stages}
