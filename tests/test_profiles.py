import os
import stat

import pytest
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


async def test_목록은_인격_보유_여부를_함께_준다(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    fake_api.create_profile("mia")
    (fake_api.get_profile_dir("mia") / "SOUL.md").write_text("나는 미아다", encoding="utf-8")

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/profiles")).json()
    by_name = {p["name"]: p for p in body["profiles"]}
    assert by_name["sophie"]["hasCustomPersona"] is False  # 기본 템플릿 그대로
    assert by_name["mia"]["hasCustomPersona"] is True


async def test_SOUL_MD_가_깨진_인코딩이어도_목록_전체는_살아있다(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    fake_api.create_profile("noah")
    # UTF-8 로 디코드되지 않는 바이트 — read_text(encoding="utf-8") 가 UnicodeDecodeError 를 던진다.
    (fake_api.get_profile_dir("noah") / "SOUL.md").write_bytes(b"\xff\xfe\x00broken")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/profiles")
    assert resp.status == 200
    body = await resp.json()
    by_name = {p["name"]: p for p in body["profiles"]}
    assert set(by_name) == {"sophie", "noah"}  # 손상된 프로필도 목록에는 남는다
    assert by_name["sophie"]["hasCustomPersona"] is False
    # 읽기 실패는 "기본 템플릿"(False)으로 둔갑하지 않고 판정 불가(None)로 남는다.
    assert by_name["noah"]["hasCustomPersona"] is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 파일 권한을 무시하므로 이 테스트가 성립하지 않는다")
async def test_SOUL_MD_읽기_권한이_없어도_목록_전체는_살아있다(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    fake_api.create_profile("noah")
    soul = fake_api.get_profile_dir("noah") / "SOUL.md"
    soul.chmod(0)  # 소유자조차 읽기 불가 — read_text() 가 PermissionError 를 던진다.
    try:
        client = await _client(aiohttp_client, fake_api)
        resp = await client.get("/deskrpg/profiles")
        assert resp.status == 200
        body = await resp.json()
        by_name = {p["name"]: p for p in body["profiles"]}
        assert set(by_name) == {"sophie", "noah"}
        assert by_name["sophie"]["hasCustomPersona"] is False
        assert by_name["noah"]["hasCustomPersona"] is None
    finally:
        soul.chmod(stat.S_IRUSR | stat.S_IWUSR)  # tmp_path 정리가 지울 수 있도록 복구


async def test_생성하면_201(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah"})
    assert resp.status == 201
    assert fake_api.profile_exists("noah")


async def test_본문이_객체가_아니면_400(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json=["noah"])
    assert resp.status == 400


async def test_이미_있으면_409(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah"})
    assert resp.status == 409


async def test_이름이_이상하면_400(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "../etc"})
    assert resp.status == 400


async def test_confirm_이_없으면_지우지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah")
    assert resp.status == 400
    assert fake_api.profile_exists("noah")


async def test_confirm_이_어긋나면_지우지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=mia")
    assert resp.status == 400
    assert fake_api.profile_exists("noah")


async def test_default_는_확인해도_지우지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("default")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/default?confirm=default")
    assert resp.status == 400
    assert fake_api.profile_exists("default")


async def test_확인이_맞으면_지우고_무엇이_사라졌는지_알린다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=noah")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["name"] == "noah"
    assert payload["removed"]["wrapperScript"] is True
    assert fake_api.profile_exists("noah") is False
