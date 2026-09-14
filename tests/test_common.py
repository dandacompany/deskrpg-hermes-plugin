"""`common.py` — 핸들러가 공유하는 도우미의 계약."""

import types

import pytest
from aiohttp import web

from deskrpg_plugin import common
from tests.fakes_kanban import install_fake_kanban


def _req(query: dict):
    return types.SimpleNamespace(query=query)


@pytest.mark.parametrize("slug", ["default", "a", "proj-1", "x_y", "0abc", "a" * 64])
def test_유효한_보드_슬러그는_그대로_돌려준다(slug):
    assert common.parse_board_slug(_req({"board": slug})) == slug


@pytest.mark.parametrize("slug", ["", "Abc", "-lead", "a.b", "한글", "a" * 65, "a b"])
def test_틀린_보드_슬러그는_400(slug):
    with pytest.raises(common.RequestError) as excinfo:
        common.parse_board_slug(_req({"board": slug}))
    assert excinfo.value.status == 400


def test_보드_쿼리가_없으면_400_board_required():
    # 기본 보드로 조용히 떨어뜨리지 않는다 — 카드가 엉뚱한 보드에 생긴다.
    with pytest.raises(common.RequestError) as excinfo:
        common.parse_board_slug(_req({}))
    assert (excinfo.value.status, excinfo.value.code) == (400, "board_required")


def test_board_slug_정규식이_Hermes_와_같다():
    assert common.BOARD_SLUG_RE.pattern == r"^[a-z0-9][a-z0-9\-_]{0,63}$"


async def test_json_error_모양(aiohttp_client):
    app = web.Application()

    async def e(request):
        return common.json_error(404, "not_found")

    async def d(request):
        return common.json_error(400, "bad", "why", field="x")

    app.router.add_get("/e", e)
    app.router.add_get("/d", d)
    client = await aiohttp_client(app)
    resp = await client.get("/e")
    assert resp.status == 404
    assert await resp.json() == {"error": "not_found"}  # detail 이 None 이면 키를 넣지 않는다
    resp = await client.get("/d")
    assert resp.status == 400
    assert await resp.json() == {"error": "bad", "detail": "why", "field": "x"}


async def test_RequestError_는_json_error_로_변환된다(aiohttp_client):
    app = web.Application()

    async def h(request):
        try:
            common.require_str({}, "title")
        except common.RequestError as e:
            return e.response()

    app.router.add_get("/", h)
    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 400
    assert await resp.json() == {"error": "missing_field", "detail": "title"}


async def test_board_conn_은_init_db_뒤_connect_하고_닫는다(tmp_path):
    api = types.SimpleNamespace()
    db = install_fake_kanban(api, tmp_path)
    db.create_board("proj")
    calls = []
    real_init = api.init_db
    api.init_db = lambda *a, **k: (calls.append(("init", k.get("board"))), real_init(*a, **k))[1]

    def work():
        with common.board_conn(api, "proj") as conn:
            calls.append(("conn", conn.board))
            return conn

    conn = await common.run_blocking(work)
    assert calls == [("init", "proj"), ("conn", "proj")]
    assert conn.closed is True


def test_require_str():
    assert common.require_str({"t": "x"}, "t") == "x"
    assert common.require_str({}, "t", required=False) is None
    assert common.require_str({"t": None}, "t", required=False, default="d") == "d"
    with pytest.raises(common.RequestError):
        common.require_str({"t": "  "}, "t")
    with pytest.raises(common.RequestError):
        common.require_str({"t": 3}, "t")
    assert common.require_str({"t": ""}, "t", allow_empty=True) == ""


def test_require_int_는_bool_을_거른다():
    assert common.require_int({"n": 3}, "n") == 3
    assert common.require_int({}, "n", required=False, default=7) == 7
    with pytest.raises(common.RequestError):
        common.require_int({"n": True}, "n")
    with pytest.raises(common.RequestError):
        common.require_int({"n": 0}, "n", minimum=1)
    with pytest.raises(common.RequestError):
        common.require_int({"n": "3"}, "n")


def test_require_bool_과_str_list():
    assert common.require_bool({"b": False}, "b") is False
    with pytest.raises(common.RequestError):
        common.require_bool({"b": 1}, "b")
    assert common.require_str_list({"s": ["a", "b"]}, "s") == ["a", "b"]
    assert common.require_str_list({}, "s", required=False, default=[]) == []
    with pytest.raises(common.RequestError):
        common.require_str_list({"s": ["a", 1]}, "s")
    with pytest.raises(common.RequestError):
        common.require_str_list({"s": "a"}, "s")


def test_객체가_아닌_본문은_invalid_body():
    with pytest.raises(common.RequestError) as excinfo:
        common.require_str(["not", "dict"], "t")
    assert excinfo.value.code == "invalid_body"


def test_log_event_는_본문을_찍지_않는다(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="deskrpg_plugin"):
        common.log_event("task.create", board="proj", task_id="t1", title="SECRET TITLE", body=b"xx", parents=["a", "b"], ok=True)
    line = caplog.records[-1].getMessage()
    assert "SECRET" not in line
    assert "title_len=12" in line
    assert "body_bytes=2" in line
    assert "parents_count=2" in line
    assert "board=proj" in line and "task_id=t1" in line and "ok=True" in line


# ---------------------------------------------------------------------------
# guarded — 모든 핸들러 팩토리의 마지막 방어선
# ---------------------------------------------------------------------------


async def test_guarded_는_RequestError_를_그_응답으로_바꾼다():
    @common.guarded
    async def handler(request):
        raise common.RequestError(409, "invalid_transition", "why")

    resp = await handler(None)
    assert resp.status == 409
    assert resp.text == '{"error": "invalid_transition", "detail": "why"}'


async def test_guarded_는_예상_못_한_예외를_500_internal_error_타입_이름만으로_바꾼다(caplog):
    @common.guarded
    async def handler(request):
        raise RuntimeError("secret-token-abc /home/dante/.hermes/config.yaml")

    with caplog.at_level("ERROR", logger="deskrpg_plugin"):
        resp = await handler(None)
    assert resp.status == 500
    assert resp.text == '{"error": "internal_error", "detail": "RuntimeError"}'
    assert "secret-token-abc" not in resp.text
    # 로그 메시지 자체에도 타입 이름만 — 트레이스는 exc_info 로 붙는다.
    assert any(r.getMessage() == "[deskrpg] 핸들러 예외: RuntimeError" and r.exc_info for r in caplog.records)


async def test_guarded_는_정상_응답을_그대로_돌려준다():
    @common.guarded
    async def handler(request):
        return web.json_response({"ok": True})

    assert (await handler(None)).status == 200
