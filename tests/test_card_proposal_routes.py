"""`POST /deskrpg/card-proposals/{id}/resolve` — 200 / 409(두 번째) / 400(잘못된 choice) / 404(없는 id)."""

import pytest
from aiohttp import web

from deskrpg_plugin import card_proposal_routes as routes
from deskrpg_plugin import card_proposal_store as store
from deskrpg_plugin.auth import Scope, require_auth
from tests.conftest import FakeAdapter


@pytest.fixture
async def client(aiohttp_client, tmp_api):
    app = web.Application()
    app.router.add_route(
        "POST", "/deskrpg/card-proposals/{proposal_id}/resolve",
        require_auth(FakeAdapter(authorized=True), Scope.DEFAULT, routes.resolve_handler(tmp_api)),
    )
    return await aiohttp_client(app)


def _seed(api) -> str:
    return store.create(api, profile="noah", title="주간 보고 정리", summary="s", body=None, acceptance=None)


async def test_첫_해소는_200_이고_선택과_카드가_기록된다(client, tmp_api):
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/resolve",
                             json={"choice": "card", "task_id": "t-7"})
    assert resp.status == 200
    assert await resp.json() == {"resolved": True}
    row = store.get(tmp_api, pid)
    assert (row["resolved_choice"], row["resolved_task_id"]) == ("card", "t-7")
    assert row["resolved_at"]


async def test_두_번째_해소는_409_이고_첫_선택을_덮지_않는다(client, tmp_api):
    pid = _seed(tmp_api)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/resolve",
                              json={"choice": "card", "task_id": "t-1"})).status == 200
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/resolve",
                             json={"choice": "inline", "task_id": "t-2"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "card_proposal_already_resolved"
    row = store.get(tmp_api, pid)
    assert (row["resolved_choice"], row["resolved_task_id"]) == ("card", "t-1")


async def test_task_id_는_없어도_된다(client, tmp_api):
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "inline"})
    assert resp.status == 200
    assert store.get(tmp_api, pid)["resolved_task_id"] is None


@pytest.mark.parametrize("body", [{"choice": "카드"}, {"choice": ""}, {}, {"choice": 3}])
async def test_허용되지_않은_choice_는_400_이고_제안은_그대로다(client, tmp_api, body):
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json=body)
    assert resp.status == 400
    assert store.get(tmp_api, pid)["resolved_at"] is None


async def test_없는_제안은_404_다(client):
    resp = await client.post("/deskrpg/card-proposals/nope/resolve", json={"choice": "card"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "card_proposal_not_found"
