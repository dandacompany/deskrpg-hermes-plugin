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
                   "envKeys": ["OPENAI_API_KEY", "OPENAI_BASE_URL"], "needsLogin": [], "keyScope": "referenced"}


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


def test_복제한_config_는_0600_으로_원자적으로_쓴다(fake_api, default_home):
    import os
    import stat

    fake_api.create_profile("noah")
    target = fake_api.get_profile_dir("noah")
    (target / "config.yaml").write_text("toolsets: [hermes-cli]\n", encoding="utf-8")
    os.chmod(target / "config.yaml", 0o644)
    cloneprofile.clone_from_default(fake_api, "noah")
    assert stat.S_IMODE(os.stat(target / "config.yaml").st_mode) == 0o600
    assert not [p.name for p in target.iterdir() if p.name.startswith(".config.yaml.")]


def _prov(auth_type, *names, base=""):
    import types
    return types.SimpleNamespace(auth_type=auth_type, api_key_env_vars=names, base_url_env_var=base)


ALL_ENV = {
    "OPENAI_API_KEY": "sk-openai-111", "OPENAI_BASE_URL": "https://o",
    "OPENROUTER_API_KEY": "sk-or-222",
    "ANTHROPIC_API_KEY": "sk-ant-333", "ANTHROPIC_TOKEN": "ant-tok-444", "CLAUDE_CODE_OAUTH_TOKEN": "cc-oauth-555",
    "COPILOT_GITHUB_TOKEN": "gho-666", "GH_TOKEN": "gh-777", "GITHUB_TOKEN": "ghp-888",
    "HF_TOKEN": "hf-999", "NOUS_API_KEY": "nous-000", "TELEGRAM_BOT_TOKEN": "bot-aaa",
}


@pytest.fixture
def wide(fake_api, default_home):
    fake_api.PROVIDER_REGISTRY = {
        "openai": _prov("api_key", "OPENAI_API_KEY", base="OPENAI_BASE_URL"),
        "openrouter": _prov("api_key", "OPENROUTER_API_KEY"),
        "anthropic": _prov("api_key", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        "copilot": _prov("api_key", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        "huggingface": _prov("api_key", "HF_TOKEN"),
        "nous": _prov("oauth_device_code", "NOUS_API_KEY"),
    }
    (default_home / ".env").write_text("".join(f"{k}={v}\n" for k, v in ALL_ENV.items()), encoding="utf-8")
    return default_home


def _write_cfg(home, cfg):
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


REFERENCING = {
    "model": {"default": "gpt-x", "provider": "openai"},
    "fallback_providers": [{"provider": "copilot", "model": "gpt-4o"}, {"provider": "ghost", "model": "x"}, "junk"],
    "auxiliary": {"vision": {"provider": "huggingface"}, "compression": {"provider": "auto"}, "free_only": False},
}


def test_referenced_는_설정이_가리키는_프로바이더의_키만_복사한다(fake_api, wide):
    _write_cfg(wide, REFERENCING)
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert got["keyScope"] == "referenced"
    assert got["envKeys"] == ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN",
                              "OPENAI_API_KEY", "OPENAI_BASE_URL"]


def test_api_keys_는_api_key_프로바이더_전부_토큰류는_빼고_참조분은_더한다(fake_api, wide):
    _write_cfg(wide, REFERENCING)
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah", key_scope="api_keys")
    assert got["keyScope"] == "api_keys"
    assert got["envKeys"] == ["ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN",
                              "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY"]


def test_api_keys_는_참조되지_않은_토큰류_이름을_복사하지_않는다(fake_api, wide):
    _write_cfg(wide, {"model": {"default": "gpt-x", "provider": "openai"}})
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah", key_scope="api_keys")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "NOUS_API_KEY", "TELEGRAM_BOT_TOKEN"):
        assert name not in got["envKeys"]
    assert "COPILOT_GITHUB_TOKEN" in got["envKeys"] and "OPENROUTER_API_KEY" in got["envKeys"]
    env = (fake_api.get_profile_dir("noah") / ".env").read_text(encoding="utf-8")
    assert "ghp-888" not in env and "nous-000" not in env


def test_모르는_key_scope_는_거절한다(fake_api, wide):
    fake_api.create_profile("noah")
    with pytest.raises(ValueError):
        cloneprofile.clone_from_default(fake_api, "noah", key_scope="all")
