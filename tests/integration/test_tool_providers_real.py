"""실제 Hermes 위의 도구별 프로바이더. 대시보드 도구 설정 라우터와 같은 심볼이 실제로 맞는지 본다."""

import pytest
import yaml

pytestmark = pytest.mark.integration

SECRET = "sk-it-TOOL-SECRET-0123456789abcdef"


async def test_TTS_는_Edge_와_키_행을_주고_키_판정은_프로필_env_로_한다(client, make_profile, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", SECRET)  # 게이트웨이 프로세스 환경 — 다른 직원에게 새면 안 된다
    make_profile("noah")
    resp = await client.get("/p/noah/deskrpg/toolsets/tts/providers")
    assert resp.status == 200
    assert SECRET not in await resp.text()
    body = await resp.json()
    assert body["hasProviders"] is True and body["cliCommand"] == "hermes -p noah tools"
    rows = {r["name"]: r for r in body["providers"]}
    assert rows["Microsoft Edge TTS"]["setup"] == "none"
    eleven = rows["ElevenLabs"]
    assert eleven["setup"] == "keys" and eleven["envVars"][0]["key"] == "ELEVENLABS_API_KEY"
    assert eleven["envVars"][0]["isSet"] is False


async def test_키와_함께_고르면_Hermes_가_그_프로바이더를_활성으로_본다(client, make_profile):
    home = make_profile("noah")
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider",
                            json={"provider": "ElevenLabs", "env": {"ELEVENLABS_API_KEY": SECRET}})
    assert resp.status == 200, await resp.text()
    assert SECRET not in await resp.text()
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["tts"]["provider"] == "elevenlabs"
    body = await (await client.get("/p/noah/deskrpg/toolsets/tts/providers")).json()
    assert body["activeProvider"] == "ElevenLabs"
    assert {r["name"]: r for r in body["providers"]}["ElevenLabs"]["status"] == "ready"


async def test_설치가_필요한_행은_CLI_로_안내하고_고르지_않는다(client, make_profile):
    make_profile("noah")
    body = await (await client.get("/p/noah/deskrpg/toolsets/tts/providers")).json()
    piper = {r["name"]: r for r in body["providers"]}.get("Piper")
    if piper is None or piper["setup"] != "cli":
        pytest.skip("이 환경에는 Piper 가 이미 설치돼 있다")
    resp = await client.put("/p/noah/deskrpg/toolsets/tts/provider", json={"provider": "Piper"})
    assert resp.status == 409


async def test_툴셋_목록이_프로바이더_여부를_알린다(client, make_profile):
    make_profile("noah")
    rows = {r["name"]: r for r in (await (await client.get("/p/noah/deskrpg/toolsets")).json())["toolsets"]}
    assert rows["tts"]["hasProviders"] is True and rows["file"]["hasProviders"] is False
