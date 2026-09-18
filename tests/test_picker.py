"""툴셋·스킬 목록 — 우리가 만들지 않고 Hermes 것을 프로필 홈 스코프로 옮긴다."""

import yaml
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def _seed(fake_api, data):
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


async def test_툴셋_목록은_켜짐과_키_설정_여부를_함께_준다(aiohttp_client, fake_api):
    _seed(fake_api, {"platform_toolsets": {"api_server": ["web", "tts"]}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/toolsets")).json()
    assert body["platform"] == "api_server"
    rows = {r["name"]: r for r in body["toolsets"]}
    assert rows["web"] == {"name": "web", "label": "🔍 Web", "description": "검색과 스크래핑",
                           "enabled": True, "configured": True}
    assert rows["tts"]["enabled"] is True and rows["tts"]["configured"] is False
    assert rows["file"]["enabled"] is False


async def test_api_server_에서_못_쓰는_툴셋은_목록에_없다(aiohttp_client, fake_api):
    _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/toolsets")).json()
    assert "discord" not in {r["name"] for r in body["toolsets"]}


async def test_키_판정이_던져도_그_행만_null_이다(aiohttp_client, fake_api):
    _seed(fake_api, {})

    def boom(name, cfg=None):
        if name == "web":
            raise RuntimeError("secret-leak-should-not-appear")
        return True

    fake_api._toolset_has_keys = boom
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/toolsets")
    assert resp.status == 200
    rows = {r["name"]: r for r in (await resp.json())["toolsets"]}
    assert rows["web"]["configured"] is None
    assert rows["file"]["configured"] is True


async def test_목록은_요청_프로필의_홈에서_읽는다(aiohttp_client, fake_api):
    _seed(fake_api, {})
    seen = []
    original = fake_api._get_effective_configurable_toolsets

    def spy():
        seen.append(str(fake_api.get_hermes_home()))
        return original()

    fake_api._get_effective_configurable_toolsets = spy
    client = await _client(aiohttp_client, fake_api)
    await client.get("/p/sophie/deskrpg/toolsets")
    assert seen == [str(fake_api.get_profile_dir("sophie"))]


async def test_스킬_목록은_꺼짐과_필수_여부를_함께_준다(aiohttp_client, fake_api):
    _seed(fake_api, {"skills": {"disabled": ["xlsx"]}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/skills")).json()
    rows = {r["name"]: r for r in body["skills"]}
    assert rows["xlsx"]["disabled"] is True
    assert rows["pdf"] == {"name": "pdf", "category": "docs", "description": "PDF 다루기",
                           "disabled": False, "essential": False}
    assert rows["hermes-agent"]["essential"] is True
    assert [r["name"] for r in body["skills"]] == ["hermes-agent", "pdf", "xlsx"]


async def test_없는_프로필은_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/ghost/deskrpg/toolsets")
    assert resp.status == 404
    assert (await resp.json())["error"] == "profile_not_found"


async def test_config_가_망가졌으면_409(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text("a: [", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/toolsets")
    assert resp.status == 409
    assert (await resp.json())["error"] == "config_unreadable"


async def test_심볼이_없는_빌드에는_라우트가_없다(aiohttp_client, fake_api):
    fake_api._find_all_skills = None
    fake_api.create_profile("sophie")
    client = await _client(aiohttp_client, fake_api)
    assert (await client.get("/p/sophie/deskrpg/skills")).status == 404
    assert (await client.get("/p/sophie/deskrpg/toolsets")).status == 200


async def test_망가진_config_의_비밀_값은_409_본문과_로그에_없다(aiohttp_client, fake_api, caplog):
    import logging

    secret = "sk-PICKER-LEAK-0123456789abcdef"
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text(
        f'providers:\n  x:\n    api_key: "{secret}\n  y: [\n', encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    with caplog.at_level(logging.DEBUG):
        for path in ("/p/sophie/deskrpg/toolsets", "/p/sophie/deskrpg/skills"):
            resp = await client.get(path)
            assert resp.status == 409
            text = await resp.text()
            assert (await resp.json())["error"] == "config_unreadable"
            assert secret not in text
    assert secret not in caplog.text
