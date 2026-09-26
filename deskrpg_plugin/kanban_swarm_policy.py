"""A swarm whose result cards carry approval policies — assembled here, in one transaction.

Hermes `create_swarm` puts no review policy on any card it makes, so on an approval-policy board its workers,
verifier and synthesizer would finish without anyone approving them. A policy cannot be added afterwards either:
`update_review_policy` refuses a task that has none, and `create_swarm` promotes the workers to `ready` right after
its commit, so the dispatcher could claim one before a late policy lands.

So this module runs Hermes' own steps inside ONE write transaction, adding the policies before the commit:

1. `_create_swarm_uncommitted` — the same topology Hermes builds (root, workers, verifier, synthesizer).
2. `create_policy` on every result card — workers get the requested policy (inherited like any child of the
   root), the verifier and the synthesizer are always human-approved: Hermes forces human on children of
   protected cards anyway, and the final result is where a person decides.
3. `_activate_root_inline` — the root flips `blocked → done`. The root is a **structure card**: no result, no
   attachments, no policy. Its completion means "the split has started", not an approval; with no policy row the
   `review_policy_done_guard` trigger does not apply to it, so the flip cannot collide with it.

After the commit, `recompute_ready` promotes the workers — they already carry their policies — and the completion
hook fires for the root, exactly as Hermes does. This relies on Hermes internals (decision 2026-09-26, option B);
`contract_fields.has_swarm_policy_symbols` checks them and their signatures, and the route is refused otherwise.
"""

from __future__ import annotations

HUMAN_POLICY = {"version": 1, "mode": "human", "reviewer_profile": None}
ACTIVATION_SUMMARY = "Swarm topology planned; root remains the shared blackboard."
VERIFIER_TITLE = "Verify swarm outputs"
SYNTHESIZER_TITLE = "Synthesize swarm outputs"


class SwarmPolicyConflict(Exception):
    """An existing swarm (idempotent replay) whose result cards lack a policy — it was made without one."""


def _ensure_policy(api, conn, task_id, policy, assignee):
    """Adds the policy unless Hermes already attached one (it forces human on children of protected sources)."""
    if api.get_review_state(conn, task_id) is None:
        api.create_policy(conn, task_id, policy, assignee)


def result_card_ids(created):
    return [*created.worker_ids, created.verifier_id, created.synthesizer_id]


def create_swarm_with_policy(
    api, conn, *, goal, workers, verifier_assignee, synthesizer_assignee, worker_policy,
    tenant=None, created_by="swarm-orchestrator", priority=0, idempotency_key=None,
):
    workers = list(workers)
    activated = False
    with api.write_txn(conn):
        created = api._create_swarm_uncommitted(
            conn, goal=goal, workers=workers, verifier_assignee=verifier_assignee,
            synthesizer_assignee=synthesizer_assignee, root_title=None, verifier_title=VERIFIER_TITLE,
            synthesizer_title=SYNTHESIZER_TITLE, tenant=tenant, created_by=created_by, workspace_kind=None,
            workspace_path=None, priority=priority, idempotency_key=idempotency_key,
        )
        root = api.get_task(conn, created.root_id)
        if root is None or root.status != "blocked":
            # Idempotent replay: the swarm already exists. Never retrofit policies onto cards that may be running.
            if any(api.get_review_state(conn, task_id) is None for task_id in result_card_ids(created)):
                raise SwarmPolicyConflict(created.root_id)
            return created
        for task_id, spec in zip(created.worker_ids, workers):
            policy = api.inherited_policy(conn, worker_policy, (created.root_id,))
            _ensure_policy(api, conn, task_id, policy, spec.profile)
        _ensure_policy(api, conn, created.verifier_id, HUMAN_POLICY, verifier_assignee)
        _ensure_policy(api, conn, created.synthesizer_id, HUMAN_POLICY, synthesizer_assignee)
        if api.get_review_state(conn, created.root_id) is not None:
            raise RuntimeError("swarm root must stay a structure card without a review policy")
        if not api._activate_root_inline(
            conn, created.root_id, summary=ACTIVATION_SUMMARY,
            metadata={"kind": "kanban_swarm_v1", "goal": goal.strip(), "worker_count": len(created.worker_ids)},
        ):
            raise RuntimeError("could not activate the completed swarm topology")
        activated = True
    if activated:
        # After commit, like Hermes: recompute_ready opens its own transaction.
        api.recompute_ready(conn)
        root = api.get_task(conn, created.root_id)
        run = api.latest_run(conn, created.root_id)
        api._fire_kanban_lifecycle_hook(
            "kanban_task_completed", created.root_id, board=api.get_current_board(),
            assignee=root.assignee if root else None, run_id=run.id if run else None,
            summary=ACTIVATION_SUMMARY,
        )
    return created
