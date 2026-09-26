"""The card's `review` field on a Hermes without the policy core patch, built from the approval store.

DeskRPG reads one shape (`KanbanReviewState`) whichever way approvals are enforced. With the review hooks the
policy and the human decisions live in the plugin's approval store, and the rest comes from Hermes' own card state
through public reads: the column, the assignee and the card's `review_requested` events.

A person is being waited on exactly when the card is in `review` with nobody assigned.
"""

from __future__ import annotations

import logging

from .kanban_common import run_claimed_from_review

log = logging.getLogger(__name__)

POLICY_VERSION = 1
POLICY_REVISION = 1


def submission_id(run_id) -> str:
    """The id a submission is approved by: the run that asked for review."""
    return f"run:{run_id}"


def open_approval_store(api):
    """The approval store for this Hermes, or None when it cannot be opened (logged)."""
    from . import review_store

    try:
        return review_store.open_store(review_store.sidecar_path(api))
    except Exception as exc:  # noqa: BLE001 — a missing store must not break the card read
        log.warning("approval store unavailable: %s", exc)
        return None


def card_policy(store, task_id: str, board: str | None):
    """The card's own policy, else its board's default, else None."""
    from . import review_store

    policy = review_store.get_policy(store, task_id)
    if policy is not None:
        return policy.mode, policy.reviewer_profile, policy.implementer
    default = review_store.get_board_default(store, board) if board else None
    if default is not None:
        return default[0], default[1], None
    return None


def _submissions(api, conn, task_id: str) -> list:
    return [e for e in api.list_events(conn, task_id) if e.kind == "review_requested"]


def _active_reviewer_run(api, conn, task) -> bool:
    run_id = getattr(task, "current_run_id", None)
    return task.status == "running" and run_claimed_from_review(api, conn, task.id, run_id)


def review_state(api, conn, store, task, board: str | None = None) -> dict | None:
    """`KanbanReviewState` for `task`, or None when the card has no approval policy."""
    from . import review_store

    resolved = card_policy(store, task.id, board)
    if resolved is None:
        return None
    mode, reviewer_profile, _implementer = resolved
    submissions = _submissions(api, conn, task.id)
    latest = submissions[-1] if submissions else None
    decided = review_store.decisions(store, task.id)
    approval_row = next((d for d in reversed(decided) if d["verdict"] == "approve"), None)

    reason = None
    if task.status == "done":
        state = "approved"
        if approval_row is None:
            reason = "external_done"
    elif task.status == "review" and not task.assignee:
        state = "human_required"
    elif task.status == "review" or _active_reviewer_run(api, conn, task):
        state = "reviewing"
    elif latest is not None:
        state = "submitted"
    else:
        state = "awaiting_submission"

    submission = None
    if latest is not None and latest.run_id is not None:
        submission = {"id": submission_id(latest.run_id), "run_id": latest.run_id, "hash": "",
                      "policy_revision": POLICY_REVISION}
    approval = None
    if approval_row is not None and task.status == "done":
        approval = {
            "actor_kind": approval_row["actor_kind"],
            "actor_id": approval_row["actor"],
            "submission_id": submission["id"] if submission else "",
            "policy_revision": POLICY_REVISION,
            "hash": "",
            "approved_at": int(approval_row["at"]),
            "request_id": None,
        }
    return {
        "policy": {"version": POLICY_VERSION, "mode": mode, "reviewer_profile": reviewer_profile},
        "policy_revision": POLICY_REVISION,
        "submission": submission,
        "review_round": len(submissions),
        "state": state,
        "reason": reason,
        "approval": approval,
    }


def hooks_enabled(api) -> bool:
    """Whether approvals are enforced by the review hooks (and not by the policy core patch)."""
    from . import contract_fields

    if contract_fields.has_review_policy(api):
        return False
    probe = getattr(contract_fields, "has_review_hooks", None)
    return bool(probe and probe(api))


def review_field(api, conn, task, board: str | None, store=None):
    """The card's `review` value for any Hermes: the patch core's own state, the approval store's, or None.

    `store` lets a board listing open the approval store once for all its cards."""
    from . import contract_fields

    if contract_fields.has_review_policy(api):
        return api.get_review_state(conn, task.id)
    if not hooks_enabled(api):
        return None
    own = store is None
    if own:
        store = open_approval_store(api)
    if store is None:
        return None
    try:
        return review_state(api, conn, store, task, board)
    finally:
        if own:
            store.close()
