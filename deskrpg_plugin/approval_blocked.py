"""무인 실행 막힘 사건(0.18.0) — 크론·칸반 워커의 도구 결과에서 승인 거부를 찾아 `approval.blocked` 로 남긴다.

사람이 없는 실행(크론, 칸반 워커 `-q`)에서 Hermes 는 위험 명령·untrusted MCP 쓰기를 물을 곳이 없어 거부하고,
그 사실을 **도구 결과**로만 모델에게 알린다. DeskRPG 가 시킨 사람에게 "무엇이 왜 막혔는지" 알리려면 여기서
그 결과를 알아보고 사건으로 남겨야 한다. 사건은 아티팩트 레지스트리의 `artifact_events` 에 쌓여
`/deskrpg/events?include=approvals` 로만 나간다(카드 제안과 같은 경로).

알아보는 결과(Hermes v2026.9.21, 결과는 JSON 문자열 — 평문도 받는다):
- 위험 명령: `BLOCKED: … set approvals.cron_mode: approve …`(크론) / `approvals.single_query_mode`(칸반 워커).
  터미널은 `{"error": "BLOCKED: …", "status": "blocked"}`(`tools/terminal_tool.py` `_error_json`),
  execute_code 도 같은 문구 끝(`… approvals.cron_mode: approve only if …`)을 쓴다.
- MCP 쓰기: `The user did not approve running write-capable MCP tool '<t>' on untrusted server '<s>'` 와
  승인 경로가 없을 때의 `MCP tool '<t>' on untrusted server '<s>' was blocked`(`tools/mcp_tool_handlers.py`).
정책 문구가 없는 BLOCKED(하드라인·`approvals.deny`)는 정책으로 풀 수 없으니 알리지 않는다. `unattended_mode`
(대화 플랫폼)는 DeskRPG 대화 경로라 이번 범위 밖이다.

훅 인자(`model_tools.py:_emit_post_tool_call_hook`): tool_name, args, result, task_id, session_id, tool_call_id,
turn_id, api_request_id, duration_ms, status, error_type, error_message, middleware_trace. 크론 에이전트의
task_id 는 `cron:{job_id}:{execution_id}`(`cron/scheduler.py`), 칸반 워커는 환경변수 `HERMES_KANBAN_TASK`·
`HERMES_KANBAN_RUN_ID` 를 가진다.

훅은 **절대 던지지 않는다.** 대부분의 도구 결과는 첫 줄의 문자열 검사에서 끝난다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time

from . import artifacts_context as _context
from . import artifacts_store as _store

logger = logging.getLogger("deskrpg_plugin")

EVENT_KIND = "approval.blocked"
KANBAN_RUN_ENV = "HERMES_KANBAN_RUN_ID"
COMMAND_MAX = 300

_MODE_RE = re.compile(r"approvals\.(cron_mode|single_query_mode)\b")
_MCP_RES = (
    re.compile(r"did not approve running write-capable MCP tool '([^']+)' on untrusted server '([^']+)'"),
    re.compile(r"MCP tool '([^']+)' on untrusted server '([^']+)' was blocked"),
)
_MODE_SOURCE = {"cron_mode": ("cron", "cron"), "single_query_mode": ("single_query", "kanban")}

# 한 프로세스(크론 한 번·칸반 실행 한 번) 안에서 같은 막힘은 한 번만 남긴다 — 에이전트가 같은 명령을 되풀이해도
# 알림이 쏟아지지 않게.
_SEEN: set = set()
_SEEN_LOCK = threading.Lock()


def _text(result) -> str:
    if isinstance(result, dict):
        value = result.get("error")
        return value if isinstance(value, str) else ""
    if not isinstance(result, str):
        return ""
    stripped = result.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            return result
        return _text(data) if isinstance(data, dict) else ""
    return result


def classify_blocked(tool_name, args, result) -> dict | None:
    """도구 결과가 무인 실행 정책 때문에 막힌 것이면 `{kind, …}`, 아니면 None. 순수 함수."""
    text = _text(result)
    if not text:
        return None
    if text.startswith("BLOCKED:"):
        match = _MODE_RE.search(text)
        if match is None:
            return None
        return {"kind": "command", "mode": _MODE_SOURCE[match.group(1)][0]}
    for pattern in _MCP_RES:
        match = pattern.search(text)
        if match:
            return {"kind": "mcp", "mcpServer": match.group(2), "mcpTool": match.group(1)}
    return None


def _cheap_miss(result) -> bool:
    # json 파싱 전에 거른다 — 거의 모든 도구 결과가 여기서 끝난다.
    text = result if isinstance(result, str) else (result.get("error") if isinstance(result, dict) else None)
    return not isinstance(text, str) or ("BLOCKED:" not in text and "untrusted server" not in text)


def _context_ids(api, task_id) -> dict:
    """실행 맥락: 칸반 워커면 {source: kanban, taskId, runId, board}, 크론이면 {source: cron, jobId}, 아니면 {}."""
    kanban_task = os.environ.get(_context.KANBAN_TASK_ENV)
    if kanban_task:
        ids = {"source": "kanban", "taskId": kanban_task, "runId": os.environ.get(KANBAN_RUN_ENV) or None}
        with contextlib.suppress(Exception):
            ids["board"] = api.get_current_board()
        return ids
    if isinstance(task_id, str) and task_id.startswith("cron:"):
        parts = task_id.split(":")
        return {"source": "cron", "jobId": parts[1] if len(parts) > 1 and parts[1] else None}
    return {}


def _pattern(api, command: str) -> dict:
    detect = getattr(api, "detect_dangerous_command", None)
    if detect is None or not command:
        return {}
    try:
        is_dangerous, key, description = detect(command)
    except Exception:  # noqa: BLE001
        return {}
    if not is_dangerous or not key:
        return {}
    return {"patternKey": str(key), "patternDescription": str(description or "") or None}


def _command(api, command: str):
    """가린 명령. 가리는 함수가 없으면 명령을 싣지 않는다 — 가리지 않은 명령은 내보내지 않는다."""
    redact = getattr(api, "redact_sensitive_text", None)
    if redact is None or not command:
        return None
    try:
        return str(redact(command, force=True))[:COMMAND_MAX]
    except Exception:  # noqa: BLE001
        return None


def build_payload(api, *, tool_name, args, result, task_id) -> dict | None:
    """사건 payload, 또는 남길 것이 아니면 None."""
    verdict = classify_blocked(tool_name, args, result)
    if verdict is None:
        return None
    ctx = _context_ids(api, task_id)
    if verdict["kind"] == "command":
        # 문구가 맥락을 정한다 — 크론 문구면 크론 정책(`cron_mode`), `-q` 문구면 칸반 워커 정책이다.
        source = _MODE_SOURCE["cron_mode" if verdict["mode"] == "cron" else "single_query_mode"][1]
        if ctx.get("source") != source:
            ctx = {"source": source}
        command = str((args or {}).get("command") or "") if isinstance(args, dict) else ""
        extra = {**_pattern(api, command), "command": _command(api, command)}
    else:
        if not ctx:
            return None  # 대화에서 사람이 거절한 것 — 무인 실행 막힘이 아니다
        extra = {"mcpServer": verdict["mcpServer"], "mcpTool": verdict["mcpTool"]}
    payload = {
        "profile": _context.current_profile(api),
        **ctx,
        "tool": str(tool_name or ""),
        "kind": verdict["kind"],
        **extra,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return {k: v for k, v in payload.items() if v is not None}


def _dedupe_key(payload: dict) -> tuple:
    return (payload.get("source"), payload.get("jobId"), payload.get("taskId"), payload.get("runId"),
            payload.get("kind"), payload.get("patternKey") or payload.get("mcpServer"),
            payload.get("mcpTool") or (None if payload.get("patternKey") else payload.get("tool")))


def _record(api, payload: dict) -> None:
    with contextlib.closing(_store.open_registry(api)) as conn, conn:
        _store._append_event(conn, int(time.time()), EVENT_KIND, payload)


def make_hook(api):
    def hook(**kwargs):
        try:
            result = kwargs.get("result")
            if _cheap_miss(result):
                return
            payload = build_payload(api, tool_name=kwargs.get("tool_name"), args=kwargs.get("args"),
                                    result=result, task_id=kwargs.get("task_id"))
            if payload is None:
                return
            key = _dedupe_key(payload)
            with _SEEN_LOCK:
                if key in _SEEN:
                    return
                _SEEN.add(key)
            _record(api, payload)
        except Exception as exc:  # noqa: BLE001 — 훅 실패가 도구 호출을 흔들면 안 된다
            logger.warning("[deskrpg] approval-blocked hook failed: %s", type(exc).__name__)

    return hook
