"""첨부 파일과 워커 로그 — 카드에 딸린 **바이트**를 다루는 라우트.

- `GET  /deskrpg/kanban/tasks/{id}/attachments?board=` → `{attachments}`
- `POST /deskrpg/kanban/tasks/{id}/attachments?board=` multipart(`file`) → 201 `{attachment}`
- `GET  /deskrpg/kanban/attachments/{id}?board=` → 파일 바이트
- `DELETE /deskrpg/kanban/attachments/{id}?board=` → `{ok}`
- `GET  /deskrpg/kanban/tasks/{id}/log?board=&tail=` → WorkerLog

저장은 Hermes 의 단일 쓰기 경로 `store_attachment_bytes` 에 맡긴다(안전한 이름·충돌 회피·
blob 과 행의 원자성). 내려받기는 DB 행이 가리키는 경로가 **그 보드의 attachments_root 아래**인지
다시 확인한다 — 행이 변조되면 임의 파일 읽기가 되기 때문이다(대시보드와 같은 방어).
삭제는 Hermes `delete_attachment` 가 blob 까지 지우므로 우리가 따로 unlink 하지 않는다.
"""

import urllib.parse
from pathlib import Path

from aiohttp import web

from .common import RequestError, guarded, log_event, parse_board_slug, run_blocking
from .kanban_common import actor_from_request, attachment_payload, open_board, require_task

# 워커 로그 tail 의 기본·상한(바이트). 로그는 2 MiB 에서 회전하므로 1 MiB 면 최근 절반이다.
DEFAULT_LOG_TAIL_BYTES = 16384
MAX_LOG_TAIL_BYTES = 1024 * 1024

UPLOAD_CHUNK_BYTES = 1024 * 1024


def attachment_max_bytes(api) -> int:
    """`/deskrpg/info` 의 `attachment_max_bytes` 와 같은 식 — 게이트웨이 본문 상한과 칸반 상한 중 작은 쪽."""
    return min(int(api.MAX_REQUEST_BYTES), int(api.KANBAN_ATTACHMENT_MAX_BYTES))


def _attachment_id(request) -> int:
    raw = request.match_info["id"]
    try:
        return int(raw)
    except ValueError:
        raise RequestError(400, "invalid_attachment_id", raw)


def _too_large(max_bytes: int) -> web.Response:
    return web.json_response({"error": "attachment_too_large", "max_bytes": max_bytes}, status=413)


# ---------------------------------------------------------------------------
# 목록·업로드
# ---------------------------------------------------------------------------


def list_attachments_handler(api):
    @guarded
    async def handler(request):
        task_id = request.match_info["id"]

        def work(slug):
            with open_board(api, slug) as conn:
                require_task(api, conn, task_id)
                return [attachment_payload(a) for a in api.list_attachments(conn, task_id)]

        slug = parse_board_slug(request)
        rows = await run_blocking(work, slug)
        return web.json_response({"attachments": rows})

    return handler


async def _read_file_part(request, max_bytes: int):
    """multipart 에서 `file` 파트를 찾아 `(filename, content_type, data)` 를 돌려준다.

    상한을 넘는 순간 읽기를 멈춘다 — 디스크가 아니라 메모리에 쌓는 동안에도 한 요청이 상한의
    몇 배를 붙들고 있어선 안 된다. 넘으면 `None` 을 돌려주고 호출자가 413 을 낸다.
    """
    if not (request.content_type or "").startswith("multipart/"):
        raise RequestError(400, "invalid_body", "multipart/form-data 의 `file` 파트가 필요하다")
    reader = await request.multipart()
    async for part in reader:
        if part.name != "file":
            await part.release()
            continue
        chunks = []
        total = 0
        while True:
            chunk = await part.read_chunk(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        content_type = part.headers.get("Content-Type") or None
        return part.filename or "", content_type, b"".join(chunks)
    raise RequestError(400, "missing_file", "multipart 에 `file` 파트가 없다")


def upload_attachment_handler(api):
    @guarded
    async def handler(request):
        task_id = request.match_info["id"]
        max_bytes = attachment_max_bytes(api)
        actor = actor_from_request(request)

        def check_task(slug):
            with open_board(api, slug) as conn:
                require_task(api, conn, task_id)

        def store(slug, filename, content_type, data):
            with open_board(api, slug) as conn:
                try:
                    att_id = api.store_attachment_bytes(
                        conn, task_id, filename, data,
                        content_type=content_type, uploaded_by=actor, board=slug, max_bytes=max_bytes,
                    )
                except api.AttachmentTooLarge:
                    raise RequestError(413, "attachment_too_large")
                except ValueError as exc:
                    # `_safe_attachment_name` — 디렉토리·제어문자·점을 걷어내면 아무것도 안 남는 이름
                    raise RequestError(400, "invalid_filename", str(exc))
                att = api.get_attachment(conn, att_id)
                return attachment_payload(att) if att else {"id": att_id, "filename": filename, "size": len(data)}

        try:
            slug = parse_board_slug(request)
            # 카드가 없으면 본문을 읽기 전에 끊는다 — 없는 카드에 25 MB 를 받아 줄 이유가 없다.
            await run_blocking(check_task, slug)
            part = await _read_file_part(request, max_bytes)
            if part is None:
                log_event("kanban.attachment.upload", board=slug, task_id=task_id, status="413")
                return _too_large(max_bytes)
            filename, content_type, data = part
            payload = await run_blocking(store, slug, filename, content_type, data)
        except RequestError as exc:
            if exc.status == 413:
                return _too_large(max_bytes)  # 413 만 `max_bytes` 를 싣는 다른 모양이다
            raise
        log_event("kanban.attachment.upload", board=slug, task_id=task_id, attachment_id=payload["id"], data_bytes=len(data))
        return web.json_response({"attachment": payload}, status=201)

    return handler


# ---------------------------------------------------------------------------
# 내려받기·삭제
# ---------------------------------------------------------------------------


def _content_disposition(filename: str) -> str:
    """RFC 6266 — ASCII 폴백과 UTF-8 확장 표기를 함께 싣는다(한글 파일 이름)."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_") or "attachment"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def download_attachment_handler(api):
    @guarded
    async def handler(request):
        def work(slug, att_id):
            with open_board(api, slug) as conn:
                att = api.get_attachment(conn, att_id)
                if att is None:
                    raise RequestError(404, "attachment_not_found", str(att_id))
                root = Path(api.attachments_root(board=slug)).resolve()
                try:
                    stored = Path(att.stored_path).resolve()
                    stored.relative_to(root)
                except (ValueError, OSError):
                    # 행이 첨부 루트 밖을 가리킨다 — 변조됐거나 보드가 옮겨졌다. 바이트를 내주지 않는다.
                    raise RequestError(404, "attachment_unavailable", str(att_id))
                if not stored.is_file():
                    raise RequestError(404, "attachment_missing", str(att_id))
                return att, stored.read_bytes()

        slug = parse_board_slug(request)
        att, data = await run_blocking(work, slug, _attachment_id(request))
        return web.Response(
            body=data,
            content_type=getattr(att, "content_type", None) or "application/octet-stream",
            headers={"Content-Disposition": _content_disposition(att.filename)},
        )

    return handler


def delete_attachment_handler(api):
    @guarded
    async def handler(request):
        def work(slug, att_id):
            with open_board(api, slug) as conn:
                if api.delete_attachment(conn, att_id) is None:
                    raise RequestError(404, "attachment_not_found", str(att_id))

        slug = parse_board_slug(request)
        att_id = _attachment_id(request)
        await run_blocking(work, slug, att_id)
        log_event("kanban.attachment.delete", board=slug, attachment_id=att_id)
        return web.json_response({"ok": True})

    return handler


# ---------------------------------------------------------------------------
# 워커 로그
# ---------------------------------------------------------------------------


def _parse_tail(request) -> int:
    raw = request.query.get("tail")
    if raw is None or raw == "":
        return DEFAULT_LOG_TAIL_BYTES
    try:
        tail = int(raw)
    except ValueError:
        raise RequestError(400, "invalid_tail", raw)
    if tail < 1:
        raise RequestError(400, "invalid_tail", "tail 은 1 이상이어야 한다")
    return min(tail, MAX_LOG_TAIL_BYTES)


def worker_log_handler(api):
    @guarded
    async def handler(request):
        task_id = request.match_info["id"]

        def work(slug, tail):
            with open_board(api, slug) as conn:
                require_task(api, conn, task_id)
            # Hermes 는 로그 경로를 내주지 않고(`worker_log_path` 는 SPEC 밖) 내용만 준다. 크기는
            # 전체를 한 번 읽어 잰다 — 로그는 2 MiB 에서 회전하므로 상한이 있는 읽기다.
            full = api.read_worker_log(task_id, board=slug)
            if full is None:
                return {"exists": False, "size_bytes": 0, "content": "", "truncated": False}
            size = len(full.encode("utf-8"))
            truncated = size > tail
            content = api.read_worker_log(task_id, tail_bytes=tail, board=slug) if truncated else full
            return {"exists": True, "size_bytes": size, "content": content or "", "truncated": truncated}

        slug = parse_board_slug(request)
        tail = _parse_tail(request)
        payload = await run_blocking(work, slug, tail)
        return web.json_response(payload)

    return handler


__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "MAX_LOG_TAIL_BYTES",
    "attachment_max_bytes",
    "attachment_payload",
    "list_attachments_handler",
    "upload_attachment_handler",
    "download_attachment_handler",
    "delete_attachment_handler",
    "worker_log_handler",
]
