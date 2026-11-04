"""R6 control-plane migrations (R6-032).

The v3.0.3 database is not replaced. Everything here is expand-only: new tables,
new indexes, no column rewrites and no data backfill that could change an
existing row. That is what lets an old binary keep reading the same file while
the new one learns about generations, jobs and cache versions.

Rules that the tests enforce:

* a migration runs inside `BEGIN IMMEDIATE`, so it is either fully applied or
  not applied at all;
* `schema_migrations` records (version, name, checksum). Re-running is a no-op,
  and a checksum change is an error, not a silent re-run;
* a database written by a *newer* code version refuses to open here instead of
  being half-read;
* a copy of the database is taken before the first migration that changes it,
  because "we can always restore from backup" is only true if one exists.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 8


class MigrationError(RuntimeError):
    """Raised when the database cannot be migrated safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# (version, name, [statements]) — append only. Never edit an applied step.
STEPS = [
    (
        1,
        'control-plane-entities',
        [
            """CREATE TABLE IF NOT EXISTS storage_roots (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                path TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS logical_files (
                id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL REFERENCES storage_roots(id),
                rel_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                content_sig TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(root_id, rel_path)
            )""",
            """CREATE TABLE IF NOT EXISTS generations (
                file_id TEXT NOT NULL REFERENCES logical_files(id),
                generation INTEGER NOT NULL,
                raw_bytes INTEGER NOT NULL,
                content_sig TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(file_id, generation)
            )""",
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                principal TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1,
                action_key TEXT,
                payload TEXT NOT NULL,
                progress TEXT NOT NULL,
                error TEXT,
                resume_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            'CREATE UNIQUE INDEX IF NOT EXISTS jobs_action_key ON jobs(action_key) WHERE action_key IS NOT NULL',
            'CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, priority DESC, created_at)',
            'CREATE INDEX IF NOT EXISTS jobs_principal ON jobs(principal, created_at DESC)',
            """CREATE TABLE IF NOT EXISTS job_leases (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                epoch INTEGER NOT NULL,
                owner TEXT NOT NULL,
                fencing INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS job_checkpoints (
                job_id TEXT NOT NULL REFERENCES jobs(id),
                epoch INTEGER NOT NULL,
                stage TEXT NOT NULL,
                watermark TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, epoch, stage)
            )""",
        ],
    ),
    (
        2,
        'cache-and-index-publication',
        [
            """CREATE TABLE IF NOT EXISTS cache_versions (
                file_id TEXT NOT NULL REFERENCES logical_files(id),
                generation INTEGER NOT NULL,
                layer TEXT NOT NULL,
                signature TEXT NOT NULL,
                state TEXT NOT NULL,
                bytes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(file_id, generation, layer)
            )""",
            """CREATE TABLE IF NOT EXISTS index_publications (
                file_id TEXT PRIMARY KEY REFERENCES logical_files(id),
                generation INTEGER NOT NULL,
                published_at TEXT NOT NULL,
                publish_seq INTEGER NOT NULL,
                shard_path TEXT NOT NULL,
                watermark TEXT NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS index_publications_seq ON index_publications(publish_seq)',
            """CREATE TABLE IF NOT EXISTS upload_sessions (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                display_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                chunk_size INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS upload_parts (
                session_id TEXT NOT NULL REFERENCES upload_sessions(id),
                part_no INTEGER NOT NULL,
                size INTEGER NOT NULL,
                digest TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, part_no)
            )""",
        ],
    ),
    (
        3,
        'query-snapshots',
        [
            """CREATE TABLE IF NOT EXISTS query_snapshots (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                signature TEXT NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS query_snapshots_principal ON query_snapshots(principal, created_at DESC)',
            'CREATE INDEX IF NOT EXISTS query_snapshots_expiry ON query_snapshots(expires_at)',
        ],
    ),
    (
        4,
        'job-actions',
        [
            # Control actions are idempotent by `action_id` (C9): a retried POST
            # returns the first outcome instead of starting a second transition.
            """CREATE TABLE IF NOT EXISTS job_actions (
                action_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS job_actions_job ON job_actions(job_id, created_at DESC)',
        ],
    ),
    (
        5,
        'layered-cache-registry',
        [
            # One row per (file, generation, layer, scope) *attempt*. A version is
            # a staging object until it is published; the pointer table below is
            # the only thing a read path follows.
            """CREATE TABLE IF NOT EXISTS layer_versions (
                version_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                layer TEXT NOT NULL,
                scope TEXT NOT NULL,
                owner TEXT NOT NULL,
                signature TEXT NOT NULL,
                deps TEXT NOT NULL,
                state TEXT NOT NULL,
                rows INTEGER NOT NULL DEFAULT 0,
                digest TEXT,
                bytes INTEGER NOT NULL DEFAULT 0,
                stats TEXT NOT NULL DEFAULT '{}',
                supersedes TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT
            )""",
            'CREATE UNIQUE INDEX IF NOT EXISTS layer_versions_sig ON layer_versions(file_id, generation, layer, scope, signature, owner)',
            'CREATE INDEX IF NOT EXISTS layer_versions_state ON layer_versions(file_id, layer, state)',
            # The single publication pointer per (file, layer, scope). Publishing is
            # one UPDATE of this row, which is what makes the switch atomic.
            """CREATE TABLE IF NOT EXISTS layer_pointers (
                file_id TEXT NOT NULL,
                layer TEXT NOT NULL,
                scope TEXT NOT NULL,
                version_id TEXT NOT NULL REFERENCES layer_versions(version_id),
                seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(file_id, layer, scope)
            )""",
            """CREATE TABLE IF NOT EXISTS layer_pins (
                version_id TEXT NOT NULL REFERENCES layer_versions(version_id),
                token TEXT NOT NULL,
                owner TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(version_id, token)
            )""",
            'CREATE INDEX IF NOT EXISTS layer_pins_owner ON layer_pins(owner)',
            """CREATE TABLE IF NOT EXISTS layer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS layer_events_version ON layer_events(version_id, id)',
        ],
    ),
    (
        6,
        'query-hits-and-upload-lifecycle',
        [
            # Query results are paged by sequence, not by SQL OFFSET: a cursor is a
            # position in this table, and re-scanning to page 900 is not a thing.
            """CREATE TABLE IF NOT EXISTS query_hits (
                snapshot_id TEXT NOT NULL REFERENCES query_snapshots(id),
                seq INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY(snapshot_id, seq)
            )""",
            'CREATE INDEX IF NOT EXISTS query_hits_ordinal ON query_hits(snapshot_id, ordinal)',
            # Upload sessions gain the lifecycle fields a private store needs:
            # when the parts expire, what the sealed file is, and how much quota the
            # principal is holding.
            'ALTER TABLE upload_sessions ADD COLUMN expires_at REAL NOT NULL DEFAULT 0',
            'ALTER TABLE upload_sessions ADD COLUMN sealed_path TEXT',
            'ALTER TABLE upload_sessions ADD COLUMN content_sig TEXT',
            'ALTER TABLE upload_sessions ADD COLUMN file_id TEXT',
            'ALTER TABLE upload_sessions ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0',
            'ALTER TABLE upload_sessions ADD COLUMN job_id TEXT',
            'CREATE INDEX IF NOT EXISTS upload_sessions_principal ON upload_sessions(principal, created_at DESC)',
        ],
    ),
    (
        7,
        'collectors-and-archive',
        [
            # A credential is a *reference*: the row names a file under
            # state/credentials (0700 dir, 0600 file) and stores only its
            # fingerprint. Nothing that reaches an API, an audit row or an
            # exception message is derivable from this table.
            """CREATE TABLE IF NOT EXISTS collector_credentials (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                secret_path TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            # `host_pin` is the approved host key / certificate fingerprint that a
            # probe recorded and an administrator accepted. A later connection
            # whose pin differs stops collecting instead of trusting the change.
            """CREATE TABLE IF NOT EXISTS remote_sources (
                id TEXT PRIMARY KEY,
                adapter TEXT NOT NULL,
                label TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                credential_ref TEXT,
                host_pin TEXT,
                acl TEXT NOT NULL DEFAULT 'admin',
                revision INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                policy TEXT NOT NULL DEFAULT '{}',
                capabilities TEXT NOT NULL DEFAULT '{}',
                last_probe TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS remote_sources_adapter ON remote_sources(adapter, enabled)',
            """CREATE TABLE IF NOT EXISTS directory_mappings (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES remote_sources(id) ON DELETE CASCADE,
                remote_dir TEXT NOT NULL,
                root_id TEXT NOT NULL,
                target_subdir TEXT NOT NULL,
                include TEXT NOT NULL DEFAULT '[]',
                exclude TEXT NOT NULL DEFAULT '[]',
                recursive INTEGER NOT NULL DEFAULT 1,
                rule_revision TEXT,
                mode TEXT NOT NULL DEFAULT 'manual',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, remote_dir, root_id, target_subdir)
            )""",
            'CREATE INDEX IF NOT EXISTS directory_mappings_source ON directory_mappings(source_id)',
            # A probe is only valid for the exact configuration revision it
            # measured. Editing the endpoint, credential, pin or mappings inserts a
            # new revision, and the old probe cannot enable it.
            """CREATE TABLE IF NOT EXISTS source_probes (
                source_id TEXT NOT NULL REFERENCES remote_sources(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                ok INTEGER NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(source_id, revision)
            )""",
            """CREATE TABLE IF NOT EXISTS source_actions (
                action_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS source_actions_source ON source_actions(source_id, created_at DESC)',
            # One row per (mapping, remote path). The archive watermarks live here
            # so a scheduler restart resumes from committed bytes, not from what the
            # in-memory loop last saw.
            """CREATE TABLE IF NOT EXISTS source_files (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                mapping_id TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                file_id TEXT,
                generation INTEGER NOT NULL DEFAULT 0,
                root_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                evidence TEXT NOT NULL DEFAULT '{}',
                archived_bytes INTEGER NOT NULL DEFAULT 0,
                contiguous_bytes INTEGER NOT NULL DEFAULT 0,
                gap_state TEXT NOT NULL DEFAULT 'none',
                sealed INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'new',
                last_seen_at TEXT,
                last_error TEXT,
                retry_at REAL,
                UNIQUE(mapping_id, remote_path)
            )""",
            'CREATE INDEX IF NOT EXISTS source_files_file ON source_files(file_id, generation)',
            'CREATE INDEX IF NOT EXISTS source_files_state ON source_files(state, retry_at)',
            # Interval set plus contiguous watermark are separate on purpose: a
            # tail-first read must not be mistaken for "everything from 0 is here".
            """CREATE TABLE IF NOT EXISTS archive_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                digest TEXT NOT NULL,
                writer TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(file_id, generation, byte_start, byte_end)
            )""",
            'CREATE INDEX IF NOT EXISTS archive_intervals_file ON archive_intervals(file_id, generation, byte_start)',
            """CREATE TABLE IF NOT EXISTS archive_manifests (
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                manifest TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(file_id, generation)
            )""",
            # Stage watermarks are deliberately not one number: archive can be
            # ahead of parsed, parsed ahead of rules, rules ahead of search.
            """CREATE TABLE IF NOT EXISTS index_watermarks (
                file_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                stage TEXT NOT NULL,
                watermark TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(file_id, generation, stage)
            )""",
            # A bounded, replayable event log: SSE replays from here, and an id
            # that has aged out becomes `resync_required` instead of a silent gap.
            """CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""",
            'CREATE INDEX IF NOT EXISTS event_log_scope ON event_log(scope, id)',
            'CREATE INDEX IF NOT EXISTS event_log_expiry ON event_log(expires_at)',
            """CREATE TABLE IF NOT EXISTS collector_runs (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                mapping_id TEXT,
                state TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                detail TEXT NOT NULL DEFAULT '{}'
            )""",
            'CREATE INDEX IF NOT EXISTS collector_runs_source ON collector_runs(source_id, started_at DESC)',
        ],
    ),
    (
        8,
        'multi-root-library',
        [
            # A root needs an identity that survives a remount: the record keeps the
            # device id and a sentinel token, and every write re-checks both.
            'ALTER TABLE storage_roots ADD COLUMN revision INTEGER NOT NULL DEFAULT 1',
            "ALTER TABLE storage_roots ADD COLUMN volume_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE storage_roots ADD COLUMN sentinel_id TEXT NOT NULL DEFAULT ''",
            'ALTER TABLE storage_roots ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0',
            "ALTER TABLE storage_roots ADD COLUMN probe_state TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE storage_roots ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE storage_roots ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''",
            # Not UNIQUE: rows written before this migration may share a parent
            # directory, and a failed migration is worse than a duplicate that the
            # registration API refuses. Uniqueness is enforced in RootService.
            'CREATE INDEX IF NOT EXISTS storage_roots_path ON storage_roots(path)',
            "ALTER TABLE logical_files ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE logical_files ADD COLUMN scope TEXT NOT NULL DEFAULT 'shared'",
            "ALTER TABLE logical_files ADD COLUMN state TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE logical_files ADD COLUMN observed_at TEXT NOT NULL DEFAULT ''",
            'ALTER TABLE logical_files ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0',
            'CREATE INDEX IF NOT EXISTS logical_files_root ON logical_files(root_id, rel_path)',
            'CREATE INDEX IF NOT EXISTS logical_files_state ON logical_files(root_id, state)',
            """CREATE TABLE IF NOT EXISTS storage_root_probes (
                root_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                writable INTEGER NOT NULL DEFAULT 0,
                volume_id TEXT NOT NULL DEFAULT '',
                sentinel_id TEXT NOT NULL DEFAULT '',
                free_bytes INTEGER,
                total_bytes INTEGER,
                fs_type TEXT NOT NULL DEFAULT 'unknown',
                checked_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}'
            )""",
            'CREATE INDEX IF NOT EXISTS storage_root_probes_recent ON storage_root_probes(root_id, checked_at DESC)',
            # One recycling bin per root: `trash_path` is recorded before the move
            # so a crash between record and rename is repairable, not lost.
            """CREATE TABLE IF NOT EXISTS trash_entries (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                root_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                generation INTEGER NOT NULL,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                owner TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'shared',
                digest TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                deleted_by TEXT NOT NULL,
                restored_at TEXT,
                restore_path TEXT,
                detail TEXT NOT NULL DEFAULT '{}'
            )""",
            'CREATE INDEX IF NOT EXISTS trash_entries_root ON trash_entries(root_id, deleted_at DESC)',
            'CREATE INDEX IF NOT EXISTS trash_entries_owner ON trash_entries(owner, deleted_at DESC)',
            # Reversible physical-name mapping for names a log root cannot hold as
            # written (control characters, over-long components, Windows shapes).
            """CREATE TABLE IF NOT EXISTS catalog_name_map (
                root_id TEXT NOT NULL,
                physical_name TEXT NOT NULL,
                logical_name TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(root_id, physical_name)
            )""",
            # Personal bindings are per namespace; a share-wide write stays in
            # `bindings`/`root_bindings` and requires an administrator.
            """CREATE TABLE IF NOT EXISTS personal_bindings (
                owner TEXT NOT NULL,
                scope TEXT NOT NULL,
                root_id TEXT NOT NULL,
                path TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, scope, root_id, path)
            )""",
            """CREATE TABLE IF NOT EXISTS root_bindings (
                scope TEXT NOT NULL,
                root_id TEXT NOT NULL,
                path TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                PRIMARY KEY(scope, root_id, path)
            )""",
            'CREATE INDEX IF NOT EXISTS root_bindings_rule ON root_bindings(rule_id)',
            'CREATE INDEX IF NOT EXISTS personal_bindings_rule ON personal_bindings(rule_id)',
        ],
    ),
]

EXPECTED_TABLES = {
    'storage_roots', 'logical_files', 'generations', 'jobs', 'job_leases',
    'job_checkpoints', 'cache_versions', 'index_publications',
    'upload_sessions', 'upload_parts', 'query_snapshots', 'job_actions',
    'layer_versions', 'layer_pointers', 'layer_pins', 'layer_events', 'query_hits',
    'collector_credentials', 'remote_sources', 'directory_mappings', 'source_probes',
    'source_actions', 'source_files', 'archive_intervals', 'archive_manifests',
    'index_watermarks', 'event_log', 'collector_runs',
    'storage_root_probes', 'trash_entries', 'catalog_name_map',
    'personal_bindings', 'root_bindings',
}


def checksum(statements: list[str]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(' '.join(statement.split()).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()[:16]


def _ensure_table(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""")


def current_version(db: sqlite3.Connection) -> int:
    _ensure_table(db)
    row = db.execute('SELECT MAX(version) AS v FROM schema_migrations').fetchone()
    return int(row['v'] or 0) if row else 0


def _backup_once(path: Path, version: int) -> Path | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    target = path.with_name('%s.pre-r6-%d' % (path.name, version))
    if target.exists():
        return target
    shutil.copy2(path, target)
    return target


def apply(store, steps=None) -> dict:
    """Apply pending steps. Returns a summary a caller can log or assert on."""
    steps = STEPS if steps is None else steps
    applied = []
    backup = None
    # The bookkeeping table is committed on its own, before the step transaction.
    # Otherwise a failing step would roll its own version record away and the next
    # attempt could not tell "never started" from "half applied".
    with store.connect() as db:
        _ensure_table(db)
    with store.transaction() as db:
        version = current_version(db)
        if version > SCHEMA_VERSION:
            raise MigrationError(
                'schema_too_new',
                '数据库结构版本 %d 高于本程序支持的 %d，请升级程序而不是降级数据。'
                % (version, SCHEMA_VERSION))
        for step_version, name, statements in steps:
            if step_version <= version:
                recorded = db.execute(
                    'SELECT checksum FROM schema_migrations WHERE version=?', (step_version,)).fetchone()
                if recorded and recorded['checksum'] != checksum(statements):
                    raise MigrationError(
                        'migration_checksum_changed',
                        '迁移 %d（%s）的内容在应用后被修改；请新增一个迁移步骤。' % (step_version, name))
                continue
            if step_version != version + 1:
                raise MigrationError('migration_gap', '迁移步骤不连续：期望 %d，得到 %d'
                                     % (version + 1, step_version))
            if backup is None:
                backup = _backup_once(Path(store.path), version)
            for statement in statements:
                db.execute(statement)
            db.execute(
                'INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)',
                (step_version, name, checksum(statements), _stamp()))
            version = step_version
            applied.append({'version': step_version, 'name': name})
        db.execute('PRAGMA user_version=%d' % version)
    return {
        'version': version,
        'applied': applied,
        'backup': str(backup) if backup else None,
        'reused': not applied,
    }


def verify(store) -> dict:
    """Cheap structural check used by the release preflight."""
    with store.connect() as db:
        version = current_version(db)
        present = {
            row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    missing = sorted(EXPECTED_TABLES - present)
    return {
        'version': version,
        'expected': SCHEMA_VERSION,
        'missing': missing,
        'ok': version == SCHEMA_VERSION and not missing,
    }
