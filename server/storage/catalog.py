"""Logical library over several physical roots (R6-054).

The library is a *view*. No file is moved, linked or copied: `logical_files` rows
record which root and which relative path hold the bytes, and the listing joins
them with the root's live state. That is what lets two roots with the same
`sw/app.log` coexist without a name collision, and what keeps the legacy `data/`
directory where the operator put it.

Three behaviours are deliberate and tested:

* **browsing never reads log content.** A directory listing stats entries and
  reads a bounded sample only when registering a new generation; it does not
  download or parse a log to decide what to show.
* **an offline root is not an empty root.** Rows stay, `state` becomes `offline`
  with a reason, and a listing for that root reports the reason instead of zero
  files. Deleting the registration is a separate, explicit action.
* **a generation is a new row, not an overwrite.** When the bytes change, the
  counter advances and the old generation stays readable; nothing silently
  re-points a published index at different content.
"""
from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Optional

from ..models import content_signature, file_id_for
from .guard import SENTINEL_NAME, TRASH_NAME, StorageError

SAMPLE = 64 * 1024
MAX_PAGE = 200
DEFAULT_PAGE = 100
RESERVED = frozenset({TRASH_NAME, SENTINEL_NAME})


def _stamp() -> str:
    from ..app import now
    return now()


def _encode_cursor(root_id: str, rel_path: str) -> str:
    raw = json.dumps({'r': root_id, 'p': rel_path}, ensure_ascii=False).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii')


def _decode_cursor(value: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(value.encode('ascii'))
        parsed = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, TypeError):
        raise StorageError('invalid_cursor', '分页游标无效。')
    if not isinstance(parsed, dict) or not isinstance(parsed.get('r'), str) \
            or not isinstance(parsed.get('p'), str):
        raise StorageError('invalid_cursor', '分页游标无效。')
    return parsed


class CatalogService:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ------------------------------------------------------------------ roots
    def visible_roots(self, actor: dict, *, scope: str = 'shared') -> list:
        kinds = ('shared_raw',) if scope == 'shared' else ('private_raw',)
        return [item for item in self.app.storage.roots.all(kinds=kinds)]

    def root_for_path(self, absolute: Path) -> dict:
        """Find the registered root that owns a real path (longest prefix)."""
        target = os.path.realpath(str(absolute))
        best, best_len = None, -1
        for item in self.app.storage.roots.all(with_state=False):
            base = os.path.realpath(item['path'])
            if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
                if len(base) > best_len:
                    best, best_len = item, len(base)
        if best is None:
            raise StorageError('unknown_root', '该路径不属于任何已登记的存储位置。', path=target)
        return best

    # ------------------------------------------------------------ registration
    def ensure_root_allows(self, root: dict):
        if root['kind'] == 'index':
            raise StorageError('invalid_root', '索引位置不存放日志原文。', rootId=root['id'])

    def register_path(self, absolute: Path, *, owner: str = '', scope: Optional[str] = None,
                      logical_name: Optional[str] = None) -> dict:
        """Register (or refresh) one file and return its stable identity.

        Called by indexing and by directory scans. It reads a 64 KiB head/tail
        sample, never the whole file: identity must be cheap enough to recompute
        on every scan.
        """
        absolute = Path(os.path.realpath(str(absolute)))
        root = self.root_for_path(absolute)
        self.ensure_root_allows(root)
        base = Path(os.path.realpath(root['path']))
        rel = absolute.relative_to(base).as_posix()
        if any(part.startswith('.') for part in rel.split('/')):
            raise StorageError('invalid_path', '保留目录不进入文件库。', path=rel)
        stat_result = absolute.stat()
        if not stat.S_ISREG(stat_result.st_mode):
            raise StorageError('not_a_file', '只登记常规文件。', path=rel)
        try:
            with open(absolute, 'rb') as handle:
                head = handle.read(SAMPLE)
                tail = head
                if stat_result.st_size > SAMPLE:
                    handle.seek(max(SAMPLE, stat_result.st_size - SAMPLE))
                    tail = handle.read(SAMPLE)
        except OSError:
            # A file the service account cannot read is still a file the library
            # must list: the operator needs to see it and the reason, instead of a
            # silently missing row. Identity falls back to size+mtime only.
            head = tail = b''
        signature = content_signature(stat_result.st_size, stat_result.st_mtime_ns, head, tail)
        file_id = file_id_for(root['id'], rel)
        scope = scope or root['scope']
        display = logical_name or absolute.name
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM logical_files WHERE id=?', (file_id,)).fetchone()
            if row is None:
                db.execute(
                    'INSERT INTO logical_files(id,root_id,rel_path,display_name,size,generation,'
                    'content_sig,created_at,updated_at,owner,scope,state,observed_at,mtime_ns) '
                    'VALUES(?,?,?,?,?,1,?,?,?,?,?,\'active\',?,?)',
                    (file_id, root['id'], rel, display, stat_result.st_size, signature,
                     _stamp(), _stamp(), row_owner(scope, owner), scope, _stamp(),
                     stat_result.st_mtime_ns))
                db.execute('INSERT INTO generations(file_id,generation,raw_bytes,content_sig,'
                           'state,created_at) VALUES(?,1,?,?,\'active\',?)',
                           (file_id, stat_result.st_size, signature, _stamp()))
                generation = 1
            else:
                generation = row['generation']
                if row['content_sig'] != signature:
                    generation = row['generation'] + 1
                    db.execute('INSERT INTO generations(file_id,generation,raw_bytes,content_sig,'
                               'state,created_at) VALUES(?,?,?,?,\'active\',?)',
                               (file_id, generation, stat_result.st_size, signature, _stamp()))
                    db.execute('UPDATE logical_files SET size=?,generation=?,content_sig=?,'
                               'state=\'active\',updated_at=?,observed_at=?,mtime_ns=? WHERE id=?',
                               (stat_result.st_size, generation, signature, _stamp(), _stamp(),
                                stat_result.st_mtime_ns, file_id))
                else:
                    db.execute('UPDATE logical_files SET state=\'active\',observed_at=?,'
                               'size=?,updated_at=? WHERE id=?',
                               (_stamp(), stat_result.st_size, _stamp(), file_id))
                if display != row['display_name']:
                    # Renaming the root's label or the file itself must not change
                    # the id: it is keyed by (root, relative path), not by the name.
                    db.execute('UPDATE logical_files SET display_name=? WHERE id=?',
                               (display, file_id))
            if display != absolute.name or logical_name:
                db.execute('INSERT INTO catalog_name_map(root_id,physical_name,logical_name,'
                           'rel_path,updated_at) VALUES(?,?,?,?,?) '
                           'ON CONFLICT(root_id,physical_name) DO UPDATE SET '
                           'logical_name=excluded.logical_name,rel_path=excluded.rel_path,'
                           'updated_at=excluded.updated_at',
                           (root['id'], absolute.name, display, rel, _stamp()))
        return {'file_id': file_id, 'root_id': root['id'], 'rel_path': rel,
                'generation': generation, 'content_sig': signature,
                'size': stat_result.st_size, 'mtime_ns': stat_result.st_mtime_ns}

    # ------------------------------------------------------------------- scan
    def sync_directory(self, root: dict, rel_dir: str = '', *, limit: int = 500,
                       offset: int = 0) -> dict:
        """Refresh metadata for one directory. Bounded, metadata-only."""
        guard = self.app.storage.guard
        guard.require_online(root)
        directory = guard.resolve(root, rel_dir, want='dir')
        entries = []
        try:
            with os.scandir(str(directory)) as iterator:
                for entry in iterator:
                    if entry.name.startswith('.') or entry.name in RESERVED:
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        entries.append(entry)
                    except OSError:
                        continue
        except OSError:
            raise StorageError('storage_root_offline', '目录不可读取，已保留现有登记。',
                               rootId=root['id'], path=rel_dir)
        entries.sort(key=lambda item: item.name.casefold())
        window = entries[offset:offset + limit]
        registered, seen, failed = 0, [], []
        for entry in window:
            seen.append(entry.name)
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                self.register_path(Path(entry.path))
                registered += 1
            except (OSError, StorageError) as exc:
                failed.append({'name': entry.name,
                               'code': getattr(exc, 'code', 'scan_failed')})
        # A file that disappeared from a directory we just listed is marked
        # `missing`, not deleted: an unmounted-but-mounted directory looks exactly
        # like an empty one, and the registration is the operator's record.
        prefix = rel_dir.rstrip('/')
        with self.store.transaction() as db:
            rows = db.execute(
                'SELECT id,rel_path FROM logical_files WHERE root_id=? AND state=\'active\'',
                (root['id'],)).fetchall()
            for row in rows:
                parent = row['rel_path'].rsplit('/', 1)[0] if '/' in row['rel_path'] else ''
                if parent == prefix and row['rel_path'].rsplit('/', 1)[-1] not in seen:
                    db.execute('UPDATE logical_files SET state=\'missing\',updated_at=? WHERE id=?',
                               (_stamp(), row['id']))
        return {'root_id': root['id'], 'path': rel_dir, 'registered': registered,
                'visited': len(window), 'total': len(entries),
                'failed': failed, 'nextOffset': offset + limit if offset + limit < len(entries) else None}

    # ------------------------------------------------------------------- list
    def list_files(self, actor: dict, params: dict) -> dict:
        scope = params.get('scope') or 'shared'
        if scope not in ('shared', 'private'):
            raise StorageError('invalid_scope', '范围只支持 shared 或 private。')
        limit = max(1, min(int(params.get('limit') or DEFAULT_PAGE), MAX_PAGE))
        if scope == 'shared':
            roots = self.visible_roots(actor, scope='shared')
        else:
            # A private row may physically live inside the legacy root (the default
            # upload area), so visibility is decided by `owner`, not by root kind.
            roots = self.visible_roots(actor, scope='shared') + \
                self.visible_roots(actor, scope='private')
        if params.get('root') and params.get('root') != 'all':
            roots = [item for item in roots if item['id'] == params['root']]
            if not roots:
                raise StorageError('unknown_root', '存储位置不存在或无权访问。', rootId=params.get('root'))
        if not roots:
            return {'items': [], 'nextCursor': None, 'scope': scope, 'total': 0,
                    'counted': 'exact', 'roots': []}
        root_ids = [item['id'] for item in roots]
        where, args = [], []
        where.append('f.root_id IN (%s)' % ','.join('?' * len(root_ids)))
        args.extend(root_ids)
        where.append("f.state<>'trashed'")
        where.append('f.scope=?')
        args.append(scope)
        if scope == 'private':
            # A private row is visible to its owner only. Administrators keep the
            # registration and the counters, not the content.
            where.append('f.owner=?')
            args.append(actor['id'])
        if params.get('status'):
            where.append('f.state=?')
            args.append(params['status'])
        if params.get('directory'):
            prefix = params['directory'].rstrip('/')
            where.append('(f.rel_path=? OR f.rel_path LIKE ?)')
            args.extend([prefix, prefix + '/%'])
        if params.get('q'):
            needle = '%' + escape_like(params['q']) + '%'
            where.append("(f.rel_path LIKE ? ESCAPE '\\' OR f.display_name LIKE ? ESCAPE '\\')")
            args.extend([needle, needle])
        if params.get('cursor'):
            cursor = _decode_cursor(params['cursor'])
            where.append('(f.root_id>? OR (f.root_id=? AND f.rel_path>?))')
            args.extend([cursor['r'], cursor['r'], cursor['p']])
        sql = ('SELECT f.*, r.kind AS root_kind, r.label AS root_label, '
               'r.enabled AS root_enabled, r.volume_id AS root_volume, '
               'r.sentinel_id AS root_sentinel, r.path AS root_path '
               'FROM logical_files f JOIN storage_roots r ON r.id=f.root_id '
               'WHERE ' + ' AND '.join(where) +
               ' ORDER BY f.root_id,f.rel_path LIMIT ?')
        args.append(limit + 1)
        with self.store.connect() as db:
            rows = db.execute(sql, args).fetchall()
            total = db.execute('SELECT count(*) FROM logical_files f WHERE ' +
                               ' AND '.join(where), args[:-1]).fetchone()[0]
        page, more = rows[:limit], len(rows) > limit
        layers = self._layers([row['id'] for row in page])
        root_states = {item['id']: item for item in roots}
        items = [self._public(actor, row, layers, roots=root_states) for row in page]
        next_cursor = _encode_cursor(page[-1]['root_id'], page[-1]['rel_path']) if more and page else None
        return {'items': items, 'nextCursor': next_cursor, 'scope': scope,
                'total': int(total), 'counted': 'exact',
                'roots': [{'root_id': item['id'], 'label': item['label'],
                           'state': item['state'], 'reason': item['reason'],
                           'files': item['files'], 'offline': item['state'] != 'online'}
                          for item in roots]}

    def _layers(self, file_ids: list) -> dict:
        if not file_ids:
            return {}
        marks = ','.join('?' * len(file_ids))
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT p.file_id,p.layer,v.state,v.rows,v.published_at,v.generation '
                'FROM layer_pointers p LEFT JOIN layer_versions v ON v.version_id=p.version_id '
                "WHERE p.scope='shared' AND p.file_id IN (%s)" % marks, file_ids).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row['file_id'], {})[row['layer']] = {
                'state': row['state'] or 'published', 'rows': row['rows'],
                'publishedAt': row['published_at'], 'generation': row['generation']}
        return result

    def _public(self, actor: dict, row, layers: dict, *, roots: dict) -> dict:
        states = layers.get(row['id'], {})
        root_info = roots.get(row['root_id']) or {}
        online = root_info.get('state', 'online') == 'online'
        coverage = 'none'
        if states.get('structure', {}).get('state') in ('complete', 'published'):
            coverage = 'structure'
        if states.get('rules', {}).get('state') in ('complete', 'published'):
            coverage = 'rules'
        if states.get('search', {}).get('state') in ('complete', 'published'):
            coverage = 'search'
        binding = None
        try:
            binding = self.app.storage.bindings.resolve(actor, root_id=row['root_id'],
                                                        rel_path=row['rel_path'])
        except StorageError:
            binding = None
        return {
            'fileId': row['id'],
            'rootId': row['root_id'],
            'rootLabel': row['root_label'],
            'rootState': root_info.get('state', 'unknown'),
            'relPath': row['rel_path'],
            'logicalPath': '%s:%s' % (row['root_label'], row['rel_path']),
            'displayName': row['display_name'],
            'size': row['size'],
            'generation': row['generation'],
            'state': row['state'],
            'scope': row['scope'],
            'owner': row['owner'] or None,
            'observedAt': row['observed_at'],
            'coverage': coverage,
            'layers': states,
            'rule': {'ruleId': (binding or {}).get('ruleId'),
                     'layer': (binding or {}).get('layer'),
                     'explanation': (binding or {}).get('explanation'),
                     'chain': (binding or {}).get('chain')},
            'physicalPath': str(Path(row['root_path']) / row['rel_path'])
                            if actor['role'] == 'admin' else None,
            'available': online and row['state'] == 'active',
            'downloadable': online and row['state'] == 'active',
        }

    # ------------------------------------------------------------------- tree
    def tree(self, actor: dict, params: dict) -> dict:
        """One level of the directory tree. Roots are the top level."""
        guard = self.app.storage.guard
        root_id = params.get('root') or ''
        scope = params.get('scope') or 'shared'
        limit = max(1, min(int(params.get('limit') or 200), 500))
        if not root_id or root_id == 'all':
            roots = self.visible_roots(actor, scope=scope)
            items = [{'type': 'root', 'rootId': item['id'], 'name': item['label'],
                      'relPath': '', 'state': item['state'], 'reason': item['reason'],
                      'files': item['files'], 'bytes': item['bytes'],
                      'offline': item['state'] != 'online'}
                     for item in roots if item['kind'] in ('shared_raw', 'private_raw')]
            return {'rootId': None, 'path': '', 'items': items, 'nextCursor': None,
                    'state': 'online' if any(item['state'] == 'online' for item in roots) else 'offline',
                    'reason': None if roots else 'no_root'}
        root = self.app.storage.roots.get(root_id)
        if root['kind'] == 'index':
            raise StorageError('invalid_root', '索引位置不是文件库目录。', rootId=root_id)
        if root['scope'] == 'private' and not actor['role'] == 'admin':
            # Private roots are still listed by label for their owner, but a
            # non-owner gets the same answer as a missing root.
            with self.store.connect() as db:
                mine = db.execute("SELECT 1 FROM logical_files WHERE root_id=? AND owner=? LIMIT 1",
                                  (root_id, actor['id'])).fetchone()
            if not mine:
                raise StorageError('unknown_root', '存储位置不存在或无权访问。', rootId=root_id)
        state = guard.state(root)
        if state['state'] != 'online':
            return {'rootId': root_id, 'path': params.get('path') or '', 'items': [],
                    'nextCursor': None, 'state': state['state'], 'reason': state['reason'],
                    'registrationKept': True, 'rootLabel': root['label']}
        rel = guard.normal_rel(params.get('path') or '', allow_root=True)
        if params.get('sync') != '0':
            try:
                self.sync_directory(root, rel, limit=500)
            except StorageError:
                pass
        directory = guard.resolve(root, rel, want='dir')
        cursor = params.get('cursor') or ''
        entries = []
        try:
            with os.scandir(str(directory)) as iterator:
                for entry in iterator:
                    if entry.name.startswith('.') or entry.name in RESERVED:
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        entries.append((entry.name, entry.is_dir(follow_symlinks=False)))
                    except OSError:
                        continue
        except OSError:
            raise StorageError('storage_root_offline', '目录不可读取。', rootId=root_id, path=rel)
        entries.sort(key=lambda pair: (not pair[1], pair[0].casefold()))
        if cursor:
            entries = [pair for pair in entries if pair[0] > cursor]
        window = entries[:limit]
        names = [name for name, _ in window]
        stats = self._child_stats(root_id, rel, names)
        items = []
        for name, is_dir in window:
            child_rel = (rel + '/' + name) if rel else name
            info = stats.get(name, {})
            items.append({
                'type': 'directory' if is_dir else 'file',
                'name': name, 'relPath': child_rel,
                'fileId': info.get('file_id'), 'size': info.get('size'),
                'generation': info.get('generation'), 'state': info.get('state'),
                'files': info.get('files', 0), 'bytes': info.get('bytes', 0),
                'entries': info.get('entries'),
                'indexed': info.get('indexed', 0),
                'hasChildren': True if is_dir else None,
            })
        next_cursor = window[-1][0] if len(entries) > len(window) else None
        return {'rootId': root_id, 'rootLabel': root['label'], 'path': rel,
                'items': items, 'nextCursor': next_cursor, 'state': 'online',
                'reason': None, 'totalCandidates': len(entries)}

    def _child_stats(self, root_id: str, rel: str, names: list) -> dict:
        if not names:
            return {}
        marks = ','.join('?' * len(names))
        prefix = (rel + '/') if rel else ''
        with self.store.connect() as db:
            files = db.execute(
                'SELECT rel_path,id,size,generation,state FROM logical_files WHERE root_id=? '
                "AND rel_path IN (%s)" % marks,
                [root_id] + [prefix + name for name in names]).fetchall()
            dirs = db.execute(
                'SELECT rel_path,count(*) AS files,COALESCE(SUM(size),0) AS bytes '
                'FROM logical_files WHERE root_id=? AND state=\'active\' AND rel_path LIKE ? '
                'GROUP BY rel_path', (root_id, prefix + '%')).fetchall()
        result = {}
        for row in files:
            name = row['rel_path'].rsplit('/', 1)[-1]
            result.setdefault(name, {}).update({'file_id': row['id'], 'size': row['size'],
                                                'generation': row['generation'],
                                                'state': row['state']})
        for name in names:
            child = (prefix + name + '/') if rel else (name + '/')
            total, size = 0, 0
            for row in dirs:
                if row['rel_path'].startswith(child):
                    total += row['files']
                    size += row['bytes']
            entry = result.setdefault(name, {})
            entry['files'] = total
            entry['bytes'] = size
            if not total:
                # Lazy tree: an unexpanded directory has no registered rows yet, and
                # "0 files" would be a lie. Count its direct entries instead — a
                # stat-only scandir, never a log read.
                entry['entries'] = self._direct_entries(Path(self._root_path(root_id)) / child)
        return result

    @staticmethod
    def _direct_entries(directory: Path, limit: int = 500) -> int:
        try:
            with os.scandir(str(directory)) as iterator:
                count = 0
                for entry in iterator:
                    if entry.name.startswith('.'):
                        continue
                    count += 1
                    if count >= limit:
                        break
                return count
        except OSError:
            return 0

    def _root_path(self, root_id: str) -> str:
        with self.store.connect() as db:
            row = db.execute('SELECT path FROM storage_roots WHERE id=?', (root_id,)).fetchone()
        return row['path'] if row else ''

    # --------------------------------------------------------------- metadata
    def metadata(self, actor: dict, file_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT f.*, r.label AS root_label, r.kind AS root_kind, '
                             'r.path AS root_path, r.enabled AS root_enabled '
                             'FROM logical_files f JOIN storage_roots r ON r.id=f.root_id '
                             'WHERE f.id=?', (file_id,)).fetchone()
            generations = db.execute('SELECT generation,raw_bytes,content_sig,state,created_at '
                                     'FROM generations WHERE file_id=? ORDER BY generation DESC',
                                     (file_id,)).fetchall()
        if row is None:
            raise StorageError('not_found', '文件不存在。')
        if row['scope'] == 'private' and row['owner'] != actor['id']:
            # Same answer as missing: private content must not even be disclosed
            # to another principal by a distinguishable error.
            raise StorageError('not_found', '文件不存在。')
        root = self.app.storage.roots.get(row['root_id'])
        binding = self.app.storage.bindings.resolve(actor, root_id=row['root_id'],
                                                    rel_path=row['rel_path'])
        layers = self._layers([file_id]).get(file_id, {})
        return {
            'fileId': row['id'], 'rootId': row['root_id'], 'rootLabel': row['root_label'],
            'relPath': row['rel_path'], 'displayName': row['display_name'],
            'size': row['size'], 'generation': row['generation'], 'state': row['state'],
            'scope': row['scope'], 'owner': row['owner'] or None,
            'observedAt': row['observed_at'], 'layers': layers,
            'generations': [dict(item) for item in generations],
            'rule': {'ruleId': binding['ruleId'], 'layer': binding['layer'],
                     'explanation': binding['explanation'], 'chain': binding['chain']},
            'rootState': root['state'], 'rootReason': root['reason'],
            'physicalPath': str(Path(row['root_path']) / row['rel_path'])
                            if actor['role'] == 'admin' else None,
        }


def row_owner(scope: str, owner: str) -> str:
    """Private rows always belong to somebody; shared rows to nobody."""
    return owner if scope == 'private' else ''


def escape_like(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
