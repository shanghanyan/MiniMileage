"""Test-suite invariants: every test runs OFFLINE and against a throwaway DB.

Two properties the plan promised but the suite did not enforce (§10/§12 Phase 5):

  1. **Hermetic / deterministic.** `MILEAGE_OFFLINE=1` pins the aggregator to its
     `file://` fixtures — no live HTTP, no Wayback — so tests never touch the
     network and can't hang on a blocked egress (the old failure mode: live URLs
     in `sources.yaml` made the fetcher block ~10s/target under the politeness
     backoff, timing the suite out).
  2. **No collateral writes.** Point `MILEAGE_DB` at a temp file so a test run
     never mutates the developer's real `mileage.db` (and SQLite never tries to
     lock a file on a synced/mounted folder, which raised disk-I/O errors).

This file is auto-loaded by pytest AND imported by the standalone
`python tests/test_phaseN.py` entrypoints (see each test's header), so the
guarantees hold no matter how the tests are launched.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("MILEAGE_OFFLINE", "1")
# Keep live API providers self-disabled (no keys) and Redis out of the way.
os.environ.pop("MILEAGE_REDIS_URL", None)
if not os.environ.get("MILEAGE_DB") or os.environ["MILEAGE_DB"] == "mileage.db":
    os.environ["MILEAGE_DB"] = os.path.join(
        tempfile.gettempdir(), "mileage_test_suite.db"
    )
