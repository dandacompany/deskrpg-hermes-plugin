"""NPC MCP 커넥터 관리(0.17.0) — 서버 목록·상세·추가·수정·삭제·enabled·trust·도구 선택·비밀값.

모든 Hermes 호출은 요청 프로필 홈 스코프 안의 워커 스레드에서 한다. 저장은 서버 단위
(`_save_mcp_server`/`_remove_mcp_server`)로만 하고, 저장 전에 Hermes 보안 검사를 직접 불러 사유를 돌려준다.

**읽기는 원본 config.yaml 에서 한다.** Hermes `_get_mcp_servers()` 는 `load_config()` 를 거쳐 `${VAR}` 를
환경값으로 펼친다 — 그 결과로 응답·revision·내보내기를 만들면 비밀값이 섞인다. 쓰기는 Hermes 에 맡긴다
(`save_config` 가 펼쳐진 값을 원래 템플릿으로 되돌려 쓴다).
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path

from aiohttp import web

from . import config as _config
from . import envfile, mcp_state
from .common import RequestError, guarded, read_json_object, require_bool, require_str, require_str_list, run_blocking
from .cron import resolve_profile_home
from .picker import _home_scope
from .skills_common import actor_of

logger = logging.getLogger("deskrpg_plugin")

_STDIO_KEYS = ("command", "args", "cwd", "env")
_AUTH = ("none", "bearer", "oauth", "env")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")


def raw_servers(home: Path) -> dict:
    """프로필 config.yaml 의 `mcp_servers` 를 펼치지 않은 채로 — `${VAR}` 참조가 그대로 남는다."""
    try:
        data = _config._load(Path(home) / _config.CONFIG_FILENAME)
    except _config.ConfigUnreadable as exc:
        # 사유를 싣지 않는다 — YAML 오류 문구는 망가진 줄(값)을 인용한다.
        logger.warning("[deskrpg] mcp config read failed: %s", type(exc.__cause__ or exc).__name__)
        raise RequestError(409, "config_unreadable", "config.yaml cannot be parsed") from None
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def require_entry(home: Path, name: str) -> dict:
    entry = raw_servers(home).get(name)
    if entry is None:
        raise RequestError(404, "server_not_found", name)
    return entry


def server_kind(api, name: str) -> str:
    try:
        return "catalog" if api.mcp_get_catalog_entry(name) is not None else "custom"
    except Exception:  # noqa: BLE001 — 카탈로그 읽기 실패가 목록을 막지 않는다
        return "custom"


def view_of(api, home: Path, name: str, entry: dict, *, detail: bool = False) -> dict:
    """응답 뷰. `_oauth_tokens_present` 가 현재 home 을 보므로 그 프로필 스코프 안에서 만든다."""
    build = mcp_state.server_detail if detail else mcp_state.server_view
    with _home_scope(api, home):
        return build(api, home, name, entry, mcp_state.read_checks(home), kind=server_kind(api, name))


def refresh_tool_flags(home: Path, name: str, entry: dict) -> None:
    """도구 선택이 바뀌면 마지막 확인 결과의 `on` 을 다시 계산한다(목록의 "도구 N/M" 이 바로 맞도록)."""
    check = mcp_state.read_checks(home).get(name)
    if check and isinstance(check.get("tools"), list):
        from .mcp_probe import tool_rows

        check["tools"] = tool_rows(entry, check["tools"])
        mcp_state.write_check(home, name, check)


def save_entry(api, home: Path, name: str, entry: dict) -> None:
    """보안 검사(422) → 서버 단위 저장(실패 500) → 저장된 도구 on 표시 갱신."""
    reasons = api.validate_mcp_server_entry(name, entry)
    if reasons:
        raise RequestError(422, "mcp_security_rejected", "server entry rejected by Hermes security check") \
            .with_extra(reasons=[str(r)[:200] for r in reasons])
    with mcp_state.CONFIG_LOCK, _home_scope(api, home):
        if not api._save_mcp_server(name, entry):
            raise RequestError(500, "mcp_save_failed", "Hermes refused to save the server entry")
    refresh_tool_flags(home, name, entry)


def _header_env_key(name: str, header: str) -> str:
    return "MCP_" + re.sub(r"[^A-Za-z0-9]", "_", f"{name}_{header}").upper()


def _headers_from_body(name: str, raw) -> tuple[dict, dict]:
    """헤더 값은 config 에 평문으로 두지 않는다 — `${VAR}` 참조가 아니면 `.env` 로 옮길 값으로 떼어 낸다.

    돌려주는 것: (config 에 쓸 헤더, `.env` 에 쓸 {키: 값}).
    """
    if not isinstance(raw, dict):
        raise RequestError(400, "invalid_field", "headers must be an object")
    headers: dict = {}
    secrets: dict = {}
    for key, value in raw.items():
        key = str(key)
        if not _HEADER_RE.fullmatch(key) or not isinstance(value, str) or "\n" in value or "\r" in value:
            raise RequestError(400, "invalid_field", "headers")
        if mcp_state.ENV_REF_RE.search(value):
            headers[key] = value
            continue
        env_key = _header_env_key(name, key)
        headers[key] = "${" + env_key + "}"
        if value.strip():
            secrets[env_key] = value.strip()
    return headers, secrets


def _entry_from_body(name: str, body: dict, *, existing: dict | None) -> tuple[dict, dict]:
    """요청 본문 → 저장할 항목과 `.env` 에 쓸 값. 값(비밀)은 항목에 남기지 않는다."""
    entry = copy.deepcopy(existing or {})
    env_values: dict = {}
    transport = require_str(body, "transport", required=existing is None, default=None)
    if transport is None:
        transport = "http" if entry.get("url") else "stdio"
    if transport not in ("http", "stdio"):
        raise RequestError(400, "invalid_field", "transport")
    if transport == "http":
        url = require_str(body, "url", required=existing is None or "url" in body, default=entry.get("url"))
        if not str(url).startswith(("https://", "http://")):
            raise RequestError(400, "invalid_field", "url")
        entry["url"] = url
        for key in _STDIO_KEYS:
            entry.pop(key, None)
        if body.get("headers") is not None:
            headers, env_values = _headers_from_body(name, body["headers"])
            keep = {"Authorization": entry["headers"]["Authorization"]} \
                if "Authorization" in (entry.get("headers") or {}) and "Authorization" not in headers else {}
            entry["headers"] = {**keep, **headers}
            if not entry["headers"]:
                entry.pop("headers")
    else:
        entry["command"] = require_str(body, "command", required=existing is None, default=entry.get("command"))
        entry["args"] = require_str_list(body, "args", required=False, default=entry.get("args") or [])
        cwd = require_str(body, "cwd", required=False, default=entry.get("cwd"))
        if cwd:
            entry["cwd"] = cwd
        env = dict(entry.get("env") or {})
        if body.get("env") is not None:
            if not isinstance(body["env"], dict):
                raise RequestError(400, "invalid_field", "env must be an object")
            # 값은 받지 않는다 — 키만 받아 `${KEY}` 참조로 둔다. 값은 secrets 라우트로 쓴다.
            env = {str(k): "${" + str(k) + "}" for k in body["env"]}
        for key in require_str_list(body, "passthroughEnv", required=False, default=[]):
            env[key] = "${" + key + "}"
        for key in env:
            if not _ENV_KEY_RE.fullmatch(key):
                raise RequestError(400, "invalid_field", "env keys must be environment variable names")
        if env:
            entry["env"] = env
        else:
            entry.pop("env", None)
        entry.pop("url", None)
        entry.pop("headers", None)
        entry.pop("auth", None)
    auth = require_str(body, "auth", required=existing is None, default=None)
    if auth is not None:
        if auth not in _AUTH:
            raise RequestError(400, "invalid_field", "auth")
        if auth == "oauth":
            if transport != "http":
                raise RequestError(400, "invalid_field", "oauth requires http transport")
            entry["auth"] = "oauth"
        else:
            entry.pop("auth", None)
        if auth == "bearer" and transport != "http":
            raise RequestError(400, "invalid_field", "bearer requires http transport")
    trust = body.get("trust")
    if trust is not None:
        if trust not in ("full", "untrusted"):
            raise RequestError(400, "invalid_field", "trust")
        entry["trust"] = trust
    return entry, env_values


def _stdio_changed(old: dict | None, new: dict) -> bool:
    if "command" not in new:
        return False
    if old is None:
        return True
    return any(old.get(k) != new.get(k) for k in _STDIO_KEYS)


def _write_env(home: Path, values: dict) -> None:
    if values:
        envfile.upsert_lines(Path(home) / ".env", {k: f"{k}={v}" for k, v in values.items()})


def list_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])

        def work():
            return [view_of(api, home, n, e) for n, e in sorted(raw_servers(home).items())]

        return web.json_response({"servers": await run_blocking(work)})

    return handler


def detail_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])

        def work():
            return view_of(api, home, name, require_entry(home, name), detail=True)

        return web.json_response(await run_blocking(work))

    return handler


def create_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        name = mcp_state.require_name(body.get("name"))
        actor = actor_of(request)

        def work():
            if name in raw_servers(home):
                raise RequestError(409, "name_taken", name)
            entry, env_values = _entry_from_body(name, body, existing=None)
            if _stdio_changed(None, entry):
                if body.get("confirmName") != name:
                    raise RequestError(400, "confirmation_required", "stdio servers need confirmName == name")
                entry.setdefault("trust", "untrusted")
            if body.get("auth") == "bearer":
                with _home_scope(api, home):
                    entry["headers"] = {**(entry.get("headers") or {}), **api._bearer_auth_headers(name)}
            save_entry(api, home, name, entry)
            _write_env(home, env_values)
            mcp_state.audit(home, actor, "create", name, transport="http" if entry.get("url") else "stdio",
                            command=Path(str(entry.get("command") or "")).name or None)
            return view_of(api, home, name, require_entry(home, name))

        return web.json_response(await run_blocking(work), status=201)

    return handler


def _require_revision(body: dict, entry: dict) -> None:
    if require_str(body, "baseRevision") != mcp_state.revision(entry):
        raise RequestError(409, "revision_conflict", "server entry changed since it was read")


def update_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        body = await read_json_object(request)
        actor = actor_of(request)

        def work():
            old = require_entry(home, name)
            _require_revision(body, old)
            new, env_values = _entry_from_body(name, body, existing=old)
            if _stdio_changed(old, new) and body.get("confirmName") != name:
                raise RequestError(400, "confirmation_required", "stdio command changes need confirmName == name")
            save_entry(api, home, name, new)
            _write_env(home, env_values)
            extra = {"command": Path(str(new.get("command") or "")).name} if _stdio_changed(old, new) else {}
            mcp_state.audit(home, actor, "update", name, **extra)
            return view_of(api, home, name, require_entry(home, name))

        return web.json_response(await run_blocking(work))

    return handler


def delete_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        actor = actor_of(request)

        def work():
            servers = raw_servers(home)
            entry = servers.get(name)
            if entry is None:
                raise RequestError(404, "server_not_found", name)
            with _home_scope(api, home):
                own_key = api._env_key_for_server(name)
            # 이 서버만 쓰던 MCP_* 키만 지운다 — 다른 서버가 같은 키를 참조하면 남긴다.
            shared = {k for n, e in servers.items() if n != name for k in mcp_state.env_refs(e)}
            keys = sorted({k for k in (*mcp_state.env_refs(entry), own_key) if k.startswith("MCP_")} - shared)
            with mcp_state.CONFIG_LOCK, _home_scope(api, home):
                api._remove_mcp_server(name)
            if keys:
                envfile.remove_keys(home / ".env", keys)
            tokens = home / "mcp-tokens"
            if tokens.is_dir():
                for path in tokens.glob(f"{name}.*"):
                    path.unlink(missing_ok=True)
            mcp_state.write_check(home, name, None)
            mcp_state.audit(home, actor, "delete", name)
            return {"ok": True}

        return web.json_response(await run_blocking(work))

    return handler


def _patch(api, action: str, mutate):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        body = await read_json_object(request)
        actor = actor_of(request)

        def work():
            entry = copy.deepcopy(require_entry(home, name))
            mutate(entry, body)
            save_entry(api, home, name, entry)
            mcp_state.audit(home, actor, action, name)
            return view_of(api, home, name, require_entry(home, name))

        return web.json_response(await run_blocking(work))

    return handler


def enabled_handler(api):
    def mutate(entry, body):
        entry["enabled"] = require_bool(body, "enabled")

    return _patch(api, "enabled", mutate)


def trust_handler(api):
    def mutate(entry, body):
        trust = require_str(body, "trust")
        if trust not in ("full", "untrusted"):
            raise RequestError(400, "invalid_field", "trust")
        entry["trust"] = trust

    return _patch(api, "trust", mutate)


def tools_put_handler(api):
    def mutate(entry, body):
        _require_revision(body, entry)
        tools = dict(entry.get("tools") or {})
        tools.pop("include", None)
        tools.pop("exclude", None)
        if "include" in body:
            tools["include"] = require_str_list(body, "include")
        if "exclude" in body:
            tools["exclude"] = require_str_list(body, "exclude")
        if tools:
            entry["tools"] = tools
        else:
            entry.pop("tools", None)

    return _patch(api, "tools", mutate)


def _secret_key(request, entry: dict) -> str:
    key = request.match_info["key"]
    if key not in mcp_state.env_refs(entry):
        raise RequestError(400, "secret_key_not_referenced", key)
    return key


def secret_put_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        body = await read_json_object(request)
        value = require_str(body, "value")
        actor = actor_of(request)

        def work():
            key = _secret_key(request, require_entry(home, name))
            clean = value.strip()
            if clean[:7].lower() == "bearer ":
                clean = clean[7:].strip()
            if not clean or "\n" in clean or "\r" in clean:
                raise RequestError(400, "invalid_field", "value")
            _write_env(home, {key: clean})
            mcp_state.audit(home, actor, "secret_set", name, key=key)
            return {"key": key, "hasValue": True}

        return web.json_response(await run_blocking(work))

    return handler


def secret_delete_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        actor = actor_of(request)

        def work():
            key = _secret_key(request, require_entry(home, name))
            envfile.remove_keys(home / ".env", [key])
            mcp_state.audit(home, actor, "secret_delete", name, key=key)
            return {"key": key, "hasValue": False}

        return web.json_response(await run_blocking(work))

    return handler
