"""디스패치·운영 설정·프로필 라우트.

- `POST /deskrpg/kanban/dispatch?board=&max=`
- `GET|PUT /deskrpg/kanban/orchestration`
- `GET /deskrpg/kanban/profiles`
"""

import types

import pytest
from aiohttp import web

from deskrpg_plugin import kanban_ops
from deskrpg_plugin.auth import Scope, require_auth
from deskrpg_plugin.contract_fields import (
    DISPATCH_SPAWNED_OPTIONAL,
    DISPATCH_SPAWNED_REQUIRED,
    KANBAN_PROFILE_SUMMARY_KEYS,
    ORCHESTRATION_SETTINGS_REQUIRED,
)
from tests.conftest import FakeAdapter
from tests.fakes_kanban_actions import install_fake_kanban_actions

BOARD = "deskrpg-abc"


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


@pytest.fixture
def config(fake_api):
    """`load_config` 가 돌려주고 `save_config` 가 받는 설정 dict. 저장 호출을 기록한다."""
    cfg = {"kanban": {}, "timezone": "Asia/Seoul"}
    saved = []
    fake_api.load_config = lambda *a, **k: cfg
    fake_api.save_config = lambda c, *a, **k: saved.append(c)
    cfg_ns = types.SimpleNamespace(data=cfg, saved=saved)
    return cfg_ns


def _client(aiohttp_client, fake_api, *, authorized=True):
    app = web.Application()
    adapter = FakeAdapter(authorized=authorized)
    wrap = lambda h: require_auth(adapter, Scope.DEFAULT, h)  # noqa: E731
    app.router.add_route("POST", "/deskrpg/kanban/dispatch", wrap(kanban_ops.dispatch_handler(fake_api)))
    app.router.add_route("GET", "/deskrpg/kanban/orchestration", wrap(kanban_ops.get_orchestration_handler(fake_api)))
    app.router.add_route("PUT", "/deskrpg/kanban/orchestration", wrap(kanban_ops.put_orchestration_handler(fake_api)))
    app.router.add_route("GET", "/deskrpg/kanban/profiles", wrap(kanban_ops.profiles_handler(fake_api)))
    return aiohttp_client(app)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _dispatch_result(**kw):
    base = dict(spawned=[], skipped_locked=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


async def test_인증_없으면_401(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api, authorized=False)
    resp = await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}")
    assert resp.status == 401


async def test_dispatch_는_보드와_max_를_넘기고_spawned_를_객체로_돌려준다(aiohttp_client, fake_api, kanban, config):
    seen = {}

    def dispatch_once(conn, **kw):
        seen.update(kw, board_of_conn=conn.board)
        return _dispatch_result(spawned=[("t0001", "sophie", "/tmp/ws"), ("t0002", None, None)])

    fake_api.dispatch_once = dispatch_once
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}&max=3")
    body = await resp.json()
    assert resp.status == 200, body
    assert seen == {"board": BOARD, "max_spawn": 3, "board_of_conn": BOARD}
    assert body == {
        "spawned": [{"task_id": "t0001", "profile": "sophie"}, {"task_id": "t0002", "profile": None}],
        "skipped_locked": False,
    }
    for entry in body["spawned"]:
        assert DISPATCH_SPAWNED_REQUIRED <= set(entry) <= DISPATCH_SPAWNED_REQUIRED | DISPATCH_SPAWNED_OPTIONAL


async def test_dispatch_의_max_기본은_8(aiohttp_client, fake_api, kanban, config):
    seen = {}
    fake_api.dispatch_once = lambda conn, **kw: seen.update(kw) or _dispatch_result()
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}")
    assert resp.status == 200
    assert seen["max_spawn"] == 8


async def test_dispatch_의_max_가_양의_정수가_아니면_400(aiohttp_client, fake_api, kanban, config):
    client = await _client(aiohttp_client, fake_api)
    for bad in ("0", "x", "-2"):
        resp = await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}&max={bad}")
        assert resp.status == 400, bad


async def test_dispatch_가_잠금에_밀리면_skipped_locked(aiohttp_client, fake_api, kanban, config):
    fake_api.dispatch_once = lambda conn, **kw: _dispatch_result(skipped_locked=True)
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}")).json()
    assert body == {"spawned": [], "skipped_locked": True}


async def test_dispatch_in_gateway_가_꺼져_있으면_실행은_하되_경고(aiohttp_client, fake_api, kanban, config):
    config.data["kanban"]["dispatch_in_gateway"] = False
    called = []
    fake_api.dispatch_once = lambda conn, **kw: called.append(1) or _dispatch_result()
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.post(f"/deskrpg/kanban/dispatch?board={BOARD}")).json()
    assert called == [1]
    assert body["warning"] == "embedded_dispatcher_disabled"


async def test_dispatch_는_모르는_보드면_404(aiohttp_client, fake_api, kanban, config):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/kanban/dispatch?board=nope")
    assert resp.status == 404
    assert (await resp.json())["error"] == "board_not_found"


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


async def test_GET_orchestration_은_설정과_해소값을_준다(aiohttp_client, fake_api, config):
    fake_api.create_profile("sophie")
    config.data["kanban"].update(orchestrator_profile="sophie", default_assignee="ghost", auto_decompose=False,
                                 max_in_progress=4)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/kanban/orchestration")
    body = await resp.json()
    assert resp.status == 200
    assert ORCHESTRATION_SETTINGS_REQUIRED <= set(body)
    assert body["orchestrator_profile"] == "sophie" and body["resolved_orchestrator_profile"] == "sophie"
    # 없는 프로필은 활성 기본 프로필로 해소된다(대시보드와 같은 규칙).
    assert body["default_assignee"] == "ghost" and body["resolved_default_assignee"] == "default"
    assert body["auto_decompose"] is False and body["auto_promote_children"] is True
    assert body["max_in_progress"] == 4 and "max_in_progress_per_profile" not in body
    assert body["dispatch_in_gateway"] is True


async def test_GET_orchestration_빈_설정의_기본값(aiohttp_client, fake_api, config):
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/orchestration")).json()
    assert body["orchestrator_profile"] == "" and body["default_assignee"] == ""
    assert body["resolved_orchestrator_profile"] == "default" and body["resolved_default_assignee"] == "default"
    assert body["auto_decompose"] is True


async def test_PUT_orchestration_은_kanban_절을_갱신하고_저장한다(aiohttp_client, fake_api, config):
    fake_api.create_profile("sophie")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"orchestrator_profile": "sophie", "auto_decompose": False})
    body = await resp.json()
    assert resp.status == 200, body
    assert config.saved == [config.data]
    assert config.data["kanban"]["orchestrator_profile"] == "sophie" and config.data["kanban"]["auto_decompose"] is False
    assert body["orchestrator_profile"] == "sophie" and body["auto_decompose"] is False
    assert body["restart_required"] is False
    assert "timezone" in config.data  # 다른 절은 건드리지 않는다


async def test_PUT_orchestration_없는_프로필은_400_이고_저장하지_않는다(aiohttp_client, fake_api, config):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"default_assignee": "ghost"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "profile_not_found"
    assert config.saved == []


async def test_PUT_orchestration_빈_문자열은_비운다(aiohttp_client, fake_api, config):
    fake_api.create_profile("sophie")
    config.data["kanban"]["orchestrator_profile"] = "sophie"
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"orchestrator_profile": ""})
    body = await resp.json()
    assert resp.status == 200
    assert config.data["kanban"]["orchestrator_profile"] == ""
    assert body["orchestrator_profile"] == "" and body["resolved_orchestrator_profile"] == "default"


async def test_PUT_orchestration_max_를_바꾸면_restart_required(aiohttp_client, fake_api, config):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"max_in_progress": 4, "max_in_progress_per_profile": 2})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["restart_required"] is True
    assert body["max_in_progress"] == 4 and body["max_in_progress_per_profile"] == 2
    assert config.data["kanban"]["max_in_progress"] == 4
    # 같은 값을 다시 넣으면 재시작 불필요
    resp = await client.put("/deskrpg/kanban/orchestration", json={"max_in_progress": 4})
    assert (await resp.json())["restart_required"] is False


async def test_PUT_orchestration_max_에_null_을_주면_지운다(aiohttp_client, fake_api, config):
    config.data["kanban"]["max_in_progress"] = 4
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"max_in_progress": None})
    body = await resp.json()
    assert resp.status == 200, body
    assert "max_in_progress" not in config.data["kanban"] and "max_in_progress" not in body
    assert body["restart_required"] is True


async def test_PUT_orchestration_타입_오류와_모르는_키는_400(aiohttp_client, fake_api, config):
    client = await _client(aiohttp_client, fake_api)
    for bad in ({"auto_decompose": "yes"}, {"max_in_progress": 0}, {"max_in_progress": "4"},
                {"orchestrator_profile": 3}, {"auto_promote_children": True}, {"nope": 1}):
        resp = await client.put("/deskrpg/kanban/orchestration", json=bad)
        assert resp.status == 400, bad
    assert config.saved == []


async def test_PUT_orchestration_kanban_절이_dict_가_아니면_바꿔치운다(aiohttp_client, fake_api, config):
    config.data["kanban"] = "broken"
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/deskrpg/kanban/orchestration", json={"auto_decompose": True})
    assert resp.status == 200
    assert config.data["kanban"] == {"auto_decompose": True}


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


async def test_profiles_는_이름_기본_여부_설명을_준다(aiohttp_client, fake_api):
    fake_api.list_profiles = lambda: [
        types.SimpleNamespace(name="default", is_default=True),
        types.SimpleNamespace(name="sophie", is_default=False),
    ]
    fake_api.read_profile_meta = lambda d: {"description": "검토자"} if d.name == "sophie" else {}
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/kanban/profiles")
    body = await resp.json()
    assert resp.status == 200
    assert body == {"profiles": [
        {"name": "default", "is_default": True, "description": ""},
        {"name": "sophie", "is_default": False, "description": "검토자"},
    ]}
    for p in body["profiles"]:
        assert set(p) == KANBAN_PROFILE_SUMMARY_KEYS


async def test_profiles_는_메타를_못_읽어도_목록을_준다(aiohttp_client, fake_api):
    fake_api.list_profiles = lambda: [types.SimpleNamespace(name="sophie", is_default=False)]
    fake_api.read_profile_meta = lambda d: None
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/profiles")).json()
    assert body["profiles"][0]["description"] == ""
