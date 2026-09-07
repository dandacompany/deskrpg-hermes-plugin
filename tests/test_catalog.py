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
    rows = catalog._provider_rows(None)
    assert rows[0]["id"] == "zeta"
    assert rows[0]["authenticated"] is True
    assert [r["id"] for r in rows[1:]] == ["alpha", "beta"]


def test_인증_안_된_프로바이더도_목록에_남는다(monkeypatch):
    # 지우면 사용자가 "왜 내가 쓰는 모델이 없지" 를 알 수 없다. Hermes 의 CLI 피커도
    # 못 쓰는 것을 회색으로 보여주지 지우지 않는다.
    _fake_hermes(monkeypatch, registry={"a": _cfg("a"), "b": _cfg("b")}, auth={"a": {"configured": True}})
    rows = catalog._provider_rows(None)
    assert {r["id"] for r in rows} == {"a", "b"}
    assert [r["authenticated"] for r in rows] == [True, False]


def test_configured_와_logged_in_둘_중_하나면_인증이다(monkeypatch):
    # API 키형은 configured, OAuth 형은 logged_in 이 채워진다 — 한쪽만 보면 절반을 놓친다.
    _fake_hermes(
        monkeypatch,
        registry={"key": _cfg("key"), "oauth": _cfg("oauth"), "none": _cfg("none")},
        auth={"key": {"configured": True}, "oauth": {"logged_in": True}, "none": {}},
    )
    got = {r["id"]: r["authenticated"] for r in catalog._provider_rows(None)}
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
    rows = catalog._provider_rows(None)
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


def test_인증되지_않은_프로바이더의_모델은_받아오지_않는다(monkeypatch):
    # 79개 전부 채우면 응답이 거대해지고 models.dev 왕복이 그만큼 늘어난다.
    calls = []
    _fake_hermes(
        monkeypatch,
        registry={"on": _cfg("on"), "off": _cfg("off")},
        auth={"on": {"logged_in": True}},
    )
    monkeypatch.setattr(catalog, "_models_for", lambda pid: calls.append(pid) or ["m"])
    import asyncio

    handler = catalog.get_handler(None)
    req = types.SimpleNamespace()
    resp = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(handler(req))
    assert calls == ["on"]
    assert resp.status == 200


def test_추론_강도_목록에_출처가_있다():
    # Hermes 가 이 목록을 상수로 export 하지 않아 우리가 적었다 — 늘어나면 갱신해야
    # 하므로, 값이 조용히 비거나 줄지 않게 고정한다.
    assert "medium" in catalog.REASONING_EFFORTS
    assert catalog.REASONING_EFFORTS[0] == "minimal"
    assert len(catalog.REASONING_EFFORTS) >= 4
