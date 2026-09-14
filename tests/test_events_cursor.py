"""E1·E2·E7 — 커서 토큰과 핸들러의 바깥 껍데기(커서 없음·깨진 커서·보드 없음)."""

import base64
import json

import pytest

from deskrpg_plugin import events
from deskrpg_plugin.common import RequestError
from deskrpg_plugin.contract_fields import EVENTS_PAGE_KEYS
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import append_deleted, events_client, install_fake_events


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


# ---------------------------------------------------------------------------
# E2 — 토큰
# ---------------------------------------------------------------------------


def test_커서는_v1_접두사와_base64url_JSON_으로_왕복한다():
    state = {"k": 12, "d": 3, "c": {"sophie": {"t": "2026-09-14T10:00:00+09:00", "o": {"exec001": "running"}}}}
    token = events.encode_cursor(state)
    assert token.startswith("v1.")
    assert "=" not in token  # URL 에 그대로 실린다
    assert events.decode_cursor(token) == state


def test_커서는_빈_프로필_맵과_None_t_도_왕복한다():
    state = {"k": 0, "d": 0, "c": {"a": {"t": None, "o": {}}}}
    assert events.decode_cursor(events.encode_cursor(state)) == state


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        "",
        "v2." + base64.urlsafe_b64encode(b'{"k":1,"d":1,"c":{}}').decode().rstrip("="),
        "v1.!!!not-base64!!!",
        "v1." + base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        "v1." + base64.urlsafe_b64encode(b"[1,2,3]").decode().rstrip("="),
        "v1." + base64.urlsafe_b64encode(json.dumps({"k": "1", "d": 0, "c": {}}).encode()).decode().rstrip("="),
        "v1." + base64.urlsafe_b64encode(json.dumps({"k": 1, "d": 0}).encode()).decode().rstrip("="),
        "v1." + base64.urlsafe_b64encode(json.dumps({"k": 1, "d": 0, "c": {"p": {"t": 5, "o": {}}}}).encode()).decode().rstrip("="),
        "v1." + base64.urlsafe_b64encode(json.dumps({"k": True, "d": 0, "c": {}}).encode()).decode().rstrip("="),
    ],
)
def test_모르는_커서는_400_unknown_cursor(token):
    with pytest.raises(RequestError) as info:
        events.decode_cursor(token)
    assert info.value.status == 400
    assert info.value.code == "unknown_cursor"


def test_clamp_limit_은_기본_200_최대_500_최소_1():
    assert events.clamp_limit(None) == 200
    assert events.clamp_limit("abc") == 200
    assert events.clamp_limit("0") == 1
    assert events.clamp_limit("9999") == 500
    assert events.clamp_limit("42") == 42


# ---------------------------------------------------------------------------
# E1·E7 — 핸들러
# ---------------------------------------------------------------------------


async def test_커서가_없으면_빈_목록과_지금_토큰을_돌려준다(aiohttp_client, fake_api, kanban, store):
    conn = kanban.connect(board="default")
    task = kanban.make_task(conn, title="첫 카드")
    kanban.add_comment(conn, task.id, "dante", "댓글")
    append_deleted(fake_api, "default", "t9999", "지운 카드", 1_700_000_000)
    fake_api.create_profile("sophie")
    home = fake_api.get_profile_dir("sophie")
    store.add_execution(home, id="exec-run", job_id="job001", status="running", claimed_at="2026-09-14T10:00:00+09:00")
    store.add_execution(home, id="exec-done", job_id="job001", status="completed", claimed_at="2026-09-14T09:00:00+09:00")

    client = await events_client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/events?board=default")
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == EVENTS_PAGE_KEYS
    assert body["events"] == []
    assert body["has_more"] is False

    state = events.decode_cursor(body["cursor"])
    assert state["k"] == max(e.id for e in kanban.boards["default"].events)
    assert state["d"] == 1
    assert state["c"]["sophie"] == {"t": "2026-09-14T10:00:00+09:00", "o": {"exec-run": "running"}}


async def test_지금_토큰으로_바로_다시_부르면_사건이_없다(aiohttp_client, fake_api, kanban, store):
    conn = kanban.connect(board="default")
    kanban.make_task(conn, title="있던 카드")
    client = await events_client(aiohttp_client, fake_api)
    first = await (await client.get("/deskrpg/events?board=default")).json()
    second = await (await client.get(f"/deskrpg/events?board=default&cursor={first['cursor']}")).json()
    assert second["events"] == []
    assert second["has_more"] is False


async def test_깨진_커서는_400_unknown_cursor(aiohttp_client, fake_api, kanban, store):
    client = await events_client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/events?board=default&cursor=garbage")
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_cursor"


async def test_보드가_없으면_404_board_not_found(aiohttp_client, fake_api, kanban, store):
    client = await events_client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/events?board=nope")
    assert resp.status == 404
    assert (await resp.json())["error"] == "board_not_found"


async def test_board_가_없거나_모양이_틀리면_400(aiohttp_client, fake_api, kanban, store):
    client = await events_client(aiohttp_client, fake_api)
    assert (await client.get("/deskrpg/events")).status == 400
    assert (await client.get("/deskrpg/events?board=Bad%20Slug")).status == 400


async def test_인증_실패는_401(aiohttp_client, fake_api, kanban, store):
    client = await events_client(aiohttp_client, fake_api, authorized=False)
    assert (await client.get("/deskrpg/events?board=default")).status == 401


async def test_Hermes_가_예상_못_한_예외를_던지면_500_internal_error_JSON(aiohttp_client, fake_api, kanban, store):
    def boom(*a, **kw):
        raise RuntimeError("secret board path")

    fake_api.board_exists = boom
    client = await events_client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/events?board=default")
    assert resp.status == 500
    body = await resp.json()
    assert body == {"error": "internal_error", "detail": "RuntimeError"}
    assert "secret" not in await resp.text()
