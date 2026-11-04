"""Multi-root storage: registration, safe access, catalog, trash and bindings.

R6 replaces "the deployment's `data/` directory is the library" with a set of
registered roots that are merged into one logical library. That change is only
safe if every read and write goes through one gate, so the modules here own the
four things a root needs:

* `roots.py`     — the registration records, their revisions and their probes;
* `guard.py`     — path normalisation, mount identity and offline protection;
* `catalog.py`   — logical files, generations and lazy directory listing;
* `trash.py`     — per-root recycling with active-reference protection;
* `bindings.py`  — the layered rule resolution shared by folders and files.

`api.py` maps them onto `/api/v2`, and never accepts a filesystem path from a
caller: a request names a `root_id` plus a relative path, or an opaque
`file_id`, and the guard turns that into a real path.
"""
from __future__ import annotations


class Storage:
    """Wires the storage services once, on the application object.

    Kept as a plain holder rather than a god-object: each service owns one
    concern and the holder only decides construction order (guard → roots →
    bindings → catalog → trash → api).
    """

    def __init__(self, app):
        from .guard import StorageGuard
        from .roots import RootService
        from .bindings import BindingService
        from .catalog import CatalogService
        from .trash import TrashService
        from .api import StorageApi

        # Assigned before the API is built: the API looks the services up through
        # the application, so the holder has to exist first.
        app.storage = self
        self.guard = StorageGuard(app)
        self.roots = RootService(app)
        self.bindings = BindingService(app)
        self.catalog = CatalogService(app)
        self.trash = TrashService(app)
        self.api = StorageApi(app)

    def boot(self) -> dict:
        """Register the legacy root and repair interrupted recycling moves."""
        report = {'legacy': None, 'repaired': []}
        report['legacy'] = self.roots.ensure_legacy()
        try:
            report['repaired'] = self.trash.repair()['repaired']
        except Exception:  # noqa: BLE001 - a repair must never block startup
            report['repaired'] = []
        return report
