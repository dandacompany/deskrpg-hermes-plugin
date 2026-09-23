"""Catalog authentication follows the pinned Hermes profile runtime inheritance policy."""

import base64
import json
import time

import pytest

from deskrpg_plugin import catalog

pytestmark = pytest.mark.integration


def _jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def _write_codex_login(home):
    store = {
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"access_token": _jwt(int(time.time()) + 86400), "refresh_token": "rt-test"},
                "last_refresh": "2026-09-17T00:00:00Z",
                "auth_mode": "chatgpt",
            }
        },
        "credential_pool": {},
    }
    (home / "auth.json").write_text(json.dumps(store), encoding="utf-8")


async def _codex_authenticated(client, profile):
    resp = await client.get(f"/p/{profile}/deskrpg/catalog")
    assert resp.status == 200, await resp.text()
    rows = {r["id"]: r["authenticated"] for r in (await resp.json())["providers"]}
    return rows["openai-codex"]


async def test_default_로그인_표시는_프로필_런타임의_상속_정책을_따른다(client, api, profile, hermes_env, monkeypatch):
    monkeypatch.setattr(catalog, "_models_for", lambda pid: [])  # models.dev 왕복 차단
    _write_codex_login(hermes_env["home"])
    # 최신 Hermes는 루트 OAuth를 프로필 워커에도 상속한다. 피커와 실제 실행 판정이 같아야 한다.
    import inspect
    from hermes_cli.auth import resolve_codex_runtime_credentials, AuthError
    from deskrpg_plugin.cron import cron_scope

    kwargs = {"force_refresh": False, "refresh_if_expiring": False}
    if "read_only" in inspect.signature(resolve_codex_runtime_credentials).parameters:
        kwargs["read_only"] = True
    with cron_scope(api, profile):
        try:
            resolved = resolve_codex_runtime_credentials(**kwargs)
            usable = bool(resolved.get("api_key"))
        except AuthError:
            usable = False
    assert await _codex_authenticated(client, profile) is usable


async def test_프로필에서_로그인하면_인증된다(client, profile, hermes_env, monkeypatch):
    monkeypatch.setattr(catalog, "_models_for", lambda pid: [])
    _write_codex_login(hermes_env["home"] / "profiles" / profile)
    assert await _codex_authenticated(client, profile) is True


def test_실제_Hermes_에_피커_목록_함수가_있다():
    """`cached_provider_model_ids` 가 사라지거나 이름이 바뀌면 조용히 옛 폴백으로 돌아간다.

    그 폴백이 바로 결함이었다 — 인증 방식을 모르는 models.dev 목록. 가짜만으로는
    이름이 틀려도 통과하므로 실제 설치본에서 고정한다.
    """
    from hermes_cli.models import cached_provider_model_ids

    assert callable(cached_provider_model_ids)


def test_모델_목록을_피커_함수에서_가져온다(monkeypatch):
    # 옛 경로(큐레이션 + models.dev)를 타면 이 목록이 아닌 것이 나온다.
    seen = []
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids",
        lambda pid: seen.append(pid) or ["gpt-5.6-sol", "gpt-5.3-codex"],
    )
    assert catalog._models_for("openai-codex") == ["gpt-5.6-sol", "gpt-5.3-codex"]
    assert seen == ["openai-codex"]
