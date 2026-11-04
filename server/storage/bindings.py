"""Layered rule resolution for files and directories (R6-055).

The v3.0.3 chain was file → directory → default, all inside one root. R6 adds
two layers and one hard separation:

    personal file → personal directory → shared file → shared directory
                  → mapping default → service default

* **personal** bindings belong to one principal and are stored per namespace, so
  one user's "use this rule for that file" never changes what anyone else sees;
* **mapping defaults** come from a remote directory mapping and apply only to the
  files that mapping produced, never to every file that happens to share the
  path suffix;
* **a root is part of the key.** `/psg-a/sw/app.log` in the `main` root and in
  the `archive` root are two different files; inheriting one rule for both is
  exactly the bug the multi-root work exists to avoid.

The resolver always reports *why* a rule was chosen. A layer whose rule has been
deleted is reported as skipped with a reason instead of being silently dropped,
because "the rule I picked is gone, so you got the default" is information the
operator needs.
"""
from __future__ import annotations

from typing import Optional

from .guard import StorageError

LAYERS = ('personal_file', 'personal_directory', 'shared_file', 'shared_directory',
          'mapping_default', 'service_default')
SCOPES = ('file', 'directory', 'default', 'mapping')
LABELS = {
    'personal_file': '我的单文件指定',
    'personal_directory': '我的最近目录',
    'shared_file': '共享单文件指定',
    'shared_directory': '共享目录继承',
    'mapping_default': '采集映射默认',
    'service_default': '服务端默认规则',
}


def _stamp() -> str:
    from ..app import now
    return now()


class BindingService:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ------------------------------------------------------------------- read
    def _shared_rows(self, root_id: Optional[str] = None) -> list:
        """Shared bindings from both tables, normalised to one shape.

        `bindings` stays the v3.0.3 table (and the v1 API contract); rows in it
        belong to the legacy root only. `root_bindings` carries every other root,
        including a mapping default.
        """
        with self.store.connect() as db:
            legacy = db.execute('SELECT * FROM bindings').fetchall()
            extra = db.execute('SELECT * FROM root_bindings').fetchall()
        rows = []
        for row in legacy:
            rows.append({'scope': row['scope'], 'root_id': 'legacy', 'path': row['path'],
                         'rule_id': row['rule_id'], 'revision': row['revision'],
                         'updated_at': row['updated_at'], 'updated_by': 'legacy',
                         'table': 'bindings'})
        for row in extra:
            rows.append({'scope': row['scope'], 'root_id': row['root_id'], 'path': row['path'],
                         'rule_id': row['rule_id'], 'revision': row['revision'],
                         'updated_at': row['updated_at'], 'updated_by': row['updated_by'],
                         'table': 'root_bindings'})
        if root_id is not None:
            rows = [row for row in rows if row['root_id'] == root_id]
        return rows

    def personal_rows(self, actor: dict) -> list:
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM personal_bindings WHERE owner=? '
                              'ORDER BY scope,path', (actor['id'],)).fetchall()
        return [{'scope': row['scope'], 'root_id': row['root_id'], 'path': row['path'],
                 'rule_id': row['rule_id'], 'revision': row['revision'],
                 'updated_at': row['updated_at']} for row in rows]

    def rules(self) -> dict:
        with self.store.connect() as db:
            rows = db.execute('SELECT id,name,revision,deleted_at FROM rules').fetchall()
        return {row['id']: row for row in rows}

    # ---------------------------------------------------------------- resolve
    def resolve(self, actor: dict, *, root_id: str, rel_path: str,
                personal: Optional[list] = None) -> dict:
        """Return the effective rule plus the whole explanation chain.

        `personal` lets a browser pass its own overrides for *server* files when
        the server-side copy has not been written yet. It is never used for
        authorization: the rule it names must still exist and be readable, and the
        file is resolved from the database, not from the request.
        """
        from ..app import fail
        rel_path = (rel_path or '').strip('/')
        personal_rows = personal if personal is not None else self.personal_rows(actor)
        # A binding is root-scoped: the same relative path in another root is a
        # different file. The service default is the one global layer, so it is
        # collected separately instead of being filtered away with the root.
        personal_rows = [row for row in personal_rows if row.get('root_id') == root_id]
        all_shared = self._shared_rows()
        shared_rows = [row for row in all_shared if row['root_id'] == root_id]
        default_rows = [row for row in all_shared if row['scope'] == 'default']
        rules = self.rules()
        chain = []
        chosen = None

        def consider(layer, scope, rows, match):
            nonlocal chosen
            matched = [row for row in rows if row['scope'] == scope and match(row['path'])]
            if not matched:
                chain.append({'layer': layer, 'label': LABELS[layer], 'matched': False,
                              'scope': scope, 'path': None, 'ruleId': None,
                              'revision': None, 'reason': 'no_binding'})
                return
            best = max(matched, key=lambda row: len(row['path'] or ''))
            rule = rules.get(best['rule_id'])
            if rule is None or rule['deleted_at']:
                chain.append({'layer': layer, 'label': LABELS[layer], 'matched': False,
                              'scope': scope, 'path': best['path'],
                              'ruleId': best['rule_id'], 'revision': best['revision'],
                              'reason': 'rule_missing'})
                return
            entry = {'layer': layer, 'label': LABELS[layer], 'matched': True,
                     'scope': scope, 'path': best['path'], 'ruleId': best['rule_id'],
                     'revision': best['revision'], 'rootId': best['root_id'],
                     'ruleRevision': rule['revision'], 'reason': None}
            chain.append(entry)
            if chosen is None:
                chosen = entry

        def is_file(path):
            return path == rel_path

        def is_ancestor(path):
            return path == '' or rel_path == path or rel_path.startswith(path + '/')

        consider('personal_file', 'file', personal_rows, is_file)
        consider('personal_directory', 'directory', personal_rows, is_ancestor)
        consider('shared_file', 'file', shared_rows, is_file)
        consider('shared_directory', 'directory', shared_rows, is_ancestor)
        consider('mapping_default', 'mapping', shared_rows, is_ancestor)
        consider('service_default', 'default', default_rows, lambda path: path == '')

        if chosen is None:
            # No extra "none" row: the chain is exactly the six layers, so the page
            # and the server print the same list. `rule: null` is the answer.
            return {'rule': None, 'source': None, 'scope': None, 'path': None,
                    'layer': None, 'chain': chain, 'rootId': root_id, 'ruleId': None,
                    'skipped': [], 'explanation': '没有适用的规则绑定。'}
        rule = rules[chosen['ruleId']]
        source = {'scope': chosen['scope'], 'path': chosen['path'],
                  'ruleId': chosen['ruleId'], 'revision': chosen['revision'],
                  'rootId': chosen['rootId']}
        if chosen['layer'].startswith('personal'):
            source['owner'] = actor['id']
        skipped = [entry for entry in chain
                   if entry.get('reason') == 'rule_missing']
        return {
            'rule': self.app.get_rule(chosen['ruleId']) if chosen['ruleId'] else None,
            'source': source, 'scope': chosen['scope'], 'path': chosen['path'],
            'layer': chosen['layer'], 'chain': chain, 'rootId': root_id,
            'skipped': skipped,
            'explanation': self._explain(chosen, skipped),
            'ruleId': rule['id'],
        }

    @staticmethod
    def _explain(chosen: dict, skipped: list) -> str:
        text = '%s：%s' % (LABELS.get(chosen['layer'], chosen['layer']),
                           chosen['path'] or '（根目录默认）')
        if skipped:
            text += '；%d 条更高优先级的绑定引用了已删除的规则，已跳过。' % len(skipped)
        return text

    # ------------------------------------------------------------------ write
    def _validate(self, payload: dict, *, personal: bool) -> dict:
        from ..app import fail, integer, text
        scope = payload.get('scope')
        if scope not in SCOPES:
            raise StorageError('invalid_scope', '绑定范围必须是 file、directory、mapping 或 default。')
        root_id = text(payload.get('rootId') or payload.get('root_id') or 'legacy',
                       '存储位置', 64)
        self._root(root_id)
        path = payload.get('path') or ''
        if scope == 'default':
            if path:
                raise StorageError('invalid_path', '默认规则的路径必须为空。')
            path = ''
        elif scope == 'mapping':
            # A mapping default is keyed by the mapping's target sub-directory.
            path = self._normal(path, allow_root=True)
        else:
            path = self._normal(path, allow_root=scope != 'file')
        expected = integer(payload.get('expectedRevision'), '版本号')
        deleted = bool(payload.get('_deleted'))
        rule_id = payload.get('ruleId')
        if not deleted:
            if not isinstance(rule_id, str) or not rule_id:
                raise StorageError('invalid_rule', '请选择一个规则。')
            with self.store.connect() as db:
                row = db.execute('SELECT deleted_at FROM rules WHERE id=?', (rule_id,)).fetchone()
            if row is None or row['deleted_at']:
                raise StorageError('rule_not_found', '规则不存在或已删除。', ruleId=rule_id)
        return {'scope': scope, 'root_id': root_id, 'path': path, 'rule_id': rule_id,
                'expected': expected, 'deleted': deleted}

    def _root(self, root_id: str) -> dict:
        try:
            return self.app.storage.roots.get(root_id, with_state=False)
        except StorageError:
            raise StorageError('unknown_root', '存储位置不存在。', rootId=root_id)

    @staticmethod
    def _normal(raw, *, allow_root: bool) -> str:
        from .guard import StorageGuard
        return StorageGuard.normal_rel(raw if isinstance(raw, str) else '', allow_root=allow_root)

    def write_shared(self, actor: dict, payload: dict) -> dict:
        self.app.require_admin(actor)
        values = self._validate(payload, personal=False)
        scope, root_id, path = values['scope'], values['root_id'], values['path']
        with self.store.transaction() as db:
            if root_id == 'legacy':
                old = db.execute('SELECT * FROM bindings WHERE scope=? AND path=?',
                                 (scope if scope != 'mapping' else 'directory', path)).fetchone()
                current = old['revision'] if old else 0
            else:
                old = db.execute('SELECT * FROM root_bindings WHERE scope=? AND root_id=? '
                                 'AND path=?', (scope, root_id, path)).fetchone()
                current = old['revision'] if old else 0
            self.app.check_revision(values['expected'], current)
            if values['deleted']:
                if root_id == 'legacy':
                    db.execute('DELETE FROM bindings WHERE scope=? AND path=?',
                               (scope, path))
                else:
                    db.execute('DELETE FROM root_bindings WHERE scope=? AND root_id=? AND path=?',
                               (scope, root_id, path))
                self.store.audit(db, actor['ip'], 'binding.delete', path,
                                 {'scope': scope, 'rootId': root_id})
                return {'removed': True}
            if root_id == 'legacy' and scope != 'mapping':
                db.execute('INSERT INTO bindings VALUES(?,?,?,?,?) ON CONFLICT(scope,path) '
                           'DO UPDATE SET rule_id=excluded.rule_id,revision=excluded.revision,'
                           'updated_at=excluded.updated_at',
                           (scope, path, values['rule_id'], values['expected'] + 1, _stamp()))
            else:
                db.execute(
                    'INSERT INTO root_bindings(scope,root_id,path,rule_id,revision,updated_at,'
                    'updated_by) VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(scope,root_id,path) DO UPDATE SET rule_id=excluded.rule_id,'
                    'revision=excluded.revision,updated_at=excluded.updated_at,'
                    'updated_by=excluded.updated_by',
                    (scope, root_id, path, values['rule_id'], values['expected'] + 1,
                     _stamp(), actor['ip']))
            self.store.audit(db, actor['ip'], 'binding.set', path,
                             {'scope': scope, 'rootId': root_id, 'ruleId': values['rule_id']})
        return {'item': {'scope': scope, 'rootId': root_id, 'path': path,
                         'ruleId': values['rule_id'], 'revision': values['expected'] + 1}}

    def write_personal(self, actor: dict, payload: dict) -> dict:
        values = self._validate(payload, personal=True)
        scope, root_id, path = values['scope'], values['root_id'], values['path']
        if scope == 'mapping':
            # A mapping default is a shared collector decision, not a preference.
            raise StorageError('invalid_scope', '采集映射默认规则只能由管理员设置。')
        with self.store.transaction() as db:
            old = db.execute('SELECT revision FROM personal_bindings WHERE owner=? AND scope=? '
                             'AND root_id=? AND path=?',
                             (actor['id'], scope, root_id, path)).fetchone()
            self.app.check_revision(values['expected'], old['revision'] if old else 0)
            if values['deleted']:
                db.execute('DELETE FROM personal_bindings WHERE owner=? AND scope=? AND root_id=? '
                           'AND path=?', (actor['id'], scope, root_id, path))
                self.store.audit(db, actor['ip'], 'binding.personal.delete', path,
                                 {'scope': scope, 'rootId': root_id})
                return {'removed': True}
            db.execute(
                'INSERT INTO personal_bindings(owner,scope,root_id,path,rule_id,revision,'
                'updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,scope,root_id,path) '
                'DO UPDATE SET rule_id=excluded.rule_id,revision=excluded.revision,'
                'updated_at=excluded.updated_at',
                (actor['id'], scope, root_id, path, values['rule_id'], values['expected'] + 1,
                 _stamp()))
            self.store.audit(db, actor['ip'], 'binding.personal.set', path,
                             {'scope': scope, 'rootId': root_id, 'ruleId': values['rule_id']})
        return {'item': {'owner': actor['id'], 'scope': scope, 'rootId': root_id,
                         'path': path, 'ruleId': values['rule_id'],
                         'revision': values['expected'] + 1}}

    # ------------------------------------------------------------------ usage
    def usage(self, rule_id: str) -> dict:
        """Every place a rule is referenced, so a delete cannot orphan a file."""
        with self.store.connect() as db:
            shared = db.execute('SELECT count(*) FROM bindings WHERE rule_id=?',
                                (rule_id,)).fetchone()[0]
            root = db.execute('SELECT count(*) FROM root_bindings WHERE rule_id=?',
                              (rule_id,)).fetchone()[0]
            personal = db.execute('SELECT count(*) FROM personal_bindings WHERE rule_id=?',
                                  (rule_id,)).fetchone()[0]
        return {'shared': int(shared), 'root': int(root), 'personal': int(personal),
                'total': int(shared) + int(root) + int(personal)}

    def detach_path(self, actor: dict, *, root_id: str, rel_path: str) -> dict:
        """Remove bindings that point at a file/directory being deleted."""
        removed = 0
        with self.store.transaction() as db:
            for scope, path in (('file', rel_path), ('directory', rel_path)):
                if root_id == 'legacy':
                    cursor = db.execute('DELETE FROM bindings WHERE scope=? AND path=?',
                                        (scope, path))
                    removed += cursor.rowcount
                cursor = db.execute('DELETE FROM root_bindings WHERE scope=? AND root_id=? '
                                    'AND path=?', (scope, root_id, path))
                removed += cursor.rowcount
                cursor = db.execute('DELETE FROM personal_bindings WHERE scope=? AND root_id=? '
                                    'AND path=?', (scope, root_id, path))
                removed += cursor.rowcount
            self.store.audit(db, actor['ip'], 'binding.detach', rel_path,
                             {'rootId': root_id, 'removed': removed})
        return {'removed': removed}
