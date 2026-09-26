"""A person approves or sends back a card waiting for them, on a Hermes without the policy core patch.

The routes and bodies are the ones DeskRPG already uses (`approve` with `submission_id`/`request_id`,
`request-changes` with `comment`); only the enforcement behind them changes.
"""

import pytest
from aiohttp import web

from deskrpg_plugin import kanban_actions
from deskrpg_plugin.auth import Scope, require_auth
from deskrpg_plugin.contract_fields import KANBAN_TASK_ACTIONS
from deskrpg_plugin.review_state import submission_id
from tests.conftest import FakeAdapter
from tests.fakes_kanban_actions import install_fake_kanban_actions
from tests.review_fixtures import hooks_on, rs, store  # noqa: F401 — fixtures

BOARD = "deskrpg-abc"
USER = {"X-DeskRPG-User-Id": "u1", "X-DeskRPG-User-Name": "%EA%B3%BD%EC%A7%80%ED%98%B8"}


@pytest.fixture
def kanban(fake_api, tmp_path, hooks_on):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


@pytest.fixture
async def client(aiohttp_client, fake_api, kanban):
    app = web.Application()
    adapter = FakeAdapter(authorized=True)
    for action in KANBAN_TASK_ACTIONS:
        app.router.add_route("POST", f"/deskrpg/kanban/tasks/{{id}}/{action}",
                             require_auth(adapter, Scope.DEFAULT, kanban_actions.action_handler(fake_api, action)))
    return await aiohttp_client(app)


def _conn(kanban):
    return kanban.connect(board=BOARD)


def _waiting(kanban, store, mode="human", reviewer=None):
    """A card whose implementer submitted and which now waits for a person (review, nobody assigned)."""
    conn = _conn(kanban)
    tid = kanban.create_task(conn, title="card", assignee="impl")
    rs.put_policy(store, rs.Policy(tid, mode, "impl", reviewer, "card"))
    run = kanban.start_run(conn, tid, profile="impl")
    kanban.request_review(conn, tid, summary="result", reviewer=reviewer, expected_run_id=run)
    if mode == "mixed":
        # The AI reviewer leaves its verdict as a new submission without a reviewer (the hooks strip it).
        run = kanban.start_run(conn, tid, profile=reviewer, source_status="review")
        kanban.request_review(conn, tid, summary="pass: looks right", expected_run_id=run)
    kanban.assign_task(conn, tid, None)
    return tid, run


async def _approve(client, tid, run, headers=USER, **extra):
    body = {"submission_id": submission_id(run), "request_id": "r1", **extra}
    return await client.post(f"/deskrpg/kanban/tasks/{tid}/approve?board={BOARD}", json=body, headers=headers)


async def test_approval_completes_the_card_and_records_who_approved(client, kanban, store):
    tid, run = _waiting(kanban, store)
    resp = await _approve(client, tid, run)
    assert resp.status == 200
    body = await resp.json()
    assert body["task"]["status"] == "done"
    assert body["task"]["review"]["state"] == "approved" and body["task"]["review"]["approval"]["actor_id"] == "deskrpg:u1"
    call = kanban.calls["complete_task"][-1]
    assert call["metadata"] == {"approval": {"actor": "deskrpg:u1", "submission_id": submission_id(run),
                                             "request_id": "r1", "actor_name": "곽지호"}}
    assert [d["verdict"] for d in rs.decisions(store, tid)] == ["approve"]


async def test_an_older_submission_is_refused_and_nothing_changes(client, kanban, store):
    tid, run = _waiting(kanban, store)
    resp = await _approve(client, tid, run - 1)
    assert resp.status == 409 and (await resp.json())["error"] == "stale_submission"
    assert "complete_task" not in kanban.calls and rs.decisions(store, tid) == []


async def test_a_card_under_ai_review_cannot_be_approved_by_a_person(client, kanban, store):
    conn = _conn(kanban)
    tid = kanban.create_task(conn, title="card", assignee="impl")
    rs.put_policy(store, rs.Policy(tid, "agent", "impl", "rev", "card"))
    run = kanban.start_run(conn, tid, profile="impl")
    kanban.request_review(conn, tid, summary="result", reviewer="rev", expected_run_id=run)
    resp = await _approve(client, tid, run)
    assert resp.status == 409 and (await resp.json())["error"] == "not_waiting_for_approval"
    assert "complete_task" not in kanban.calls


async def test_a_repeated_approval_is_refused(client, kanban, store):
    tid, run = _waiting(kanban, store)
    assert (await _approve(client, tid, run)).status == 200
    resp = await _approve(client, tid, run)
    assert resp.status == 409 and (await resp.json())["error"] == "not_waiting_for_approval"
    assert len(kanban.calls["complete_task"]) == 1 and len(rs.decisions(store, tid)) == 1


async def test_approval_needs_the_signed_in_user(client, kanban, store):
    tid, run = _waiting(kanban, store)
    resp = await _approve(client, tid, run, headers={})
    assert resp.status == 400 and (await resp.json())["error"] == "approval_actor_required"


async def test_unknown_approval_fields_are_refused(client, kanban, store):
    tid, run = _waiting(kanban, store)
    resp = await _approve(client, tid, run, summary="x")
    assert resp.status == 400 and (await resp.json())["error"] == "invalid_field"


async def test_a_card_without_a_policy_is_approved_as_before(client, kanban, store):
    conn = _conn(kanban)
    tid = kanban.create_task(conn, title="card", assignee="impl")
    resp = await client.post(f"/deskrpg/kanban/tasks/{tid}/approve?board={BOARD}", json={"summary": "ok"})
    assert resp.status == 200 and (await resp.json())["task"]["status"] == "done"


async def test_sending_back_a_human_card_returns_it_to_the_implementer(client, kanban, store):
    tid, _run = _waiting(kanban, store)
    resp = await client.post(f"/deskrpg/kanban/tasks/{tid}/request-changes?board={BOARD}", json={"comment": "fix"})
    assert resp.status == 200
    task = (await resp.json())["task"]
    assert task["assignee"] == "impl" and task["status"] in ("ready", "todo")
    assert [(d["verdict"], d["summary"]) for d in rs.decisions(store, tid)] == [("reject", "fix")]


async def test_sending_back_a_mixed_card_returns_it_to_the_implementer_not_the_reviewer(client, kanban, store):
    tid, _run = _waiting(kanban, store, mode="mixed", reviewer="rev")
    resp = await client.post(f"/deskrpg/kanban/tasks/{tid}/request-changes?board={BOARD}", json={"comment": "fix"})
    assert resp.status == 200
    assert (await resp.json())["task"]["assignee"] == "impl"
    assert kanban.calls["assign_task"][-1] == {"task_id": tid, "profile": "impl"}
    assert rs.decisions(store, tid)[-1]["verdict"] == "reject"


async def test_approval_store_unavailable_refuses_the_decision(client, kanban, store, monkeypatch):
    tid, run = _waiting(kanban, store)
    monkeypatch.setattr(kanban_actions, "open_approval_store", lambda api: None)
    resp = await _approve(client, tid, run)
    assert resp.status == 503 and (await resp.json())["error"] == "approval_store_unavailable"
    assert "complete_task" not in kanban.calls
