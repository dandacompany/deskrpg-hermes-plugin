"""OAuth 디바이스 코드 로그인 — Hermes 대시보드의 함수를 프로필 키 경로로 감싼다.

**세션을 우리가 두지 않는다.** 시작·폴러 스레드·토큰 교환·프로필별 auth.json 저장은 Hermes
(`hermes_cli/web_routers/oauth.py`)가 한다. Codex 워커는 저장 순간 `_profile_scope(session_profile)` 로
세션을 만든 프로필의 홈에 쓴다 — 그래서 시작할 때 넘기는 `profile` 이 곧 저장 위치다.

대시보드 라우트(`start_oauth_login`·`cancel_oauth_session`)는 대시보드 세션 토큰(`_require_token`)을 요구하고
`hermes_cli.web_server` 전체를 끌어오므로 부르지 않는다. 시작은 `_start_device_code_flow`, 폴링은
`poll_oauth_session`(토큰 검사 없음), 취소는 같은 순서(`cancelled=True` 후 제거)를 여기서 한다.

**응답에 토큰·계정 이메일을 싣지 않는다.** 폴링 응답에서 상태·오류·만료만 옮긴다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from .common import RequestError, guarded, run_blocking
from .contract_fields import in_app_device_login
from .cron import resolve_profile_home
from .picker import _home_scope

logger = logging.getLogger(__name__)

_DETAIL_MAX = 200


def _hermes_profile(api, raw_name: str):
    """Hermes 의 `profile` 인자 — default 는 None(`_oauth_profile_name` 규칙).

    `_oauth_profile_name` 은 `current` 도 None(=default)으로 바꾸는데, `current` 는 Hermes 에서 만들 수 있는
    프로필 이름이다. 그대로 넘기면 `current` 프로필이 default 의 세션을 폴링·취소하고, 로그인 토큰이
    default 의 auth.json 에 저장된다. 이름이 그대로 돌아오지 않는 프로필은 거절한다.
    """
    name = api.normalize_profile_name(raw_name)
    if name == "default":
        _require_default_is_process_home(api)
        return api._oauth_profile_name(None)
    if api._oauth_profile_name(name) != name:
        raise RequestError(400, "invalid_profile", "this profile name cannot be used for OAuth login")
    return name


def _require_default_is_process_home(api):
    """Hermes 는 profile=None 을 게이트웨이 **프로세스 홈**으로 푼다(오버라이드 없는 홈).

    `hermes -p noah gateway` 처럼 프로세스 홈이 default 프로필 홈이 아니면, default 로 시작한 로그인의 토큰이
    그 프로세스의 프로필에 저장된다. 두 경로가 같을 때만 None 매핑을 쓴다 — 확인할 수 없으면 거절한다.
    """
    process_home = getattr(api, "get_process_hermes_home", None)
    default_home = resolve_profile_home(api, "default")
    if process_home is None or Path(process_home()).resolve() != Path(default_home).resolve():
        raise RequestError(400, "invalid_profile", "the gateway process home is not the default profile home")


def _http_error(exc):
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    return (status, str(detail)[:_DETAIL_MAX] if detail is not None else None) if status else (None, None)


def _require_device(api, provider_id: str):
    # Codex 만 — 다른 프로바이더는 Hermes 폴러가 취소된 세션의 토큰을 default 에 저장할 수 있다
    # (`contract_fields.IN_APP_DEVICE_LOGIN` 주석).
    if not in_app_device_login(api, provider_id):
        raise RequestError(400, "oauth_flow_unsupported", provider_id)


def start_handler(api):
    """`POST /p/{profile}/deskrpg/oauth/{provider}/start`"""

    @guarded
    async def handler(request):
        resolve_profile_home(api, request.match_info["profile"])  # 404·400 검증
        profile = _hermes_profile(api, request.match_info["profile"])
        provider_id = request.match_info["provider"]
        _require_device(api, provider_id)
        # Hermes 의 시작 라우트와 같은 순서 — 만료 세션(15분)을 먼저 치운다. 대시보드는 다른 프로세스라
        # 게이트웨이 쪽 세션 사전은 여기서 치우지 않으면 쌓이기만 한다.
        gc = getattr(api, "_gc_oauth_sessions", None)
        if gc is not None:
            gc()
        try:
            out = await api._start_device_code_flow(provider_id, profile=profile)
        except Exception as exc:  # noqa: BLE001 — Hermes 의 HTTPException 류
            status, detail = _http_error(exc)
            if status == 504:
                raise RequestError(504, "oauth_start_timeout", detail) from None
            if status == 400:
                # 디바이스 로그인 지원은 위에서 확인했다 — 여기의 400 은 Hermes 가 이 시작을 거절한 것이다
                # (예: Nous "이미 로그인됨"·무료 등급 불가).
                raise RequestError(400, "oauth_start_rejected", detail) from None
            logger.warning("[deskrpg] OAuth start failed: %s — %s", provider_id, type(exc).__name__)
            raise RequestError(502, "oauth_start_failed", detail or type(exc).__name__) from None
        url = str(out.get("verification_url") or "")
        if not url.startswith(("https://", "http://")):
            raise RequestError(502, "oauth_start_failed", "verification url is not http(s)")
        logger.info("[deskrpg] OAuth started: %s (session=%s)", provider_id, str(out.get("session_id"))[:6])
        return web.json_response({
            "sessionId": out["session_id"], "userCode": out["user_code"], "verificationUrl": url,
            "expiresIn": int(out.get("expires_in") or 0), "pollInterval": int(out.get("poll_interval") or 5),
        })

    return handler


def poll_handler(api):
    """`GET /p/{profile}/deskrpg/oauth/{provider}/sessions/{session_id}`"""

    @guarded
    async def handler(request):
        resolve_profile_home(api, request.match_info["profile"])
        profile = _hermes_profile(api, request.match_info["profile"])
        provider_id = request.match_info["provider"]
        try:
            out = await api.poll_oauth_session(provider_id, request.match_info["session_id"], profile=profile)
        except Exception as exc:  # noqa: BLE001
            status, _detail = _http_error(exc)
            if status == 404:
                raise RequestError(404, "oauth_session_not_found") from None
            if status == 400:
                raise RequestError(400, "oauth_session_mismatch") from None
            raise
        error = out.get("error_message")
        return web.json_response({
            "status": out.get("status"),
            "error": str(error)[:_DETAIL_MAX] if error else None,
            "expiresAt": out.get("expires_at"),
            "retryable": out.get("retryable"),
            "retryAfter": out.get("retry_after"),
        })

    return handler


def cancel_handler(api):
    """`DELETE /p/{profile}/deskrpg/oauth/sessions/{session_id}` — Hermes `cancel_oauth_session` 과 같은 순서."""

    @guarded
    async def handler(request):
        resolve_profile_home(api, request.match_info["profile"])
        want = _hermes_profile(api, request.match_info["profile"])
        sid = request.match_info["session_id"]
        with api._oauth_sessions_lock:
            sess = api._oauth_sessions.get(sid)
            if sess is not None:
                if sess.get("profile") != want:
                    raise RequestError(400, "oauth_session_mismatch")
                sess["cancelled"] = True
                api._oauth_sessions.pop(sid, None)
        return web.json_response({"ok": sess is not None})

    return handler


def disconnect_handler(api):
    """`DELETE /p/{profile}/deskrpg/oauth/{provider}` — 그 프로필의 auth.json 에서만 지운다."""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        provider_id = request.match_info["provider"]
        _require_device(api, provider_id)

        def _clear():
            with _home_scope(api, home):
                return bool(api.clear_provider_auth(provider_id))

        cleared = await run_blocking(_clear)
        logger.info("[deskrpg] OAuth disconnected: %s (cleared=%s)", provider_id, cleared)
        return web.json_response({"ok": cleared})

    return handler
