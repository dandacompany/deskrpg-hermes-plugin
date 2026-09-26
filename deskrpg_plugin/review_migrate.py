"""Move approval policies from the policy core patch into the plugin's approval store, once, per board.

Installs that ran the Dante Labs policy core patch keep each card's policy in the patch's `task_review_policies`
table inside the board database. This reads a Dante Labs patch table once; never writes it — the table is opened
read-only. What it writes:

- the approval store: one policy per card (`source="migrated"`) and, for cards the patch recorded as approved,
  who approved them;
- Hermes, through the public `assign_task`: a card the patch held for a person in `review` is left unassigned,
  which is how the review hooks represent "waiting for a person".

Run it on the gateway host before switching the core back to upstream Hermes:

    python -m deskrpg_plugin.review_migrate --board <slug> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

PATCH_TABLE = "task_review_policies"


def _patch_rows(db_path) -> list[dict]:
    """The patch table's rows, read through a read-only connection. Empty when the table does not exist."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (PATCH_TABLE,)
        ).fetchone()
        if not exists:
            return []
        return [dict(r) for r in conn.execute(f"SELECT * FROM {PATCH_TABLE} ORDER BY task_id")]
    finally:
        conn.close()


def _json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def migrate(api, board: str, board_conn, store, *, dry_run: bool = False) -> dict:
    """Copy the board's patch-era policies into `store`. Returns what it did (or would do, with `dry_run`)."""
    from . import review_store

    summary = {"policies": 0, "approvals": 0, "waiting_human": 0, "skipped": []}
    for row in _patch_rows(api.kanban_db_path(board=board)):
        task_id = row["task_id"]
        policy = _json(row.get("policy")) or {}
        mode, reviewer = policy.get("mode"), policy.get("reviewer_profile")
        task = api.get_task(board_conn, task_id)
        if task is None:
            summary["skipped"].append({"task_id": task_id, "reason": "task_not_found"})
            continue
        submission = _json(row.get("submission")) or {}
        implementer = submission.get("implementer") or task.assignee
        try:
            migrated = review_store.Policy(task_id, mode, implementer, reviewer, "migrated")
            if not dry_run:
                review_store.put_policy(store, migrated)
        except ValueError as exc:
            summary["skipped"].append({"task_id": task_id, "reason": str(exc)})
            continue
        summary["policies"] += 1

        approval = _json(row.get("approval"))
        if row.get("state") == "approved" and approval:
            already = any(d["verdict"] == "approve" for d in review_store.decisions(store, task_id))
            if not already:
                if not dry_run:
                    review_store.record_decision(
                        store, task_id, approval.get("actor_kind") or "human", approval.get("actor_id") or "unknown",
                        "approve", None,
                    )
                summary["approvals"] += 1

        if row.get("state") == "human_required":
            if task.status != "review":
                summary["skipped"].append({"task_id": task_id, "reason": f"waiting_card_in_{task.status}"})
                continue
            if task.assignee:
                if not dry_run and not api.assign_task(board_conn, task_id, None):
                    summary["skipped"].append({"task_id": task_id, "reason": "unassign_refused"})
                    continue
            summary["waiting_human"] += 1
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deskrpg_plugin.review_migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--board", required=True, help="kanban board slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args(argv)

    from . import _hermes_api, review_store
    from .common import board_conn

    api = _hermes_api.load()
    if not api.board_exists(args.board):
        print(json.dumps({"error": "board_not_found", "board": args.board}))
        return 2
    store = review_store.open_store(review_store.sidecar_path(api))
    try:
        with board_conn(api, args.board) as conn:
            result = migrate(api, args.board, conn, store, dry_run=args.dry_run)
    finally:
        store.close()
    print(json.dumps({"board": args.board, "dry_run": args.dry_run, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
