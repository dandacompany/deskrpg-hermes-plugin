"""API 키 입력 — 쓰기 전용. 키 이름은 서버가 고른다."""

import logging
import os
import stat

from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter

SECRET = "sk-TEST-KEYINPUT-0123456789abcdef"


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


async def test_키를_쓰면_첫_환경변수_이름으로_저장하고_값은_돌려주지_않는다(aiohttp_client, fake_api, caplog):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    with caplog.at_level(logging.DEBUG):
        resp = await client.put("/p/noah/deskrpg/provider-keys/openai", json={"value": f"  {SECRET}  "})
    assert resp.status == 200
    text = await resp.text()
    assert SECRET not in text and SECRET not in caplog.text
    assert await resp.json() == {"configured": True, "envVar": "OPENAI_API_KEY"}
    env = fake_api.get_profile_dir("noah") / ".env"
    assert f"OPENAI_API_KEY={SECRET}\n" in env.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


async def test_기존_줄은_보존하고_같은_키는_교체한다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    env = fake_api.get_profile_dir("noah") / ".env"
    env.write_text("API_SERVER_KEY=keep-me-000000000000\nOPENAI_API_KEY=old\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/noah/deskrpg/provider-keys/openai", json={"value": SECRET})).status == 200
    body = env.read_text(encoding="utf-8")
    assert "API_SERVER_KEY=keep-me-000000000000" in body
    assert body.count("OPENAI_API_KEY=") == 1 and SECRET in body


async def test_지우면_그_프로바이더의_이름을_전부_지운다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    env = fake_api.get_profile_dir("noah") / ".env"
    env.write_text(f"OPENAI_API_KEY={SECRET}\nOPENAI_BASE_URL=https://x\nOTHER=1\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/p/noah/deskrpg/provider-keys/openai")
    assert await resp.json() == {"configured": False, "removed": ["OPENAI_API_KEY"]}
    assert env.read_text(encoding="utf-8") == "OPENAI_BASE_URL=https://x\nOTHER=1\n"


async def test_API_키_형이_아니면_400(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/provider-keys/openai-codex", json={"value": SECRET})
    assert resp.status == 400
    assert (await resp.json())["error"] == "provider_not_api_key"


async def test_모르는_프로바이더는_404(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/provider-keys/ghost", json={"value": SECRET})
    assert resp.status == 404
    assert (await resp.json())["error"] == "provider_not_found"


import pytest


@pytest.mark.parametrize("value", ["short", "x" * 513, "sk-has space-12345", "sk-new\nline-12345",
                                   "sk-quote'12345678", 'sk-dq"12345678', "sk-hash#12345678",
                                   "sk-dollar$12345678", "sk-back\\slash1234", 12345678, None])
async def test_값_형식이_틀리면_400_이고_값을_되돌려주지_않는다(aiohttp_client, fake_api, value):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/provider-keys/openai", json={"value": value})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_key_value"
    if isinstance(value, str):
        assert value not in await resp.text() or len(value) < 8
    assert not (fake_api.get_profile_dir("noah") / ".env").exists()


async def test_레지스트리가_없는_빌드에는_라우트와_capability_가_없다(aiohttp_client, fake_api):
    fake_api.PROVIDER_REGISTRY = None
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/noah/deskrpg/provider-keys/openai", json={"value": SECRET})).status == 404
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert "profile_provider_keys" not in caps
