"""MCP 카탈로그·재적재·내보내기(0.17.0).

카탈로그 설치는 Hermes `install_entry` 가 한다. 대시보드 `/api/mcp/catalog/install` 과 같은 순서로 — 비밀 값은
먼저 프로필 `.env` 에 쓰고(`install_entry` 는 미리 받은 비밀값을 저장하지 않는다), 모든 값을 `preloaded_env` 로
넘겨 대화형 입력으로 떨어지지 않게 한다. 재적재는 게이트웨이 `/reload-mcp` 의 프로필 한정 단계를 그대로 밟는다.
"""

from __future__ import annotations

import copy
import re

from aiohttp import web

from . import envfile, mcp_state
from .common import RequestError, guarded, read_json_object, require_bool, run_blocking
from .cron import resolve_profile_home
from .mcp_admin import raw_servers, require_entry, view_of
from .mcp_probe import interactive_oauth_suppressed
from .skills_common import actor_of


def _env_specs(entry) -> list:
    return list(getattr(getattr(entry, "auth", None), "env", None) or [])


def _row(entry, installed: set) -> dict:
    transport = getattr(getattr(entry, "transport", None), "type", "stdio")
    return {
        "name": entry.name,
        "description": str(getattr(entry, "description", "") or ""),
        "transport": "http" if transport == "http" else "stdio",
        "installed": entry.name in installed,
        "requiredEnv": [{"name": s.name, "prompt": str(getattr(s, "prompt", "") or ""),
                         "required": bool(getattr(s, "required", False)), "secret": bool(getattr(s, "secret", False))}
                        for s in _env_specs(entry)],
    }


def _redacted(api, exc: BaseException) -> str:
    return api.redact_mcp_probe_text(f"{type(exc).__name__}: {exc}")[:300]


def catalog_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])

        def work():
            installed = set(raw_servers(home))
            with mcp_state.profile_scope(api, home):
                return {"entries": [_row(e, installed) for e in api.mcp_list_catalog()]}

        return web.json_response(await run_blocking(work))

    return handler


def install_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["entry"])
        body = await read_json_object(request)
        env = body.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(v, str) and "\n" not in v for v in env.values()):
            raise RequestError(400, "invalid_field", "env")
        enable = require_bool(body, "enable", required=False, default=True)
        actor = actor_of(request)

        def work():
            with mcp_state.profile_scope(api, home):
                entry = api.mcp_get_catalog_entry(name)
            if entry is None:
                raise RequestError(404, "catalog_entry_not_found", name)
            if name in raw_servers(home):
                raise RequestError(409, "name_taken", name)
            specs = {s.name: s for s in _env_specs(entry)}
            unknown = sorted(set(env) - set(specs))
            if unknown:
                raise RequestError(400, "unknown_env_key", ", ".join(unknown))
            values = {k: v.strip() for k, v in env.items() if v.strip()}
            missing = sorted(k for k, s in specs.items() if getattr(s, "required", False) and k not in values)
            if missing:
                raise RequestError(400, "missing_env", ", ".join(missing))
            secrets = {k: v for k, v in values.items() if getattr(specs[k], "secret", True)}
            try:
                with mcp_state.CONFIG_LOCK:
                    if secrets:
                        envfile.upsert_lines(home / ".env", {k: f"{k}={v}" for k, v in secrets.items()})
                    with mcp_state.profile_scope(api, home), interactive_oauth_suppressed():
                        api.mcp_install_catalog_entry(entry, enable=enable, preloaded_env=values)
            except Exception as exc:  # noqa: BLE001
                raise RequestError(502, "catalog_install_failed", _redacted(api, exc)) from None
            mcp_state.audit(home, actor, "catalog_install", name)
            return view_of(api, home, name, require_entry(home, name))

        return web.json_response(await run_blocking(work), status=201)

    return handler


def gateway_runner(request):
    """게이트웨이 러너(캐시된 에이전트를 가진 쪽). aiohttp `AppRunner`(`adapter._runner`)와 헷갈리지 않는다."""
    from . import routes as _routes

    runner = request.app.get("gateway_runner") if hasattr(request.app, "get") else None
    return runner or getattr(getattr(_routes, "ADAPTER", None), "gateway_runner", None)


def reload_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = api.normalize_profile_name(request.match_info["profile"])
        actor = actor_of(request)
        runner = gateway_runner(request)

        def work():
            try:
                with mcp_state.profile_scope(api, home):
                    scope = api.mcp_registry.current_scope_key()
                    api.shutdown_mcp_servers(scope=scope)
                    api.reprobe_tool_availability()
                    with interactive_oauth_suppressed():
                        api.discover_mcp_tools()
                    refreshed = _refresh_cached_agents(runner, profile)
            except Exception as exc:  # noqa: BLE001
                raise RequestError(502, "reload_failed", _redacted(api, exc)) from None
            mcp_state.audit(home, actor, "reload", "*")
            return {"reloaded": True, "servers": sorted(raw_servers(home)), "agentsRefreshed": refreshed}

        return web.json_response(await run_blocking(work))

    return handler


def _refresh_cached_agents(runner, profile: str) -> bool:
    """게이트웨이가 캐시한 이 프로필의 에이전트가 새 도구를 보게 한다 — 러너에 닿을 때만.

    `/reload-mcp` 와 같은 호출이다. 다중 프로필 게이트웨이에서만 프로필 이름으로 거른다(그 판정은 러너 설정).
    """
    refresh = getattr(runner, "_mcp_reload_refresh_cached_agents", None)
    if refresh is None:
        return False
    multiplex = bool(getattr(getattr(runner, "config", None), "multiplex_profiles", False))
    try:
        refresh(multiplex, profile)
        return True
    except Exception:  # noqa: BLE001 — 새로고침 실패는 다음 세션부터 반영되는 것으로 남긴다
        return False


def _ref_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", key).upper()


def export_entry(entry: dict) -> tuple[dict, list[str], bool]:
    """복사용 항목 — `${KEY}` 참조만 남기고 평문 값·URL 쿼리를 뺀다. (항목, 비밀 키 목록, 쿼리를 뺐는지)."""
    out = copy.deepcopy(entry)
    for section in ("env", "headers"):
        values = out.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in list(values.items()):
            text = str(value)
            prefix = text[:7] if text[:7].lower() == "bearer " else ""
            if not mcp_state.ENV_REF_RE.fullmatch(text[len(prefix):].strip()):
                # config 에 평문이 든 비표준 항목 — 값을 옮기지 않고 참조로 바꾼다.
                values[key] = prefix + "${" + _ref_key(key) + "}"
    dropped = False
    if out.get("url") and "?" in str(out["url"]):
        out["url"] = str(out["url"]).split("?", 1)[0]
        dropped = True
    return out, mcp_state.env_refs(out), dropped


def export_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])

        def work():
            entry, secret_keys, dropped = export_entry(require_entry(home, name))
            body = {"name": name, "entry": entry, "secretKeys": secret_keys, "oauth": entry.get("auth") == "oauth"}
            if dropped:
                body["urlQueryDropped"] = True
            return body

        return web.json_response(await run_blocking(work))

    return handler
