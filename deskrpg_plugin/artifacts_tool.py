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
        "Call when the user asks to save, keep or register a result as an artifact. Before saving, put the output "
        "in the complete form for its kind — web is one complete HTML file, data is CSV/JSON, document is Markdown/PDF, "
        "image/media is a file path. If you revised something already saved, put the previous artifact_id in supersedes. "
        "To keep a link, pass only kind=link and url (title is optional)."
    ),
    "parameters": {
        "type": "object",
        "required": ["kind", "summary"],
        "properties": {
            "kind": {"type": "string", "enum": list(policy.KINDS)},
            "title": {"type": "string", "maxLength": 200, "description": "required unless kind is link"},
            "summary": {"type": "string", "maxLength": 600, "description": "1-2 sentences: what it is and who it is for"},
            "url": {"type": "string", "description": "only for kind=link. An http(s) URL. Not together with path or content"},
            "path": {"type": "string", "description": "file path under a gateway-managed root. One of path or content"},
            "content": {"type": "string", "description": "inline content (text formats only). filename is required"},
            "filename": {"type": "string", "description": "required with content. The extension decides the format"},
            "supersedes": {"type": "string", "description": "the previous artifact_id when this revises it"},
            "note": {"type": "string", "maxLength": 400, "description": "why this version exists"},
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
    return _err("artifact_incomplete", f"kind differs from the supersedes target — {exc}. Save a different kind as a new artifact without supersedes")


def _str(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def make_handler(api):
    def handler(args, **kwargs) -> str:
        try:
            return _save(api, args if isinstance(args, dict) else {}, kwargs)
        except Exception as exc:  # noqa: BLE001 — 도구는 예외를 밖으로 내지 않는다
            logger.exception("[deskrpg] artifact_save exception: %s", type(exc).__name__)
            return _err("internal_error", type(exc).__name__)

    return handler


def _save(api, args: dict, kwargs: dict) -> str:
    kind, title, summary = _str(args, "kind", 40), _str(args, "title", 200), _str(args, "summary", 600)
    if not (kind and summary):
        return _err("artifact_incomplete", "kind and summary are required")
    url = _str(args, "url", links.MAX_URL_CHARS + 64)
    if kind == "link" or url:
        return _save_link(api, args, kwargs, kind=kind, title=title, summary=summary, url=url)
    if not title:
        return _err("artifact_incomplete", "title is required (except for link)")
    path, content = _str(args, "path", 4096), args.get("content")
    if bool(path) == isinstance(content, str):
        return _err("artifact_incomplete", "pass exactly one of path or content")
    limit = artifact_storage_max_bytes()
    try:
        if path:
            source = policy.resolve_source_path(api, path)
            if source.stat().st_size > limit:
                return _err("artifact_too_large", f"exceeds {limit} bytes", max_bytes=limit)
            filename, data, origin = source.name, source.read_bytes(), str(source)
            text = data.decode("utf-8", "ignore") if policy.kind_for_filename(filename) in ("web", "react", "data", "document") else None
            policy.validate_completeness(kind, filename=filename, text=text, from_path=True)
        else:
            filename = _str(args, "filename", 200)
            if not filename:
                return _err("artifact_incomplete", "filename is required when saving content")
            policy.validate_completeness(kind, filename=filename, text=content, from_path=False)
            data, origin = content.encode("utf-8"), None
            if len(data) > limit:
                return _err("artifact_too_large", f"exceeds {limit} bytes", max_bytes=limit)
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
        return _err("artifact_too_large", f"exceeds {limit} bytes", max_bytes=limit)
    except store.ArtifactKindMismatch as exc:
        return _kind_mismatch(exc)
    log_event("artifact.save", artifact_id=result.artifact_id, version=result.version, kind=kind,
              profile=ctx.profile, source_kind=ctx.source_kind, deduped=result.deduped)
    return json.dumps({"artifact_id": result.artifact_id, "version": result.version, "deduped": result.deduped,
                       "title": title, "kind": kind}, ensure_ascii=False)


def _save_link(api, args: dict, kwargs: dict, *, kind: str, title: str, summary: str, url: str) -> str:
    if kind != "link" or not url or _str(args, "path", 4096) or isinstance(args.get("content"), str) \
            or _str(args, "filename", 200):
        return _err("artifact_incomplete", "for a link pass kind=link and url only — not together with path, content or filename")
    canonical = links.canonical_url(url)
    if canonical is None:
        return _err("artifact_incomplete", f"url must be an http(s) URL (at most {links.MAX_URL_CHARS} characters)")
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
