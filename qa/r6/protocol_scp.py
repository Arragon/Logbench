"""R6-061/062/065 gate probe for scp.

Run against a controlled endpoint; with no endpoint or no tool it records BLOCKED.
    env -u PYTHONPATH python3.9 qa/r6/protocol_scp.py --endpoint ... --known-hosts ...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol_probe import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main('scp'))
