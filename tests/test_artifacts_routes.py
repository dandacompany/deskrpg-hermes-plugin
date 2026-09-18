"""`/deskrpg/artifacts/*` — 목록·상세·바이트(Range)·사람 편집 버전·rework 501·삭제."""
import asyncio
import contextlib
import types

import pytest
from aiohttp import web

from deskrpg_plugin import artifacts_routes as ar
from deskrpg_plugin import artifacts_store as store
from deskrpg_plugin import contract_fields as cf
from deskrpg_plugin.auth import Scope, require_auth
from tests.conftest import FakeAdapter


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    return types.SimpleNamespace(get_hermes_home=lambda: str(home), MAX_REQUEST_BYTES=10_000_000)


@pytest.fixture
async def client(aiohttp_client, api):
    app = web.Application()
    adapter = FakeAdapter(authorized=True)
    for method, path, name in (
        ("GET", "/deskrpg/artifacts", "list"), ("GET", "/deskrpg/artifacts/{artifact_id}", "get"),
        ("GET", "/deskrpg/artifacts/{artifact_id}/versions/{v}/content", "content"),
        ("POST", "/deskrpg/artifacts/{artifact_id}/versions", "add_version"),
        ("POST", "/deskrpg/artifacts/{artifact_id}/rework", "rework"),
        ("DELETE", "/deskrpg/artifacts/{artifact_id}", "delete"),
    ):
        app.router.add_route(method, path, require_auth(adapter, Scope.DEFAULT, getattr(ar, f"{name}_handler")(api)))
    return await aiohttp_client(app)


def _seed(api, **over):
    with contextlib.closing(store.open_registry(api)) as conn:
        meta = dict(kind="document", title="보고서", summary="요약", filename="r.md", mime="text/markdown",
                    profile="sophie", source_kind="chat", session_id="s1", created_by="agent:sophie", captured_via="tool")
        meta.update(over)
        return store.store_artifact_version(api, conn, meta=store.ArtifactMeta(**meta), data=b"0123456789", max_bytes=100)


async def test_목록은_요약_모양이고_프로필_보드_OR_필터가_된다(client, api):
    _seed(api, profile="a", title="A")
    _seed(api, profile="b", title="B", source_kind="kanban", board="dev", task_id="t1")
    _seed(api, profile="c", title="C")
    resp = await client.get("/deskrpg/artifacts?profiles=a&board=dev")
    assert resp.status == 200
    body = await resp.json()
    assert [x["title"] for x in body["artifacts"]] == ["B", "A"] and body["has_more"] is False
    for item in body["artifacts"]:
        assert cf.ARTIFACT_SUMMARY_REQUIRED <= set(item) <= cf.ARTIFACT_SUMMARY_KEYS
        assert "stored_path" not in item


async def test_목록_커서는_limit_으로_잘리고_다음_페이지가_이어진다(client, api):
    for i in range(3):
        _seed(api, title=f"T{i}", session_id=f"s{i}")
    first = await (await client.get("/deskrpg/artifacts?limit=2")).json()
    assert len(first["artifacts"]) == 2 and first["has_more"] is True
    second = await (await client.get(f"/deskrpg/artifacts?limit=2&cursor={first['cursor']}")).json()
    assert len(second["artifacts"]) == 1 and second["has_more"] is False


async def test_상세는_버전_목록을_주고_stored_path_는_없다(client, api):
    r = _seed(api)
    body = await (await client.get(f"/deskrpg/artifacts/{r.artifact_id}")).json()
    assert body["artifact"]["id"] == r.artifact_id and len(body["versions"]) == 1
    assert cf.ARTIFACT_VERSION_REQUIRED <= set(body["versions"][0]) <= cf.ARTIFACT_VERSION_KEYS


async def test_없는_id_는_404_이고_삭제된_것은_410(client, api):
    assert (await client.get("/deskrpg/artifacts/nope")).status == 404
    r = _seed(api)
    assert (await client.delete(f"/deskrpg/artifacts/{r.artifact_id}", headers={"X-DeskRPG-User": "u1"})).status == 200
    assert (await client.get(f"/deskrpg/artifacts/{r.artifact_id}")).status == 410
    assert (await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")).status == 410


async def test_content_는_바이트와_inline_disposition_을_주고_download_1_이면_attachment(client, api):
    r = _seed(api)
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")
    assert resp.status == 200 and await resp.read() == b"0123456789"
    assert resp.headers["Content-Type"].startswith("text/markdown")
    assert resp.headers["Content-Disposition"].startswith("inline")
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content?download=1")
    assert resp.headers["Content-Disposition"].startswith("attachment")


async def test_content_는_Range_에_206_과_Content_Range_를_준다(client, api):
    r = _seed(api)
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content", headers={"Range": "bytes=2-5"})
    assert resp.status == 206 and await resp.read() == b"2345"
    assert resp.headers["Content-Range"] == "bytes 2-5/10" and resp.headers["Accept-Ranges"] == "bytes"
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content", headers={"Range": "bytes=50-"})
    assert resp.status == 416


async def test_blob_이_없으면_410_missing_이고_목록에_missing_표식이_붙는다(client, api):
    r = _seed(api)
    with contextlib.closing(store.open_registry(api)) as conn:
        store.blob_path_for(api, store.list_versions(conn, r.artifact_id)[0]).unlink()
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")
    assert resp.status == 410 and (await resp.json())["error"] == "artifact_blob_missing"
    body = await (await client.get("/deskrpg/artifacts")).json()
    assert body["artifacts"][0]["missing"] is True


async def test_루트_밖_stored_path_는_403(client, api, tmp_path):
    r = _seed(api)
    with contextlib.closing(store.open_registry(api)) as conn, conn:
        conn.execute("UPDATE artifact_versions SET stored_path=?", (str(tmp_path / "x"),))
    assert (await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")).status == 403


async def test_사람_편집은_JSON_본문으로_새_버전을_만들고_human_으로_기록한다(client, api):
    r = _seed(api)
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions", headers={"X-DeskRPG-User": "u1"},
                             json={"content": "# 고침", "filename": "r.md", "note": "오탈자"})
    assert resp.status == 201, await resp.text()
    v = (await resp.json())["version"]
    assert v["version"] == 2 and v["created_by"] == "human:u1" and v["captured_via"] == "edit" and v["note"] == "오탈자"


async def test_사람_편집은_multipart_도_받고_모르는_키는_400(client, api):
    r = _seed(api)
    from aiohttp import FormData
    form = FormData(); form.add_field("file", b"bytes", filename="r.md", content_type="text/markdown"); form.add_field("note", "n")
    assert (await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions", data=form)).status == 201
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions", json={"content": "x", "filename": "r.md", "bogus": 1})
    assert resp.status == 400 and (await resp.json())["error"] == "unsupported_field"


async def test_rework_는_501(client, api):
    r = _seed(api)
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/rework", json={"note": "다시"})
    assert resp.status == 501 and (await resp.json())["error"] == "not_implemented"


async def test_삭제는_ok_이고_두_번째는_410(client, api):
    r = _seed(api)
    assert (await client.delete(f"/deskrpg/artifacts/{r.artifact_id}")).status == 200
    assert (await client.delete(f"/deskrpg/artifacts/{r.artifact_id}")).status == 410


# ---------------------------------------------------------------------------
# fix round 1 — 편집 업로드는 상한에서 끊고, 413 에 max_bytes 를 싣고, content 는 sandbox 로 낸다
# ---------------------------------------------------------------------------


async def test_사람_편집_multipart_는_상한을_넘으면_413_이고_새_버전이_생기지_않는다(client, api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "8")
    r = _seed(api)
    from aiohttp import FormData
    form = FormData()
    form.add_field("file", b"0" * 32, filename="r.md", content_type="text/markdown")
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions", data=form)
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "artifact_too_large" and body["max_bytes"] == 8
    with contextlib.closing(store.open_registry(api)) as conn:
        assert len(store.list_versions(conn, r.artifact_id)) == 1


async def test_사람_편집_JSON_은_상한을_넘으면_413_이고_max_bytes_를_싣는다(client, api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "8")
    r = _seed(api)
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions",
                             json={"content": "0" * 32, "filename": "r.md"})
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "artifact_too_large" and body["max_bytes"] == 8
    with contextlib.closing(store.open_registry(api)) as conn:
        assert len(store.list_versions(conn, r.artifact_id)) == 1


async def test_사람_편집_multipart_파일_파트가_두_개면_400(client, api):
    r = _seed(api)
    from aiohttp import FormData
    form = FormData()
    form.add_field("file", b"a", filename="a.md", content_type="text/markdown")
    form.add_field("file", b"b", filename="b.md", content_type="text/markdown")
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions", data=form)
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_field"


async def test_content_는_sandbox_CSP_와_nosniff_를_200_과_206_에_모두_싣는다(client, api):
    r = _seed(api, filename="r.html", mime="text/html")
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")
    assert resp.status == 200
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content", headers={"Range": "bytes=0-3"})
    assert resp.status == 206
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_사람_편집은_요청_본문_상한으로_묶여_413_의_max_bytes_가_그것이다(client, api, monkeypatch):
    """사람 편집은 HTTP 로 오므로 min(MAX_REQUEST_BYTES, 저장소 상한) 이 실효 상한이다(I2)."""
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", raising=False)
    api.MAX_REQUEST_BYTES = 10
    r = _seed(api)
    resp = await client.post(f"/deskrpg/artifacts/{r.artifact_id}/versions",
                             json={"content": "0" * 20, "filename": "r.md"})
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "artifact_too_large" and body["max_bytes"] == 10


# ---------------------------------------------------------------------------
# 최종 수정 — 커서·숫자 파싱·사용자 헤더·바이트 스트림 경계·중복 삭제
# ---------------------------------------------------------------------------


def _b64(obj) -> str:
    import base64
    import json
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


@pytest.mark.parametrize("value", [{"a": 1}, {"0": 1, "1": "x"}, [1, "x", 3], [1], 5, "ab"])
async def test_목록_커서가_두_원소_리스트가_아니면_400_unknown_cursor(client, api, value):
    resp = await client.get(f"/deskrpg/artifacts?cursor={_b64(value)}")
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_cursor"


async def test_목록_limit_이_ASCII_숫자가_아니면_기본값이다(client, api):
    _seed(api)
    resp = await client.get("/deskrpg/artifacts", params={"limit": "²"})
    assert resp.status == 200
    assert len((await resp.json())["artifacts"]) == 1


async def test_바이트_v_가_ASCII_숫자가_아니면_400_invalid_field(client, api):
    r = _seed(api)
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/%C2%B2/content")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_field"


class _Req:
    def __init__(self, value):
        self.headers = {} if value is None else {ar.USER_HEADER: value}


@pytest.mark.parametrize("raw, expected", [
    ("dante\ud800", "human:dante"),
    ("\x00da\nnte\x7f", "human:dante"),
    ("  ", "human:unknown"),
    ("𐏿", "human:unknown"),
    (None, "human:unknown"),
    ("가" * 200, "human:" + "가" * 128),
])
def test_사용자_헤더는_제어문자와_외톨이_서로게이트를_지우고_128자로_자른다(raw, expected):
    out = ar._user(_Req(raw))
    assert out == expected
    out.encode("utf-8")  # 외톨이 서로게이트가 남으면 여기서 UnicodeEncodeError


async def test_확인과_열기_사이에_blob_이_사라지면_응답을_시작하기_전에_410(client, api, monkeypatch):
    r = _seed(api)

    def gone(path, start):
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(ar, "_open_and_seek", gone)
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")
    assert resp.status == 410
    assert (await resp.json())["error"] == "artifact_blob_missing"


async def test_쓰는_중_연결이_끊기면_두_번째_응답을_보내지_않고_파일을_닫는다(client, api, monkeypatch, caplog):
    r = _seed(api)
    opened = []
    real_open = ar._open_and_seek

    def tracking(path, start):
        fh = real_open(path, start)
        opened.append(fh)
        return fh

    async def broken_write(self, data):
        raise ConnectionResetError("peer gone")

    monkeypatch.setattr(ar, "_open_and_seek", tracking)
    monkeypatch.setattr(web.StreamResponse, "write", broken_write)
    caplog.set_level("INFO", logger="deskrpg_plugin")
    resp = await client.get(f"/deskrpg/artifacts/{r.artifact_id}/versions/1/content")
    assert resp.status == 200  # 헤더는 이미 나갔다 — 본문은 기다리지 않는다(가짜 끊김이라 전송로는 살아 있다)
    resp.close()
    for _ in range(100):  # 핸들러가 finally 까지 도는 것을 기다린다
        if opened and opened[0].closed:
            break
        await asyncio.sleep(0.01)
    assert opened and opened[0].closed
    assert "핸들러 예외" not in caplog.text
    assert "artifact.content_aborted" in caplog.text


async def test_열기를_기다리다_취소되면_늦게_열린_핸들도_닫힌다(tmp_path, monkeypatch):
    import threading
    target = tmp_path / "blob.bin"; target.write_bytes(b"0123456789")
    gate, opened = threading.Event(), []
    real_open = ar._open_and_seek

    def slow(path, start):
        gate.wait(5)
        fh = real_open(path, start)
        opened.append(fh)
        return fh

    monkeypatch.setattr(ar, "_open_and_seek", slow)
    waiter = asyncio.ensure_future(ar._open_blob(target, 0))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    for _ in range(200):
        if opened and opened[0].closed:
            break
        await asyncio.sleep(0.01)
    assert opened and opened[0].closed
