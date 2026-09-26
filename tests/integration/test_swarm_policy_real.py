"""A swarm on an approval-policy board, on the real Hermes kanban core.

Pins the three things the plugin's own assembly (kanban_swarm_policy.py) must guarantee:
- every result card (workers, verifier, synthesizer) carries a policy, and the structure root carries none;
- the root's instant completion does not trip `review_policy_done_guard` and is not an approval;
- no worker is ever `ready` without its policy — the policies are committed before `recompute_ready` runs.
"""

import sqlite3

import pytest

from deskrpg_plugin import contract_fields as cf
from tests.integration.test_kanban_real import BOARD, B, _board

pytestmark = pytest.mark.integration

HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}


@pytest.fixture
def crew(make_profile, profile):
    for name in ("oliver", "noah"):
        make_profile(name)
    return {"a": profile, "b": "oliver", "reviewer": "noah"}


def _body(crew, policy=HUMAN, **extra):
    return {
        "goal": "10월 뉴스레터 조사와 초안",
        "workers": [
            {"profile": crew["a"], "title": "사례 조사"},
            {"profile": crew["b"], "title": "통계 조사"},
        ],
        "verifier": crew["reviewer"],
        "synthesizer": crew["a"],
        "review_policy": policy,
        **extra,
    }


def _require_capability(api):
    if not cf.has_swarm_policy_symbols(api):
        pytest.skip("this Hermes build has no policy-aware swarm internals")


def _state(api, task_id):
    with api.connect_closing(board=BOARD) as conn:
        task = api.get_task(conn, task_id)
        return task.status, api.get_review_state(conn, task_id), task.result


async def test_capability_is_announced_on_a_policy_hermes(client, api):
    _require_capability(api)
    info = await (await client.get("/deskrpg/info")).json()
    assert "swarm_review_policy" in info["capabilities"]


async def test_every_result_card_has_a_policy_before_any_worker_is_ready(client, api, crew, monkeypatch):
    _require_capability(api)
    await _board(client)
    seen = []
    real_recompute = api.recompute_ready

    def watched_recompute(conn, *args, **kwargs):
        # Runs right after the creating transaction committed — look from a separate connection.
        with api.connect_closing(board=BOARD) as other:
            rows = other.execute("SELECT id, status FROM tasks").fetchall()
            seen.append({row["id"]: (row["status"], api.get_review_state(other, row["id"]) is not None)
                         for row in rows})
        return real_recompute(conn, *args, **kwargs)

    monkeypatch.setattr(api, "recompute_ready", watched_recompute)
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=_body(crew))
    assert resp.status == 200, await resp.text()
    created = await resp.json()
    results = [*created["worker_ids"], created["verifier_id"], created["synthesizer_id"]]

    # Committed state before promotion: every result card already has its policy, none is ready yet.
    assert len(seen) == 1
    before = seen[0]
    assert all(before[task_id][1] for task_id in results)
    assert all(before[task_id][0] != "ready" for task_id in results)

    # After promotion: workers ready with the requested policy, verifier/synthesizer waiting and human-approved.
    for worker_id in created["worker_ids"]:
        status, review, _ = _state(api, worker_id)
        assert status == "ready" and review["policy"] == HUMAN
    for task_id in (created["verifier_id"], created["synthesizer_id"]):
        status, review, _ = _state(api, task_id)
        assert status == "todo" and review["policy"] == HUMAN

    # The root is a structure card: done, no policy, no result.
    status, review, result = _state(api, created["root_id"])
    assert status == "done" and review is None and not result
    with api.connect_closing(board=BOARD) as conn:
        run = api.latest_run(conn, created["root_id"])
        assert run.metadata["kind"] == "kanban_swarm_v1"


async def test_results_cannot_finish_without_approval_even_by_raw_sql(client, api, crew):
    _require_capability(api)
    await _board(client)
    created = await (await client.post(f"/deskrpg/kanban/swarm{B}", json=_body(crew))).json()
    worker = created["worker_ids"][0]
    resp = await client.patch(f"/deskrpg/kanban/tasks/{worker}{B}", json={"status": "done"})
    assert resp.status == 409, await resp.text()
    with api.connect_closing(board=BOARD) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (created["synthesizer_id"],))
    assert _state(api, created["synthesizer_id"])[0] == "todo"


async def test_agent_policy_for_workers_keeps_the_final_result_human(client, api, crew):
    _require_capability(api)
    await _board(client)
    agent = {"version": 1, "mode": "agent", "reviewer_profile": crew["reviewer"]}
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=_body(crew, policy=agent))
    assert resp.status == 200, await resp.text()
    created = await resp.json()
    for worker_id in created["worker_ids"]:
        assert _state(api, worker_id)[1]["policy"] == agent
    assert _state(api, created["synthesizer_id"])[1]["policy"] == HUMAN


async def test_a_policy_hermes_refuses_a_bad_policy_and_leaves_nothing_behind(client, api, crew):
    _require_capability(api)
    await _board(client)
    # The reviewer is one of the workers — Hermes refuses reviewer == implementer.
    bad = {"version": 1, "mode": "agent", "reviewer_profile": crew["a"]}
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=_body(crew, policy=bad))
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_swarm"
    with api.connect_closing(board=BOARD) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    missing = _body(crew)
    missing.pop("review_policy")
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=missing)
    assert resp.status == 400


async def test_idempotent_replay_returns_the_same_swarm(client, api, crew):
    _require_capability(api)
    await _board(client)
    body = _body(crew, idempotency_key="swarm-once")
    first = await (await client.post(f"/deskrpg/kanban/swarm{B}", json=body)).json()
    resp = await client.post(f"/deskrpg/kanban/swarm{B}", json=body)
    assert resp.status == 200, await resp.text()
    assert await resp.json() == first
    with api.connect_closing(board=BOARD) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
