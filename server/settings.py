"""Runtime settings that outlive a restart but do not belong in `config.json`.

`config.json` is read once at boot and describes how the process starts
(directories, ports, allowed hosts). A cache quota is different: an operator
sets it while the server is running, and the next read must honour it without a
restart.

The `settings` table is not new — the v3 workbench has had `(key, value,
revision)` since before R6, and `runtime_budget` already lives in it. Reusing it
means one place holds every runtime knob, and an upgrade does not have to
rewrite a table an older binary still reads. Values are JSON text, so a reader
never has to guess whether `"500"` means the integer 500 or the string `"500"`.

A missing key and a null value are deliberately different: missing means "never
configured" (the caller's default applies), null means "no limit".
"""
from __future__ import annotations

import json
from typing import Any, Optional

CACHE_QUOTA = 'cache.quotaBytes'


class SettingsService:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ----------------------------------------------------------------- reading
    def get(self, key: str, default: Any = None) -> Any:
        with self.store.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (ValueError, TypeError):
            # A hand-edited row must not take the page down; the caller's default
            # is the honest answer when the stored value cannot be parsed.
            return default

    def revision(self, key: str) -> int:
        with self.store.connect() as db:
            row = db.execute('SELECT revision FROM settings WHERE key=?', (key,)).fetchone()
        return int(row['revision']) if row else 0

    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        value = self.get(key, default)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ----------------------------------------------------------------- writing
    def set(self, actor: dict, key: str, value: Any) -> dict:
        from .app import compact
        payload = compact(value)
        with self.store.transaction() as db:
            before = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            db.execute(
                'INSERT INTO settings(key,value,revision) VALUES(?,?,1) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, '
                'revision=settings.revision+1', (key, payload))
            after = db.execute('SELECT revision FROM settings WHERE key=?', (key,)).fetchone()
            self.store.audit(db, actor.get('id') or actor.get('ip') or '', 'settings.write',
                             key, {'before': json.loads(before['value']) if before else None,
                                   'after': value,
                                   'revision': after['revision'] if after else None})
        return {'key': key, 'value': value,
                'revision': after['revision'] if after else None}
