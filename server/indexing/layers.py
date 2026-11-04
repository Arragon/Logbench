"""Derived layer builders: rules projection and search postings (R6-038/R6-043 groundwork).

Every builder here starts from the shard, never from `parse`. That is the whole
economic argument for layered caches: editing a rule or adding a rule field must
cost a projection pass, not a re-frame of a 1 GiB file.

Two honest caveats, stated because the alternative is a metric that flatters
itself:

* the rules layer needs the record's **raw lines**, because conditions may target
  `raw`. Those bytes are read from the authorized source by byte range in one
  forward pass. Framing and rule-free parsing are not repeated, but I/O is, and
  `stats['source_bytes']` reports it rather than hiding it.
* the search layer indexes **every** trigram window, including ones containing
  spaces. Pruning with a subset of windows would silently drop matches, and a
  candidate generator that returns fewer rows than the oracle is a wrong answer,
  not a fast one.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from .shards import ShardReader, ShardWriter, ShardError

GRAM = 3
#: Mirrors `PROJECTIONS` in src/shared/query.js. The matcher builds its haystack by
#: joining these chunks with `\n`, so the index must join them the same way — a
#: needle that spans two chunks is a real match, and pruning it would be a missed hit.
#: `tests/test_r6_query_planner.py` compares pruned and full-scan results on the
#: fixtures, so a drift between the two lists fails a test instead of losing rows.
PROJECTION_FIELDS = {
    'v1': ('message', 'body'),
    'v2': ('message', 'body', 'fields', 'logger', 'fn', 'src'),
}


def haystacks(record: dict, rule_row: Optional[dict] = None) -> list:
    """(projection, text) pairs to index, matching what the matcher will search."""
    fields = {}
    if rule_row:
        fields = {item.get('key'): item.get('value') for item in (rule_row.get('fields') or [])
                  if isinstance(item, dict)}
    if not fields:
        fields = {item.get('key'): item.get('value') for item in (record.get('fields') or [])
                  if isinstance(item, dict)}
    pairs = []
    for version, names in PROJECTION_FIELDS.items():
        chunks = []
        for name in names:
            if name == 'body':
                body = record.get('body') or []
                if body:
                    chunks.append('\n'.join(str(line) for line in body))
            elif name == 'fields':
                if fields:
                    chunks.append('\n'.join('%s=%s' % (key, value)
                                            for key, value in fields.items()))
            else:
                value = record.get(name)
                if value is not None:
                    chunks.append(str(value))
        pairs.append((version, '\n'.join(chunks)))
    return pairs


def search_text(record: dict, rule_row: Optional[dict] = None) -> list:
    """Backwards-compatible single-projection view (v2 superset, sorted by field)."""
    return [(field, text) for field, text in haystacks(record, rule_row)]


def fold(text: str) -> str:
    """Case-fold for gram extraction. Both sides must fold identically."""
    return unicodedata.normalize('NFC', (text or '')).casefold()


def grams(text: str, size: int = GRAM) -> set:
    folded = fold(text)
    if len(folded) < size:
        return {folded} if folded else set()
    return {folded[index:index + size] for index in range(len(folded) - size + 1)}


class _RangeReader:
    """Forward-only byte-range reader for record `bytes` ranges."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = open(self.path, 'rb')
        self.position = 0
        self.bytes_read = 0

    def read(self, byte_range: dict) -> list:
        try:
            start = int(byte_range.get('start') or 0)
            end = int(byte_range.get('end') or 0)
        except (TypeError, ValueError):
            return []
        if end <= start:
            return []
        if start < self.position:
            self.handle.seek(start)
            self.position = start
        elif start > self.position:
            self.handle.seek(start)
            self.position = start
        chunk = self.handle.read(end - start)
        self.position += len(chunk)
        self.bytes_read += len(chunk)
        text = chunk.decode('utf-8', errors='replace')
        return text.splitlines()

    def close(self):
        try:
            self.handle.close()
        except OSError:
            pass


def build_rules_layer(shard_path: Path, *, source_path: Path, pool, profile: Optional[dict],
                      version: str, job: str = 'rules-rebuild', page: int = 2000,
                      writer: Optional[ShardWriter] = None) -> dict:
    """Recompute fields/issues/nodes/timeline for `version`; no re-framing."""
    reader = ShardReader(shard_path)
    own_writer = writer is None
    writer = writer or ShardWriter(Path(shard_path), file_id=reader.meta().get('file_id', 'file'),
                                   generation=int(reader.meta().get('generation', 1) or 1),
                                   signature=reader.meta().get('signature', 'default')).open()
    ranges = _RangeReader(source_path)
    digest = hashlib.sha256()
    total = reader.count()
    written = 0
    rules_failed = None
    try:
        for offset in range(0, total, page):
            records = reader.page(offset, page)
            for record in records:
                record['raw'] = ranges.read(record.get('bytes') or {})
            reply = pool.apply(job, records, profile)
            applied = reply.get('records') or []
            if reply.get('rules_failed'):
                rules_failed = reply.get('rules_failed_reason') or 'rules_failed'
            for record in applied:
                digest.update(json.dumps(
                    [record.get('ordinal'), record.get('fields'), record.get('issue'),
                     record.get('issueSeverity'), record.get('event')],
                    sort_keys=True, ensure_ascii=True).encode('utf-8'))
                digest.update(b'\0')
            written += writer.write_rule_rows(version, applied, reset=(offset == 0))
        if written != total:
            raise ShardError('coverage_mismatch', '规则层覆盖不完整。',
                             expected=total, actual=written)
        row = writer.write_layer('rules', version, signature='', rows=written,
                                 digest=digest.hexdigest())
    finally:
        ranges.close()
        if own_writer:
            writer.close()
    return {'layer': 'rules', 'version': version, 'rows': written, 'digest': row['digest'],
            'stats': {'parses': 0, 'source_bytes': ranges.bytes_read, 'records': written,
                      'rules_failed': rules_failed}}


def build_search_layer(shard_path: Path, *, version: str, rules_version: Optional[str] = None,
                       gram_size: int = GRAM, page: int = 2000,
                       writer: Optional[ShardWriter] = None) -> dict:
    """Build trigram postings for `version` from the structure (and rules) layer."""
    reader = ShardReader(shard_path)
    own_writer = writer is None
    writer = writer or ShardWriter(Path(shard_path), file_id=reader.meta().get('file_id', 'file'),
                                   generation=int(reader.meta().get('generation', 1) or 1),
                                   signature=reader.meta().get('signature', 'default')).open()
    digest = hashlib.sha256()
    total = reader.count()
    triples = 0
    try:
        for offset in range(0, total, page):
            records = reader.page(offset, page)
            rules = {}
            if rules_version:
                rules = {row['ordinal']: row for row in reader.rule_rows(rules_version, offset, page)}
            postings = []
            for record in records:
                ordinal = record['ordinal']
                seen = set()
                for field, text in search_text(record, rules.get(ordinal)):
                    for gram in grams(text, gram_size):
                        if (gram, field) in seen:
                            continue
                        seen.add((gram, field))
                        postings.append((gram, ordinal, field))
                        digest.update(('%s\0%d\0%s\0' % (gram, ordinal, field)).encode('utf-8'))
            triples += writer.write_postings(version, postings, reset=(offset == 0))
        writer.write_layer('search', version, rows=triples, digest=digest.hexdigest())
    finally:
        if own_writer:
            writer.close()
    return {'layer': 'search', 'version': version, 'rows': triples,
            'digest': digest.hexdigest(),
            'stats': {'parses': 0, 'source_bytes': 0, 'postings': triples}}


def records_digest(records: Iterable[dict]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(json.dumps(record, sort_keys=True, ensure_ascii=True).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()
