"""Pure approval decisions for the review hooks. No I/O: review_hooks gathers the situation and applies the result."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .review_contract import (
    BLOCK_RETURN_MESSAGE, BLOCK_SUBMIT_MESSAGE, BLOCK_TERMINAL_MESSAGE, BLOCK_UNAVAILABLE_MESSAGE, BLOCK_VERDICT_MESSAGE,
)
from .review_store import Policy

TERMINAL_TOOLS = ("terminal", "shell", "bash")
_COMPLETE_RE = re.compile(r"\bhermes\b.*\bkanban\s+(complete|done)\b|\bhermes\b.*\bkanban\b.*--status[= ]done\b")


@dataclass(frozen=True)
class Situation:
    policy: Policy | None
    caller: str
    reviewer_run: bool
    waiting_human: bool
    store_ok: bool


def resolve_policy(card, board_default, task_id, assignee):
    if card is not None:
        return card
    if board_default is None:
        return None
    mode, reviewer = board_default
    return Policy(task_id, mode, assignee, reviewer, "board_default")


def is_terminal_completion(command: str) -> bool:
    return bool(_COMPLETE_RE.search(command or ""))


def _block(message):
    return {"action": "block", "message": message}


def decide_pre(tool, args, s):
    if tool == "kanban_complete":
        if not s.store_ok:
            return _block(BLOCK_UNAVAILABLE_MESSAGE)
        p = s.policy
        if p is None:
            return None
        if s.waiting_human:
            return _block(BLOCK_RETURN_MESSAGE)
        if s.reviewer_run and p.mode == "agent":
            return None
        if s.reviewer_run and p.mode == "mixed":
            return _block(BLOCK_VERDICT_MESSAGE)
        return _block(BLOCK_SUBMIT_MESSAGE)
    if tool == "kanban_request_changes" and s.policy is not None and s.waiting_human:
        # A run that slipped onto a card waiting for a person must not send it back on its own.
        return _block(BLOCK_RETURN_MESSAGE)
    if tool == "kanban_request_review" and s.policy is not None:
        rest = {k: v for k, v in args.items() if k != "reviewer"}
        if s.policy.mode == "human" or (s.policy.mode == "mixed" and s.reviewer_run):
            return {"action": "modify", "args": rest}
        return {"action": "modify", "args": {**rest, "reviewer": s.policy.reviewer_profile}}
    if tool in TERMINAL_TOOLS and s.policy is not None and is_terminal_completion(str(args.get("command", ""))):
        return _block(BLOCK_TERMINAL_MESSAGE)
    return None


def should_release_to_human(tool, result, s):
    if tool != "kanban_request_review" or s.policy is None:
        return False
    if not (isinstance(result, dict) and result.get("ok") and result.get("status") == "review"):
        return False
    return s.policy.mode == "human" or (s.policy.mode == "mixed" and s.reviewer_run)
