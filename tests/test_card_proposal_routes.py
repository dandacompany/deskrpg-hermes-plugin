"""`POST /deskrpg/card-proposals/{id}/resolve` 와 `…/unresolve` — 상태 코드와 롤백 가드."""

import pytest
from aiohttp import web

from deskrpg_plugin import card_proposal_routes as routes
from deskrpg_plugin import card_proposal_store as store
from deskrpg_plugin.auth import Scope, require_auth
from tests.conftest import FakeAdapter


@pytest.fixture
async def client(aiohttp_client, tmp_api):
    app = web.Application()
    adapter = FakeAdapter(authorized=True)
    for action, factory in (("resolve", routes.resolve_handler), ("unresolve", routes.unresolve_handler),
                            ("task", routes.record_task_handler)):
        app.router.add_route("POST", f"/deskrpg/card-proposals/{{proposal_id}}/{action}",
                             require_auth(adapter, Scope.DEFAULT, factory(tmp_api)))
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


async def test_inline_해소는_task_id_를_받아도_적지_않고_되돌릴_수_있다(client, tmp_api):
    """inline 은 카드를 만들지 않는다. 여기에 카드 id 가 적히면 카드는 없는데 unresolve 가 영영 막힌다."""
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/resolve",
                             json={"choice": "inline", "task_id": "x"})
    assert resp.status == 200
    row = store.get(tmp_api, pid)
    assert (row["resolved_choice"], row["resolved_task_id"]) == ("inline", None)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/unresolve")).status == 200
    assert store.get(tmp_api, pid)["resolved_at"] is None


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


async def test_카드가_없는_해소는_되돌려지고_다시_해소할_수_있다(client, tmp_api):
    """롤백의 목적은 재시도다 — 되돌린 뒤 resolve 가 다시 성공해야 의미가 있다."""
    pid = _seed(tmp_api)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "card"})).status == 200
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/unresolve")
    assert resp.status == 200
    assert await resp.json() == {"resolved": False}
    row = store.get(tmp_api, pid)
    assert row["resolved_at"] is None and row["resolved_choice"] is None
    again = await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "card", "task_id": "t-9"})
    assert again.status == 200
    assert store.get(tmp_api, pid)["resolved_task_id"] == "t-9"


async def test_카드가_기록된_제안은_되돌려지지_않고_상태도_그대로다(client, tmp_api):
    pid = _seed(tmp_api)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/resolve",
                              json={"choice": "card", "task_id": "t-1"})).status == 200
    before = store.get(tmp_api, pid)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/unresolve")
    assert resp.status == 409
    assert (await resp.json())["error"] == "card_proposal_not_unresolvable"
    assert store.get(tmp_api, pid) == before


async def test_해소되지_않은_제안의_되돌리기는_409_다(client, tmp_api):
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/unresolve")
    assert resp.status == 409
    assert store.get(tmp_api, pid)["resolved_at"] is None


async def test_없는_제안의_되돌리기는_404_다(client):
    resp = await client.post("/deskrpg/card-proposals/nope/unresolve")
    assert resp.status == 404
    assert (await resp.json())["error"] == "card_proposal_not_found"


async def test_카드_id_를_적으면_되돌리기가_막힌다(client, tmp_api):
    """이 작업의 핵심 — task_id 가 적히는 순간 unresolve 의 가드가 되살아난다."""
    pid = _seed(tmp_api)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "card"})).status == 200
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/task", json={"task_id": "t-42"})
    assert resp.status == 200
    assert await resp.json() == {"recorded": True}
    assert store.get(tmp_api, pid)["resolved_task_id"] == "t-42"
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/unresolve")).status == 409


async def test_이미_적힌_카드_id_는_덮이지_않는다(client, tmp_api):
    pid = _seed(tmp_api)
    await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "card", "task_id": "t-1"})
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/task", json={"task_id": "t-2"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "card_proposal_task_not_recordable"
    assert store.get(tmp_api, pid)["resolved_task_id"] == "t-1"


async def test_해소되지_않은_제안에는_적을_수_없다(client, tmp_api):
    pid = _seed(tmp_api)
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/task", json={"task_id": "t-1"})
    assert resp.status == 409
    row = store.get(tmp_api, pid)
    assert row["resolved_task_id"] is None and row["resolved_at"] is None


@pytest.mark.parametrize("body", [{}, {"task_id": ""}, {"task_id": 7}])
async def test_잘못된_task_id_본문은_400_이다(client, tmp_api, body):
    pid = _seed(tmp_api)
    await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "card"})
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/task", json=body)
    assert resp.status == 400
    assert store.get(tmp_api, pid)["resolved_task_id"] is None


async def test_없는_제안에는_적을_수_없다_404(client):
    resp = await client.post("/deskrpg/card-proposals/nope/task", json={"task_id": "t-1"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "card_proposal_not_found"


async def test_inline_로_해소된_제안에는_카드_id_를_적을_수_없다(client, tmp_api):
    """`inline` 갈래에는 만들어진 카드가 없다 — 카드 id 가 적힐 자리가 아니다."""
    pid = _seed(tmp_api)
    assert (await client.post(f"/deskrpg/card-proposals/{pid}/resolve", json={"choice": "inline"})).status == 200
    resp = await client.post(f"/deskrpg/card-proposals/{pid}/task", json={"task_id": "t-1"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "card_proposal_task_not_recordable"
    assert store.get(tmp_api, pid)["resolved_task_id"] is None
