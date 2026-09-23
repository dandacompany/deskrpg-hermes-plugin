"""프로바이더 API 키 입력 — **쓰기 전용**.

키 이름은 서버가 정한다(`PROVIDER_REGISTRY[id].api_key_env_vars[0]`). 클라이언트가 이름을 고르면 이 라우트가
임의 환경변수 쓰기(예: `API_SERVER_KEY` 교체)가 된다. 값은 어떤 응답·로그·예외에도 싣지 않는다.
Hermes 의 `load_env` 는 파일 서명으로 캐시를 무효화하고, 멀티플렉스 게이트웨이는 턴마다 프로필 `.env` 를
다시 읽으므로(`gateway/run.py` `_load_profile_secret_scope`) 재시작 없이 다음 턴부터 쓰인다.
"""

from __future__ import annotations

import logging
import re

from aiohttp import web

from . import envfile
from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home

logger = logging.getLogger(__name__)

# dotenv 에 따옴표 없이 안전하게 쓸 수 있는 문자만. 따옴표·공백·#·$·백슬래시·백틱·제어문자는 해석이 갈린다.
_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=:@,%!*^()\[\]{}|<>?-]{8,512}$")


def _provider(api, provider_id: str):
    registry = getattr(api, "PROVIDER_REGISTRY", None) or {}
    pcfg = registry.get(provider_id)
    if pcfg is None:
        raise RequestError(404, "provider_not_found", provider_id)
    return pcfg


def api_key_env_names(api, provider_id: str) -> tuple[str, ...]:
    pcfg = _provider(api, provider_id)
    names = tuple(str(n) for n in (getattr(pcfg, "api_key_env_vars", None) or ()) if n)
    if getattr(pcfg, "auth_type", "api_key") != "api_key" or not names:
        raise RequestError(400, "provider_not_api_key", provider_id)
    return names


def _clean_value(raw) -> str:
    if not isinstance(raw, str):
        raise RequestError(400, "invalid_key_value", "value must be a string")
    value = raw.strip()
    if not _VALUE_RE.fullmatch(value):
        raise RequestError(400, "invalid_key_value", "8~512 chars without spaces, quotes, #, $, backslash")
    return value


def put_handler(api):
    """`PUT /p/{profile}/deskrpg/provider-keys/{provider}`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        provider_id = request.match_info["provider"]
        names = api_key_env_names(api, provider_id)
        try:
            payload = await request.json()
        except Exception:
            raise RequestError(400, "invalid_key_value", "body must be JSON") from None
        value = _clean_value(payload.get("value") if isinstance(payload, dict) else None)
        name = names[0]
        try:
            await run_blocking(envfile.upsert_lines, home / ".env", {name: f"{name}={value}"})
        except OSError as exc:
            raise RequestError(500, "env_write_failed", type(exc).__name__) from None
        logger.info("[deskrpg] provider key set: %s -> %s", provider_id, name)
        return web.json_response({"configured": True, "envVar": name})

    return handler


def delete_handler(api):
    """`DELETE /p/{profile}/deskrpg/provider-keys/{provider}`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        provider_id = request.match_info["provider"]
        names = api_key_env_names(api, provider_id)
        try:
            removed = await run_blocking(envfile.remove_keys, home / ".env", names)
        except OSError as exc:
            raise RequestError(500, "env_write_failed", type(exc).__name__) from None
        logger.info("[deskrpg] provider keys removed: %s (%d)", provider_id, len(removed))
        return web.json_response({"configured": False, "removed": removed})

    return handler
