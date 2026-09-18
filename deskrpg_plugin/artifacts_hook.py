"""자동 승격 훅 둘 — 사용자가 말하지 않아도 산출물을 아티팩트로 남긴다.

- `post_tool_call`(`make_hook`): 산출 도구가 만든 **파일**.
- `post_llm_call`(`make_response_hook`, 0.8.1·0.8.3): 턴의 최종 응답 속 **큰 코드 블록**(artifacts_fences)과 **링크**(artifacts_links).

아래는 `post_tool_call` 훅의 설명이다.

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
import os
import sqlite3
import time

from . import artifacts_context as context
from . import artifacts_detect as detect
from . import artifacts_fences as fences
from . import artifacts_links as links
from . import artifacts_policy as policy
from . import artifacts_store as store
from .artifacts_tool import TOOL_NAME, artifact_storage_max_bytes
from .common import log_event

logger = logging.getLogger("deskrpg_plugin")

MAX_FILES_PER_CALL = 8
MAX_CANDIDATES_SCANNED = 64
MAX_RESPONSE_BLOCKS = 8
MAX_RESPONSE_LINKS = 30
MAX_LINKS_PER_CALL = 8
RESPONSE_SWITCH_ENV = "HERMES_DESKRPG_CAPTURE_RESPONSES"
LINKS_SWITCH_ENV = "HERMES_DESKRPG_CAPTURE_LINKS"
RESPONSE_SOURCE = "assistant_response"  # capture_failed 의 tool_name 자리에 쓰는 출처 이름
_OFF_VALUES = frozenset({"0", "false", "no", "off"})


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
    limit = artifact_storage_max_bytes()
    roots = policy.allowed_source_roots(api)
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
        # 작업 공간(git 저장소) 안의 파일과 설정 파일은 결과물이 아니라 작업 중간물이다(0.8.2 결정).
        # 저장소 안의 보고서가 필요하면 NPC 가 artifact_save 로 명시 저장한다.
        if policy.is_config_file(source) or policy.is_workspace_file(source, roots):
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


# ---------------------------------------------------------------------------
# post_llm_call — 응답 속 큰 코드 블록 (0.8.1)
# ---------------------------------------------------------------------------


def responses_enabled() -> bool:
    """`HERMES_DESKRPG_CAPTURE_RESPONSES` 가 0/false/no/off 면 끈다. 없거나 그 밖의 값이면 켠다."""
    return os.environ.get(RESPONSE_SWITCH_ENV, "").strip().lower() not in _OFF_VALUES


def links_enabled() -> bool:
    """`HERMES_DESKRPG_CAPTURE_LINKS` 가 0/false/no/off 면 두 훅의 링크 자동 수집을 끈다(명시 저장은 유지)."""
    return os.environ.get(LINKS_SWITCH_ENV, "").strip().lower() not in _OFF_VALUES


class _TurnWriter:
    """한 번의 훅 호출이 쓰는 저장 비용 묶음 — 연결은 하나, 컨텍스트는 한 번, 실패 사건은 이유를 합쳐 한 번.
    `sqlite3.OperationalError`(잠금 등)를 만나면 `stopped` 가 되고 이후 저장을 하지 않는다."""

    def __init__(self, api, session_id: str, task_id):
        self.api, self.session_id, self.task_id = api, session_id, task_id
        self.ctx = self.conn = None
        self.reasons: list = []
        self.stopped = False

    def context(self):
        if self.ctx is None:
            self.ctx = context.resolve_context(self.api, session_id=self.session_id, task_id=self.task_id)
        return self.ctx

    def fail(self, reason: str) -> None:
        self.reasons.append(reason)

    def save(self, meta, data: bytes, limit: int, event: str) -> bool:
        try:
            if self.conn is None:
                self.conn = store.open_registry(self.api)
            out = store.store_artifact_version(self.api, self.conn, meta=meta, data=data, max_bytes=limit)
            log_event(event, artifact_id=out.artifact_id, version=out.version, kind=meta.kind, deduped=out.deduped)
            return True
        except sqlite3.OperationalError as exc:
            logger.warning("[deskrpg] 아티팩트 저장 중단(레지스트리): %s", type(exc).__name__)
            self.reasons.append(type(exc).__name__)
            self.stopped = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deskrpg] 아티팩트 저장 실패: %s", type(exc).__name__)
            self.reasons.append(type(exc).__name__)
        return False

    def close(self, tool_name: str) -> None:
        if self.conn is not None:
            self.conn.close()
        if self.reasons:
            record_capture_failure(self.api, tool_name=tool_name, reason=",".join(dict.fromkeys(self.reasons)))


def _link_meta(ctx, link, captured_via: str):
    return store.ArtifactMeta(
        kind="link", title=link.title, summary=link.url, filename=links.link_filename(link.title),
        mime=links.LINK_MIME, profile=ctx.profile, source_kind=ctx.source_kind, session_id=ctx.session_id,
        created_by=f"agent:{ctx.profile}", captured_via=captured_via, board=ctx.board, task_id=ctx.task_id,
        identity=link.url,
    )


def make_response_hook(api):
    """턴의 최종 응답에서 데스크톱 기준을 넘는 코드 블록을 아티팩트로 승격하는 `post_llm_call` 콜백.

    Hermes 가 부르는 kwargs: session_id, task_id, turn_id, user_message, assistant_response,
    conversation_history, model, platform (`agent/turn_finalizer.py:_apply_output_hooks`, 0.21.x).
    턴 중간의 어시스턴트 메시지는 보지 않는다 — 사용자가 실제로 받는 답이 기준이다.
    절대 던지지 않는다. 비용은 스위치와 블록 감지(순수 문자열 처리)에서 끊는다.
    """

    def hook(*, assistant_response=None, session_id="", task_id=None, **_rest) -> None:
        try:
            _capture_response(api, assistant_response, session_id or "", task_id or None)
        except Exception as exc:  # noqa: BLE001 — 마지막 방어선
            logger.exception("[deskrpg] 응답 아티팩트 훅 예외: %s", type(exc).__name__)
            record_capture_failure(api, tool_name=RESPONSE_SOURCE, reason=type(exc).__name__)

    return hook


def _capture_response(api, response, session_id: str, task_id) -> None:
    """턴 하나의 비용을 묶는다: 코드 블록은 `MAX_RESPONSE_BLOCKS` 번, 링크는 `MAX_RESPONSE_LINKS` 번까지
    시도하고, 둘이 레지스트리 연결 하나와 capture_failed 사건 한 번을 함께 쓴다. 잠기면 그 자리에서 멈춘다."""
    if not responses_enabled():
        return
    blocks = fences.extract_blocks(response)
    found_links = links.links_in_response(response) if links_enabled() else []
    if not blocks and not found_links:
        return
    limit = artifact_storage_max_bytes()
    writer = _TurnWriter(api, session_id, task_id)
    try:
        attempts = 0
        for block in blocks:
            if attempts >= MAX_RESPONSE_BLOCKS or writer.stopped:
                break
            if len(block.content.encode("utf-8")) > limit:
                # 정규식 감지 전에 거른다 — 상한을 넘는 블록은 어차피 저장하지 못한다.
                if fences.is_candidate_language(block.language):
                    writer.fail("too_large")
                continue
            found = fences.detect(block.language, block.content)
            if found is None:
                continue
            attempts += 1
            ctx = writer.context()
            meta = store.ArtifactMeta(
                kind=found.kind, title=found.title, summary=f"응답 속 {found.language} 코드 블록",
                filename=found.filename, mime=policy.mime_for_filename(found.filename), profile=ctx.profile,
                source_kind=ctx.source_kind, session_id=ctx.session_id, created_by=f"agent:{ctx.profile}",
                captured_via="response", board=ctx.board, task_id=ctx.task_id,
            )
            writer.save(meta, found.content.encode("utf-8"), limit, "artifact.capture_response")
        for link in found_links[:MAX_RESPONSE_LINKS]:
            if writer.stopped:
                break
            writer.save(_link_meta(writer.context(), link, "response"), links.url_blob(link.url), limit,
                        "artifact.capture_response")
    finally:
        writer.close(RESPONSE_SOURCE)
