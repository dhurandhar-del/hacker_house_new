"""Which run directory the service reads: cases/*.json, containment/*.json, audit/ledger.jsonl.

In order:

1. ``SENTINEL_RUN_DIR``, when set.
2. ``eval/out/regulator_demo``, when it holds investigations (a developer's own run).
3. ``backend/var/regulator_run``, seeded on first start from the committed demo run in
   ``demo/regulator_run`` — the twenty Round 2 investigations and their hash-chained audit
   ledger. ``backend/var`` is the service's writable state directory on the server, so new
   sign-ins, approvals and questions append to the same chain, which stays intact.

Without (3) a fresh deployment would show an empty regulator view: ``eval/out`` is not in
the repository, so the investigations never reach the server.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL_RUN = ROOT / "eval" / "out" / "regulator_demo"
DEMO_RUN = ROOT / "demo" / "regulator_run"
SEEDED_RUN = ROOT / "backend" / "var" / "regulator_run"


def _has_investigations(d: Path) -> bool:
    return any((d / "cases").glob("*.json"))


def resolve() -> Path:
    if os.getenv("SENTINEL_RUN_DIR"):
        return Path(os.environ["SENTINEL_RUN_DIR"])
    if _has_investigations(LOCAL_RUN):
        return LOCAL_RUN
    if not _has_investigations(SEEDED_RUN) and _has_investigations(DEMO_RUN):
        SEEDED_RUN.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(DEMO_RUN, SEEDED_RUN, dirs_exist_ok=True)
    return SEEDED_RUN
