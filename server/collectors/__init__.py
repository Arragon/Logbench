"""Controlled remote collection (R6-059 … R6-069).

The package owns four things:

* `credentials` — server-only secret storage; the API only ever sees a reference;
* `security` — host admission, argv shape, redaction and DNS re-checks;
* `base` / `process` — the adapter contract and the bounded process runner;
* one module per protocol (`sftp`, `smb`, `http_range`, `http_listing`,
  `scp_snapshot`) plus `sources` / `mappings` / `api` for configuration.

Nothing here archives or schedules. Bytes handed over by an adapter are only
committed by `archive.py` (R6-070), which is the single owner of a generation.
"""

# Importing the adapters registers them. `app.py` imports this package during
# start-up, and `worker.py --self-test` imports it in the release preflight, so a
# syntax error in a collector fails the update instead of the first request.
from . import base, credentials, process, security  # noqa: F401,E402
from . import http_range, http_listing, sftp, smb, scp_snapshot  # noqa: F401,E402
