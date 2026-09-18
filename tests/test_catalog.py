"""카탈로그 — 목록을 우리가 만들지 않고 Hermes 것을 옮긴다."""

import sys
import types

import pytest

from deskrpg_plugin import catalog


def _fake_hermes(monkeypatch, *, registry, auth, curated=None, mdev=None):
    """Hermes 내부 모듈을 흉내 낸다. 실제 Hermes 없이 카탈로그 로직만 검증한다."""
    auth_mod = types.SimpleNamespace(
        PROVIDER_REGISTRY=registry,
        get_auth_status=lambda pid: auth.get(pid, {}),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth_mod)
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.model_catalog",
        types.SimpleNamespace(get_catalog=lambda: {"providers": curated or {}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.model_setup_flows_common",
        types.SimpleNamespace(_models_dev_merged=lambda pid, cur: (mdev or {}).get(pid, list(cur))),
    )


def _cfg(name):
    return types.SimpleNamespace(id=name, name=name.title())


def test_인증된_프로바이더가_먼저_온다(monkeypatch):
    # 79개 중 실제로 쓸 수 있는 것이 몇 개뿐이다 — 그것들을 찾아 스크롤하게 하면 안 된다.
    _fake_hermes(
        monkeypatch,
        registry={"zeta": _cfg("zeta"), "alpha": _cfg("alpha"), "beta": _cfg("beta")},
        auth={"zeta": {"logged_in": True}},
    )
    rows = catalog._provider_rows(None, profile="noah")
    assert rows[0]["id"] == "zeta"
    assert rows[0]["authenticated"] is True
    assert [r["id"] for r in rows[1:]] == ["alpha", "beta"]


def test_인증_안_된_프로바이더도_목록에_남는다(monkeypatch):
    # 지우면 사용자가 "왜 내가 쓰는 모델이 없지" 를 알 수 없다. Hermes 의 CLI 피커도
    # 못 쓰는 것을 회색으로 보여주지 지우지 않는다.
    _fake_hermes(monkeypatch, registry={"a": _cfg("a"), "b": _cfg("b")}, auth={"a": {"configured": True}})
    rows = catalog._provider_rows(None, profile="noah")
    assert {r["id"] for r in rows} == {"a", "b"}
    assert [r["authenticated"] for r in rows] == [True, False]


def test_configured_와_logged_in_둘_중_하나면_인증이다(monkeypatch):
    # API 키형은 configured, OAuth 형은 logged_in 이 채워진다 — 한쪽만 보면 절반을 놓친다.
    _fake_hermes(
        monkeypatch,
        registry={"key": _cfg("key"), "oauth": _cfg("oauth"), "none": _cfg("none")},
        auth={"key": {"configured": True}, "oauth": {"logged_in": True}, "none": {}},
    )
    got = {r["id"]: r["authenticated"] for r in catalog._provider_rows(None, profile="noah")}
    assert got == {"key": True, "oauth": True, "none": False}


def test_한_프로바이더의_상태_조회_실패가_목록을_죽이지_않는다(monkeypatch):
    def boom(pid):
        if pid == "bad":
            raise RuntimeError("auth store corrupt")
        return {"logged_in": True}

    auth_mod = types.SimpleNamespace(
        PROVIDER_REGISTRY={"good": _cfg("good"), "bad": _cfg("bad")},
        get_auth_status=boom,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth_mod)
    rows = catalog._provider_rows(None, profile="noah")
    got = {r["id"]: r["authenticated"] for r in rows}
    # 모르면 "인증 안 됨" 으로 두되 **목록에서 사라지지는 않는다**.
    assert got == {"good": True, "bad": False}


def test_models_dev_가_비면_큐레이션만이라도_준다(monkeypatch):
    _fake_hermes(
        monkeypatch,
        registry={"p": _cfg("p")},
        auth={"p": {"logged_in": True}},
        curated={"p": {"models": [{"id": "curated-1"}, "curated-2"]}},
        mdev={},  # models.dev 없음 → _models_dev_merged 가 큐레이션을 그대로 돌려줌
    )
    assert catalog._models_for("p") == ["curated-1", "curated-2"]


def _request(profile):
    return types.SimpleNamespace(match_info={"profile": profile})


def _run(handler, request):
    import asyncio

    return asyncio.run(handler(request))


def test_인증되지_않은_프로바이더의_모델은_받아오지_않는다(monkeypatch, fake_api):
    # 79개 전부 채우면 응답이 거대해지고 models.dev 왕복이 그만큼 늘어난다.
    calls = []
    _fake_hermes(
        monkeypatch,
        registry={"on": _cfg("on"), "off": _cfg("off")},
        auth={"on": {"logged_in": True}},
    )
    monkeypatch.setattr(catalog, "_models_for", lambda pid: calls.append(pid) or ["m"])
    fake_api.get_profile_dir("noah").mkdir(parents=True)

    resp = _run(catalog.get_handler(fake_api), _request("noah"))
    assert calls == ["on"]
    assert resp.status == 200


def test_인증_상태는_요청한_프로필의_홈에서_읽는다(monkeypatch, fake_api):
    # 게이트웨이 프로세스 하나가 모든 /p/{profile} 요청을 받는다. 홈을 갈아 끼우지 않으면
    # default 의 로그인이 noah 의 것처럼 보이고, 실제 대화는 "No Codex credentials" 로 실패한다
    # (2026-09-17 Hostinger VPS 실측, 0.21.3 이미지 재현).
    import json

    homes = [None]
    seen = []

    def set_override(path):
        homes.append(path)
        return len(homes) - 1

    def reset_override(token):
        del homes[token:]

    def get_auth_status(pid):
        seen.append(homes[-1])
        # default(오버라이드 없음)에만 로그인이 있다.
        return {"logged_in": homes[-1] is None}

    monkeypatch.setattr(fake_api, "set_hermes_home_override", set_override, raising=False)
    monkeypatch.setattr(fake_api, "reset_hermes_home_override", reset_override, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.auth",
        types.SimpleNamespace(PROVIDER_REGISTRY={"openai-codex": _cfg("openai-codex")}, get_auth_status=get_auth_status),
    )
    monkeypatch.setattr(catalog, "_models_for", lambda pid: ["gpt-5.5"])
    noah = fake_api.get_profile_dir("noah")
    noah.mkdir(parents=True)

    resp = _run(catalog.get_handler(fake_api), _request("noah"))
    body = json.loads(resp.body)
    assert body["providers"] == [
        {
            "id": "openai-codex",
            "name": "Openai-Codex",
            "authenticated": False,
            "authType": "oauth_device",
            "envVars": [],
            "cliCommand": None,
        }
    ]
    assert body["models"] == {}
    assert seen == [str(noah)]
    assert homes == [None]  # 블록을 나가면 원상복구된다


def test_없는_프로필의_카탈로그는_404(monkeypatch, fake_api):
    import json

    _fake_hermes(monkeypatch, registry={"p": _cfg("p")}, auth={"p": {"logged_in": True}})
    resp = _run(catalog.get_handler(fake_api), _request("ghost"))
    assert resp.status == 404
    assert json.loads(resp.body)["error"] == "profile_not_found"


def test_추론_강도_목록에_출처가_있다():
    # Hermes 가 이 목록을 상수로 export 하지 않아 우리가 적었다 — 늘어나면 갱신해야
    # 하므로, 값이 조용히 비거나 줄지 않게 고정한다.
    assert "medium" in catalog.REASONING_EFFORTS
    assert catalog.REASONING_EFFORTS[0] == "minimal"
    assert len(catalog.REASONING_EFFORTS) >= 4


def test_프로바이더_행에_인증_방식과_키_이름과_CLI_안내가_실린다(monkeypatch):
    registry = {
        "openai": types.SimpleNamespace(id="openai", name="OpenAI", auth_type="api_key",
                                        api_key_env_vars=("OPENAI_API_KEY",), base_url_env_var="OPENAI_BASE_URL"),
        "openai-codex": types.SimpleNamespace(id="openai-codex", name="Codex", auth_type="oauth_external",
                                              api_key_env_vars=(), base_url_env_var=""),
        "qwen-oauth": types.SimpleNamespace(id="qwen-oauth", name="Qwen", auth_type="oauth_external",
                                            api_key_env_vars=(), base_url_env_var=""),
        "bedrock": types.SimpleNamespace(id="bedrock", name="Bedrock", auth_type="aws_sdk",
                                         api_key_env_vars=(), base_url_env_var=""),
    }
    _fake_hermes(monkeypatch, registry=registry, auth={})
    api = types.SimpleNamespace(
        PROVIDER_REGISTRY=registry,
        _DEVICE_CODE_STARTERS={"openai-codex": object()},
        _OAUTH_PROVIDER_CATALOG=(
            {"id": "openai-codex", "flow": "device_code", "cli_command": "hermes auth add openai-codex"},
            {"id": "qwen-oauth", "flow": "external", "cli_command": "hermes auth add qwen-oauth"},
        ),
    )
    rows = {r["id"]: r for r in catalog._provider_rows(api, profile="noah")}
    assert rows["openai"]["authType"] == "api_key" and rows["openai"]["envVars"] == ["OPENAI_API_KEY"]
    assert rows["openai"]["cliCommand"] is None
    assert rows["openai-codex"]["authType"] == "oauth_device" and rows["openai-codex"]["envVars"] == []
    assert rows["qwen-oauth"]["authType"] == "external"
    assert rows["qwen-oauth"]["cliCommand"] == "hermes -p noah auth add qwen-oauth"
    assert rows["bedrock"]["authType"] == "external" and rows["bedrock"]["cliCommand"] is None


def test_디바이스_로그인_심볼이_없는_빌드는_OAuth_형을_external_로_둔다(monkeypatch):
    registry = {"openai-codex": types.SimpleNamespace(id="openai-codex", name="Codex", auth_type="oauth_external",
                                                      api_key_env_vars=(), base_url_env_var="")}
    _fake_hermes(monkeypatch, registry=registry, auth={})
    api = types.SimpleNamespace(PROVIDER_REGISTRY=registry, _DEVICE_CODE_STARTERS=None, _OAUTH_PROVIDER_CATALOG=None)
    row = catalog._provider_rows(api, profile="noah")[0]
    assert row["authType"] == "external" and row["cliCommand"] is None


def test_default_프로필이면_CLI_안내에_p_옵션을_붙이지_않는다(monkeypatch):
    registry = {"qwen-oauth": types.SimpleNamespace(id="qwen-oauth", name="Qwen", auth_type="oauth_external",
                                                    api_key_env_vars=(), base_url_env_var="")}
    _fake_hermes(monkeypatch, registry=registry, auth={})
    api = types.SimpleNamespace(PROVIDER_REGISTRY=registry, _DEVICE_CODE_STARTERS={},
                                _OAUTH_PROVIDER_CATALOG=({"id": "qwen-oauth", "cli_command": "hermes auth add qwen-oauth"},))
    assert catalog._provider_rows(api, profile="default")[0]["cliCommand"] == "hermes auth add qwen-oauth"
