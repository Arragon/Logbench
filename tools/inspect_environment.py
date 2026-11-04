#!/usr/bin/env python3
"""Print the R6 capability report for this machine.

    env -u PYTHONPATH python3.9 tools/inspect_environment.py --dir data --out qa/evidence/capabilities.json

Never installs anything and never prints the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.capabilities import collect, main as capabilities_main, summary  # noqa: E402


def run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--dir' not in argv:
        argv += ['--dir', str(ROOT / 'data')]
    if '--json' in argv:
        argv.remove('--json')
    if '--out' in argv:
        return capabilities_main(argv)
    import json
    print(json.dumps(collect(argv[argv.index('--dir') + 1]), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    if '--summary' in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != '--summary']
        if '--dir' not in argv:
            argv += ['--dir', str(ROOT / 'data')]
        print(summary(collect(argv[argv.index('--dir') + 1])))
        raise SystemExit(0)
    raise SystemExit(run())
