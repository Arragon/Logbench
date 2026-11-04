#!/usr/bin/env python3
"""R6-016: drive the R5 cache migration page in a real browser.

Thin wrapper over the shared browser runner so the Gate command matches the
ticket naming:

    env -u PYTHONPATH python3.9 qa/r6/cache_migration.py --lan --out qa/evidence/R6-016
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import idb_store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    argv = ['--page', 'qa/r6/migrate_harness.html', '--name', 'migrate']
    argv += [item for item in sys.argv[1:] if item not in ('--page',)]
    if '--out' not in argv:
        argv += ['--out', str(ROOT / 'qa/evidence/R6-016')]
    sys.argv = [sys.argv[0]] + argv
    return idb_store.main()


if __name__ == '__main__':
    raise SystemExit(main())
