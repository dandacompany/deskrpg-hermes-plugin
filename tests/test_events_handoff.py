"""HTTP contract for transferring the channel event carrier cursor."""

import pytest
from aiohttp import web

from deskrpg_plugin import events, routes
from deskrpg_plugin.contract_fields import EVENT_HANDOFF_KEYS
from tests.conftest import FakeAdapter
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import install_fake_events


@pytest.fixture
def api(fake_api, tmp_path):
    install_fake_events(fake_api, tmp_path / "kanban")
    install_fake_cron(fake_api, tmp_path)
    return fake_api


async def client_for(aiohttp_client, api, *, authorized=True):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=authorized), api)
    return await aiohttp_client(app)


def payload(board_cursor=None, carrier_cursor=None, board="default"):
    return {"board": board, "board_cursor": board_cursor, "carrier_cursor": carrier_cursor}


async def test_두_커서의_보드와_전역_위치를_정확히_합친다(aiohttp_client, api):
    client = await client_for(aiohttp_client, api)
    target = events.encode_cursor({"k": 7, "d": 3, "c": {"old": {"t": None, "o": {}}}, "a": 99})
    donor_c = {"sophie": {"t": "2026-09-14T10:00:00+09:00", "o": {"run1": "running"}}}
    source = events.encode_cursor({"k": 42, "d": 24, "c": donor_c, "a": 2})
    response = await client.post("/deskrpg/events/handoff", json=payload(target, source))
    assert response.status == 200
    body = await response.json()
    assert set(body) == EVENT_HANDOFF_KEYS
    assert events.decode_cursor(body["cursor"]) == {"k": 7, "d": 3, "c": donor_c, "a": 2}
    assert (await (await client.post("/deskrpg/events/handoff", json=payload(target, source))).json()) == body


async def test_대상_커서가_없으면_보드만_현재화한다(aiohttp_client, api):
    client = await client_for(aiohttp_client, api)
    source = events.encode_cursor({"k": 44, "d": 32, "c": {}, "a": 5})
    response = await client.post("/deskrpg/events/handoff", json=payload(carrier_cursor=source))
    assert response.status == 200
    assert events.decode_cursor((await response.json())["cursor"]) == {"k": 0, "d": 0, "c": {}, "a": 5}


@pytest.mark.parametrize("field", ["board_cursor", "carrier_cursor"])
async def test_깨진_토큰은_400_이고_unknown_cursor_자동초기화가_아니다(aiohttp_client, api, field):
    client = await client_for(aiohttp_client, api)
    body = payload(events.encode_cursor({"k": 1, "d": 2, "c": {}, "a": 3}),
                   events.encode_cursor({"k": 1, "d": 2, "c": {}, "a": 3}))
    body[field] = "broken"
    response = await client.post("/deskrpg/events/handoff", json=body)
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_handoff_cursor"


async def test_기증_커서에_a가_없으면_409(aiohttp_client, api):
    client = await client_for(aiohttp_client, api)
    response = await client.post("/deskrpg/events/handoff", json=payload(carrier_cursor=events.encode_cursor({"k": 1, "d": 2, "c": {}})))
    assert response.status == 409
    assert (await response.json())["error"] == "carrier_cursor_incomplete"


@pytest.mark.parametrize("state", [
    {"k": -1, "d": 0, "c": {}, "a": 0},
    {"k": 0, "d": -1, "c": {}, "a": 0},
    {"k": 0, "d": 0, "c": {}, "a": -1},
    {"k": 0, "d": 0, "c": {"sophie": {"t": None, "o": {"run1": 3}}}, "a": 0},
    {"k": 0, "d": 0, "c": {"sophie": {"t": None, "o": {"run1": ""}}}, "a": 0},
    {"k": 0, "d": 0, "c": {"": {"t": None, "o": {}}}, "a": 0},
])
async def test_인계에서만_잘못된_해독상태를_거절한다(aiohttp_client, api, state):
    token = events.encode_cursor(state)
    assert events.decode_cursor(token) == state  # legacy GET decoder remains permissive
    client = await client_for(aiohttp_client, api)
    response = await client.post("/deskrpg/events/handoff", json=payload(carrier_cursor=token))
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_handoff_cursor"


async def test_대상_커서도_음수_위치를_거절한다(aiohttp_client, api):
    client = await client_for(aiohttp_client, api)
    source = events.encode_cursor({"k": 0, "d": 0, "c": {}, "a": 0})
    target = events.encode_cursor({"k": -1, "d": 0, "c": {}})
    response = await client.post("/deskrpg/events/handoff", json=payload(target, source))
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_handoff_cursor"


async def test_보드와_입력_인증을_검사한다(aiohttp_client, api):
    source = events.encode_cursor({"k": 1, "d": 2, "c": {}, "a": 3})
    client = await client_for(aiohttp_client, api)
    assert (await client.post("/deskrpg/events/handoff", json=payload(carrier_cursor=source, board="missing"))).status == 404
    for body in (payload(carrier_cursor=source) | {"extra": 1}, payload(carrier_cursor=None),
                 payload(carrier_cursor=source, board="Bad Slug"), [1]):
        assert (await client.post("/deskrpg/events/handoff", json=body)).status == 400
    unauthenticated = await client_for(aiohttp_client, api, authorized=False)
    assert (await unauthenticated.post("/deskrpg/events/handoff", json=payload(carrier_cursor=source))).status == 401
    assert (await client.post("/p/sophie/deskrpg/events/handoff", json=payload(carrier_cursor=source))).status == 404
