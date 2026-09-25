"""MCP OAuth(0.17.0) — 웹 화면용 **리다이렉트 URL 붙여넣기** 흐름.

Hermes 는 루프백 `client_redirect_uri` 만 받는다(`tools/connectors/mcp_oauth.py` `_validate_client_redirect_uri`).
고정 루프백 주소를 넘기면 게이트웨이 쪽 리스너 없이 redirect 만 고정되고, 사용자가 승인 뒤 브라우저 주소창에 남은
`?code=&state=` 를 DeskRPG 가 받아 여기로 넘긴다. 토큰 저장·커밋은 Hermes 흐름이 한다 — 여기서는 세션이
어느 프로필·서버 것인지만 기억한다(토큰·code 는 담지 않는다).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from aiohttp import web

from . import mcp_state
from .common import RequestError, guarded, read_json_object, require_str, run_blocking
from .cron import resolve_profile_home
from .mcp_admin import require_entry
from .skills_common import actor_of

MCP_OAUTH_REDIRECT_PORT = 8412
REDIRECT_URI = f"http://127.0.0.1:{MCP_OAUTH_REDIRECT_PORT}/callback"
# 붙여넣기를 기다리는 최대 시간보다 넉넉히. 버려진 세션 기록이 쌓이지 않게 한다.
_SESSION_TTL = 30 * 60

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _remember(sid: str, profile: str, name: str, home: Path) -> None:
    now = time.monotonic()
    with _LOCK:
        for old in [k for k, v in _SESSIONS.items() if now - v["at"] > _SESSION_TTL]:
            del _SESSIONS[old]
        _SESSIONS[sid] = {"profile": profile, "name": name, "home": str(Path(home).resolve()), "at": now}


def _forget(sid: str) -> None:
    with _LOCK:
        _SESSIONS.pop(sid, None)


def _session(api, request) -> dict:
    sid = request.match_info["session_id"]
    resolve_profile_home(api, request.match_info["profile"])
    profile = api.normalize_profile_name(request.match_info["profile"])
    with _LOCK:
        sess = _SESSIONS.get(sid)
    if sess is None:
        raise RequestError(404, "oauth_session_not_found", sid)
    if sess["profile"] != profile:
        raise RequestError(400, "oauth_session_mismatch", sid)
    return {"sid": sid, **sess}


def _redacted(api, exc: BaseException) -> str:
    return api.redact_mcp_probe_text(f"{type(exc).__name__}: {exc}")[:300]


def start_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = api.normalize_profile_name(request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        actor = actor_of(request)

        def work():
            if require_entry(home, name).get("auth") != "oauth":
                raise RequestError(400, "oauth_not_configured", name)
            try:
                # Hermes 는 인증 URL 이 나올 때까지(최대 수십 초) 여기서 기다린다 — 워커 스레드라 괜찮다.
                with mcp_state.profile_scope(api, home):
                    attempt = api.mcp_oauth_start(name, client_redirect_uri=REDIRECT_URI)
            except Exception as exc:  # noqa: BLE001 — 공급자 오류는 가려서 사유만
                raise RequestError(502, "oauth_start_failed", _redacted(api, exc)) from None
            mcp_state.audit(home, actor, "oauth_start", name)
            if not attempt.auth_url:
                return {"status": "approved"}  # 저장된 인증이 아직 유효하다
            sid = attempt.flow.flow_id
            _remember(sid, profile, name, home)
            return {"sessionId": sid, "authUrl": attempt.auth_url}

        return web.json_response(await run_blocking(work))

    return handler


def callback_handler(api):
    @guarded
    async def handler(request):
        sess = _session(api, request)
        body = await read_json_object(request)
        code = require_str(body, "code")
        state = require_str(body, "state")
        iss = require_str(body, "iss", required=False, default=None)
        actor = actor_of(request)

        def work():
            out = api.deliver_callback_flow(sess["sid"], sess["name"], code=code, state=state, iss=iss)
            if not (isinstance(out, dict) and out.get("ok")):
                raise RequestError(400, "oauth_callback_invalid", "callback rejected (state mismatch or already used)")
            mcp_state.audit(Path(sess["home"]), actor, "oauth_callback", sess["name"])
            return {"ok": True}

        return web.json_response(await run_blocking(work))

    return handler


def _tool_names(tools) -> list[str]:
    # Hermes `poll_flow` 는 도구를 dict(`{name, description, …}`)로 준다. 이름만 싣는다.
    names = []
    for tool in tools or []:
        name = tool.get("name") if isinstance(tool, dict) else tool
        if isinstance(name, str) and name:
            names.append(name)
    return names


def poll_handler(api):
    @guarded
    async def handler(request):
        sess = _session(api, request)

        def work():
            out = api.poll_flow(sess["sid"], sess["name"]) or {}
            status = out.get("status") if out.get("status") in ("approved", "error") else "pending"
            result = {"status": status}
            if status == "error":
                raw = out.get("error_message") or out.get("error") or ""
                result["error"] = api.redact_mcp_probe_text(str(raw))[:300]
            if status == "approved":
                result["tools"] = _tool_names(out.get("tools"))
            if status != "pending":
                _forget(sess["sid"])
            return result

        return web.json_response(await run_blocking(work))

    return handler


def cancel_handler(api):
    @guarded
    async def handler(request):
        sess = _session(api, request)

        def work():
            out = api.cancel_flow(sess["sid"], sess["name"], sess["home"]) or {}
            _forget(sess["sid"])
            return {"ok": bool(out.get("ok"))}

        return web.json_response(await run_blocking(work))

    return handler
