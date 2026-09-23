"""도구별 프로바이더 — `hermes tools` 의 프로바이더 선택·API 키 입력을 프로필 단위로.

**행을 우리가 만들지 않는다.** Hermes 대시보드의 도구 설정 라우터(`hermes_cli/web_routers/tools.py` —
GET `/api/tools/toolsets/{name}/config`, PUT `…/provider`, PUT `…/env`)가 쓰는 함수를 그대로 부른다:
`TOOL_CATEGORIES`·`_visible_providers`·`provider_readiness_status`·`_is_provider_active`·
`apply_provider_selection`. 그 라우터는 대시보드 세션 토큰에 묶여 있어 게이트웨이로 부를 수 없다.

두 가지는 대시보드와 다르게 한다.

1. **키 설정 여부는 프로필 `.env` 로 판정한다.** 게이트웨이 프로세스 하나가 모든 `/p/{profile}` 요청을
   받고, 그 프로세스 환경에는 default 의 키가 올라와 있다. Hermes 의 `get_env_value` 로 물으면 다른 직원
   화면에 "키 있음" 이 샌다. default 프로필만 프로세스 환경도 본다(그 키가 곧 default 의 키다).
2. **설치나 구독 로그인이 필요한 행은 앱에서 적용하지 않는다.** post_setup(Piper·KittenTTS·로컬 Whisper …)
   은 서버에 패키지를 설치하고, Nous 구독 행은 Portal 로그인이 필요하다. 고르기만 하면 설정만 바뀌고 도구는
   동작하지 않는다 — `setup: "cli"` 로 표시하고 PUT 은 409 로 거절한다.

키 값은 쓰기 전용이다. 어떤 응답·로그·예외에도 싣지 않는다.
"""

from __future__ import annotations

import logging
import os
import time

import yaml
from aiohttp import web

from . import config as _config
from . import envfile
from . import picker
from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home
from .provider_keys import _clean_value

logger = logging.getLogger(__name__)

# Hermes 가 "설정만으로는 못 쓴다" 고 판정한 상태. 앱이 대신 해 줄 수 없는 일(설치·Portal 로그인)이다.
_CLI_STATUSES = frozenset({"needs_setup", "needs_auth"})


def _profile_name(api, raw) -> str:
    return api.normalize_profile_name(raw)


def cli_command(profile: str) -> str:
    return "hermes tools" if profile == "default" else f"hermes -p {profile} tools"


def _env_is_set(assignments: dict, profile: str, key: str) -> bool:
    line = assignments.get(key)
    if line is not None and line.split("=", 1)[1].strip().strip("'\"") != "":
        return True
    return profile == "default" and bool(os.environ.get(key))


def _category(api, toolset: str):
    """`(cat | None)` — 설정 가능한 툴셋이 아니면 404."""
    known = {row[0] for row in api._get_effective_configurable_toolsets()}
    if toolset not in known:
        raise RequestError(404, "toolset_not_found", toolset)
    return api.TOOL_CATEGORIES.get(toolset)


def _provider_rows(api, home, profile: str, toolset: str) -> tuple[list[dict], dict | None]:
    cfg = picker._load_config(home)
    with picker._home_scope(api, home):
        cat = _category(api, toolset)
        if cat is None:
            return [], None
        features = picker._features_kwargs(api, cfg)
        assignments = envfile.read_assignments(home / ".env")
        rows = []
        for prov in api._visible_providers(cat, cfg, force_fresh=True):
            env_vars = [
                {
                    "key": str(e["key"]),
                    "prompt": str(e.get("prompt") or e["key"]),
                    "url": e.get("url"),
                    "isSet": _env_is_set(assignments, profile, str(e["key"])),
                }
                for e in prov.get("env_vars", []) or []
            ]
            try:
                active = bool(api._is_provider_active(prov, cfg, force_fresh=True))
            except Exception as exc:  # noqa: BLE001 — 한 행의 판정 실패가 목록을 죽이지 않는다
                logger.warning("[deskrpg] provider active check failed: %s", type(exc).__name__)
                active = False
            # 키 행이 아닌 부분(설치·구독)은 Hermes 판정을 따른다. 키가 있는 행에 설치 훅이 붙어 있으면
            # 키 판정과 섞이지 않게 키 없는 사본으로 묻는다.
            try:
                hermes_status = api.provider_readiness_status(
                    {**prov, "env_vars": []}, cfg, is_active=active, **features
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[deskrpg] provider readiness check failed: %s", type(exc).__name__)
                hermes_status = "ready"
            if hermes_status in _CLI_STATUSES:
                status, setup = hermes_status, "cli"
            elif env_vars:
                status = "ready" if all(e["isSet"] for e in env_vars) else "needs_keys"
                setup = "keys"
            else:
                status, setup = "ready", "none"
            rows.append(
                {
                    "name": str(prov["name"]),
                    "badge": prov.get("badge", "") or "",
                    "tag": prov.get("tag", "") or "",
                    "envVars": env_vars,
                    "active": active,
                    "status": status,
                    "setup": setup,
                }
            )
        return rows, cat


def providers_handler(api):
    """`GET /p/{profile}/deskrpg/toolsets/{toolset}/providers`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = _profile_name(api, request.match_info["profile"])
        toolset = request.match_info["toolset"]
        rows, cat = await run_blocking(_provider_rows, api, home, profile, toolset)
        active = next((row["name"] for row in rows if row["active"]), None)
        return web.json_response(
            {
                "toolset": toolset,
                "hasProviders": cat is not None,
                "providers": rows,
                "activeProvider": active,
                "cliCommand": cli_command(profile),
            }
        )

    return handler


def _write_config(api, home, toolset: str, provider_name: str) -> None:
    path = home / _config.CONFIG_FILENAME
    data = picker._load_config(home)
    if path.is_file():
        # 같은 초에 두 번 써도 백업이 서로 덮지 않도록 마이크로초까지(config.py 와 같다).
        backup = path.with_name(
            f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
        )
        envfile.write_text_atomic(backup, path.read_text(encoding="utf-8"))
    with picker._home_scope(api, home):
        try:
            api.apply_provider_selection(toolset, provider_name, data)
        except KeyError:
            raise RequestError(404, "provider_not_found", provider_name) from None
    # 키가 인라인으로 들어갈 수 있는 파일이라 0600·원자적으로 쓴다.
    envfile.write_text_atomic(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def _select(api, home, profile: str, toolset: str, provider_name: str, env: dict) -> dict:
    rows, cat = _provider_rows(api, home, profile, toolset)
    if cat is None:
        raise RequestError(400, "toolset_has_no_providers", toolset)
    row = next((r for r in rows if r["name"] == provider_name), None)
    if row is None:
        raise RequestError(404, "provider_not_found", provider_name)
    if row["setup"] == "cli":
        raise RequestError(409, "provider_needs_cli", cli_command(profile))

    allowed = {e["key"] for e in row["envVars"]}
    unknown = sorted(k for k in env if k not in allowed)
    if unknown:
        raise RequestError(400, "unknown_env_key", ", ".join(unknown))
    # 빈 값은 "그대로 둔다"(대시보드 PUT env 와 같다). 그 밖의 값은 형식을 검사한다 — 문자열이 아니면 400.
    values = {
        k: _clean_value(v) for k, v in env.items() if not (v is None or (isinstance(v, str) and not v.strip()))
    }
    missing = sorted(e["key"] for e in row["envVars"] if not e["isSet"] and e["key"] not in values)
    if missing:
        raise RequestError(400, "missing_keys", ", ".join(missing))

    if values:
        envfile.upsert_lines(home / ".env", {k: f"{k}={v}" for k, v in values.items()})
    _write_config(api, home, toolset, provider_name)
    assignments = envfile.read_assignments(home / ".env")
    return {k: _env_is_set(assignments, profile, k) for k in sorted(allowed)}


def select_handler(api):
    """`PUT /p/{profile}/deskrpg/toolsets/{toolset}/provider` — `{provider, env?: {KEY: value}}`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = _profile_name(api, request.match_info["profile"])
        toolset = request.match_info["toolset"]
        try:
            payload = await request.json()
        except Exception:
            raise RequestError(400, "bad_request", "body must be JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("provider"), str):
            raise RequestError(400, "bad_request", "provider is required")
        env = payload.get("env") or {}
        if not isinstance(env, dict):
            raise RequestError(400, "bad_request", "env must be an object")
        try:
            is_set = await run_blocking(_select, api, home, profile, toolset, payload["provider"], env)
        except OSError as exc:
            raise RequestError(500, "config_write_failed", type(exc).__name__) from None
        logger.info("[deskrpg] tool provider selected: %s -> %s", toolset, payload["provider"])
        return web.json_response({"provider": payload["provider"], "isSet": is_set})

    return handler
