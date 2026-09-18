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


async def test_생성_응답이_새_프로필의_키를_싣는다(aiohttp_client, fake_api):
    # Hermes 는 빈 .env 를 씨딩할 뿐이라, 키가 없으면 갓 만든 프로필은 인증
    # fail-closed 에 막혀 아무도 말을 걸 수 없다. 생성 시점이 이 키가 평문으로
    # 오가는 유일한 순간이므로, 응답이 그것을 실어 나르지 못하면 사용자는
    # 결국 셸로 돌아가야 한다 — 이 플러그인의 존재 이유가 무너진다.
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah"})
    body = await resp.json()
    assert resp.status == 201
    assert body["keyIssued"] is True
    assert len(body["apiKey"]) >= 16
    env = (fake_api.get_profile_dir("noah") / ".env").read_text(encoding="utf-8")
    assert f"API_SERVER_KEY={body['apiKey']}" in env


async def test_키_발급이_실패해도_프로필이_생겼다는_사실을_숨기지_않는다(
    aiohttp_client, fake_api, monkeypatch
):
    # 500 으로 덮으면 사용자는 만들어진 프로필을 모른 채 같은 이름으로 다시
    # 시도하고 409 를 만난다 — 무엇이 잘못됐는지 알 방법이 없어진다.
    from deskrpg_plugin import keyissue

    def boom(_dir):
        raise keyissue.KeyIssueFailed(".env 을 쓸 수 없다: PermissionError")

    monkeypatch.setattr(keyissue, "issue", boom)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah"})
    body = await resp.json()
    assert resp.status == 201
    assert body["keyIssued"] is False
    assert "PermissionError" in body["keyError"]
    assert "apiKey" not in body
    assert fake_api.profile_exists("noah")  # 프로필은 실제로 남아 있다


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


async def test_DELETE는_동기_삭제_중에도_이벤트_루프를_막지_않는다(aiohttp_client, fake_api, monkeypatch):
    # I-5: 실제 delete_profile 은 systemctl + rmtree 를 동기로 돈다. 핸들러가
    # 이걸 직접 부르면 그동안 다른 요청이 전혀 처리되지 않는다. asyncio.to_thread
    # 로 감쌌다면 느린 삭제가 진행 중이어도 동시 요청(/deskrpg/info)이 먼저
    # 끝나야 한다.
    fake_api.create_profile("noah")
    # 느리게 만들 대상은 **핸들러가 실제로 부르는 것**이어야 한다. 예전에는
    # fake_api.delete_profile 을 느리게 했는데, 안전 삭제로 바꾸면서 그 함수를
    # 더 이상 부르지 않게 되자 이 테스트가 조용히 아무것도 지키지 않게 됐다
    # (실측: to_thread 를 벗겨도 전부 통과).
    from deskrpg_plugin import safedelete

    original_tree = safedelete.delete_profile_tree

    def slow_tree(name, profile_dir, home=None):
        time.sleep(0.3)  # 스레드에서 도는지 확인할 만큼만 느리게
        return original_tree(name, profile_dir, home)

    monkeypatch.setattr(safedelete, "delete_profile_tree", slow_tree)

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


async def test_전용_서비스가_있으면_지우지_않고_409(aiohttp_client, fake_api, tmp_path, monkeypatch):
    # Hermes 의 delete_profile 은 이 상황에서 게이트웨이 자신에게 systemctl stop 을
    # 걸어 전체를 죽인다. 우리는 그 함수를 부르지 않고, 전용 유닛이 있으면 아예
    # 손대지 않는다 — 지우면 고아 유닛이 남기 때문이다.
    from deskrpg_plugin import safedelete

    fake_api.create_profile("noah")
    # 유닛이 어디 사는지는 플랫폼마다 다르다. 경로를 테스트에 하드코딩하면
    # 한쪽 OS 에서만 무는 테스트가 된다 — 구현이 보는 그 자리에 놓는다.
    unit_path = safedelete._candidate_unit_paths("noah", tmp_path / "home")[0]
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(safedelete, "user_home", lambda: tmp_path / "home")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=noah")
    body = await resp.json()
    assert resp.status == 409
    assert body["error"] == "profile_has_service"
    assert body["unit"] == "hermes-gateway-noah"
    assert "hermes profile delete noah" in body["reason"]
    assert fake_api.profile_exists("noah")  # 아무것도 지우지 않았다


async def test_삭제는_Hermes의_delete_profile을_부르지_않는다(aiohttp_client, fake_api, tmp_path, monkeypatch):
    # 이 한 줄이 게이트웨이를 죽였다. 다시 호출되면 즉시 알아야 한다.
    from deskrpg_plugin import safedelete

    monkeypatch.setattr(safedelete, "user_home", lambda: tmp_path / "empty")
    called = []
    fake_api.delete_profile = lambda *a, **k: called.append(a)

    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/deskrpg/profiles/noah?confirm=noah")
    assert resp.status == 200
    assert called == [], "Hermes 의 delete_profile 을 부르면 게이트웨이가 죽는다"
    assert not fake_api.profile_exists("noah")


def test_유닛_이름은_프로필별로_갈리고_공용_게이트웨이와_겹치지_않는다():
    from deskrpg_plugin import safedelete

    assert safedelete.service_unit_name("noah") == "hermes-gateway-noah"
    # 공용 게이트웨이의 유닛 이름과 절대 같아지지 않는다 — 같아지는 순간
    # 우리가 지금 도는 게이트웨이를 지우게 된다.
    assert safedelete.service_unit_name("noah") != safedelete.SERVICE_BASE


async def test_cloneFrom_default_면_복제_결과의_이름만_싣는다(aiohttp_client, fake_api, tmp_path):
    import yaml
    home = tmp_path / "default-home"
    home.mkdir()
    orig = fake_api.get_profile_dir
    fake_api.get_profile_dir = lambda name: home if name == "default" else orig(name)
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "m", "provider": "openai"}}), encoding="utf-8")
    (home / ".env").write_text("OPENAI_API_KEY=sk-SECRET-abcdefgh12345678\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah", "cloneFrom": "default"})
    assert resp.status == 201
    text = await resp.text()
    assert "sk-SECRET" not in text
    body = await resp.json()
    assert body["cloned"] == {"configKeys": ["model"], "envKeys": ["OPENAI_API_KEY"]}
    assert body["needsLogin"] == []
    assert body["keyIssued"] is True
    env = (orig("noah") / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-SECRET" in env and "API_SERVER_KEY=" in env


async def test_cloneFrom_은_default_만_받는다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah", "cloneFrom": "mia"})
    assert resp.status == 400
    assert not fake_api.profile_exists("noah")


async def test_복제가_실패해도_프로필은_만들어졌다고_말한다(aiohttp_client, fake_api, tmp_path):
    home = tmp_path / "default-home"
    home.mkdir()
    orig = fake_api.get_profile_dir
    fake_api.get_profile_dir = lambda name: home if name == "default" else orig(name)
    (home / "config.yaml").write_text("a: [", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles", json={"name": "noah", "cloneFrom": "default"})
    assert resp.status == 201
    body = await resp.json()
    assert "cloned" not in body and isinstance(body["cloneError"], str)
    assert body["keyIssued"] is True


async def test_cloneFrom_이_없으면_예전과_같다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.post("/deskrpg/profiles", json={"name": "noah"})).json()
    assert "cloned" not in body and "needsLogin" not in body and "cloneError" not in body
