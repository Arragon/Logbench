#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "${PYTHON39:-python3.9}" -B run.py "$@"
