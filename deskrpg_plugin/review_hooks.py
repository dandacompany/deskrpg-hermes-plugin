"""Hook adapters for per-card approval: gather what a kanban worker knows about its card and apply review_rules.

A kanban worker is a `hermes -p <profile> chat -q "work kanban task <id>"` process. The dispatcher pins its card,
run, board and profile in the environment (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_BOARD`,
`HERMES_PROFILE_NAME` on upstream main); `tests/integration/test_worker_env_real.py` pins what a real worker sees.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .kanban_common import run_claimed_from_review
from .review_contract import BLOCK_UNAVAILABLE_MESSAGE
from .review_rules import (
    TERMINAL_TOOLS, Situation, decide_pre, is_terminal_completion, resolve_policy, should_release_to_human,
)
from .review_store import get_board_default, get_policy, open_store, sidecar_path


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


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


logger = logging.getLogger("deskrpg_plugin")

# Set by the plugin's register(): both hooks are in place in this process. `review_hooks_v1` is announced only then.
HOOKS_REGISTERED = False

_GUARDED_TOOLS = frozenset({"kanban_complete", "kanban_request_review", "kanban_request_changes", *TERMINAL_TOOLS})
# Tools whose call is refused when the approval state cannot be read — completing without knowing the policy
# would skip an approval that may be required.
_FAIL_CLOSED_TOOLS = frozenset({"kanban_complete", *TERMINAL_TOOLS})


def situation(api, ctx: WorkerContext) -> Situation:
    """What the hooks decide on: the card's effective policy, and whether this run reviews or waits for a person.

    A run claimed from the `review` column is a review run. On a human card, or on a mixed card when the run's
    profile is not the policy's reviewer, such a run can only have started in the moment between a submission and
    the hook unassigning the card — the card is waiting for a person, so the run is sent back."""
    try:
        store = open_store(sidecar_path(api))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deskrpg] approval store unavailable: %s", type(exc).__name__)
        return Situation(None, ctx.profile, False, False, store_ok=False)
    try:
        card = get_policy(store, ctx.task_id)
        board_default = get_board_default(store, ctx.board) if ctx.board else None
    finally:
        store.close()
    with api.connect_closing(board=ctx.board) as conn:
        task = api.get_task(conn, ctx.task_id)
        run_id = ctx.run_id if ctx.run_id is not None else getattr(task, "current_run_id", None)
        from_review = run_claimed_from_review(api, conn, ctx.task_id, run_id)
    policy = resolve_policy(card, board_default, ctx.task_id, getattr(task, "assignee", None))
    waiting = bool(
        policy is not None and from_review
        and (policy.mode == "human" or (policy.mode == "mixed" and ctx.profile != policy.reviewer_profile))
    )
    return Situation(policy, ctx.profile, from_review and not waiting, waiting, store_ok=True)


def _result_dict(result) -> dict:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def make_pre_hook(api):
    def hook(tool_name=None, args=None, **_kwargs):
        if tool_name not in _GUARDED_TOOLS:
            return None
        args = args if isinstance(args, dict) else {}
        if tool_name in TERMINAL_TOOLS and not is_terminal_completion(str(args.get("command", ""))):
            return None
        ctx = worker_context(api)
        if ctx is None:
            return None
        try:
            return decide_pre(tool_name, args, situation(api, ctx))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deskrpg] review pre hook failed for %s: %s", tool_name, type(exc).__name__)
            if tool_name in _FAIL_CLOSED_TOOLS:
                return {"action": "block", "message": BLOCK_UNAVAILABLE_MESSAGE}
            return None

    return hook


def make_post_hook(api):
    def hook(tool_name=None, args=None, result=None, **_kwargs):
        if tool_name != "kanban_request_review":
            return None
        ctx = worker_context(api)
        if ctx is None:
            return None
        try:
            if not should_release_to_human(tool_name, _result_dict(result), situation(api, ctx)):
                return None
            with api.connect_closing(board=ctx.board) as conn:
                api.assign_task(conn, ctx.task_id, None)
        except Exception as exc:  # noqa: BLE001 — the card stays assigned; the next run is sent back by the pre hook
            logger.warning("[deskrpg] review post hook failed: %s", type(exc).__name__)
        return None

    return hook
