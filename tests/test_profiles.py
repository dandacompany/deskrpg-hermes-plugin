import asyncio
import os
import stat
import time

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
    fake_api.create_profile("noah")  # fake 도 실제 CLI 처럼 wrapper 를 함께 만든다
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=noah")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["name"] == "noah"
    assert payload["removed"]["wrapperScript"] is True
    assert fake_api.profile_exists("noah") is False


async def test_DELETE는_동기_삭제_중에도_이벤트_루프를_막지_않는다(aiohttp_client, fake_api):
    # I-5: 실제 delete_profile 은 systemctl + rmtree 를 동기로 돈다. 핸들러가
    # 이걸 직접 부르면 그동안 다른 요청이 전혀 처리되지 않는다. asyncio.to_thread
    # 로 감쌌다면 느린 삭제가 진행 중이어도 동시 요청(/deskrpg/info)이 먼저
    # 끝나야 한다.
    fake_api.create_profile("noah")
    original_delete = fake_api.delete_profile

    def slow_delete(name, yes=False):
        time.sleep(0.3)  # 스레드에서 도는지 확인할 만큼만 느리게
        return original_delete(name, yes=yes)

    fake_api.delete_profile = slow_delete

    client = await _client(aiohttp_client, fake_api)

    async def do_delete():
        return await client.delete("/deskrpg/profiles/noah?confirm=noah")

    async def do_info():
        return await client.get("/deskrpg/info")

    # 상대 타이밍(wait_for)으로는 잡히지 않는다 — 루프가 막히면 그 sleep 자체가
    # 삭제가 끝날 때까지 밀리고, wait_for 는 이미 풀린 뒤에 시작해 즉시 성공한다.
    # 요청 시작부터의 절대 경과시간을 재야 블로킹이 드러난다.
    t0 = time.monotonic()
    delete_task = asyncio.ensure_future(do_delete())
    await asyncio.sleep(0.05)  # delete 가 먼저 진행되게 살짝 양보
    info_resp = await do_info()
    elapsed = time.monotonic() - t0
    assert info_resp.status == 200
    assert elapsed < 0.25, (
        f"info 응답까지 {elapsed:.3f}s — 느린 삭제(0.3s)가 이벤트 루프를 막았다"
    )

    delete_resp = await delete_task
    assert delete_resp.status == 200


async def test_wrapper가_없던_프로필은_wrapperScript가_거짓이다(aiohttp_client, fake_api):
    # I-3: delete_profile 이 wrapper 를 스스로 지우므로, 그 뒤에 다시 지우려
    # 들면 항상 False 가 나오는 버그가 있었다. 이 테스트는 반대 극단을
    # 확인한다 — wrapper 가 애초에 없던 경우에도 응답이 거짓으로 True 를
    # 주장하지 않아야 한다.
    fake_api.create_profile("noah")
    fake_api.get_wrapper_path("noah").unlink()  # wrapper 가 아예 없었던 상황을 만든다
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=noah")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["removed"]["wrapperScript"] is False
