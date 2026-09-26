"""Move approval policies from the policy core patch into the plugin's approval store, once, per board.

Installs that ran the Dante Labs policy core patch keep each card's policy in the patch's `task_review_policies`
table inside the board database. This reads a Dante Labs patch table once; never writes it — the table is opened
read-only. What it writes:

- the approval store: one policy per card (`source="migrated"`) and, for cards the patch recorded as approved,
  who approved them;
- Hermes, through the public `assign_task`: a card the patch held for a person in `review` is left unassigned,
  which is how the review hooks represent "waiting for a person".

With `--drop-patch-triggers` it also removes the patch's triggers from the board database. They stay in the
database after the core is replaced, and `review_policy_done_guard` would then refuse to complete any card the patch
had not approved — including through the hooks' own approval. Only the patch's named triggers are dropped; its
tables stay as a record. This is the one write to the board database, and it happens only when asked.

Run it on the gateway host before switching the core back to upstream Hermes:

    python -m deskrpg_plugin.review_migrate --board <slug> [--dry-run] [--drop-patch-triggers]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

PATCH_TABLE = "task_review_policies"

# The triggers the policy core patch installs on the board database (`_install_mutation_triggers`), by name.
# Nothing else is ever dropped.
PATCH_TRIGGERS = (
    "review_policy_done_guard",
    "review_policy_reviewer_edit_guard",
    "review_policy_done_edit_guard",
    "review_policy_condition_changed",
    "review_policy_reopened",
    "review_attachment_insert_guard",
    "review_attachment_insert_invalidate",
    "review_attachment_delete_guard",
    "review_attachment_delete_invalidate",
    "review_attachment_update_guard",
    "review_attachment_update_invalidate",
)


def _present_patch_triggers(db_path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    finally:
        conn.close()
    return [name for name in PATCH_TRIGGERS if name in names]


def drop_patch_triggers(db_path, *, dry_run: bool = False) -> list[str]:
    """Drop the patch's triggers that are present; return their names (the ones that would go, with `dry_run`)."""
    present = _present_patch_triggers(db_path)
    if dry_run or not present:
        return present
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        with conn:
            for name in present:
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    finally:
        conn.close()
    return present


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


def migrate(api, board: str, board_conn, store, *, dry_run: bool = False, drop_triggers: bool = False) -> dict:
    """Copy the board's patch-era policies into `store`. Returns what it did (or would do, with `dry_run`).

    With `drop_triggers`, the patch's triggers are dropped after the policies are copied."""
    from . import review_store

    summary = {"policies": 0, "approvals": 0, "waiting_human": 0, "skipped": []}
    if drop_triggers:
        summary["dropped_triggers"] = []
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
    if drop_triggers:
        summary["dropped_triggers"] = drop_patch_triggers(api.kanban_db_path(board=board), dry_run=dry_run)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deskrpg_plugin.review_migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--board", required=True, help="kanban board slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument("--drop-patch-triggers", action="store_true",
                        help="also drop the policy core patch's triggers from the board database (tables are kept)")
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
            result = migrate(api, args.board, conn, store, dry_run=args.dry_run,
                             drop_triggers=args.drop_patch_triggers)
    finally:
        store.close()
    print(json.dumps({"board": args.board, "dry_run": args.dry_run, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
