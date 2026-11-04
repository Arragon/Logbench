"""Generation identity: append, rotate, truncate, replace, and honest gaps (R6-071).

A log file is not a stream with a stable name. It grows, gets renamed, is truncated in
place (`copytruncate`), is replaced by a new file with the same name, or is compressed
and a fresh one starts. Treating any of those as "the same file, continue at the old
offset" is how an archive ends up with two generations spliced together — and how a
"complete" claim gets attached to data that has a hole in it.

This module makes the decision explicit and returns a *reason*, not just a boolean:

| observation | action | what the archive does |
|---|---|---|
| first sight | `new` | start at 0 |
| size grew, prefix unchanged | `append` | continue at the watermark |
| size grew, prefix changed | `rewritten` | close the old generation, start a new one |
| size shrank | `truncated` | close the old generation, start a new one, record a gap |
| same size, prefix changed | `replaced` | new generation |
| same size, prefix same, mtime unchanged | `unchanged` | nothing |
| renamed away, successor found by identity | `rotated` | finish the old generation |
| source gone | `disappeared` | keep the archive, mark the file unavailable |
| evidence too weak to decide | `unknown` | stop and ask, never guess |

`prefix` here means the first `min(committed, 64 KiB)` bytes, hashed. It cannot prove
that the middle of the file never changed; that limitation is why an `append` result
carries `confidence='prefix-only'` and why a source that has not declared an append
contract cannot be used for follow mode.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

PREFIX = 64 * 1024

ACTIONS = ('new', 'append', 'rewritten', 'truncated', 'replaced', 'rotated',
           'disappeared', 'unchanged', 'unknown')
NEW_GENERATION = {'rewritten', 'truncated', 'replaced'}


@dataclass
class Observed:
    size: int
    mtime_ns: Optional[int] = None
    identity: dict = field(default_factory=dict)
    head: bytes = b''
    exists: bool = True
    name: str = ''

    def digest(self) -> str:
        return hashlib.sha256((self.head or b'')[:PREFIX]).hexdigest()[:32]


@dataclass
class Previous:
    size: int = 0
    mtime_ns: Optional[int] = None
    identity: dict = field(default_factory=dict)
    head_digest: str = ''
    generation: int = 1
    sealed: bool = False
    #: How many leading bytes the local archive actually holds. The comparison must
    #: use this, not the last observed remote size: a file that grew from 1000 to
    #: 1040 has only 1000 bytes we can vouch for.
    prefix_len: int = 0


@dataclass
class Decision:
    action: str
    generation: int
    reason: str
    confidence: str = 'strong'
    gap: Optional[dict] = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'action': self.action, 'generation': self.generation, 'reason': self.reason,
                'confidence': self.confidence, 'gap': self.gap, 'details': self.details}


def _same_head(previous: Previous, observed: Observed) -> bool:
    """Compare the **committed prefix**, not the whole observed head.

    Hashing everything the adapter returned would make every append look like a
    rewrite: the first 64 KiB of a grown file are not equal to the first 64 KiB of
    the smaller file.
    """
    if not previous.head_digest:
        # No prefix evidence: cannot claim the prefix is unchanged.
        return False
    limit = min(previous.prefix_len or previous.size, PREFIX)
    if limit <= 0:
        return False
    digest = hashlib.sha256(observed.head[:limit]).hexdigest()[:32]
    return previous.head_digest == digest


def _identity_changed(previous: Previous, observed: Observed) -> bool:
    if not previous.identity or not observed.identity:
        return False
    # `modTime` is deliberately not an identity key: a file that is appended to has a
    # new mtime, and treating that as a replacement would re-archive every growing log.
    # Identity is the protocol's own identifier (inode / ETag / object id); the prefix
    # and size checks cover everything else.
    keys = [key for key in ('id', 'etag', 'inode') if previous.identity.get(key) not in (None, '')]
    for key in keys:
        if observed.identity.get(key) not in (None, '') \
                and str(previous.identity.get(key)) != str(observed.identity.get(key)):
            return True
    return False


def decide(previous: Optional[Previous], observed: Observed, *,
           rotation_candidate: bool = False) -> Decision:
    """The single place that decides what an observation means."""
    if not observed.exists:
        if rotation_candidate:
            return Decision('rotated', previous.generation if previous else 1,
                            'renamed-away-with-successor', 'strong')
        return Decision('disappeared', previous.generation if previous else 1,
                        'source-file-missing', 'strong',
                        details={'note': '远端删除不传播为归档删除'})
    if previous is None or previous.generation <= 0:
        return Decision('new', 1, 'first-observation')

    generation = previous.generation
    if previous.sealed:
        return Decision('rewritten', generation + 1, 'previous-generation-sealed',
                        details={'previousSize': previous.size})

    if _identity_changed(previous, observed):
        return Decision('rewritten', generation + 1, 'identity-changed',
                        details={'previousIdentity': previous.identity,
                                 'observedIdentity': observed.identity})

    if observed.size < previous.size:
        reason = 'copytruncate' if observed.size > 0 else 'truncated-to-zero'
        return Decision('truncated', generation + 1, reason, 'strong',
                        gap={'state': 'unknown_length', 'reason': reason,
                             'lostFrom': previous.size, 'lostTo': None},
                        details={'previousSize': previous.size, 'observedSize': observed.size})

    if observed.size == previous.size:
        if _same_head(previous, observed):
            if previous.mtime_ns is not None and observed.mtime_ns is not None \
                    and observed.mtime_ns < previous.mtime_ns:
                return Decision('unknown', generation, 'mtime-went-backwards', 'weak',
                                details={'previousMtime': previous.mtime_ns,
                                         'observedMtime': observed.mtime_ns})
            return Decision('unchanged', generation, 'size-and-prefix-unchanged')
        if not previous.head_digest:
            return Decision('unknown', generation, 'no-prefix-evidence', 'weak')
        return Decision('replaced', generation + 1, 'same-size-different-prefix', 'strong')

    # observed.size > previous.size
    if _same_head(previous, observed):
        return Decision('append', generation, 'size-grew-prefix-unchanged', 'prefix-only',
                        details={'previousSize': previous.size, 'observedSize': observed.size})
    if not previous.head_digest:
        return Decision('unknown', generation, 'size-grew-without-prefix-evidence', 'weak')
    return Decision('rewritten', generation + 1, 'size-grew-prefix-changed', 'strong',
                    gap={'state': 'unknown_length', 'reason': 'rewritten-under-same-name',
                         'lostFrom': previous.size, 'lostTo': None})


def previous_from_row(row, watermark: Optional[dict], head: bytes) -> Previous:
    evidence = {}
    try:
        evidence = json.loads(row['evidence'] or '{}')
    except (TypeError, ValueError):
        evidence = {}
    contiguous = int((watermark or {}).get('contiguous') or row['contiguous_bytes'] or 0)
    return Previous(size=int(row['size'] or 0), mtime_ns=evidence.get('mtimeNs'),
                    identity=evidence.get('identity') or {},
                    head_digest=hashlib.sha256(head[:PREFIX]).hexdigest()[:32] if head else '',
                    generation=int(row['generation'] or 1),
                    sealed=bool(row['sealed']), prefix_len=len(head or b''))


def apply_decision(db, row, decision: Decision, *, observed: Observed, source_id: str) -> dict:
    """Persist the generation transition and the honest gap marker.

    A new generation resets the per-file watermarks, but the old rows in
    `archive_intervals` are kept: they belong to the previous generation id, and the
    reader can still page through them.
    """
    stamp = json.loads(json.dumps({'identity': observed.identity, 'mtimeNs': observed.mtime_ns,
                                  'headDigest': observed.digest(), 'size': observed.size}))
    gap_state = 'none'
    if decision.gap:
        gap_state = decision.gap.get('state') or 'unknown_length'
    if decision.action in NEW_GENERATION:
        db.execute('UPDATE source_files SET generation=?, archived_bytes=0, contiguous_bytes=0,'
                   ' sealed=0, gap_state=?, state=?, size=?, evidence=?, last_seen_at=?, '
                   'retry_at=NULL WHERE id=?',
                   (decision.generation, gap_state, 'gap' if decision.gap else 'new',
                    observed.size, json.dumps(stamp, ensure_ascii=False),
                    _now(), row['id']))
    else:
        # A decision taken on a fresh observation ends any pending retry window.
        db.execute('UPDATE source_files SET size=?, evidence=?, last_seen_at=?, gap_state=?, '
                   'retry_at=NULL WHERE id=?',
                   (observed.size, json.dumps(stamp, ensure_ascii=False), _now(),
                    gap_state if decision.gap else row['gap_state'], row['id']))
    return {'generation': decision.generation, 'gapState': gap_state,
            'action': decision.action, 'reason': decision.reason}


def _now() -> str:
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def is_compressed(name: str) -> bool:
    return str(name).lower().endswith(('.gz', '.xz', '.bz2', '.zst', '.zip', '.7z'))
