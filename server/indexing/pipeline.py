"""Streaming index pipeline (R6-036).

Python reads 4 MiB blocks from an authorized file descriptor, Node frames and
applies rules, the writer commits records **and** the checkpoint in one
transaction, and only then does the pipeline advance.

Three properties that are easy to claim and hard to keep:

* **bounded memory.** One block in, one batch out. The file is never read whole,
  and records from previous blocks are not retained.
* **no checkpoint ahead of the index.** If the writer fails, the job's watermark
  does not move; a restart re-reads that block. A crash between Node's reply and
  the ACK therefore loses nothing and duplicates nothing (the shard write is
  idempotent by ordinal).
* **generation conflict is fatal for this run.** If the file changes while being
  read (same path, different bytes), continuing would splice two generations
  into one shard. The run stops with `generation_conflict` and keeps what was
  already committed under the *old* generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..models import content_signature, file_id_for, new_id
from .node_pool import NodePool, PoolError
from .shards import ShardError, ShardWriter, shard_path

CHUNK = 1024 * 1024
#: The wire carries base64, and the worker caps a reply at 10k records, so a block
#: is sized to be answered in one reply for ordinary logs (~110 B/line). A larger
#: block does not read faster; it just re-sends the same bytes several times,
#: because the framer cannot rewind to a mid-block position.
MAX_CHUNK = 4 * 1024 * 1024
SAMPLE = 64 * 1024


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class Generation:
    file_id: str
    generation: int
    content_sig: str
    size: int


class Pipeline:
    def __init__(self, store, pool: NodePool, index_root: Path, *,
                 chunk_size: int = CHUNK, root_id: str = 'default'):
        self.store = store
        self.pool = pool
        self.index_root = Path(index_root)
        self.chunk_size = max(64 * 1024, min(chunk_size, MAX_CHUNK))
        self.root_id = root_id
        self.stats = {'chunks': 0, 'bytes': 0, 'batches': 0, 'records': 0}

    # ------------------------------------------------------------------ identity
    def sample(self, path: Path) -> tuple[int, int, bytes, bytes]:
        stat_result = path.stat()
        with open(path, 'rb') as handle:
            head = handle.read(SAMPLE)
            if stat_result.st_size > SAMPLE:
                handle.seek(max(SAMPLE, stat_result.st_size - SAMPLE))
                tail = handle.read(SAMPLE)
            else:
                tail = head
        return stat_result.st_size, stat_result.st_mtime_ns, head, tail

    def ensure_generation(self, path: Path, file_id: Optional[str] = None,
                          identity: Optional[dict] = None) -> Generation:
        if identity:
            # R6-054: the catalog already decided the identity, including which root
            # the file belongs to. Re-registering it here would create a second,
            # disagreeing id for the same bytes.
            return Generation(file_id=identity['file_id'],
                              generation=int(identity['generation']),
                              content_sig=identity['content_sig'],
                              size=int(identity['size']))
        path = Path(path).resolve()
        rel = str(path)
        fid = file_id or file_id_for(self.root_id, rel)
        size, mtime_ns, head, tail = self.sample(path)
        signature = content_signature(size, mtime_ns, head, tail)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM logical_files WHERE id=?', (fid,)).fetchone()
            root = db.execute('SELECT 1 FROM storage_roots WHERE id=?', (self.root_id,)).fetchone()
            if not root:
                db.execute('INSERT INTO storage_roots(id,kind,label,path,enabled,created_at) '
                           "VALUES(?,?,?,?,1,datetime('now'))",
                           (self.root_id, 'local', 'Local library', str(path.parent)))
            if not row:
                generation = 1
                db.execute('INSERT INTO logical_files(id,root_id,rel_path,display_name,size,generation,'
                           'content_sig,created_at,updated_at) VALUES(?,?,?,?,?,?,?,datetime(\'now\'),datetime(\'now\'))',
                           (fid, self.root_id, rel, path.name, size, generation, signature))
            else:
                generation = row['generation']
                if row['content_sig'] != signature:
                    generation = row['generation'] + 1
                    db.execute('UPDATE logical_files SET size=?,generation=?,content_sig=?,'
                               "updated_at=datetime('now') WHERE id=?", (size, generation, signature, fid))
                    # The previous generation's shard is not wrong, it is simply no
                    # longer this file. Retiring it is what stops a reader from
                    # serving spliced bytes under a fresh identity.
                    db.execute("UPDATE generations SET state='superseded' "
                               'WHERE file_id=? AND generation<? AND state<>?',
                               (fid, generation, 'superseded'))
            db.execute('INSERT OR REPLACE INTO generations(file_id,generation,raw_bytes,content_sig,state,'
                       "created_at) VALUES(?,?,?,?,?,datetime('now'))",
                       (fid, generation, size, signature, 'indexing'))
        return Generation(file_id=fid, generation=generation, content_sig=signature, size=size)

    # ---------------------------------------------------------------------- run
    def run(self, job_id: str, path: Path, *, token: int = 1, profile: Optional[dict] = None,
            file_id: Optional[str] = None, on_progress: Optional[Callable[[dict], None]] = None,
            max_bytes: Optional[int] = None, signature: Optional[str] = None,
            identity: Optional[dict] = None) -> dict:
        path = Path(path)
        # `identity` comes from the catalog in the integrated service. Without it a
        # run would re-register the file under this pipeline's fallback root, which
        # is how two ids for one file used to appear.
        generation = self.ensure_generation(path, file_id, identity=identity)
        tag = signature or _signature_of(profile)
        target = shard_path(self.index_root, generation.file_id, generation.generation, tag)
        writer = ShardWriter(target, file_id=generation.file_id, generation=generation.generation,
                             signature=tag, meta={'content_sig': generation.content_sig}).open()

        checkpoint = writer.checkpoint('parsed') or {}
        stats = {'bytes': int(checkpoint.get('bytes') or 0), 'records': int(checkpoint.get('records') or 0),
                 'lines': int(checkpoint.get('lines') or 0),
                 'batches': 0, 'chunks': 0, 'restarted_from': int(checkpoint.get('bytes') or 0)}
        descriptor = None
        try:
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            info = os.fstat(descriptor)
            if info.st_size != generation.size:
                raise PipelineError('generation_conflict', '文件在建立索引前已改变大小。',
                                    expected=generation.size, actual=info.st_size)
            offset = self._prepare_resume(job_id, descriptor, checkpoint, stats, profile)
            rules_failed = False
            carry = b''
            while True:
                if not carry:
                    if max_bytes is not None and offset >= max_bytes:
                        break
                    want = self.chunk_size
                    if max_bytes is not None:
                        want = min(want, max_bytes - offset)
                    carry = os.read(descriptor, want)
                    if not carry:
                        break
                    if generation.size < offset + len(carry):
                        raise PipelineError('generation_conflict', '文件比建立索引时更短。',
                                            expected=generation.size, read=offset + len(carry))
                stats['chunks'] += 1
                eof = offset + len(carry) >= generation.size
                try:
                    reply = self.pool.parse(job_id, token, carry, eof=eof, profile=profile,
                                            offset=offset + len(carry))
                except PoolError as exc:
                    raise PipelineError(exc.code, exc.message, **exc.details)
                consumed = int(reply.get('consumed', len(carry)))
                if consumed <= 0:
                    raise PipelineError('no_progress', '解析进程未消费任何字节。')
                offset += consumed
                # Keep the unanswered tail in memory: re-reading it from the file
                # would hit the page cache anyway and only makes the byte accounting
                # misleading.
                carry = carry[consumed:]
                eof = eof and not carry
                records = reply.get('records') or []
                for record in records:
                    # The worker names records after the job it was given; the shard
                    # names them after the *file*, so a re-index keeps the same ids
                    # and a record id stays resolvable across runs.
                    record['file_id'] = generation.file_id
                    record['record_id'] = '%s:%d' % (generation.file_id, record.get('ordinal', 0))
                if any(record.get('rules_failed') for record in records):
                    rules_failed = True
                stats['records'] += len(records)
                stats['batches'] += 1
                stats['bytes'] = offset
                framer_state = reply.get('checkpoint')
                if framer_state:
                    stats['lines'] = int(framer_state.get('line_no') or stats['lines'])
                watermark = {'bytes': offset, 'records': stats['records'],
                             'lines': stats['lines'], 'eof': eof}
                if framer_state:
                    # Persisted with the batch, in the same transaction: the
                    # framer's ordinal/offset/pending bytes are part of the
                    # watermark, not a side note.
                    watermark['framer'] = framer_state
                writer.commit_batch(records, watermark, stage='parsed')
                self.stats['chunks'] += 1
                self.stats['bytes'] += consumed
                self.stats['records'] += len(records)
                self.stats['batches'] += 1
                if on_progress:
                    on_progress({'bytes': offset, 'records': stats['records'],
                                 'total': generation.size, 'job': job_id})
                if eof:
                    break
            self.pool.reset(job_id)
            # Confirm the file did not change underneath the read.
            _, _, head, tail = self.sample(path)
            if content_signature(path.stat().st_size, path.stat().st_mtime_ns, head, tail) != generation.content_sig:
                raise PipelineError('generation_conflict', '读取期间文件被改写，本次索引未完成。',
                                    file_id=generation.file_id, generation=generation.generation)
            state = 'partial' if rules_failed else 'indexed'
            with self.store.transaction() as db:
                db.execute('UPDATE generations SET state=? WHERE file_id=? AND generation=?',
                           (state, generation.file_id, generation.generation))
            return {
                'file_id': generation.file_id,
                'generation': generation.generation,
                'state': state,
                'reason': 'rules_failed' if rules_failed else None,
                'shard': str(target),
                'stats': stats,
                'content_sig': generation.content_sig,
            }
        except (ShardError, OSError) as exc:
            code = getattr(exc, 'code', 'io_error')
            raise PipelineError(code, str(exc))
        finally:
            writer.close()
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    # ------------------------------------------------------------------ helpers
    def _prepare_resume(self, job_id: str, descriptor: int, checkpoint: dict, stats: dict,
                        profile: Optional[dict]) -> int:
        """Put the worker's framer back where the shard says it stopped.

        There are two states a shard can be in:

        * **a framer checkpoint was stored with the batch** — the worker gets its
          pending bytes, open record, ordinal and absolute offset back, so the
          run continues byte-for-byte and record-for-record.
        * **only a byte watermark exists** (a shard from before this checkpoint
          was introduced) — skip to the next line boundary and continue numbering
          after the committed records. Weaker, still monotonic, and never
          duplicates an ordinal.
        """
        start = int(checkpoint.get('bytes') or 0)
        framer_state = checkpoint.get('framer')
        if start and framer_state:
            reply = self.pool.restore(job_id, framer_state,
                                      start_ordinal=stats['records'], profile=profile)
            position = int(reply.get('position') or start)
            stats['bytes'] = position
            os.lseek(descriptor, position, os.SEEK_SET)
            return position
        if start:
            offset = self._align_to_line(descriptor, start)
            self.pool.restore(job_id, None, start_ordinal=stats['records'],
                              start_position=offset, start_line=stats['lines'], profile=profile)
            stats['bytes'] = offset
            os.lseek(descriptor, offset, os.SEEK_SET)
            return offset
        self.pool.restore(job_id, None, start_ordinal=0, profile=profile)
        os.lseek(descriptor, 0, os.SEEK_SET)
        stats['bytes'] = 0
        return 0

    def _align_to_line(self, descriptor: int, offset: int) -> int:
        """
        A checkpoint is a byte offset, which can land mid-line. Resume skips to
        the next newline so the framer starts on a boundary; the skipped bytes
        belong to a record already committed in the previous batch.
        """
        if offset <= 0:
            return 0
        os.lseek(descriptor, offset - 1, os.SEEK_SET)
        if os.read(descriptor, 1) == b'\n':
            return offset
        position = offset
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                return position
            finder = block.find(b'\n')
            if finder >= 0:
                return position + finder + 1
            position += len(block)

    def verify_shard(self, file_id: str, generation: int, signature: str = 'default') -> dict:
        from .shards import ShardReader
        reader = ShardReader(shard_path(self.index_root, file_id, generation, signature))
        return {'count': reader.count(), 'checkpoint': reader.checkpoint('parsed'),
                'meta': reader.meta()}


def _signature_of(profile: Optional[dict]) -> str:
    if not profile:
        return 'default'
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return 'p' + digest.hexdigest()[:12]
