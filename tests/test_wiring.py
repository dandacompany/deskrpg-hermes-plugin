"""배선(T6) 끝에서 끝까지 — `routes.attach` 가 실제로 붙인 테이블 위에서 각 라우트 군을 한 번씩 때린다.

단위 테스트는 각 핸들러를 자기 미니 앱에 붙여 검증했다. 여기서는 **테이블·팩토리 매핑·경로 변수 이름·
등록 순서**가 함께 맞는지 본다 — 핸들러가 다 맞아도 `{task_id}` 와 `{id}` 가 어긋나거나 comments 가
`{action}` 와일드카드 뒤에 오면 이 테스트만 잡는다.
"""

import pytest
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import FakeEventsKanban
from tests.fakes_kanban import KANBAN_DB_SYMBOLS
from tests.fakes_kanban_actions import FakeKanbanActionsDb

BOARD = "deskrpg-wiring"
B = f"?board={BOARD}"


class _FakeAllKanban(FakeEventsKanban, FakeKanbanActionsDb):
    """사건 tail SQL(T5) + 동작·첨부·로그(T3)를 한 가짜에 — 한 앱으로 모든 군을 돌리기 위해서다."""


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = _FakeAllKanban(tmp_path / "kanban")
    for name in KANBAN_DB_SYMBOLS:
        setattr(fake_api, name, getattr(db, name))
    fake_api.kanban = db
    db.create_board(BOARD, name="배선")
    return db


@pytest.fixture
def cron_store(fake_api, tmp_path):
    store = install_fake_cron(fake_api, tmp_path)
    fake_api.create_profile("sophie")
    return store


@pytest.fixture
async def client(aiohttp_client, fake_api, kanban, cron_store):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


async def test_info_가_예순아홉_라우트와_capability_전부를_보고한다(client):
    body = await (await client.get("/deskrpg/info")).json()
    assert body["version"] == "0.14.0"
    assert len(body["routes"]) == 75
    # fake_api 는 스웜·피커 심볼을 모두 갖춘 빌드를 흉내 낸다 — capability 에 다 붙는다.
    assert body["capabilities"] == [
        "kanban", "cron", "events", "event_cursor_handoff", "artifacts", "kanban_views", "card_proposals", "worker_plugin", "kanban_attachment_list", "swarm",
        "profile_toolsets", "profile_skills", "profile_skill_admin", "profile_clone", "profile_provider_keys",
        "profile_oauth", "profile_tool_providers", "initial_status",
    ]
    assert "GET /deskrpg/events" in body["routes"]
    assert "POST /deskrpg/events/handoff" in body["routes"]
    assert "POST /p/{profile}/deskrpg/cron/jobs" in body["routes"]
    assert "POST /deskrpg/card-proposals/{proposal_id}/resolve" in body["routes"]
    assert "POST /deskrpg/worker-plugin" in body["routes"]


async def test_아티팩트_군_목록과_rework_501(client):
    assert (await client.get("/deskrpg/artifacts")).status == 200
    assert (await client.post("/deskrpg/artifacts/x/rework", json={})).status == 501


async def test_보드_군_보드_생성과_보기(client):
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": "deskrpg-new", "name": "새 보드"})
    assert resp.status == 201
    resp = await client.get("/deskrpg/kanban/board?board=deskrpg-new")
    assert resp.status == 200
    assert [c["name"] for c in (await resp.json())["columns"]][:2] == ["triage", "todo"]


async def test_카드_군_생성_조회_댓글_은_와일드카드로_새지_않는다(client, kanban):
    created = await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "카드"})).json()
    task_id = created["task"]["id"]
    assert (await client.get(f"/deskrpg/kanban/tasks/{task_id}{B}")).status == 200
    # comments 는 `{task_id}/comments` 고정 행 — `{id}/{action}` 보다 앞에 있어야 여기로 온다.
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/comments{B}", json={"body": "안녕"})
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["comment"]["body"] == "안녕"


async def test_동작_군_approve_는_동작_디스패처로_가고_모르는_이름은_404(client, kanban):
    created = await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "카드"})).json()
    task_id = created["task"]["id"]
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/approve{B}", json={})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["task"]["status"] == "done"
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/frobnicate{B}", json={})
    assert resp.status == 404
    assert (await resp.json()) == {"error": "unknown_action", "detail": "frobnicate"}
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/estimate{B}", json={})
    assert resp.status == 501


async def test_첨부_로그_군은_고정_행으로_간다(client, kanban):
    created = await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "카드"})).json()
    task_id = created["task"]["id"]
    resp = await client.get(f"/deskrpg/kanban/tasks/{task_id}/attachments{B}")
    assert resp.status == 200 and (await resp.json()) == {"attachments": []}
    # attachments POST 가 `{action}` 로 흘렀다면 404 unknown_action 이 났을 것이다 — 400(file 파트 없음)이 맞다.
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/attachments{B}", json={})
    assert resp.status == 400
    assert (await resp.json())["error"] != "unknown_action"
    resp = await client.get(f"/deskrpg/kanban/tasks/{task_id}/log{B}")
    assert resp.status == 200 and (await resp.json())["exists"] is False
    assert (await client.get(f"/deskrpg/kanban/attachments/999{B}")).status == 404


async def test_링크_디스패치_운영설정_프로필_군(client, kanban):
    a = (await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "부모"})).json())["task"]["id"]
    b = (await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "자식"})).json())["task"]["id"]
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": b})
    assert resp.status == 200 and (await resp.json()) == {"ok": True}
    resp = await client.delete(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": b})
    assert resp.status == 200
    resp = await client.post(f"/deskrpg/kanban/dispatch{B}")
    assert resp.status == 200 and (await resp.json())["spawned"] == []
    resp = await client.get("/deskrpg/kanban/orchestration")
    assert resp.status == 200 and "dispatch_in_gateway" in (await resp.json())
    resp = await client.get("/deskrpg/kanban/profiles")
    assert resp.status == 200 and [p["name"] for p in (await resp.json())["profiles"]] == ["sophie"]


async def test_사건_군_커서_왕복(client, kanban):
    first = await (await client.get(f"/deskrpg/events{B}")).json()
    assert first["events"] == [] and first["has_more"] is False
    await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "카드"})
    second = await (await client.get(f"/deskrpg/events{B}&cursor={first['cursor']}")).json()
    assert [e["kind"] for e in second["events"]] == ["task.created"]
    assert (await client.get(f"/deskrpg/events{B}&cursor=garbage")).status == 400


async def test_크론_군_프로필_스코프_생성과_목록(client):
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 10 m", "prompt": "안부"})
    assert resp.status == 201, await resp.text()
    job_id = (await resp.json())["job"]["id"]
    assert (await client.get("/p/sophie/deskrpg/cron/jobs")).status == 200
    assert (await client.post(f"/p/sophie/deskrpg/cron/jobs/{job_id}/pause")).status == 200
    assert (await client.get("/p/sophie/deskrpg/cron/delivery-targets")).status == 200
    assert (await client.get("/p/sophie/deskrpg/cron/blueprints")).status == 200
