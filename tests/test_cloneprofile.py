import logging

import pytest
import yaml

from deskrpg_plugin import cloneprofile

SECRET = "sk-test-SECRETVALUE-1234567890"
BOT = "123456:BOT-TOKEN-SECRET"


@pytest.fixture
def default_home(fake_api, tmp_path):
    home = tmp_path / "default-home"
    home.mkdir()
    fake_api.get_profile_dir = (lambda orig: lambda name: home if name == "default" else orig(name))(
        fake_api.get_profile_dir
    )
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "gpt-x", "provider": "openai"},
        "reasoning_effort": "high",
        "auxiliary": {"vision": {"model": "v"}},
        "platform_toolsets": {"cli": ["web"]},
        "memory": {"provider": "agentmemory"},
    }), encoding="utf-8")
    (home / ".env").write_text(
        f"OPENAI_API_KEY={SECRET}\nOPENAI_BASE_URL=https://x\nTELEGRAM_BOT_TOKEN={BOT}\nAPI_SERVER_KEY=owner-key-000000000\n",
        encoding="utf-8",
    )
    return home


def test_모델_설정과_모델_키만_복사한다(fake_api, default_home):
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    target = fake_api.get_profile_dir("noah")
    cfg = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert cfg == {"model": {"default": "gpt-x", "provider": "openai"}, "reasoning_effort": "high",
                   "auxiliary": {"vision": {"model": "v"}}}
    env = (target / ".env").read_text(encoding="utf-8")
    assert f"OPENAI_API_KEY={SECRET}" in env and "OPENAI_BASE_URL=https://x" in env
    assert "TELEGRAM" not in env and "API_SERVER_KEY" not in env
    assert got == {"configKeys": ["auxiliary", "model", "reasoning_effort"],
                   "envKeys": ["OPENAI_API_KEY", "OPENAI_BASE_URL"], "needsLogin": []}


def test_기본_프로바이더가_OAuth_면_needsLogin_에_싣는다(fake_api, default_home):
    (default_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "gpt-5", "provider": "openai-codex"}}), encoding="utf-8")
    fake_api.create_profile("noah")
    assert cloneprofile.clone_from_default(fake_api, "noah")["needsLogin"] == ["openai-codex"]


def test_새_프로필의_기존_config_키는_보존한다(fake_api, default_home):
    fake_api.create_profile("noah")
    target = fake_api.get_profile_dir("noah")
    (target / "config.yaml").write_text(yaml.safe_dump({"toolsets": ["hermes-cli"]}), encoding="utf-8")
    cloneprofile.clone_from_default(fake_api, "noah")
    cfg = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["toolsets"] == ["hermes-cli"] and cfg["model"]["default"] == "gpt-x"


def test_값은_반환값과_로그_어디에도_없다(fake_api, default_home, caplog):
    fake_api.create_profile("noah")
    with caplog.at_level(logging.DEBUG):
        got = cloneprofile.clone_from_default(fake_api, "noah")
    assert SECRET not in repr(got) and SECRET not in caplog.text and BOT not in caplog.text


def test_default_config_가_망가졌으면_실패하고_사유에_값이_없다(fake_api, default_home):
    (default_home / "config.yaml").write_text("a: [", encoding="utf-8")
    fake_api.create_profile("noah")
    with pytest.raises(cloneprofile.CloneFailed) as exc:
        cloneprofile.clone_from_default(fake_api, "noah")
    assert SECRET not in exc.value.reason
    assert not (fake_api.get_profile_dir("noah") / ".env").exists() or SECRET not in (
        fake_api.get_profile_dir("noah") / ".env").read_text(encoding="utf-8")
