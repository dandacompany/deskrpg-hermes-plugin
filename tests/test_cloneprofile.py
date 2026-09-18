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


def test_referenced_는_Hermes_와_같이_별칭과_대소문자를_풀어_찾는다(fake_api, wide):
    fake_api._plugin_aliases = lambda: {"claude": "anthropic", "hf": "huggingface"}
    _write_cfg(wide, {"model": {"default": "c", "provider": " Claude "},
                      "auxiliary": {"vision": {"provider": "HF"}}})
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert got["envKeys"] == ["ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "HF_TOKEN"]


def test_별칭_심볼이_없는_빌드는_소문자만_맞춘다(fake_api, wide):
    fake_api._plugin_aliases = None
    _write_cfg(wide, {"model": {"default": "c", "provider": "OpenAI"}})
    fake_api.create_profile("noah")
    assert cloneprofile.clone_from_default(fake_api, "noah")["envKeys"] == ["OPENAI_API_KEY", "OPENAI_BASE_URL"]


# F1 — openrouter/custom 은 실제 Hermes PROVIDER_REGISTRY 에 없다(hermes_cli/auth.py:1339).
# `wide` 의 PROVIDER_REGISTRY 에서 openrouter 항목을 지워 그 현실을 재현한다.


def test_referenced_는_레지스트리에_없는_openrouter_키도_명시적으로_복사한다(fake_api, wide):
    del fake_api.PROVIDER_REGISTRY["openrouter"]
    _write_cfg(wide, {"model": {"default": "m", "provider": "openrouter"}})
    (wide / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-222\nOPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n", encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert got["envKeys"] == ["OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"]


def test_referenced_는_openrouter_가_참조되지_않으면_복사하지_않는다(fake_api, wide):
    del fake_api.PROVIDER_REGISTRY["openrouter"]
    _write_cfg(wide, {"model": {"default": "m", "provider": "openai"}})
    (wide / ".env").write_text(
        "OPENAI_API_KEY=sk-openai-111\nOPENROUTER_API_KEY=sk-or-222\n", encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert "OPENROUTER_API_KEY" not in got["envKeys"]


def test_api_keys_모드는_openrouter_가_참조되지_않아도_늘_복사한다(fake_api, wide):
    del fake_api.PROVIDER_REGISTRY["openrouter"]
    _write_cfg(wide, {"model": {"default": "m", "provider": "openai"}})
    (wide / ".env").write_text(
        "OPENAI_API_KEY=sk-openai-111\nOPENROUTER_API_KEY=sk-or-222\nOPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n",
        encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah", key_scope="api_keys")
    assert "OPENROUTER_API_KEY" in got["envKeys"] and "OPENROUTER_BASE_URL" in got["envKeys"]


def test_referenced_는_참조된_이름있는_custom_provider의_key_env를_복사한다(fake_api, wide):
    _write_cfg(wide, {
        "model": {"default": "m", "provider": "my-lmstudio"},
        "custom_providers": [{"name": "my-lmstudio", "base_url": "http://localhost:1234/v1",
                               "key_env": "HERMES_CUSTOM_LMSTUDIO_API_KEY"}],
    })
    (wide / ".env").write_text("HERMES_CUSTOM_LMSTUDIO_API_KEY=sk-custom-333\n", encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert got["envKeys"] == ["HERMES_CUSTOM_LMSTUDIO_API_KEY"]


def test_referenced_는_참조되지_않은_custom_provider의_key_env는_건너뛴다(fake_api, wide):
    _write_cfg(wide, {
        "model": {"default": "m", "provider": "openai"},
        "custom_providers": [{"name": "my-lmstudio", "base_url": "http://localhost:1234/v1",
                               "key_env": "HERMES_CUSTOM_LMSTUDIO_API_KEY"}],
    })
    (wide / ".env").write_text(
        "OPENAI_API_KEY=sk-openai-111\nHERMES_CUSTOM_LMSTUDIO_API_KEY=sk-custom-333\n", encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert "HERMES_CUSTOM_LMSTUDIO_API_KEY" not in got["envKeys"]


def test_api_keys_모드는_custom_provider의_key_env를_참조_없이도_복사한다(fake_api, wide):
    _write_cfg(wide, {
        "model": {"default": "m", "provider": "openai"},
        "custom_providers": [{"name": "my-lmstudio", "base_url": "http://localhost:1234/v1",
                               "key_env": "HERMES_CUSTOM_LMSTUDIO_API_KEY"}],
    })
    (wide / ".env").write_text(
        "OPENAI_API_KEY=sk-openai-111\nHERMES_CUSTOM_LMSTUDIO_API_KEY=sk-custom-333\n", encoding="utf-8")
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah", key_scope="api_keys")
    assert "HERMES_CUSTOM_LMSTUDIO_API_KEY" in got["envKeys"]


def test_referenced_는_key_env_없는_custom_provider는_그냥_건너뛴다(fake_api, wide):
    _write_cfg(wide, {
        "model": {"default": "m", "provider": "my-lmstudio"},
        "custom_providers": [{"name": "my-lmstudio", "base_url": "http://localhost:1234/v1"}],
    })
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert got["envKeys"] == []


# F2 — `_iter_fallback_entries`(hermes_cli/fallback_config.py:63-78) 는 provider·model 둘 다
# 없으면 그 항목을 버린다. cloneprofile 도 model 없는 항목의 provider 키는 복사하면 안 된다.


def test_referenced_는_model_없는_fallback_항목을_Hermes_처럼_건너뛴다(fake_api, wide):
    _write_cfg(wide, {
        "model": {"default": "gpt-x", "provider": "openai"},
        "fallback_providers": [{"provider": "copilot"}],  # model 없음 — Hermes 도 버린다
    })
    fake_api.create_profile("noah")
    got = cloneprofile.clone_from_default(fake_api, "noah")
    assert "COPILOT_GITHUB_TOKEN" not in got["envKeys"]
    assert "GH_TOKEN" not in got["envKeys"] and "GITHUB_TOKEN" not in got["envKeys"]
