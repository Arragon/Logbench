"""Index jobs: authorized file in, layered caches out (R6-038).

The HTTP handler only validates and creates a job; the work runs off the request
thread under a lease, and progress is written as a job checkpoint so a page can
watch it without polling the filesystem.

Three deliberate constraints:

* the path goes through `Application.normal_path`, so the R5 file cage (no `..`,
  no absolute escapes, no symlink traversal) still applies;
* the shard tag is derived from the **semantic** rule signature, not from a hash
  of the whole profile — otherwise recolouring a node would point the reader at a
  different shard and re-parse the file for no reason;
* the job does not report `indexed` when a layer failed to publish. It goes
  `failed` with the cache code (`coverage_mismatch`, `generation_conflict`, …),
  because claiming success while the reader silently falls back to a slow path is
  worse than an honest failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .cache_versions import CacheError, CacheRegistry, deps_for
from .layers import build_rules_layer, build_search_layer
from .node_pool import NodePool, PoolError
from .pipeline import Pipeline, PipelineError
from .shards import ShardError, shard_path

RUNTIME_TAG = 'node-managed'
DEFAULT_RULES = ('rules', 'cpqpsgx-default.json')
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class IndexService:
    def __init__(self, app, *, workers: int = 1, background: bool = True,
                 index_root: Optional[Path] = None, entry: Optional[Path] = None):
        self.app = app
        self.store = app.store
        from ..jobs.repository import JobRepository
        self.repo = JobRepository(self.store)
        self.registry = CacheRegistry(self.store)
        self.index_root = Path(index_root or (app.config.state_dir / 'index'))
        self.entry = Path(entry or (PACKAGE_ROOT / 'src' / 'workers' / 'server-engine.cjs'))
        self.workers = max(1, workers)
        self.pool = NodePool(self.entry, workers=self.workers, request_timeout=120)
        self.pipeline = Pipeline(self.store, self.pool, self.index_root)
        self.lock = threading.RLock()
        self.executor = (ThreadPoolExecutor(max_workers=1, thread_name_prefix='lb-index')
                         if background else None)
        self.failures: list = []
        self._defaults: Optional[dict] = None

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> dict:
        report = {'pool': {}, 'executor': False}
        if self.executor is not None:
            self.executor.shutdown(wait=False)
            report['executor'] = True
        try:
            report['pool'] = self.pool.close(drain=False)
        except (OSError, PoolError):
            report['pool'] = {'error': 'close_failed'}
        return report

    # --------------------------------------------------------------------- create
    def create(self, actor: dict, body: dict) -> dict:
        from ..app import fail, text
        file_id = body.get('fileId')
        rel = None
        if file_id:
            # Multi-root: the caller names a file, never a path. The catalog
            # resolves the root and the authorized relative path.
            meta = self.app.storage.catalog.metadata(actor, file_id)
            if meta['scope'] != 'private':
                self.app.require_admin(actor)
            if meta['rootState'] != 'online':
                fail(409, 'STORAGE_ROOT_OFFLINE', '存储位置离线，无法建立索引。')
            root = self.app.storage.roots.get(meta['rootId'])
            path = self.app.storage.guard.resolve(root, meta['relPath'], want='file')
            rel = meta['relPath'] if meta['rootId'] == 'legacy' else None
        else:
            self.app.require_admin(actor)
            rel = text(body.get('path'), '文件路径', 2048)
            path = self.app.safe_path(rel)
            if path.is_dir():
                fail(400, 'INVALID_PATH', '只能为文件建立索引。')
        action_key = body.get('actionId')
        if action_key is not None and not isinstance(action_key, str):
            fail(400, 'INVALID_INPUT', 'actionId 必须是字符串。')
        profile = body.get('profile')
        if profile is not None and not isinstance(profile, dict):
            fail(400, 'INVALID_INPUT', 'profile 必须是对象。')
        # One registry decides file ids: the catalog owns (root, relative path) →
        # id, and the pipeline is told the identity instead of inventing a second
        # one from an absolute path.
        identity = self.app.storage.catalog.register_path(path)
        generation = self.pipeline.ensure_generation(path, file_id=identity['file_id'],
                                                     identity=identity)
        # The `raw` layer doubles as the access record: it names the file for this
        # principal, carries the authorized relative path, and is never collected.
        raw = self.registry.ensure(file_id=generation.file_id, layer='raw', owner=actor['id'],
                                   generation=generation.generation,
                                   deps=deps_for('raw', content_sig=generation.content_sig,
                                                 rel_path=rel or identity['rel_path'],
                                                 generation=generation.generation))
        if not raw['reused']:
            self.registry.publish(raw['version_id'], rows=1,
                                  digest=_digest(generation.content_sig),
                                  stats={'bytes': generation.size, 'records': 1})
        job = self.repo.create('index', actor['id'],
                               {'path': str(path), 'file_id': generation.file_id,
                                'generation': generation.generation,
                                'content_sig': generation.content_sig,
                                'root_id': identity['root_id'],
                                'rel_path': identity['rel_path'],
                                'profile': profile},
                               action_key=action_key)
        if self.executor is not None:
            self.executor.submit(self._run_quietly, job.id)
        return {'item': self.app.jobs.get(actor, job.id, include_payload=True), 'accepted': True}

    def status(self, actor: dict, job_id: str) -> dict:
        job = self.app.jobs.get(actor, job_id)
        layers = self.registry.versions(file_id=(job.get('payload') or {}).get('file_id'),
                                        owner=actor['id'])
        return {'item': job, 'layers': [_public(row) for row in layers]}

    def _run_quietly(self, job_id: str):
        try:
            self.run_now(job_id)
        except (PipelineError, PoolError, ShardError, CacheError) as exc:
            # `run_now` already recorded the failure on the job; this only keeps
            # the executor from swallowing it silently.
            self.failures.append({'job': job_id, 'code': getattr(exc, 'code', 'index_failed')})

    # ------------------------------------------------------------------------ run
    def run_now(self, job_id: str) -> dict:
        """Run one index job to completion. Safe to call directly in tests."""
        job = self.repo.get(job_id)
        if job is None:
            raise CacheError('unknown_job', '任务不存在。', job_id=job_id)
        payload = job.payload or {}
        path = Path(payload['path'])
        file_id = payload['file_id']
        owner = job.principal
        generation = int(payload['generation'])
        lease = self.repo.claim(job_id, 'index:%d' % os.getpid(), ttl=600)
        try:
            # Everything that can fail now happens under the lease, so the job is
            # never left `running` because a worker could not even be started.
            profile = payload.get('profile') or self.default_profile()
            semantics = self.pool.signatures('signature:%s' % job_id, profile)['signatures']
            tag = 'r-%s-%s' % (semantics['structure'], semantics['extract'])
            self._transition(job_id, 'reading')
            # The framing phase always runs, even on a cache hit: it is the state
            # the transition table allows, and a job that skips a state cannot
            # reach `indexed` afterwards.
            self._transition(job_id, 'framing')
            structure = self.registry.ensure(
                file_id, 'structure', owner=owner, generation=generation,
                deps=deps_for('structure', content_sig=payload.get('content_sig'),
                              generation=generation, framing='r6-009', runtime=RUNTIME_TAG,
                              schema='r6-shard/2', encoding='utf-8'))
            shard = shard_path(self.index_root, file_id, generation, tag)
            if structure['reused']:
                # A rule-only rebuild must read the shard the *published structure
                # version* wrote. Recomputing the path from the current rule tag
                # produces a shard tag that does not exist yet — that is exactly the
                # "changing a rule re-parses the file (or fails)" bug this guard
                # removes.
                pointer = self.registry.pointer(file_id, 'structure')
                published = Path(str((pointer or {}).get('stats', {}).get('shard') or ''))
                if not published.is_file():
                    raise PipelineError('structure_shard_missing',
                                        '结构层已发布但分片缺失，需要重建结构层。',
                                        file_id=file_id)
                shard = published
            else:
                result = self.pipeline.run(
                    job_id, path, file_id=file_id, profile=profile, signature=tag,
                    identity={'file_id': file_id, 'generation': generation,
                              'content_sig': payload.get('content_sig'),
                              'size': Path(path).stat().st_size},
                    on_progress=lambda event: self._progress(lease, 'framing', event))
                shard = Path(result['shard'])
                self.registry.publish(
                    structure['version_id'], rows=result['stats']['records'],
                    digest=_digest(result['content_sig']),
                    stats={'parses': result['stats']['chunks'], 'bytes': result['stats']['bytes'],
                           'records': result['stats']['records'], 'source': 'parse',
                           'shard': str(shard)},
                    expected_rows=result['stats']['records'])

            self._transition(job_id, 'indexing')
            rules = self.registry.ensure(
                file_id, 'rules', owner=owner, generation=generation,
                deps=deps_for('rules', structure_signature=structure['signature'],
                              rule_structure=semantics['structure'],
                              rule_extract=semantics['extract'], engine='rules/1'))
            if not rules['reused']:
                built = build_rules_layer(shard, source_path=path, pool=self.pool,
                                          profile=profile, version=rules['version_id'],
                                          job='job:%s' % job_id)
                self._progress(lease, 'indexing', {'layer': 'rules', 'records': built['rows']})
                self.registry.publish(rules['version_id'], rows=built['rows'], digest=built['digest'],
                                      stats=built['stats'], expected_rows=built['rows'])
            search = self.registry.ensure(
                file_id, 'search', owner=owner, generation=generation,
                deps=deps_for('search', rules_signature=rules['signature'],
                              projection='grams/v2', gram_size=3))
            if not search['reused']:
                built = build_search_layer(shard, version=search['version_id'],
                                           rules_version=rules['version_id'])
                self.registry.publish(search['version_id'], rows=built['rows'],
                                      digest=built['digest'], stats=built['stats'])
            finished = self.repo.finish(lease, 'indexed', progress={
                'phase': 'indexed',
                'layers': {'structure': structure['version_id'], 'rules': rules['version_id'],
                           'search': search['version_id']},
                'reused': {'structure': structure['reused'], 'rules': rules['reused'],
                           'search': search['reused']}})
            return {'job': finished.id, 'state': finished.state, 'shard': str(shard),
                    'layers': {'structure': structure['version_id'], 'rules': rules['version_id'],
                               'search': search['version_id']},
                    'reused': {'structure': structure['reused'], 'rules': rules['reused'],
                               'search': search['reused']}}
        except (PipelineError, PoolError, ShardError, CacheError) as exc:
            code = getattr(exc, 'code', 'index_failed')
            self.failures.append({'job': job_id, 'code': code, 'message': str(exc)})
            self._finish_failed(lease, code, str(exc))
            raise
        except BaseException as exc:  # noqa: BLE001 - a job must not stay `running`
            self.failures.append({'job': job_id, 'code': 'internal', 'message': repr(exc)})
            self._finish_failed(lease, 'internal', repr(exc))
            raise

    # -------------------------------------------------------------------- helpers
    def default_profile(self) -> Optional[dict]:
        for base in (self.app.config.code_root, PACKAGE_ROOT):
            path = Path(base).joinpath(*DEFAULT_RULES)
            if path.is_file():
                return json.loads(path.read_text('utf-8'))
        return None

    def default_signatures(self) -> dict:
        if self._defaults is None:
            profile = self.default_profile()
            if profile is None:
                return {'structure': None, 'extract': None, 'search': None}
            self._defaults = self.pool.signatures('signature:default', profile)['signatures']
        return self._defaults

    def _transition(self, job_id: str, state: str):
        try:
            self.repo.set_state(job_id, state)
        except Exception:  # noqa: BLE001 - a resubmitted job may already be past it
            current = self.repo.get(job_id)
            if current is None or current.state in ('indexed', 'failed', 'cancelled'):
                raise

    def _progress(self, lease, stage: str, event: dict):
        self.repo.checkpoint(lease, stage, dict(event))

    def _finish_failed(self, lease, code: str, message: str):
        try:
            self.repo.finish(lease, 'failed', error={'code': code, 'message': message[:400]})
        except Exception:  # noqa: BLE001 - already terminal, nothing left to record
            pass


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _public(row: dict) -> dict:
    return {key: row.get(key) for key in ('version_id', 'layer', 'generation', 'state', 'signature',
                                          'rows', 'stats', 'owner', 'created_at', 'published_at')}
