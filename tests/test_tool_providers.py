"""도구별 프로바이더 — `hermes tools` 의 프로바이더 선택·키 입력을 프로필 단위로."""

import logging
import os
import stat

import yaml
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter

SECRET = "sk-TEST-TOOLKEY-0123456789abcdef"


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def _rows(body):
    return {row["name"]: row for row in body["providers"]}


async def test_프로바이더_행과_키_이름을_주고_값은_주지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    (home / ".env").write_text(f"VOICE_TOOLS_OPENAI_KEY={SECRET}\n", encoding="utf-8")
    (home / "config.yaml").write_text("tts:\n  provider: openai\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/noah/deskrpg/toolsets/tts/providers")
    assert resp.status == 200
    text = await resp.text()
    assert SECRET not in text
    body = await resp.json()
    assert body["toolset"] == "tts" and body["hasProviders"] is True
    rows = _rows(body)
    assert list(rows) == ["Microsoft Edge TTS", "OpenAI TTS", "Piper", "Nous Subscription"]
    openai = rows["OpenAI TTS"]
    assert openai["envVars"] == [{"key": "VOICE_TOOLS_OPENAI_KEY", "prompt": "OpenAI API key",
                                  "url": "https://platform.openai.com/api-keys", "isSet": True}]
    # 키 판정은 프로필 .env 가 정본이다 — Hermes 가 프로세스 환경으로 틀리게 답해도 따르지 않는다.
    assert openai["status"] == "ready" and openai["setup"] == "keys" and openai["active"] is True
    assert rows["Microsoft Edge TTS"]["setup"] == "none" and rows["Microsoft Edge TTS"]["active"] is False
    assert body["activeProvider"] == "OpenAI TTS"


async def test_설치나_구독이_필요한_행은_CLI_안내로_표시한다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/noah/deskrpg/toolsets/tts/providers")).json()
    rows = _rows(body)
    for name, status in (("Piper", "needs_setup"), ("Nous Subscription", "needs_auth")):
        assert rows[name]["setup"] == "cli" and rows[name]["status"] == status
    assert body["cliCommand"] == "hermes -p noah tools"


async def test_게이트웨이_프로세스의_환경변수는_키_설정으로_보지_않는다(aiohttp_client, fake_api, monkeypatch):
    # 멀티플렉스 게이트웨이 프로세스에는 default 의 .env 가 환경변수로 올라와 있다. 다른 직원 화면에
    # "키 있음" 으로 새면 안 된다.
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", SECRET)
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/noah/deskrpg/toolsets/tts/providers")).json()
    assert _rows(body)["OpenAI TTS"]["envVars"][0]["isSet"] is False
    assert _rows(body)["OpenAI TTS"]["status"] == "needs_keys"


async def test_프로바이더가_없는_툴셋은_빈_목록(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/noah/deskrpg/toolsets/file/providers")).json()
    assert body["hasProviders"] is False and body["providers"] == []


async def test_모르는_툴셋은_404(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/noah/deskrpg/toolsets/ghost/providers")
    assert resp.status == 404
    assert (await resp.json())["error"] == "toolset_not_found"


async def test_키와_함께_고르면_env_와_config_에_쓰고_값은_돌려주지_않는다(aiohttp_client, fake_api, caplog):
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    (home / "config.yaml").write_text("model:\n  default: gpt-6\n", encoding="utf-8")
    (home / ".env").write_text("API_SERVER_KEY=keep-me-000000000000\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    with caplog.at_level(logging.DEBUG):
        resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider",
                                json={"provider": "OpenAI TTS", "env": {"VOICE_TOOLS_OPENAI_KEY": f" {SECRET} "}})
    assert resp.status == 200
    text = await resp.text()
    assert SECRET not in text and SECRET not in caplog.text
    assert await resp.json() == {"provider": "OpenAI TTS", "isSet": {"VOICE_TOOLS_OPENAI_KEY": True}}
    env = home / ".env"
    assert f"VOICE_TOOLS_OPENAI_KEY={SECRET}" in env.read_text(encoding="utf-8")
    assert "API_SERVER_KEY=keep-me-000000000000" in env.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["tts"]["provider"] == "openai" and cfg["model"]["default"] == "gpt-6"
    assert list(home.glob("config.yaml.bak-*")), "기존 config 를 백업하지 않았다"


async def test_키가_필요_없는_프로바이더는_바로_고른다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider", json={"provider": "Microsoft Edge TTS"})
    assert resp.status == 200
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["tts"]["provider"] == "edge"


async def test_이미_있는_키는_다시_넣지_않아도_된다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    (home / ".env").write_text(f"VOICE_TOOLS_OPENAI_KEY={SECRET}\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider",
                            json={"provider": "OpenAI TTS", "env": {"VOICE_TOOLS_OPENAI_KEY": ""}})
    assert resp.status == 200
    assert (home / ".env").read_text(encoding="utf-8").count("VOICE_TOOLS_OPENAI_KEY=") == 1


async def test_필요한_키가_없으면_400_이고_아무것도_쓰지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider", json={"provider": "OpenAI TTS"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "missing_keys"
    assert not (home / "config.yaml").exists()


async def test_그_행의_키가_아닌_이름은_쓰지_않는다(aiohttp_client, fake_api):
    # 이름을 클라이언트가 고르면 임의 환경변수 쓰기(API_SERVER_KEY 교체)가 된다.
    fake_api.create_profile("noah")
    home = fake_api.get_profile_dir("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider",
                            json={"provider": "OpenAI TTS",
                                  "env": {"VOICE_TOOLS_OPENAI_KEY": SECRET, "API_SERVER_KEY": SECRET}})
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_env_key"
    assert not (home / ".env").exists()


async def test_설치나_구독이_필요한_행은_409_와_CLI_안내(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    for name in ("Piper", "Nous Subscription"):
        resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider", json={"provider": name})
        assert resp.status == 409
        body = await resp.json()
        assert body["error"] == "provider_needs_cli"


async def test_모르는_프로바이더는_404(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider", json={"provider": "Ghost"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "provider_not_found"


async def test_키_값_형식이_틀리면_400_이고_값을_되돌려주지_않는다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    bad = "sk-has space-12345"
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider",
                            json={"provider": "OpenAI TTS", "env": {"VOICE_TOOLS_OPENAI_KEY": bad}})
    assert resp.status == 400
    assert bad not in await resp.text()


async def test_심볼이_없는_빌드는_라우트와_capability_가_없다(aiohttp_client, fake_api):
    fake_api.apply_provider_selection = None
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    assert (await client.get("/p/noah/deskrpg/toolsets/tts/providers")).status == 404
    info = await (await client.get("/deskrpg/info")).json()
    assert "profile_tool_providers" not in info["capabilities"]


async def test_capability_와_툴셋_목록의_hasProviders(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    info = await (await client.get("/deskrpg/info")).json()
    assert "profile_tool_providers" in info["capabilities"]
    body = await (await client.get("/p/noah/deskrpg/toolsets")).json()
    flags = {row["name"]: row["hasProviders"] for row in body["toolsets"]}
    assert flags["tts"] is True and flags["file"] is False
