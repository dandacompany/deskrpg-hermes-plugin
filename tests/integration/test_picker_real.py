"""실제 Hermes 위의 피커·복제. 가짜 api 로는 "심볼 이름이 맞는가·프로필 홈이 실제로 갈리는가" 를 못 잡는다."""

import pytest
import yaml

pytestmark = pytest.mark.integration


async def test_툴셋_목록이_비지_않고_모양이_맞다(client, make_profile):
    make_profile("noah")
    body = await (await client.get("/p/noah/deskrpg/toolsets")).json()
    assert body["platform"] == "api_server"
    names = {r["name"] for r in body["toolsets"]}
    assert {"web", "file", "terminal"} <= names
    assert "discord" not in names
    for row in body["toolsets"]:
        assert set(row) == {"name", "label", "description", "enabled", "configured"}


async def test_저장한_툴셋이_Hermes_의_판정에_반영된다(client, make_profile):
    make_profile("noah")
    assert (await client.put("/p/noah/deskrpg/config", json={"enabledToolsets": ["file", "web"]})).status == 200
    rows = {r["name"]: r for r in (await (await client.get("/p/noah/deskrpg/toolsets")).json())["toolsets"]}
    assert rows["web"]["enabled"] and rows["file"]["enabled"]
    assert not rows["terminal"]["enabled"]


async def test_스킬_목록은_프로필의_스킬_폴더에서_온다(client, make_profile):
    home = make_profile("noah")
    skill = home / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: 탐침\n---\n본문\n", encoding="utf-8")
    make_profile("mia")
    noah = {r["name"] for r in (await (await client.get("/p/noah/deskrpg/skills")).json())["skills"]}
    mia = {r["name"] for r in (await (await client.get("/p/mia/deskrpg/skills")).json())["skills"]}
    assert "probe-skill" in noah and "probe-skill" not in mia


async def test_끈_스킬이_Hermes_의_꺼짐_목록에_들어간다(client, make_profile):
    home = make_profile("noah")
    skill = home / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: 탐침\n---\n본문\n", encoding="utf-8")
    assert (await client.put("/p/noah/deskrpg/config", json={"disabledSkills": ["probe-skill"]})).status == 200
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["skills"]["disabled"] == ["probe-skill"]
    rows = {r["name"]: r for r in (await (await client.get("/p/noah/deskrpg/skills")).json())["skills"]}
    assert rows["probe-skill"]["disabled"] is True


async def test_복제된_프로필이_default_의_모델과_키_이름을_물려받는다(client, hermes_home):
    secret = "sk-it-SECRET-0123456789abcdef"
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "gpt-x", "provider": "openai"}}), encoding="utf-8")
    (hermes_home / ".env").write_text(f"OPENAI_API_KEY={secret}\nTELEGRAM_BOT_TOKEN=bot-secret\n", encoding="utf-8")
    resp = await client.post("/deskrpg/profiles", json={"name": "cloned", "cloneFrom": "default"})
    assert resp.status == 201
    text = await resp.text()
    assert secret not in text and "bot-secret" not in text
    body = await resp.json()
    assert body["cloned"]["envKeys"] == ["OPENAI_API_KEY"]
    got = await (await client.get("/p/cloned/deskrpg/config")).json()
    assert got["model"] == "gpt-x" and got["provider"] == "openai"


async def test_저장은_MCP_항목을_남기고_disabled_toolsets_의_막힌_툴셋을_푼다(client, make_profile):
    home = make_profile("noah")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "platform_toolsets": {"api_server": ["terminal", "my-mcp"]},
        "agent": {"disabled_toolsets": ["web", "memory"]},
    }), encoding="utf-8")
    assert (await client.put("/p/noah/deskrpg/config", json={"enabledToolsets": ["file", "web"]})).status == 200
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["platform_toolsets"]["api_server"] == ["file", "my-mcp", "web"]
    assert cfg["agent"]["disabled_toolsets"] == ["memory"]
    assert "web" in cfg["known_builtin_toolsets"]["api_server"]
    rows = {r["name"]: r for r in (await (await client.get("/p/noah/deskrpg/toolsets")).json())["toolsets"]}
    assert rows["web"]["enabled"] and rows["file"]["enabled"] and not rows["terminal"]["enabled"]
