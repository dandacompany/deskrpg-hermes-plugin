"""Attaching approval policies on a Hermes without the policy core patch: cards, swarms and board defaults."""

import pytest
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter
from tests.review_fixtures import hooks_on, rs, store  # noqa: F401 — fixtures

B = "?board=default"
HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}


@pytest.fixture(autouse=True)
def _profiles(tmp_path):
    for name in ("impl", "rev", "nova", "luna", "sophie", "dante"):
        (tmp_path / "profiles" / name).mkdir(parents=True, exist_ok=True)


@pytest.fixture
async def client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


async def _create(client, **body):
    return await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "card", "assignee": "impl", **body})


async def test_a_card_created_with_a_policy_stores_it(client, fake_api, store, hooks_on):
    resp = await _create(client, review_policy=HUMAN)
    assert resp.status == 201
    task = (await resp.json())["task"]
    assert rs.get_policy(store, task["id"]) == rs.Policy(task["id"], "human", "impl", None, "card")
    assert task["review"]["policy"] == HUMAN and task["review"]["state"] == "awaiting_submission"
    # Hermes' own create_task never sees the plugin's policy: upstream's has no such parameter, and neither does
    # the fake's, so a leak would have failed the create above.


async def test_an_ai_policy_needs_an_existing_reviewer_other_than_the_implementer(client, fake_api, store, hooks_on):
    for policy, error in (
        ({"version": 1, "mode": "agent", "reviewer_profile": None}, "invalid_field"),
        ({"version": 1, "mode": "mixed", "reviewer_profile": "ghost"}, "profile_not_found"),
        ({"version": 1, "mode": "agent", "reviewer_profile": "impl"}, "invalid_field"),
        ({"version": 1, "mode": "auto", "reviewer_profile": None}, "invalid_field"),
    ):
        resp = await _create(client, review_policy=policy)
        assert (resp.status, (await resp.json())["error"]) == (400, error), policy
    assert fake_api.kanban.list_tasks(fake_api.kanban.connect(board="default")) == []


async def test_without_hooks_or_patch_an_explicit_policy_is_refused(client, fake_api, store):
    resp = await _create(client, review_policy=HUMAN)
    assert resp.status == 428 and (await resp.json())["error"] == "review_policy_required"


async def test_a_policy_that_cannot_be_stored_leaves_no_card(client, fake_api, store, hooks_on, monkeypatch):
    def broken(conn, policy):
        raise OSError("read-only")

    monkeypatch.setattr(rs, "put_policy", broken)
    resp = await _create(client, review_policy=HUMAN)
    assert resp.status == 503 and (await resp.json())["error"] == "approval_store_unavailable"
    assert fake_api.kanban.list_tasks(fake_api.kanban.connect(board="default")) == []


async def test_a_policy_edit_updates_the_store_and_keeps_the_implementer(client, fake_api, store, hooks_on):
    tid = (await (await _create(client, review_policy=HUMAN)).json())["task"]["id"]
    agent = {"version": 1, "mode": "agent", "reviewer_profile": "rev"}
    resp = await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"review_policy": agent, "expected_revision": 1})
    assert resp.status == 200 and (await resp.json())["task"]["review"]["policy"] == agent
    assert rs.get_policy(store, tid) == rs.Policy(tid, "agent", "impl", "rev", "card")
    stale = await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"review_policy": HUMAN, "expected_revision": 2})
    assert stale.status == 409


async def test_a_swarm_policy_covers_every_result_card_but_not_the_root(client, fake_api, store, hooks_on):
    body = {"goal": "g", "workers": [{"profile": "nova", "title": "w1"}, {"profile": "luna", "title": "w2"}],
            "verifier": "sophie", "synthesizer": "dante",
            "review_policy": {"version": 1, "mode": "agent", "reviewer_profile": "rev"}}
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=body)
    assert resp.status == 200
    ids = await resp.json()
    assert rs.get_policy(store, ids["root_id"]) is None
    assert [rs.get_policy(store, t) for t in ids["worker_ids"]] == [
        rs.Policy(ids["worker_ids"][0], "agent", "nova", "rev", "swarm"),
        rs.Policy(ids["worker_ids"][1], "agent", "luna", "rev", "swarm")]
    assert rs.get_policy(store, ids["verifier_id"]) == rs.Policy(ids["verifier_id"], "human", "sophie", None, "swarm")
    assert rs.get_policy(store, ids["synthesizer_id"]).mode == "human"


async def test_a_swarm_reviewer_cannot_be_one_of_its_workers(client, fake_api, store, hooks_on):
    body = {"goal": "g", "workers": [{"profile": "nova", "title": "w1"}], "verifier": "sophie", "synthesizer": "dante",
            "review_policy": {"version": 1, "mode": "agent", "reviewer_profile": "nova"}}
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=body)
    assert resp.status == 400 and (await resp.json())["error"] == "invalid_swarm"


async def test_board_default_is_keyed_by_the_board_slug_a_worker_sees(client, fake_api, store, hooks_on):
    # A kanban worker learns its board from HERMES_KANBAN_BOARD, which Hermes sets to the board slug; the default
    # must be stored under that same key or the hooks would never find it.
    resp = await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": "human", "reviewer_profile": None})
    assert resp.status == 200
    assert await resp.json() == {"board": "default", "default": HUMAN}
    assert rs.get_board_default(store, "default") == ("human", None)

    # And a card without its own policy picks it up.
    task = (await (await _create(client)).json())["task"]
    assert task["review"]["policy"] == HUMAN


async def test_board_default_validation_and_removal(client, fake_api, store, hooks_on):
    bad = await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": "mixed", "reviewer_profile": None})
    assert bad.status == 400
    unknown = await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": "human", "x": 1})
    assert unknown.status == 400
    await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": "human", "reviewer_profile": None})
    cleared = await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": None})
    assert cleared.status == 200 and (await cleared.json())["default"] is None
    assert rs.get_board_default(store, "default") is None


async def test_board_default_without_hooks_is_refused(client, fake_api, store):
    resp = await client.put("/deskrpg/kanban/boards/default/default-policy", json={"mode": "human", "reviewer_profile": None})
    assert resp.status == 428


async def test_reading_the_board_default(client, fake_api, store, hooks_on):
    # The exact shape DeskRPG (and its fake plugin server) relies on: `default` is null until one is set.
    empty = await client.get("/deskrpg/kanban/boards/default/default-policy")
    assert empty.status == 200 and await empty.json() == {"board": "default", "default": None}
    await client.put("/deskrpg/kanban/boards/default/default-policy",
                     json={"mode": "agent", "reviewer_profile": "rev"})
    got = await client.get("/deskrpg/kanban/boards/default/default-policy")
    assert await got.json() == {"board": "default",
                                "default": {"version": 1, "mode": "agent", "reviewer_profile": "rev"}}


async def test_reading_the_board_default_without_hooks_is_refused(client, fake_api, store):
    resp = await client.get("/deskrpg/kanban/boards/default/default-policy")
    assert resp.status == 428 and (await resp.json())["error"] == "review_policy_required"


async def test_reading_an_unknown_board_default_is_404(client, fake_api, store, hooks_on):
    resp = await client.get("/deskrpg/kanban/boards/deskrpg-nope/default-policy")
    assert resp.status == 404
