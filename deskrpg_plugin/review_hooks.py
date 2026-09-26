"""Hook adapters for per-card approval: gather what a kanban worker knows about its card and apply review_rules.

A kanban worker is a `hermes -p <profile> chat -q "work kanban task <id>"` process. The dispatcher pins its card,
run, board and profile in the environment (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_BOARD`,
`HERMES_PROFILE_NAME` on upstream main); `tests/integration/test_worker_env_real.py` pins what a real worker sees.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerContext:
    task_id: str
    run_id: int | None
    profile: str
    board: str | None


def _profile(api) -> str:
    # Upstream main pins the worker's profile; older builds only have the profile home to go on.
    pinned = (os.environ.get("HERMES_PROFILE_NAME") or os.environ.get("HERMES_PROFILE") or "").strip()
    if pinned:
        return pinned
    try:
        return str(api.get_active_profile_name())
    except Exception:  # noqa: BLE001 — a worker with an unreadable home still gets a (non-matching) name
        return "unknown"


def worker_context(api) -> WorkerContext | None:
    """The card this process works on, or None outside a kanban worker."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
    return WorkerContext(
        task_id=task_id,
        run_id=int(raw_run) if raw_run.isdigit() else None,
        profile=_profile(api),
        board=board,
    )
