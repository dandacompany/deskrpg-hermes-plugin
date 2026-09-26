"""Per-card approval on a real Hermes, with real dispatcher-spawned kanban workers and a scripted model.

Every worker profile loads this plugin (as `worker_plugin.ensure` sets it up), so the approval hooks run inside the
worker processes; the test process plays the gateway: it creates cards through the plugin routes, runs dispatch
ticks and makes the human decisions. Each profile's model is scripted by its name:

- an implementer tries `kanban_complete` first and, whenever a hook refuses a call, submits with
  `kanban_request_review` — which is exactly what the refusal messages tell it to do;
- `rev_no` sends the card back with `kanban_request_changes`;
- `shell` tries to complete its card from the terminal.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from deskrpg_plugin import contract_fields, review_hooks, review_store, worker_plugin
from deskrpg_plugin.review_contract import (
    BLOCK_RETURN_MESSAGE, BLOCK_SUBMIT_MESSAGE, BLOCK_TERMINAL_MESSAGE, BLOCK_UNAVAILABLE_MESSAGE, BLOCK_VERDICT_MESSAGE,
)
from deskrpg_plugin.review_state import hooks_enabled
from tests.integration.test_kanban_real import BOARD, B, _board
from tests.integration.worker_harness import ScriptedModel, configure_profile, tool_results, wait_until

# The flows below are the hooks' path, which the plugin takes on a Hermes without the policy core patch. On the
# pinned patched core the plugin takes the patch's path instead, so they run only in the upstream job; the test
# at the bottom pins which path each core gets.
pytestmark = pytest.mark.integration
upstream_only = pytest.mark.upstream_only

HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}
USER = {"X-DeskRPG-User-Id": "u1"}
PROFILES = ("impl", "rev", "rev_no", "shell")


def _refused(results) -> bool:
    return any(msg in r for r in results for msg in (
        BLOCK_SUBMIT_MESSAGE, BLOCK_VERDICT_MESSAGE, BLOCK_RETURN_MESSAGE, BLOCK_TERMINAL_MESSAGE,
        BLOCK_UNAVAILABLE_MESSAGE))


def _script(model, messages):
    results = tool_results(messages)
    if model == "rev_no":
        return ("tool", "kanban_request_changes", {"reason": "the numbers do not add up"}) if not results else (
            "text", "sent back")
    if model == "shell" and not results:
        task = os.environ.get("REVIEW_IT_TASK", "")
        return ("tool", "terminal", {"command": f"hermes kanban complete {task} --summary done"})
    if not results:
        return ("tool", "kanban_complete", {"summary": f"{model}: done"})
    if _refused(results[-1:]):
        return ("tool", "kanban_request_review", {"summary": f"{model}: submitted for review"})
    return ("text", "finished")


@pytest.fixture
def crew(api, make_profile, monkeypatch):
    model = ScriptedModel(_script)
    for name in PROFILES:
        home = make_profile(name)
        configure_profile(home, model=name, base_url=model.base_url)
        worker_plugin.ensure(api, name)
    monkeypatch.delenv("HERMES_BIN", raising=False)
    # The gateway side of this process: the plugin's register() would have set this.
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", True)
    if not hooks_enabled(api):
        pytest.fail("the review hooks are not available on this Hermes build")
    yield model
    # A failed flow can leave a worker looping on its card; stop every worker this test started.
    if api.board_exists(BOARD):
        with api.connect_closing(board=BOARD) as conn:
            for task in api.list_tasks(conn):
                if task.status == "running":
                    api.reclaim_task(conn, task.id, reason="test teardown")
    model.close()


def _task(api, task_id):
    with api.connect_closing(board=BOARD) as conn:
        return api.get_task(conn, task_id)


def _events(api, task_id):
    with api.connect_closing(board=BOARD) as conn:
        return [e.kind for e in api.list_events(conn, task_id)]


async def _create(client, *, assignee="impl", policy=HUMAN, title="card"):
    body = {"title": title, "assignee": assignee}
    if policy is not None:
        body["review_policy"] = policy
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json=body)
    assert resp.status == 201, await resp.text()
    return (await resp.json())["task"]["id"]


async def _drive(client, api, task_id, settled, timeout=150):
    """Run dispatch ticks until `settled(task)` holds and no worker holds the card."""
    import asyncio
    import time

    end = time.time() + timeout
    while time.time() < end:
        task = _task(api, task_id)
        if task.status != "running" and settled(task):
            return task
        if task.status != "running":
            resp = await client.post(f"/deskrpg/kanban/dispatch{B}")
            assert resp.status == 200, await resp.text()
        await asyncio.sleep(2)
    return _task(api, task_id)


def _waiting_for_a_person(task) -> bool:
    return task.status == "review" and not task.assignee


async def _review(client, task_id):
    return (await (await client.get(f"/deskrpg/kanban/tasks/{task_id}{B}")).json())["task"]["review"]


async def _approve(client, task_id):
    review = await _review(client, task_id)
    return await client.post(f"/deskrpg/kanban/tasks/{task_id}/approve{B}",
                             json={"submission_id": review["submission"]["id"], "request_id": "r1"}, headers=USER)


async def _send_back(client, task_id):
    return await client.post(f"/deskrpg/kanban/tasks/{task_id}/request-changes{B}", json={"comment": "fix it"})


def _saw(model, message) -> bool:
    return any(message in r for req in model.requests for r in tool_results(req.get("messages") or []))


@upstream_only
async def test_human_card_waits_for_a_person_and_completes_on_approval(client, api, crew):
    await _board(client)
    tid = await _create(client)
    task = await _drive(client, api, tid, _waiting_for_a_person)
    assert _waiting_for_a_person(task), (task.status, task.assignee)
    assert _saw(crew, BLOCK_SUBMIT_MESSAGE)
    assert (await _review(client, tid))["state"] == "human_required"

    resp = await _approve(client, tid)
    assert resp.status == 200, await resp.text()
    assert _task(api, tid).status == "done"
    store = review_store.open_store(review_store.sidecar_path(api))
    assert [d["verdict"] for d in review_store.decisions(store, tid)] == ["approve"]
    store.close()


@upstream_only
async def test_a_human_card_sent_back_twice_returns_to_a_person_each_time(client, api, crew):
    await _board(client)
    tid = await _create(client)
    for round_ in range(3):
        task = await _drive(client, api, tid, _waiting_for_a_person)
        assert _waiting_for_a_person(task), (round_, task.status, task.assignee)
        if round_ < 2:
            assert (await _send_back(client, tid)).status == 200
            back = _task(api, tid)
            assert back.assignee == "impl" and back.status in ("ready", "todo"), (back.status, back.assignee)
    assert "triage" not in {e for e in _events(api, tid)}
    assert _task(api, tid).status != "triage"


@upstream_only
async def test_agent_review_approves_or_sends_back(client, api, crew):
    await _board(client)
    ok = await _create(client, policy={"version": 1, "mode": "agent", "reviewer_profile": "rev"})
    assert (await _drive(client, api, ok, lambda t: t.status == "done")).status == "done"

    no = await _create(client, policy={"version": 1, "mode": "agent", "reviewer_profile": "rev_no"}, title="no")
    task = await _drive(client, api, no, lambda t: "changes_requested" in _events(api, no))
    assert "changes_requested" in _events(api, no)
    assert task.assignee == "impl" and task.status != "done"


@upstream_only
async def test_mixed_card_gets_an_ai_verdict_then_a_person_who_sends_it_to_the_implementer(client, api, crew):
    await _board(client)
    tid = await _create(client, policy={"version": 1, "mode": "mixed", "reviewer_profile": "rev"})
    task = await _drive(client, api, tid,
                        lambda t: _waiting_for_a_person(t) and _events(api, tid).count("review_requested") >= 2)
    assert _waiting_for_a_person(task), (task.status, task.assignee)
    assert _saw(crew, BLOCK_VERDICT_MESSAGE)

    assert (await _send_back(client, tid)).status == 200
    back = _task(api, tid)
    assert back.assignee == "impl", back.assignee  # not the AI reviewer who submitted last


@upstream_only
async def test_a_run_that_slips_onto_a_waiting_card_is_sent_back(client, api, crew):
    await _board(client)
    tid = await _create(client)
    await _drive(client, api, tid, _waiting_for_a_person)
    # Reproduce the gap between a submission and the unassignment: give the waiting card an assignee again, so
    # the review dispatcher claims it.
    with api.connect_closing(board=BOARD) as conn:
        assert api.assign_task(conn, tid, "impl")
    task = await _drive(client, api, tid, lambda t: _waiting_for_a_person(t) and "claimed" in _events(api, tid)[-6:])
    assert _waiting_for_a_person(task), (task.status, task.assignee)
    assert _saw(crew, BLOCK_RETURN_MESSAGE)
    assert task.status != "done"


@upstream_only
async def test_completing_from_the_terminal_is_refused(client, api, crew, monkeypatch):
    await _board(client)
    tid = await _create(client, assignee="shell")
    monkeypatch.setenv("REVIEW_IT_TASK", tid)
    task = await _drive(client, api, tid, lambda t: t.status in ("review", "done", "blocked"))
    assert _saw(crew, BLOCK_TERMINAL_MESSAGE)
    assert task.status != "done"


@upstream_only
async def test_an_unreadable_approval_store_blocks_completion(client, api, crew):
    await _board(client)
    tid = await _create(client)
    shared = review_store.sidecar_path(api).parent
    for path in shared.iterdir():
        path.chmod(stat.S_IRUSR)
    shared.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        task = await _drive(client, api, tid, lambda t: t.status != "ready" and _saw(crew, BLOCK_UNAVAILABLE_MESSAGE),
                            timeout=90)
    finally:
        shared.chmod(stat.S_IRWXU)
        for path in shared.iterdir():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert _saw(crew, BLOCK_UNAVAILABLE_MESSAGE)
    assert task.status != "done"


@upstream_only
async def test_a_card_without_a_policy_follows_upstream(client, api, crew):
    await _board(client)
    tid = await _create(client, policy=None)
    assert (await _drive(client, api, tid, lambda t: t.status == "done")).status == "done"
    assert (await _review(client, tid)) is None


async def test_each_core_gets_one_approval_path(api, monkeypatch):
    # Runs on every core. A patched core keeps the patch's own enforcement and the hooks' path stays off; a core
    # without the patch uses the hooks once they are registered.
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", True)
    if contract_fields.has_review_policy(api):
        assert not hooks_enabled(api)
    else:
        assert hooks_enabled(api)
