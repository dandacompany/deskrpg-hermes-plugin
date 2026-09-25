"""MCP OAuth(0.17.0) — 웹 화면용 **리다이렉트 URL 붙여넣기** 흐름.

Hermes 는 루프백 `client_redirect_uri` 만 받는다(`tools/connectors/mcp_oauth.py` `_validate_client_redirect_uri`).
고정 루프백 주소를 넘기면 게이트웨이 쪽 리스너 없이 redirect 만 고정되고, 사용자가 승인 뒤 브라우저 주소창에 남은
`?code=&state=` 를 DeskRPG 가 받아 여기로 넘긴다. 토큰 저장·커밋은 Hermes 흐름이 한다 — 여기서는 세션이
어느 프로필·서버 것인지와 그 flow 객체(워커가 끝났는지 보려고)만 기억한다(토큰·code 는 담지 않는다).

**같은 서버의 시도를 겹치지 않는다(0.17.1).** Hermes `start()` 는 새 시도가 옛 시도를 취소하게 두지만, 새 시도가
먼저 끝나 `_ACTIVE` 에서 빠진 뒤 옛 워커가 끝나면 옛 워커의 롤백(`probe_with_rollback.undo`)이 시작 전 토큰
스냅샷을 되돌려 방금 받은 인증을 지운다(스테이징 실측). 그래서 열린 시도가 있으면 409 로 막고, 다시 시작은
옛 시도를 취소한 뒤 그 워커가 끝난 것을 확인하고서야 한다.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from aiohttp import web

from . import mcp_state
from .common import RequestError, guarded, read_json_object, require_bool, require_str, run_blocking
from .cron import resolve_profile_home
from .mcp_admin import require_entry
from .skills_common import actor_of

MCP_OAUTH_REDIRECT_PORT = 8412
REDIRECT_URI = f"http://127.0.0.1:{MCP_OAUTH_REDIRECT_PORT}/callback"
# 붙여넣기를 기다리는 최대 시간보다 넉넉히. 버려진 세션 기록이 쌓이지 않게 한다.
_SESSION_TTL = 30 * 60

# 취소한 시도의 워커가 끝나기를 기다리는 최대 시간. 넘기면 새 시도를 시작하지 않는다.
_WORKER_WAIT = 10.0

_SESSIONS: dict[str, dict] = {}
# 지금 Hermes `start()` 를 부르는 중인 (profile, name) — 두 번 누름이 확인과 시작 사이로 끼어들지 않게.
_STARTING: set[tuple[str, str]] = set()
_LOCK = threading.Lock()


def _worker_done(flow) -> bool:
    # 속성이 없는 빌드(흐름 객체 모양이 다름)는 기다릴 방법이 없으니 끝난 것으로 본다.
    return bool(getattr(flow, "worker_done", True)) if flow is not None else True


def _wait_worker(flow, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _worker_done(flow):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _remember(sid: str, profile: str, name: str, home: Path, flow) -> None:
    now = time.monotonic()
    with _LOCK:
        for old in [k for k, v in _SESSIONS.items() if now - v["at"] > _SESSION_TTL and _worker_done(v["flow"])]:
            del _SESSIONS[old]
        _SESSIONS[sid] = {"profile": profile, "name": name, "home": str(Path(home).resolve()), "at": now,
                          "flow": flow}


def _open_session(profile: str, name: str) -> tuple[str, dict] | None:
    """이 (profile, name) 의 아직 워커가 도는 시도. 워커가 끝난 기록은 여기서 지운다. `_LOCK` 안에서 부른다."""
    for sid, sess in list(_SESSIONS.items()):
        if sess["profile"] == profile and sess["name"] == name:
            if not _worker_done(sess["flow"]):
                return sid, sess
            del _SESSIONS[sid]
    return None


def _cancel(api, sid: str, sess: dict) -> tuple[dict, bool]:
    """시도를 취소하고 워커가 끝나기를 기다린다. (cancel_flow 결과, 워커가 끝났는지).

    `cancel_attempt` 가 커밋과 같은 잠금으로 취소를 걸어 이 시도가 나중에 커밋하지 못하게 하고, `cancel_flow` 가
    세션 대기와 리스너를 푼다. 워커가 끝나야 롤백까지 끝난 것이다 — 끝나기 전에는 기록을 지우지 않는다.
    """
    flow = sess.get("flow")
    if flow is not None:
        api.mcp_oauth_cancel_attempt(flow)
    out = api.cancel_flow(sid, sess["name"], sess["home"]) or {}
    done = _wait_worker(flow, _WORKER_WAIT)
    if done:
        _forget(sid)
    return out, done


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


async def _optional_body(request) -> dict:
    # 본문 없는 POST(0.17.0 호출)도 받는다.
    if not request.can_read_body:
        return {}
    return await read_json_object(request)


def _redacted(api, exc: BaseException) -> str:
    return api.redact_mcp_probe_text(f"{type(exc).__name__}: {exc}")[:300]


def start_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = api.normalize_profile_name(request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        actor = actor_of(request)
        body = await _optional_body(request)
        restart = require_bool(body, "restart", required=False, default=False)

        def work():
            if require_entry(home, name).get("auth") != "oauth":
                raise RequestError(400, "oauth_not_configured", name)
            key = (profile, name)
            with _LOCK:
                open_ = _open_session(profile, name)
                if key in _STARTING or (open_ and not restart):
                    raise RequestError(409, "oauth_in_progress", "an OAuth attempt for this server is open") \
                        .with_extra(**({"sessionId": open_[0]} if open_ else {}))
                _STARTING.add(key)
            try:
                if open_:
                    _out, done = _cancel(api, *open_)
                    if not done:
                        raise RequestError(409, "oauth_busy", "the previous OAuth attempt has not finished yet")
                try:
                    # Hermes 는 인증 URL 이 나올 때까지(최대 수십 초) 여기서 기다린다 — 워커 스레드라 괜찮다.
                    with mcp_state.profile_scope(api, home):
                        attempt = api.mcp_oauth_start(name, client_redirect_uri=REDIRECT_URI)
                except Exception as exc:  # noqa: BLE001 — 공급자 오류는 가려서 사유만
                    raise RequestError(502, "oauth_start_failed", _redacted(api, exc)) from None
                mcp_state.audit(home, actor, "oauth_restart" if open_ else "oauth_start", name)
                if not attempt.auth_url:
                    return {"status": "approved"}  # 저장된 인증이 아직 유효하다
                sid = attempt.flow.flow_id
                _remember(sid, profile, name, home, attempt.flow)
                return {"sessionId": sid, "authUrl": attempt.auth_url}
            finally:
                with _LOCK:
                    _STARTING.discard(key)

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
            out, done = _cancel(api, sess["sid"], sess)
            # 워커가 아직 안 끝났으면 기록을 남겨 둔다 — 그동안 새 시도는 409 oauth_in_progress 로 막힌다.
            return {"ok": bool(out.get("ok")) or done, **({} if done else {"workerDone": False})}

        return web.json_response(await run_blocking(work))

    return handler
