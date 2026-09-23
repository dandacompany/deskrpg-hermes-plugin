"""첨부 파일과 워커 로그 — 카드에 딸린 **바이트**를 다루는 라우트.

- `GET  /deskrpg/kanban/tasks/{id}/attachments?board=` → `{attachments}`
- `GET  /deskrpg/kanban/attachments?board=&limit=&cursor=` → `{attachments, next_cursor}` (보드 전체, 최신순)
- `POST /deskrpg/kanban/tasks/{id}/attachments?board=` multipart(`file`) → 201 `{attachment}`
- `GET  /deskrpg/kanban/attachments/{id}?board=` → 파일 바이트
- `DELETE /deskrpg/kanban/attachments/{id}?board=` → `{ok}`
- `GET  /deskrpg/kanban/tasks/{id}/log?board=&tail=` → WorkerLog

저장은 Hermes 의 단일 쓰기 경로 `store_attachment_bytes` 에 맡긴다(안전한 이름·충돌 회피·
blob 과 행의 원자성). 내려받기는 DB 행이 가리키는 경로가 **그 보드의 attachments_root 아래**인지
다시 확인한다 — 행이 변조되면 임의 파일 읽기가 되기 때문이다(대시보드와 같은 방어).
삭제는 Hermes `delete_attachment` 가 blob 까지 지우므로 우리가 따로 unlink 하지 않는다.
"""

import base64
import binascii
import json
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


# 보드 전체 목록 — 결과물 갤러리가 쓴다. 상한을 넘기면 400 이 아니라 상한으로 자른다.
BOARD_ATTACHMENTS_LIMIT_DEFAULT = 50
BOARD_ATTACHMENTS_LIMIT_MAX = 200
_CURSOR_VERSION = 1


def _encode_cursor(slug: str, created_at, att_id) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "b": slug, "t": created_at, "i": att_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(token: str, slug: str) -> tuple:
    """`(created_at, id)`. 모양이 틀리거나 **다른 보드의 커서**면 400 `unknown_cursor` — 사건 스트림과 같은 코드다.
    다른 보드의 위치로 이 보드를 자르면 조용히 앞부분이 빠진다."""
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if data["v"] != _CURSOR_VERSION or data["b"] != slug:
            raise ValueError
        return data["t"], int(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise RequestError(400, "unknown_cursor", "call again without a cursor")


def _limit(request) -> int:
    raw = request.query.get("limit")
    if raw is None or raw == "":
        return BOARD_ATTACHMENTS_LIMIT_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(400, "invalid_query", f"limit must be an integer: {raw!r}")
    if value < 1:
        raise RequestError(400, "invalid_query", f"limit must be 1 or greater: {value}")
    return min(value, BOARD_ATTACHMENTS_LIMIT_MAX)


def _sort_key_of(created_at, att_id) -> tuple:
    # `created_at` 이 없는 행은 가장 오래된 것으로 친다 — 정렬이 None 비교로 깨지지 않게.
    return (created_at if created_at is not None else -1, int(att_id))


def _sort_key(att) -> tuple:
    return _sort_key_of(att.created_at, att.id)


def list_board_attachments_handler(api):
    """보드 전체의 카드 첨부를 최신순(`created_at` 내림차순, 같으면 `id` 내림차순)으로 준다.

    끝난 카드·보관한 카드의 첨부도 빼지 않는다. 칸반 워커의 `scratch` 워크스페이스는 카드가 끝나면
    지워지므로, 워커가 만든 파일 중 남는 것이 첨부뿐이다 — 빼면 갤러리가 보여 줄 것이 없어진다.

    Hermes 에는 카드 단위 목록(`list_attachments`)만 있어서 한 연결 안에서 카드마다 부른다. 보드 DB 를
    직접 조회하지 않는 이유: 표 모양에 묶이지 않고 Hermes 가 쓰는 판정(삭제된 카드 등)을 그대로 따른다.
    요소는 `attachment_payload` 의 상위집합이다 — 카드별 목록·상세·업로드와 같은 모양에 `task_id`·
    `task_title` 만 더한다(갤러리가 어느 카드인지 보이고 그 카드로 가려면 필요하다).
    """

    @guarded
    async def handler(request):
        slug = parse_board_slug(request)
        limit = _limit(request)
        token = request.query.get("cursor")
        after = _sort_key_of(*_decode_cursor(token, slug)) if token else None

        def work():
            with open_board(api, slug) as conn:
                rows = []
                for task in api.list_tasks(conn, include_archived=True):
                    for att in api.list_attachments(conn, task.id):
                        rows.append((att, task))
            rows.sort(key=lambda pair: _sort_key(pair[0]), reverse=True)
            if after is not None:
                rows = [pair for pair in rows if _sort_key(pair[0]) < after]
            page, rest = rows[:limit], rows[limit:]
            out = [{**attachment_payload(att), "task_id": task.id, "task_title": task.title} for att, task in page]
            last = page[-1][0] if page else None
            cursor = _encode_cursor(slug, last.created_at, int(last.id)) if rest and last is not None else None
            return {"attachments": out, "next_cursor": cursor}

        return web.json_response(await run_blocking(work))

    return handler


async def _read_file_part(request, max_bytes: int):
    """multipart 에서 `file` 파트를 찾아 `(filename, content_type, data)` 를 돌려준다.

    상한을 넘는 순간 읽기를 멈춘다 — 디스크가 아니라 메모리에 쌓는 동안에도 한 요청이 상한의
    몇 배를 붙들고 있어선 안 된다. 넘으면 `None` 을 돌려주고 호출자가 413 을 낸다.
    """
    if not (request.content_type or "").startswith("multipart/"):
        raise RequestError(400, "invalid_body", "a `file` part in multipart/form-data is required")
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
    raise RequestError(400, "missing_file", "the multipart body has no `file` part")


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
        raise RequestError(400, "invalid_tail", "tail must be 1 or greater")
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
    "list_board_attachments_handler",
    "upload_attachment_handler",
    "download_attachment_handler",
    "delete_attachment_handler",
    "worker_log_handler",
]
