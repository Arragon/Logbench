"""Candidate planning for server-side queries (R6-043).

The planner's only job is to produce a **superset** of the records that the exact
matcher might accept, and to be honest when it cannot prove anything. Two failure
modes matter more than speed:

* **a missed hit** — a plan that prunes a record the matcher would have accepted.
  This is a wrong answer, so every pruning step must be justified by a property the
  data actually has (an exact index, a gram that is literally contained).
* **a fake fast path** — reporting `btree` because a condition *looks* selective.
  The plan therefore carries `EXPLAIN QUERY PLAN` output for the SQL it will run,
  and the report distinguishes "index chosen" from "table scan with a WHERE".

Order of preference:

1. `postings` — the trigram postings built by the search layer (R6-038). Every gram
   window of `message`/`body`/`logger`/`level`/`fn`/field values is indexed, so a
   literal needle of at least `gram_size` folded characters is contained in every
   matching record; intersecting the needle's grams is a sound superset test.
2. `fts` — only when postings are missing and FTS5 with the trigram tokenizer is
   actually available, and only for shards small enough that building a temporary
   index is cheaper than a scan. Never assumed, always probed.
3. `btree` — structural conditions (level / time range) pushed into SQL. This is a
   *filter*, not a candidate shortcut: it uses real indexes, and `explain` shows it.
4. `full_scan` — the default. Bounded, ordered by ordinal, and never dressed up as
   something faster.

What disqualifies a term from gram pruning: regex, whole-word wrapping (the
boundary is not part of the text), a needle shorter than the gram size (a two
character Chinese word is a very common case), and any case-fold whose result
differs between Python and JS. In all of those the plan falls back, because a
superset that is too small cannot be repaired downstream.
"""
from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

GRAM = 3
MAX_FTS_ROWS = 50000


@dataclass
class Plan:
    mode: str
    superset: bool
    reason: str
    predicates: list = field(default_factory=list)
    gram_terms: list = field(default_factory=list)
    explain: list = field(default_factory=list)
    fts_available: bool = False
    postings_version: Optional[str] = None
    postings_complete: bool = False
    sql: Optional[str] = None
    params: tuple = ()
    cost: Optional[float] = None
    projection: str = 'v1'
    joiner: str = 'and'

    def report(self) -> dict:
        return {'mode': self.mode, 'superset': self.superset, 'reason': self.reason,
                'predicates': self.predicates, 'gram_terms': self.gram_terms,
                'explain': self.explain, 'fts_available': self.fts_available,
                'postings_version': self.postings_version,
                'postings_complete': self.postings_complete,
                'projection': self.projection, 'joiner': self.joiner}


def fold(text: str) -> str:
    """Same folding as `layers.fold`, so grams are comparable on both sides."""
    return unicodedata.normalize('NFC', (text or '')).casefold()


def grams(text: str, size: int = GRAM) -> set:
    folded = fold(text)
    if len(folded) < size:
        return set()
    return {folded[index:index + size] for index in range(len(folded) - size + 1)}


def gram_usable(term: dict) -> tuple:
    """(usable, reason) for one term of the JS-normalised summary."""
    if term.get('kind') != 'literal':
        return False, 'regex'
    if term.get('whole_word'):
        return False, 'whole_word'
    folded = fold(term.get('value'))
    if len(folded) < GRAM:
        return False, 'shorter_than_gram'
    return True, ''


def fts_available(conn: sqlite3.Connection) -> bool:
    """Probe for FTS5 with the trigram tokenizer, without assuming it."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_probe USING fts5("
                     "text, tokenize='trigram')")
        conn.execute('DROP TABLE IF EXISTS temp.fts_probe')
        return True
    except sqlite3.Error:
        return False


def _like_pattern_safe(text: str) -> str:
    """Escape LIKE metacharacters. The planner never writes user text into SQL.

    Kept as a guard for callers that would otherwise be tempted: `%` and `_` are
    literal characters in a log search, not wildcards.
    """
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def plan_query(shard_path: Path, spec: dict, *, postings_version: Optional[str],
               postings_complete: bool, total_records: int) -> Plan:
    """Choose a candidate strategy for one normalised spec."""
    terms = spec.get('terms') or []
    levels = spec.get('levels') or []
    time_range = spec.get('time_range') or None
    projection = 'v2' if spec.get('projection_version') == 'v2' else 'v1'
    joiner = 'or' if spec.get('joiner') == 'or' else 'and'

    predicates = []
    clauses = []
    params: list = []
    if levels:
        clauses.append('level IN (%s)' % ','.join('?' * len(levels)))
        params.extend(levels)
        predicates.append({'kind': 'level', 'values': list(levels)})
    if time_range:
        from_ns = time_range.get('from_ns')
        to_ns = time_range.get('to_ns')
        if from_ns is not None:
            clauses.append('(ts_key IS NOT NULL AND ts_key >= ?)')
            params.append(int(int(from_ns) // 1_000_000))
        if to_ns is not None:
            clauses.append('(ts_key IS NOT NULL AND ts_key <= ?)')
            params.append(int(int(to_ns) // 1_000_000))
        predicates.append({'kind': 'time_range', 'from_ns': from_ns, 'to_ns': to_ns})

    conn = _connect(shard_path)
    try:
        available_fts = fts_available(conn)
        usable, blocked = [], []
        for term in terms:
            ok, why = gram_usable(term)
            if ok:
                usable.append(term)
            else:
                blocked.append(why)

        sql = 'SELECT ordinal FROM records'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        # `plan.sql` has no ORDER BY: the iterator wraps it so a resume can add
        # `ordinal > ?` without rewriting the caller's predicate.
        explain = _explain(conn, sql + ' ORDER BY ordinal', tuple(params))

        if usable and not blocked and postings_version:
            needle_grams = [grams(term.get('value')) for term in usable]
            if all(needle_grams):
                return Plan(mode='postings', superset=True,
                            reason='literal_terms_indexed', projection=projection, joiner=joiner,
                            predicates=predicates,
                            gram_terms=[sorted(item) for item in needle_grams],
                            explain=explain, fts_available=available_fts,
                            postings_version=postings_version,
                            postings_complete=postings_complete, sql=sql, params=tuple(params))
        if usable and blocked and postings_version:
            # Some terms prune, some do not: prune on the ones that do. That is
            # still a superset, because a record matching *all* terms must match
            # each prunable term as well.
            needle_grams = [grams(term.get('value')) for term in usable if grams(term.get('value'))]
            if needle_grams:
                return Plan(mode='postings', superset=True,
                            reason='partial_terms_indexed: ' + ','.join(sorted(set(blocked))),
                            projection=projection, joiner=joiner,
                            predicates=predicates,
                            gram_terms=[sorted(item) for item in needle_grams],
                            explain=explain, fts_available=available_fts,
                            postings_version=postings_version,
                            postings_complete=postings_complete, sql=sql, params=tuple(params))
        if available_fts and not postings_version and total_records and total_records <= MAX_FTS_ROWS:
            return Plan(mode='fts', superset=True, reason='ad_hoc_trigram_index',
                        predicates=predicates, explain=explain, fts_available=True,
                        postings_complete=False, sql=sql, params=tuple(params))
        if available_fts and usable and not blocked and not postings_version:
            # FTS exists but the shard is large: an ad-hoc index would cost more
            # than the scan, and a scan is still correct.
            return Plan(mode='full_scan', superset=True, reason='fts_available_too_large',
                        predicates=predicates, explain=explain, fts_available=True,
                        postings_complete=False, sql=sql, params=tuple(params))
        return Plan(mode='full_scan', superset=True,
                    reason='no_pruning_proof: ' + (','.join(sorted(set(blocked))) or 'no_terms'),
                    predicates=predicates, explain=explain, fts_available=available_fts,
                    postings_version=postings_version, postings_complete=postings_complete,
                    sql=sql, params=tuple(params))
    finally:
        conn.close()


def iter_candidates(shard_path: Path, plan: Plan, *, start_after: int = -1) -> Iterator[int]:
    """Yield candidate ordinals in ascending order, resuming after `start_after`.

    For `postings` the candidates are intersected in Python: the posting lists are
    bounded by the index, and intersection shrinks rather than grows, so memory is
    the smallest list, not the largest.
    """
    conn = _connect(shard_path)
    try:
        if plan.mode in ('full_scan', 'btree', 'fts') or plan.sql is None:
            base = plan.sql or 'SELECT ordinal FROM records'
            params = list(plan.params)
            if start_after >= 0:
                sql = 'SELECT ordinal FROM (%s) WHERE ordinal > ? ORDER BY ordinal' % base
                params.append(int(start_after))
            else:
                sql = base + ' ORDER BY ordinal'
            cursor = conn.execute(sql, tuple(params))
            while True:
                batch = cursor.fetchmany(2000)
                if not batch:
                    break
                for row in batch:
                    yield row['ordinal']
            return
        if plan.mode == 'postings':
            if not plan.postings_complete:
                # An incomplete index cannot justify an empty or a small set.
                sql = (plan.sql or 'SELECT ordinal FROM records') + ' ORDER BY ordinal'
                cursor = conn.execute(sql, plan.params)
                while True:
                    batch = cursor.fetchmany(2000)
                    if not batch:
                        break
                    for row in batch:
                        if row['ordinal'] > start_after:
                            yield row['ordinal']
                return
            # `and` intersects across terms, `or` unions. Getting this backwards
            # silently drops the rows of one whole term.
            current: Optional[set] = None
            for gram_list in plan.gram_terms:
                found = _postings_for(conn, gram_list, plan.postings_version, plan.projection)
                if current is None:
                    current = found
                elif plan.joiner == 'or':
                    current |= found
                else:
                    current &= found
                if not current and plan.joiner != 'or':
                    return
            for ordinal in sorted(current or ()):
                if ordinal > start_after:
                    yield ordinal
            return
        raise ValueError('unknown plan mode: %s' % plan.mode)
    finally:
        conn.close()


def _postings_for(conn: sqlite3.Connection, gram_list: Iterable[str],
                  version: Optional[str], projection: str = 'v1') -> set:
    grams_list = list(gram_list)
    result: set = set()
    if not grams_list:
        return result
    first = True
    for index in range(0, len(grams_list), 200):
        chunk = grams_list[index:index + 200]
        rows = conn.execute(
            'SELECT DISTINCT ordinal FROM search_postings WHERE version=? AND field=? '
            'AND gram IN (%s)' % ','.join('?' * len(chunk)),
            (version, projection, *chunk)).fetchall()
        found = {row['ordinal'] for row in rows}
        result = found if first else (result | found)
        first = False
    return result


def _explain(conn: sqlite3.Connection, sql: str, params: tuple) -> list:
    try:
        rows = conn.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
        return [row['detail'] for row in rows]
    except sqlite3.Error as exc:
        return ['explain_failed: %s' % exc]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect('file:%s?mode=ro' % Path(path), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def time_plan(started: float) -> dict:
    return {'planned_in_ms': round((time.perf_counter() - started) * 1000, 2)}
