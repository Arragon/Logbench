"""R6 frontend assembly: page bundle plus Worker bundles, no build step.

The v3.0.3 page is still assembled from `engine.js + app.js`. R6 adds Worker
entry points that need the same shared modules. Rather than introduce a bundler,
each Worker is served as a concatenation of the shared files it needs, in the
declared order, followed by the entry file. `worker.py --self-test` builds every
bundle so a missing file fails the release preflight instead of the browser.
"""
from pathlib import Path

# Entry file first in the tuple, dependency order in the list.
WORKER_BUNDLES = {
    'regex-executor': ('workers/regex-executor.js', []),
    'local-engine': (
        'workers/local-engine.js',
        [
            'shared/rpc.js',
            'shared/contracts.js',
            'shared/framer.js',
            'shared/query.js',
            'shared/rules.js',
            'shared/candidates.js',
            'shared/time.js',
            'shared/query-jobs.js',
            'shared/regex-supervisor.js',
            'shared/import-jobs.js',
            'shared/local-store.js',
            'shared/index-store.js',
            'shared/engine-core.js',
            'shared/traceback.js',
        ],
    ),
}


# Symbols each bundle must contain; checked by worker.py --self-test so a
# mis-ordered concatenation fails the release preflight, not the browser.
WORKER_SYMBOLS = {
    'local-engine': ['LBRpc', 'LBEngineCore', 'LBQueryJobs', 'LBImportJobs'],
    'regex-executor': ['onmessage', 'terminate'],
}


def bundle(root: Path) -> bytes:
    src = root / 'src'
    app = (src / 'app.js').read_text('utf-8')
    app = app.replace('@@R5@@', (src / 'r5.js').read_text('utf-8'))
    app = app.replace('@@SERVICE@@', (src / 'service.js').read_text('utf-8'))
    return ((src / 'engine.js').read_text('utf-8') + '\n' + app).encode('utf-8')


# Page-side R6 layer: loaded by the new views and by the QA harnesses.
R6_PAGE_PARTS = [
    'shared/rpc.js',
    'shared/contracts.js',
    'shared/query.js',
    'shared/candidates.js',
    'shared/rules.js',
    'shared/framer.js',
    'shared/traceback.js',
    'shared/diagnostics.js',
    'shared/engine-core.js',
    'shared/time.js',
    'shared/query-jobs.js',
    'shared/import-jobs.js',
    'shared/regex-supervisor.js',
    'shared/workspace.js',
    'shared/http.js',
    'providers/local.js',
    'providers/server.js',
    # Cross-source merge (R6-078) before anything that consumes a scope.
    'providers/combined.js',
    'ui/reader-controller.js',
    'ui/highlight-controller.js',
    'ui/search-controller.js',
    'ui/details.js',
    'ui/timeline.js',
    'ui/jobs.js',
    'ui/global-time.js',
    'ui/compare.js',
    'ui/rule-impact.js',
    'ui/cache-manager.js',
    # The header meter is separate from the cache page: it must exist even when
    # that page was never opened, and it reads a totals-only endpoint.
    'ui/cache-meter.js',
    'ui/runtime.js',
    # Unified library and storage roots (R6-056). Loaded before server-mode so a
    # library row can hand a file id to the server reader.
    'ui/library.js',
    'ui/storage-roots.js',
    'ui/server-mode.js',
    # Collection centre (R6-067/068) and, from R6-075, the event client it uses.
    'ui/source-wizard.js',
    'ui/intake.js',
    'events/client.js',
]


def r6_bundle(root: Path) -> bytes:
    src = root / 'src'
    chunks = ['/* LogBench R6 page layer (assembled, no build step) */']
    for part in R6_PAGE_PARTS:
        path = src / part
        if not path.is_file():
            raise FileNotFoundError('r6 page bundle is missing %s' % part)
        chunks.append(path.read_text('utf-8'))
    return ('\n'.join(chunks)).encode('utf-8')


def worker_names():
    return sorted(WORKER_BUNDLES)


def worker_bundle(root: Path, name: str) -> bytes:
    if name not in WORKER_BUNDLES:
        raise KeyError(name)
    entry, parts = WORKER_BUNDLES[name]
    src = root / 'src'
    chunks = []
    for part in parts + [entry]:
        path = src / part
        if not path.is_file():
            raise FileNotFoundError('worker bundle %s is missing %s' % (name, part))
        chunks.append(path.read_text('utf-8'))
    return ('\n'.join(chunks)).encode('utf-8')
