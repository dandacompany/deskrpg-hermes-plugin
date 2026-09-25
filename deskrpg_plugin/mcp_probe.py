"""MCP 연결 테스트(0.17.0) — 접속 → 도구·annotation 읽기 → 해제. 결과는 `mcp-checks.json` 에 남긴다.

Hermes 공개 탐침 `_probe_single_server` 는 annotation 을 돌려주지 않는다. 같은 내부 함수
(`_connect_server`·`_run_on_mcp_loop`)로 도구의 `readOnlyHint`·`destructiveHint` 까지 읽는다 — capability 판정이
이 심볼들을 요구하므로 없는 빌드에서는 라우트 자체가 등록되지 않는다. 순서·시간 제한·OAuth 판정은 대시보드
`/api/mcp/servers/{name}/test` 와 같게 둔다.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import secrets
import threading
import time

from aiohttp import web

from . import mcp_state
from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home
from .mcp_admin import raw_servers, require_entry
from .picker import _home_scope

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 60.0
_KEEP_SECONDS = 3600
OAUTH_REQUIRED = "OAuth authentication required — no token found."


@contextlib.contextmanager
def interactive_oauth_suppressed():
    """게이트웨이에서 브라우저·stdin OAuth 가 뜨지 않게 한다(Hermes `suppress_interactive_oauth`).

    선택 심볼이다 — 없는 빌드면 그냥 지나간다. ContextVar 라 MCP 루프 스레드의 코루틴까지 전해진다.
    """
    try:
        from tools.mcp_oauth import suppress_interactive_oauth
    except Exception:  # noqa: BLE001 — 없으면 억제 없이 진행(토큰 없는 OAuth 서버는 접속 전에 걸러진다)
        yield
        return
    with suppress_interactive_oauth():
        yield


class ProbeJobs:
    """스레드로 도는 짧은 작업 표. 결과는 프로필 이름으로만 조회된다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def _gc(self) -> None:
        cutoff = time.monotonic() - _KEEP_SECONDS
        for jid in [j for j, v in self._jobs.items() if v.get("_doneAt", time.monotonic()) < cutoff]:
            del self._jobs[jid]

    def start(self, profile: str, name: str, fn) -> str:
        with self._lock:
            self._gc()
            if any(v["_profile"] == profile and v["_name"] == name and v["state"] == "running"
                   for v in self._jobs.values()):
                raise RequestError(409, "job_busy", "a connection test for this server is running")
            jid = secrets.token_hex(8)
            self._jobs[jid] = {"jobId": jid, "state": "running", "_profile": profile, "_name": name}

        def run():
            try:
                update = {"state": "succeeded", **fn()}
            except Exception as exc:  # noqa: BLE001 — fn 이 결과로 바꾸지 못한 예외. 메시지는 싣지 않는다.
                update = {"state": "failed", "ok": False, "error": type(exc).__name__}
            with self._lock:
                self._jobs[jid].update(update, _doneAt=time.monotonic())

        threading.Thread(target=run, daemon=True, name=f"deskrpg-mcp-probe-{name}").start()
        return jid

    def get(self, profile: str, jid: str) -> dict:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job["_profile"] != profile:
                raise RequestError(404, "job_unknown", jid)
            return {k: v for k, v in job.items() if not k.startswith("_")}


JOBS = ProbeJobs()


def _hint(tool, key):
    value = getattr(getattr(tool, "annotations", None), key, None)
    return value if isinstance(value, bool) else None


def _timeout(cfg: dict) -> float:
    try:
        return min(_MAX_TIMEOUT, max(1.0, float(cfg.get("connect_timeout", _DEFAULT_TIMEOUT))))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def probe(api, home, name: str, entry: dict) -> list[dict]:
    """한 번 접속해 도구 목록을 읽고 끊는다. 실패는 예외 — 호출자가 가린 오류문으로 바꾼다."""
    reasons = api.validate_mcp_server_entry(name, entry)
    if reasons:
        raise ValueError("; ".join(str(r) for r in reasons))
    with mcp_state.profile_scope(api, home), interactive_oauth_suppressed():
        cfg = api._resolve_mcp_server_config(dict(entry))
        timeout = _timeout(cfg)
        cfg["connect_timeout"] = timeout
        api._ensure_mcp_loop()

        async def run():
            try:
                server = await asyncio.wait_for(api._connect_server(name, cfg), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"connecting to MCP server '{name}' timed out after {timeout:.0f}s") from None
            try:
                return [{"name": t.name, "description": (getattr(t, "description", "") or "")[:200],
                         "readOnlyHint": _hint(t, "readOnlyHint"), "destructiveHint": _hint(t, "destructiveHint")}
                        for t in server._tools]
            finally:
                await server.shutdown()

        try:
            return api._run_on_mcp_loop(run(), timeout=timeout + 10)
        finally:
            api._stop_mcp_loop_if_idle()


def tool_rows(entry: dict, tools: list[dict]) -> list[dict]:
    """각 도구에 현재 선택(`tools.include`/`exclude`, glob — include 가 우선)으로 `on` 을 붙인다."""
    filt = entry.get("tools") or {}
    include = filt.get("include") if isinstance(filt.get("include"), list) else None
    exclude = filt.get("exclude") if isinstance(filt.get("exclude"), list) else None

    def on(name: str) -> bool:
        if include is not None:
            return any(fnmatch.fnmatchcase(name, p) for p in include)
        if exclude is not None:
            return not any(fnmatch.fnmatchcase(name, p) for p in exclude)
        return True

    return [{**t, "on": on(t["name"])} for t in tools]


def run_check(api, home, name: str, entry: dict) -> dict:
    """연결 확인 한 번 → `mcp-checks.json` 기록 → 작업 결과. 예외를 결과로 바꾼다."""
    try:
        with _home_scope(api, home):
            token_missing = entry.get("auth") == "oauth" and not api._oauth_tokens_present(name)
        if token_missing:
            # 익명으로 tools/list 를 주는 OAuth 서버가 "연결됨" 으로 보이지 않게 — 접속 전에 끊는다.
            raise PermissionError(OAUTH_REQUIRED)
        tools = probe(api, home, name, entry)
        check = {"at": mcp_state.now_iso(), "ok": True, "tools": tool_rows(entry, tools)}
    except BaseException as exc:  # noqa: BLE001 — 타임아웃·연결 오류 모두 결과로 남긴다
        text = str(exc) if isinstance(exc, PermissionError) else f"{type(exc).__name__}: {exc}"
        check = {"at": mcp_state.now_iso(), "ok": False, "error": api.redact_mcp_probe_text(text)[:500]}
    if name in raw_servers(home):  # 테스트 중 삭제됐으면 기록을 되살리지 않는다
        prev = mcp_state.read_checks(home).get(name) or {}
        if not check["ok"] and prev.get("tools"):
            check["tools"] = prev["tools"]  # 실패해도 마지막으로 안 도구 목록은 유지
        mcp_state.write_check(home, name, check)
    if check["ok"]:
        return {"ok": True, "tools": check["tools"]}
    return {"ok": False, "tools": [], "error": check["error"]}


def test_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        profile = api.normalize_profile_name(request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])
        entry = await run_blocking(require_entry, home, name)
        job_id = JOBS.start(profile, name, lambda: run_check(api, home, name, entry))
        return web.json_response({"jobId": job_id}, status=202)

    return handler


def job_handler(api):
    @guarded
    async def handler(request):
        resolve_profile_home(api, request.match_info["profile"])
        profile = api.normalize_profile_name(request.match_info["profile"])
        return web.json_response(JOBS.get(profile, request.match_info["job_id"]))

    return handler


def tools_get_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = mcp_state.require_name(request.match_info["name"])

        def work():
            entry = require_entry(home, name)
            check = mcp_state.read_checks(home).get(name)
            if not check or not check.get("tools"):
                raise RequestError(404, "tools_unknown", "run a connection test first")
            rows = tool_rows(entry, [{k: t.get(k) for k in ("name", "description", "readOnlyHint", "destructiveHint")}
                                     for t in check["tools"]])
            return {"tools": rows, "checkedAt": check.get("at"), "revision": mcp_state.revision(entry)}

        return web.json_response(await run_blocking(work))

    return handler
