"""Rule management for R6: semantic signatures, impact preview, trial run, apply (R6-081).

Three questions this module answers, and only these:

1. **What does this draft change?** `POST /api/v2/rules/impact` compares the draft's
   semantic signatures with what each file's published `rules`/`search` pointer was
   built from. It reads the cache registry; it does not re-parse anything. A colour,
   shape or name change is absent from both signatures, so it reports zero affected
   files instead of queuing a rebuild that would produce identical output.

2. **What would it match?** `POST /api/v2/rules/trial` frames a *user-supplied*
   sample through the same Node worker the index uses and runs the draft profile over
   it. The sample never comes from a local file: the server has no way to read one, so
   "try my local log" cannot silently upload it.

3. **Apply and rebuild what is affected.** `POST /api/v2/rules/{id}/apply` saves with
   an optimistic revision check and queues index jobs for the files the impact report
   named. The jobs run under the existing layered cache, so `structure` is reused and
   only the derived layers are rebuilt; the published pointer switches only after the
   new version is complete, which keeps the old evidence readable meanwhile.

The impact number is a prediction from signatures, not a measurement of how many
records will change. It is labelled `affected_files`, never "changed records".
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Optional

from ..indexing.cache_versions import (CacheRegistry, deps_for,
                                       signature as layer_signature)
from ..indexing.node_pool import PoolError

SCHEMA = 'logbench.rules/1'
#: Impact scans are bounded: an unreachable conclusion ("all 40 000 files") is worse
#: than an explicit `truncated: true`.
MAX_IMPACT_FILES = 5000
MAX_TRIAL_BYTES = 256 * 1024
TRIAL_RECORD_LIMIT = 50
APPLY_JOB_CAP = 50
REBUILD_LAYERS = ('rules', 'search')

STATUS = {'unknown_layer': 400, 'invalid_profile': 400, 'missing_profile': 400,
          'missing_rule': 404, 'revision_conflict': 409, 'node_unavailable': 503,
          'sample_too_large': 413, 'no_sample': 400, 'trial_failed': 400,
          'nothing_affected': 409}


class RulesError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def canonical(profile: dict) -> str:
    return json.dumps(profile, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def profile_digest(profile: dict) -> str:
    return hashlib.sha256(canonical(profile).encode('utf-8')).hexdigest()[:32]


class RulesApi:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.registry = CacheRegistry(app.store)
        self.pool = app.index_jobs.pool
        self._signature_cache: dict = {}

    # ------------------------------------------------------------------ routing
    def route(self, method: str, path: str, q, payload: dict, actor: dict):
        from ..app import fail
        try:
            return self._route(method, path, q, payload, actor)
        except RulesError as exc:
            fail(STATUS.get(exc.code, 400), exc.code, exc.message, **exc.details)

    def _route(self, method: str, path: str, q, payload: dict, actor: dict):
        from ..app import fail, text
        if path == '/api/v2/rules':
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '规则列表只支持读取；编辑请使用 apply。')
            return self.list(actor, include_signatures=q('signatures') == '1')
        if path == '/api/v2/rules/impact':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '影响预览只支持 POST。')
            self.app.require_admin(actor)
            return self.impact(actor, payload)
        if path == '/api/v2/rules/trial':
            if method != 'POST':
                fail(405, 'METHOD_NOT_ALLOWED', '试跑只支持 POST。')
            self.app.require_admin(actor)
            return self.trial(actor, payload)
        if path.startswith('/api/v2/rules/'):
            rest = path[len('/api/v2/rules/'):]
            rule_id, _, action = rest.partition('/')
            if action == 'apply':
                if method != 'POST':
                    fail(405, 'METHOD_NOT_ALLOWED', '应用规则只支持 POST。')
                self.app.require_admin(actor)
                return self.apply(actor, rule_id, payload)
            if action and action != 'signature':
                fail(404, 'NOT_FOUND', '规则接口不存在。')
            if method not in ('GET', 'HEAD'):
                fail(405, 'METHOD_NOT_ALLOWED', '规则只支持读取。')
            return self.get(actor, rule_id)
        fail(404, 'NOT_FOUND', '规则接口不存在。')

    # -------------------------------------------------------------------- read
    def _rows(self, include_deleted: bool = False) -> list:
        sql = 'SELECT * FROM rules WHERE deleted_at IS ' + ('NOT NULL' if include_deleted else 'NULL') + ' ORDER BY name,id'
        with self.store.connect() as db:
            return db.execute(sql).fetchall()

    def list(self, actor: dict, include_signatures: bool = False) -> dict:
        items = [self._public_rule(row, include_signatures) for row in self._rows()]
        return {'schema': SCHEMA, 'items': items,
                'semantic_source': 'node' if self.pool.available else 'unavailable'}

    def get(self, actor: dict, rule_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
        if not row:
            raise RulesError('missing_rule', '规则不存在。', ruleId=rule_id)
        return {'item': self._public_rule(row, True)}

    def _public_rule(self, row, include_signatures: bool = False) -> dict:
        profile = json.loads(row['profile'])
        item = {
            'ruleId': row['id'], 'name': row['name'], 'description': row['description'],
            'revision': row['revision'], 'updatedAt': row['updated_at'],
            'updatedBy': row['updated_by'], 'deletedAt': row['deleted_at'],
            'profile': profile,
            'signatures': None, 'signatureError': None,
            'semantic': self._semantic_summary(profile),
        }
        if not include_signatures:
            # Signatures start the Node pool. A rule *list* must not spawn a worker
            # as a side effect of being viewed; the page asks for them explicitly.
            return item
        try:
            item['signatures'] = self.signatures(profile)
        except RulesError as exc:
            # A rule list must still render when the Node runtime is missing; the
            # page then shows why impact is unavailable instead of a green "0".
            item['signatureError'] = {'code': exc.code, 'message': exc.message}
        return item

    @staticmethod
    def _semantic_summary(profile: dict) -> dict:
        def enabled(rows):
            return [row for row in (rows or []) if row.get('enabled') is not False]
        extractors = enabled(profile.get('extractors'))
        anomalies = enabled(profile.get('anomalies'))
        nodes = enabled(profile.get('nodes'))
        return {
            'extractors': len(extractors),
            'anomalies': len(anomalies),
            'nodes': len(nodes),
            'projection_version': profile.get('projection_version') or 'v1',
        }

    # -------------------------------------------------------------- signatures
    def signatures(self, profile: dict) -> dict:
        """The one implementation both sides use, via the Node pool."""
        validate_profile(profile)
        key = profile_digest(profile)
        if key in self._signature_cache:
            return dict(self._signature_cache[key])
        if not self.pool.available:
            raise RulesError('node_unavailable', '未找到 Node 运行时，规则语义签名无法计算。')
        try:
            reply = self.pool.signatures('signature:rules:%s' % key, profile)
        except PoolError as exc:
            raise RulesError('node_unavailable', '规则签名计算失败：%s' % exc.message,
                             code_detail=getattr(exc, 'code', 'pool_error'))
        values = dict(reply.get('signatures') or {})
        if not values:
            raise RulesError('node_unavailable', '规则签名计算没有返回值。')
        self._signature_cache[key] = values
        return values

    # ------------------------------------------------------------------ impact
    def impact(self, actor: dict, body: dict) -> dict:
        profile = self._profile_from(body)
        semantics = self.signatures(profile)
        rows = self._visible_indexed_files(actor, body.get('fileIds'))
        files = []
        totals = {'visible': len(rows), 'indexed': 0, 'affected': 0,
                  'rules_affected': 0, 'search_affected': 0, 'not_indexed': 0,
                  'already_current': 0}
        scanned = 0
        truncated = False
        for row in rows:
            if scanned >= MAX_IMPACT_FILES:
                truncated = True
                break
            scanned += 1
            outcome = self._file_impact(actor, row, semantics)
            if outcome['state'] == 'not_indexed':
                totals['not_indexed'] += 1
            else:
                totals['indexed'] += 1
                if outcome['layers']:
                    totals['affected'] += 1
                else:
                    totals['already_current'] += 1
                if 'rules' in outcome['layers']:
                    totals['rules_affected'] += 1
                if 'search' in outcome['layers']:
                    totals['search_affected'] += 1
            files.append(outcome)
        return {
            'schema': SCHEMA,
            'profile_digest': profile_digest(profile),
            'signatures': semantics,
            'layers': {
                'structure': {'affected': 0,
                              'reason': '结构层依赖分帧/运行时/编码，与规则语义无关，规则变更不重分帧。'},
                'rules': {'affected': totals['rules_affected']},
                'search': {'affected': totals['search_affected']},
            },
            'totals': totals,
            'files': files[:200],
            'truncated': truncated or len(files) > 200,
            'scan_limit': MAX_IMPACT_FILES,
            'notes': [
                '受影响文件数由语义签名预测，不代表命中记录数会变化。',
                '未建索引的文件无法预测影响，单独计数，不计入受影响。',
            ],
        }

    def _profile_from(self, body: dict) -> dict:
        profile = body.get('profile')
        if profile is None and body.get('ruleId'):
            with self.store.connect() as db:
                row = db.execute('SELECT profile FROM rules WHERE id=?', (body['ruleId'],)).fetchone()
            if not row:
                raise RulesError('missing_rule', '规则不存在。', ruleId=body['ruleId'])
            profile = json.loads(row['profile'])
        if not isinstance(profile, dict):
            raise RulesError('missing_profile', '影响预览需要 profile 或 ruleId。')
        validate_profile(profile)
        return profile

    def _visible_indexed_files(self, actor: dict, file_ids: Optional[list]) -> list:
        """Files this principal may see, with their current layer pointers."""
        wanted = None
        if isinstance(file_ids, list) and file_ids:
            wanted = [str(item) for item in file_ids[:MAX_IMPACT_FILES]]
        sql = ("SELECT f.id AS file_id, f.display_name, f.rel_path, f.scope, f.owner, "
               "r.label AS root_label FROM logical_files f "
               "JOIN storage_roots r ON r.id=f.root_id "
               "WHERE f.state<>'trashed' AND (f.scope='shared' OR f.owner=?)")
        args = [actor['id']]
        if actor['role'] != 'admin':
            # A viewer keeps the shared read path; private rows are already owner-bound.
            args = [actor['id']]
        if wanted:
            sql += ' AND f.id IN (%s)' % ','.join('?' * len(wanted))
            args.extend(wanted)
        sql += ' ORDER BY f.root_id, f.rel_path LIMIT ?'
        args.append(MAX_IMPACT_FILES + 1)
        with self.store.connect() as db:
            return db.execute(sql, args).fetchall()

    def _pointers(self, file_id: str, owner: str) -> dict:
        out = {}
        for layer in ('structure', 'rules', 'search'):
            pointer = self.registry.pointer(file_id, layer)
            if pointer:
                out[layer] = pointer
        return out

    def _file_impact(self, actor: dict, row, semantics: dict) -> dict:
        file_id = row['file_id']
        pointers = self._pointers(file_id, actor['id'])
        structure = pointers.get('structure')
        item = {
            'fileId': file_id, 'displayName': row['display_name'],
            'rootLabel': row['root_label'], 'scope': row['scope'],
            'layers': [], 'state': 'indexed',
        }
        if not structure:
            item['state'] = 'not_indexed'
            item['reason'] = '该文件尚无结构索引，规则影响无法预测。'
            return item
        # The candidate signature is computed with the *same* dependency record the
        # index service will use, so "unaffected" here means "the job would reuse it".
        candidate_rules = layer_signature('rules', deps_for(
            'rules', structure_signature=structure.get('signature'),
            rule_structure=semantics['structure'], rule_extract=semantics['extract'],
            engine='rules/1'))
        current_rules = (pointers.get('rules') or {}).get('signature')
        if current_rules != candidate_rules:
            item['layers'].append('rules')
        candidate_search = layer_signature('search', deps_for(
            'search', rules_signature=candidate_rules, projection='grams/v2', gram_size=3))
        current_search = (pointers.get('search') or {}).get('signature')
        if current_search != candidate_search:
            item['layers'].append('search')
        item['current'] = {
            'structure': structure.get('signature'),
            'rules': current_rules, 'search': current_search,
        }
        item['candidate'] = {'rules': candidate_rules, 'search': candidate_search}
        return item

    # ------------------------------------------------------------------- trial
    def trial(self, actor: dict, body: dict) -> dict:
        profile = self._profile_from(body)
        sample = body.get('sample')
        if not isinstance(sample, str) or not sample.strip():
            raise RulesError('no_sample', '试跑需要手动输入的样本文本。')
        data = sample.encode('utf-8')
        if len(data) > MAX_TRIAL_BYTES:
            raise RulesError('sample_too_large', '样本不能超过 256 KiB。',
                             bytes=len(data), limit=MAX_TRIAL_BYTES)
        if not self.pool.available:
            raise RulesError('node_unavailable', '未找到 Node 运行时，无法执行试跑。')
        job = 'trial:%s' % secrets.token_hex(6)
        # The sample is framed by the same worker the index uses, so what the trial
        # shows is what the pipeline would produce, not a second guess at it.
        try:
            reply = self.pool.parse(job, 0, data, eof=True, profile=profile)
        except PoolError as exc:
            raise RulesError('trial_failed', '试跑失败：%s' % exc.message,
                             code_detail=getattr(exc, 'code', 'pool_error'))
        finally:
            try:
                self.pool.reset(job)
            except (PoolError, OSError):
                pass
        records = reply.get('records') or []
        limit = TRIAL_RECORD_LIMIT
        try:
            limit = max(1, min(TRIAL_RECORD_LIMIT, int(body.get('limit') or TRIAL_RECORD_LIMIT)))
        except (TypeError, ValueError):
            limit = TRIAL_RECORD_LIMIT
        shown = [self._trial_record(record) for record in records[:limit]]
        return {
            'schema': SCHEMA,
            'framed_records': len(records),
            'returned': len(shown),
            'truncated': len(records) > len(shown),
            'records': shown,
            'rules_failed': any(record.get('rules_failed') for record in records),
            'note': '试跑只使用你粘贴的样本文本；不会读取或上传任何本机文件。',
        }

    @staticmethod
    def _trial_record(record: dict) -> dict:
        def evidence(items):
            out = []
            for item in (items or [])[:6]:
                out.append({'field': item.get('field'), 'value': item.get('value'),
                            'line': item.get('line'), 'condition': item.get('condition')})
            return out
        return {
            'ordinal': record.get('ordinal'),
            'ts': record.get('ts'), 'level': record.get('level'),
            'message': (record.get('message') or '')[:400],
            'body_lines': len(record.get('body') or []),
            'fields': [[field[0], field[1]] for field in (record.get('fields') or [])[:20]],
            'issue': record.get('issue'), 'issue_severity': record.get('issueSeverity'),
            'issues': [{'name': item.get('name'), 'severity': item.get('severity'),
                        'condition': item.get('condition'),
                        'evidence': evidence(item.get('evidence'))}
                       for item in (record.get('issueMatches') or [])[:6]],
            'nodes': [{'name': item.get('name'), 'type': item.get('type'),
                       'combine': item.get('combine'),
                       'evidence': evidence(item.get('evidence'))}
                      for item in (record.get('nodeMatches') or [])[:6]],
            'rules_failed': record.get('rules_failed'),
        }

    # ------------------------------------------------------------------- apply
    def apply(self, actor: dict, rule_id: str, body: dict) -> dict:
        from ..app import compact, fail, integer, now
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
        if not row or row['deleted_at'] is not None:
            raise RulesError('missing_rule', '规则不存在或已删除。', ruleId=rule_id)
        profile = self._profile_from(body)
        expected = integer(body.get('expectedRevision'), '版本号')
        if expected != row['revision']:
            raise RulesError('revision_conflict', '该规则已被其他管理员修改，请刷新后重试。',
                             expected=expected, current=row['revision'])
        impact = self.impact(actor, {'profile': profile})
        targets = [item['fileId'] for item in impact['files']
                   if item.get('layers') and item['state'] == 'indexed']
        requested = body.get('fileIds')
        if isinstance(requested, list) and requested:
            wanted = {str(item) for item in requested}
            targets = [file_id for file_id in targets if file_id in wanted]
        targets = targets[:APPLY_JOB_CAP]
        name = body.get('name') or row['name']
        description = body.get('description')
        if description is None:
            description = row['description']
        stored = dict(profile)
        stored['name'] = name
        with self.store.transaction() as db:
            db.execute('UPDATE rules SET name=?,description=?,profile=?,revision=revision+1,'
                       'updated_at=?,updated_by=? WHERE id=?',
                       (name, description, compact(stored), now(), actor['id'], rule_id))
            self.store.audit(db, actor['id'], 'rule.apply', rule_id,
                             {'revision': expected, 'affected': impact['totals']['affected'],
                              'queued': len(targets)})
        jobs = []
        actions = body.get('actionId')
        for file_id in targets:
            action_key = None
            if isinstance(actions, str) and actions.strip():
                action_key = '%s:%s' % (actions.strip()[:96], file_id)
            try:
                created = self.app.index_jobs.create(actor, {'fileId': file_id,
                                                             'profile': stored,
                                                             'actionId': action_key})
                jobs.append({'fileId': file_id, 'jobId': created['item']['id']})
            except Exception as exc:  # noqa: BLE001 - one file must not abort the rest
                jobs.append({'fileId': file_id, 'error': str(exc)[:200]})
        return {
            'schema': SCHEMA,
            'rule': {'ruleId': rule_id, 'name': name, 'revision': expected + 1},
            'impact': {'totals': impact['totals'], 'truncated': impact['truncated']},
            'queued': len([job for job in jobs if job.get('jobId')]),
            'jobs': jobs,
            'capped': len([item for item in impact['files']
                           if item.get('layers') and item['state'] == 'indexed']) > APPLY_JOB_CAP,
        }


def validate_profile(profile: dict):
    """Reuse the R5 structural validator: one schema, two entry points."""
    from ..r5_schema import validate
    try:
        validate(profile)
    except (ValueError, TypeError) as exc:
        raise RulesError('invalid_profile', str(exc))
