"""`post_tool_call` 훅 — 산출 도구가 만든 파일을 사용자가 말하지 않아도 아티팩트로 승격한다.

Hermes 가 부르는 kwargs: tool_name, args, result, task_id, session_id, tool_call_id, turn_id, api_request_id,
duration_ms, status, error_type, error_message, middleware_trace (`model_tools.py:_emit_post_tool_call_hook`,
0.21.x). 훅은 **절대 던지지 않는다** — 던지면 도구 호출 자체가 흔들린다. 비용은 첫 줄(산출 도구 판정)에서
끊는다. 크론·칸반 워커 세션에서도 같은 코드가 돈다(Task 1 스파이크가 워커의 플러그인 로드를 실증했다).

`artifacts_detect.candidate_paths` 는 조상 키 태그 규칙(R9) 때문에 하위 트리 전체를 후보로 묶는다 — 그
결과 "application/pdf" 같은 비경로 문자열도 후보 목록에 섞여 들어온다. 이런 값은 `resolve_source_path`
에서 곧바로 걸러지고 저장 슬롯을 쓰지 않아야 하므로, **후보를 자르지 않고** 최대 `MAX_CANDIDATES_SCANNED`
개까지 스캔하되, 실제로 저장(신규 또는 dedup)에 성공한 파일이 `MAX_FILES_PER_CALL` 개가 될 때만 멈춘다.
"""

import contextlib
import logging
import time

from . import artifacts_context as context
from . import artifacts_detect as detect
from . import artifacts_policy as policy
from . import artifacts_store as store
from .artifacts_tool import TOOL_NAME, artifact_max_bytes
from .common import log_event

logger = logging.getLogger("deskrpg_plugin")

MAX_FILES_PER_CALL = 8
MAX_CANDIDATES_SCANNED = 64


def record_capture_failure(api, *, tool_name: str, reason: str) -> None:
    try:
        with contextlib.closing(store.open_registry(api)) as conn, conn:
            store._append_event(conn, int(time.time()), "artifact.capture_failed",
                                {"tool_name": tool_name, "reason": reason})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deskrpg] capture_failed 사건 기록 실패: %s", type(exc).__name__)


def make_hook(api):
    def hook(*, tool_name="", args=None, result=None, session_id="", task_id="", **_rest) -> None:
        try:
            _capture(api, tool_name or "", result, session_id or "", task_id or "")
        except Exception as exc:  # noqa: BLE001 — 마지막 방어선
            logger.exception("[deskrpg] 아티팩트 훅 예외: %s", type(exc).__name__)
            record_capture_failure(api, tool_name=tool_name or "", reason=type(exc).__name__)

    return hook


def _capture(api, tool_name: str, result, session_id: str, task_id: str) -> None:
    if tool_name == TOOL_NAME or not detect.is_producer_tool(tool_name):
        return
    payloads = detect.parse_tool_result(result)
    if not payloads:
        return
    candidates = detect.candidate_paths(payloads, producer=True)[:MAX_CANDIDATES_SCANNED]
    if not candidates:
        return
    ctx = None
    limit = artifact_max_bytes(api)
    saved = 0
    for raw in candidates:
        if saved >= MAX_FILES_PER_CALL:
            break
        try:
            source = policy.resolve_source_path(api, raw)
        except policy.PolicyError:
            continue  # 루트 밖·민감 파일·비경로 문자열은 조용히 넘어간다 — 저장 슬롯을 쓰지 않는다
        if source.suffix.lower() not in policy.HOOK_EXTENSIONS:
            continue
        try:
            size = source.stat().st_size
        except OSError:
            continue  # 찾은 뒤 사라진 파일 — 루트 밖 경로와 같은 취급, capture_failed 는 남기지 않는다
        if size > limit:
            record_capture_failure(api, tool_name=tool_name, reason="too_large")
            continue
        if ctx is None:
            ctx = context.resolve_context(api, session_id=session_id, task_id=task_id or None)
        meta = store.ArtifactMeta(
            kind=policy.kind_for_filename(source.name), title=source.name, summary=f"`{tool_name}` 이 만든 파일",
            filename=source.name, mime=policy.mime_for_filename(source.name), profile=ctx.profile,
            source_kind=ctx.source_kind, session_id=ctx.session_id, created_by=f"agent:{ctx.profile}",
            captured_via="hook", board=ctx.board, task_id=ctx.task_id, origin_path=str(source),
        )
        try:
            with contextlib.closing(store.open_registry(api)) as conn:
                out = store.store_artifact_version(api, conn, meta=meta, data=source.read_bytes(), max_bytes=limit)
            log_event("artifact.capture", artifact_id=out.artifact_id, version=out.version, kind=meta.kind,
                      tool=tool_name, deduped=out.deduped)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deskrpg] 아티팩트 승격 실패: %s", type(exc).__name__)
            record_capture_failure(api, tool_name=tool_name, reason=type(exc).__name__)
