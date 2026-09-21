"""`artifact_save` — 사용자가 "아티팩트로 저장해" 라 하면 모델이 부르는 도구.

스키마가 곧 산출물의 형태를 강제하는 계약이다. 실패는 예외가 아니라 `{"error", "detail"}` 문자열로
돌려준다 — Hermes 는 핸들러의 반환 문자열을 모델에게 그대로 주고, 모델은 그걸 읽고 고쳐서 다시 부른다.
`session_id`·`task_id` 는 Hermes 가 kwargs 로 준다(`model_tools.py:_execute_tool`, 0.21.x). 모델 인자에
같은 이름이 있어도 무시한다.
"""

import contextlib
import json
import logging
import os

from . import artifacts_context as context
from . import artifacts_links as links
from . import artifacts_policy as policy
from . import artifacts_store as store
from .common import log_event

logger = logging.getLogger("deskrpg_plugin")

TOOL_NAME = "artifact_save"
TOOLSET = "deskrpg"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "사용자가 결과물을 아티팩트로 저장·보관·등록해 달라고 할 때 부른다. 저장 전에 산출물을 그 종류에 맞는 "
        "완결된 형태로 만든다 — web 은 하나의 완전한 HTML 파일, data 는 CSV/JSON, document 는 Markdown/PDF, "
        "image/media 는 파일 경로. 이미 저장한 것을 고쳤으면 supersedes 에 이전 artifact_id 를 넣는다. "
        "링크를 남길 때는 kind=link 와 url 만 넘긴다(title 은 선택)."
    ),
    "parameters": {
        "type": "object",
        "required": ["kind", "summary"],
        "properties": {
            "kind": {"type": "string", "enum": list(policy.KINDS)},
            "title": {"type": "string", "maxLength": 200, "description": "link 가 아니면 필수"},
            "summary": {"type": "string", "maxLength": 600, "description": "1~2문장. 무엇이고 누구를 위한 것인가"},
            "url": {"type": "string", "description": "kind=link 일 때만. http(s) 주소. path·content 와 함께 쓰지 않는다"},
            "path": {"type": "string", "description": "게이트웨이 관리 루트 아래의 파일 경로. path 또는 content 중 하나"},
            "content": {"type": "string", "description": "인라인 본문(텍스트 계열만). filename 필수"},
            "filename": {"type": "string", "description": "content 일 때 필수. 확장자가 형식을 정한다"},
            "supersedes": {"type": "string", "description": "개선한 것이면 이전 artifact_id"},
            "note": {"type": "string", "maxLength": 400, "description": "이 버전이 왜 생겼나"},
        },
    },
}


def artifact_storage_max_bytes() -> int:
    """저장소 상한 — `HERMES_DESKRPG_ARTIFACT_MAX_BYTES` 또는 100 MiB.

    도구와 훅은 HTTP 를 거치지 않으므로 이 값만 쓴다. 요청 본문 상한(`MAX_REQUEST_BYTES`)으로 깎지 않는다(I2).
    """
    raw = os.environ.get("HERMES_DESKRPG_ARTIFACT_MAX_BYTES")
    return int(raw) if raw and raw.isascii() and raw.isdigit() else DEFAULT_MAX_BYTES


def artifact_upload_max_bytes(api) -> int:
    """HTTP 로 들어오는 사람 편집의 실효 상한 — 게이트웨이 요청 본문 상한과 저장소 상한 중 작은 쪽.

    `/deskrpg/info.artifact_max_bytes` 도 이 값이다(DeskRPG 가 업로드 전에 거르는 기준).
    """
    return min(int(api.MAX_REQUEST_BYTES), artifact_storage_max_bytes())


def _err(code: str, detail: str, **extra) -> str:
    return json.dumps({"error": code, "detail": detail, **extra}, ensure_ascii=False)


def _kind_mismatch(exc) -> str:
    return _err("artifact_incomplete", f"supersedes 대상과 kind 가 다르다 — {exc}. 다른 kind 면 supersedes 없이 새로 저장한다")


def _str(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def make_handler(api):
    def handler(args, **kwargs) -> str:
        try:
            return _save(api, args if isinstance(args, dict) else {}, kwargs)
        except Exception as exc:  # noqa: BLE001 — 도구는 예외를 밖으로 내지 않는다
            logger.exception("[deskrpg] artifact_save 예외: %s", type(exc).__name__)
            return _err("internal_error", type(exc).__name__)

    return handler


def _save(api, args: dict, kwargs: dict) -> str:
    kind, title, summary = _str(args, "kind", 40), _str(args, "title", 200), _str(args, "summary", 600)
    if not (kind and summary):
        return _err("artifact_incomplete", "kind, summary 는 필수다")
    url = _str(args, "url", links.MAX_URL_CHARS + 64)
    if kind == "link" or url:
        return _save_link(api, args, kwargs, kind=kind, title=title, summary=summary, url=url)
    if not title:
        return _err("artifact_incomplete", "title 은 필수다(link 제외)")
    path, content = _str(args, "path", 4096), args.get("content")
    if bool(path) == isinstance(content, str):
        return _err("artifact_incomplete", "path 또는 content 중 정확히 하나를 넘긴다")
    limit = artifact_storage_max_bytes()
    try:
        if path:
            source = policy.resolve_source_path(api, path)
            if source.stat().st_size > limit:
                return _err("artifact_too_large", f"{limit} 바이트를 넘는다", max_bytes=limit)
            filename, data, origin = source.name, source.read_bytes(), str(source)
            text = data.decode("utf-8", "ignore") if policy.kind_for_filename(filename) in ("web", "react", "data", "document") else None
            policy.validate_completeness(kind, filename=filename, text=text, from_path=True)
        else:
            filename = _str(args, "filename", 200)
            if not filename:
                return _err("artifact_incomplete", "content 로 저장할 때는 filename 이 필요하다")
            policy.validate_completeness(kind, filename=filename, text=content, from_path=False)
            data, origin = content.encode("utf-8"), None
            if len(data) > limit:
                return _err("artifact_too_large", f"{limit} 바이트를 넘는다", max_bytes=limit)
    except policy.PolicyError as exc:
        return _err(exc.code, exc.detail)

    ctx = context.resolve_context(api, session_id=str(kwargs.get("session_id") or ""))
    meta = store.ArtifactMeta(
        kind=kind, title=title, summary=summary, filename=filename, mime=policy.mime_for_filename(filename),
        profile=ctx.profile, source_kind=ctx.source_kind, session_id=ctx.session_id, created_by=f"agent:{ctx.profile}",
        captured_via="tool", board=ctx.board, task_id=ctx.task_id, job_id=ctx.job_id, run_id=ctx.run_id,
        origin_path=origin, note=_str(args, "note", 400) or None, supersedes=_str(args, "supersedes", 64) or None,
    )
    try:
        with contextlib.closing(store.open_registry(api)) as conn:
            result = store.store_artifact_version(api, conn, meta=meta, data=data, max_bytes=limit)
    except store.ArtifactTooLarge:
        return _err("artifact_too_large", f"{limit} 바이트를 넘는다", max_bytes=limit)
    except store.ArtifactKindMismatch as exc:
        return _kind_mismatch(exc)
    log_event("artifact.save", artifact_id=result.artifact_id, version=result.version, kind=kind,
              profile=ctx.profile, source_kind=ctx.source_kind, deduped=result.deduped)
    return json.dumps({"artifact_id": result.artifact_id, "version": result.version, "deduped": result.deduped,
                       "title": title, "kind": kind}, ensure_ascii=False)


def _save_link(api, args: dict, kwargs: dict, *, kind: str, title: str, summary: str, url: str) -> str:
    if kind != "link" or not url or _str(args, "path", 4096) or isinstance(args.get("content"), str) \
            or _str(args, "filename", 200):
        return _err("artifact_incomplete", "링크는 kind=link 와 url 만 넘긴다 — path·content·filename 과 함께 쓰지 않는다")
    canonical = links.canonical_url(url)
    if canonical is None:
        return _err("artifact_incomplete", f"url 은 http(s) 주소여야 한다({links.MAX_URL_CHARS}자 이하)")
    title = title or links.label_for(canonical)
    ctx = context.resolve_context(api, session_id=str(kwargs.get("session_id") or ""))
    meta = store.ArtifactMeta(
        kind="link", title=title, summary=summary, filename=links.link_filename(title), mime=links.LINK_MIME,
        profile=ctx.profile, source_kind=ctx.source_kind, session_id=ctx.session_id, created_by=f"agent:{ctx.profile}",
        captured_via="tool", board=ctx.board, task_id=ctx.task_id, job_id=ctx.job_id, run_id=ctx.run_id,
        note=_str(args, "note", 400) or None, supersedes=_str(args, "supersedes", 64) or None, identity=canonical,
    )
    limit = artifact_storage_max_bytes()
    try:
        with contextlib.closing(store.open_registry(api)) as conn:
            result = store.store_artifact_version(api, conn, meta=meta, data=links.url_blob(canonical), max_bytes=limit)
    except store.ArtifactKindMismatch as exc:
        return _kind_mismatch(exc)
    log_event("artifact.save", artifact_id=result.artifact_id, version=result.version, kind="link",
              profile=ctx.profile, source_kind=ctx.source_kind, deduped=result.deduped)
    return json.dumps({"artifact_id": result.artifact_id, "version": result.version, "deduped": result.deduped,
                       "title": title, "kind": "link"}, ensure_ascii=False)
