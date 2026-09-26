"""The card's `review` field built from the approval store — one test per row of the state table."""

import pytest

from deskrpg_plugin.review_state import review_state, submission_id
from tests.fakes_kanban_actions import install_fake_kanban_actions
from tests.review_fixtures import hooks_on, rs, store  # noqa: F401 — fixtures

BOARD = "deskrpg-abc"


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


def _card(kanban, **kw):
    conn = kanban.connect(board=BOARD)
    return conn, kanban.get_task(conn, kanban.create_task(conn, title="card", **kw))


def _state(fake_api, kanban, conn, store, task_id, board=BOARD):
    return review_state(fake_api, conn, store, kanban.get_task(conn, task_id), board)


def _submit(kanban, conn, task_id, reviewer=None):
    run = kanban.start_run(conn, task_id, profile="impl")
    kanban.request_review(conn, task_id, summary="done", reviewer=reviewer, expected_run_id=run)
    return run


def test_no_policy_means_no_review_field(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    assert _state(fake_api, kanban, conn, store, task.id) is None


def test_board_default_applies_to_a_card_without_its_own_policy(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_board_default(store, BOARD, "human", None)
    got = _state(fake_api, kanban, conn, store, task.id)
    assert got["policy"] == {"version": 1, "mode": "human", "reviewer_profile": None}
    assert got["state"] == "awaiting_submission" and got["submission"] is None and got["review_round"] == 0


def test_submitted_to_a_person_is_human_required_with_the_submitting_run(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "human", "impl", None, "card"))
    run = _submit(kanban, conn, task.id)
    kanban.assign_task(conn, task.id, None)
    got = _state(fake_api, kanban, conn, store, task.id)
    assert got["state"] == "human_required"
    assert got["submission"] == {"id": submission_id(run), "run_id": run, "hash": "", "policy_revision": 1}
    assert got["review_round"] == 1 and got["approval"] is None


def test_review_with_an_assignee_is_reviewing(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "agent", "impl", "rev", "card"))
    _submit(kanban, conn, task.id, reviewer="rev")
    assert _state(fake_api, kanban, conn, store, task.id)["state"] == "reviewing"


def test_a_running_reviewer_run_is_reviewing(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "agent", "impl", "rev", "card"))
    _submit(kanban, conn, task.id, reviewer="rev")
    kanban.start_run(conn, task.id, profile="rev", source_status="review")
    assert _state(fake_api, kanban, conn, store, task.id)["state"] == "reviewing"


def test_reopened_after_a_submission_is_submitted(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "human", "impl", None, "card"))
    _submit(kanban, conn, task.id)
    kanban.reopen_review_task(conn, task.id)
    got = _state(fake_api, kanban, conn, store, task.id)
    assert got["state"] == "submitted" and got["review_round"] == 1


def test_done_with_an_approval_is_approved_with_who_approved(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "human", "impl", None, "card"))
    run = _submit(kanban, conn, task.id)
    kanban.complete_task(conn, task.id, summary="ok")
    rs.record_decision(store, task.id, "human", "deskrpg:u1", "approve", None)
    got = _state(fake_api, kanban, conn, store, task.id)
    assert got["state"] == "approved" and got["reason"] is None
    assert got["approval"]["actor_kind"] == "human" and got["approval"]["actor_id"] == "deskrpg:u1"
    assert got["approval"]["submission_id"] == submission_id(run)


def test_done_without_a_recorded_approval_is_an_external_completion(fake_api, kanban, store):
    conn, task = _card(kanban, assignee="impl")
    rs.put_policy(store, rs.Policy(task.id, "human", "impl", None, "card"))
    kanban.complete_task(conn, task.id, summary="done from the Hermes dashboard")
    got = _state(fake_api, kanban, conn, store, task.id)
    assert (got["state"], got["reason"], got["approval"]) == ("approved", "external_done", None)


async def test_card_detail_and_board_carry_the_review_field_from_the_store(aiohttp_client, fake_api, store, hooks_on):
    from tests.kanban_app import make_app

    db = fake_api.kanban
    conn = db.connect(board="default")
    tid = db.create_task(conn, title="card", assignee="impl")
    rs.put_policy(store, rs.Policy(tid, "human", "impl", None, "card"))
    client = await aiohttp_client(make_app(fake_api))

    detail = await (await client.get(f"/deskrpg/kanban/tasks/{tid}?board=default")).json()
    assert detail["task"]["review"]["state"] == "awaiting_submission"
    board = await (await client.get("/deskrpg/kanban/board?board=default")).json()
    cards = [t for col in board["columns"] for t in col["tasks"]]
    assert [t["review"]["policy"]["mode"] for t in cards if t["id"] == tid] == ["human"]


async def test_without_hooks_or_patch_the_review_field_stays_empty(aiohttp_client, fake_api, store):
    from tests.kanban_app import make_app

    db = fake_api.kanban
    conn = db.connect(board="default")
    tid = db.create_task(conn, title="card", assignee="impl")
    rs.put_policy(store, rs.Policy(tid, "human", "impl", None, "card"))
    client = await aiohttp_client(make_app(fake_api))
    detail = await (await client.get(f"/deskrpg/kanban/tasks/{tid}?board=default")).json()
    assert detail["task"]["review"] is None
