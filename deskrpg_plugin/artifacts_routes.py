"""`/deskrpg/artifacts/*` — 조회·바이트·사람 편집·삭제. 전부 소유자 키(호스트 공유 자원).

바이트는 `stored_path` 가 `artifacts_root` 아래인지 **다시** 확인하고 낸다(`kanban_files.py` 와 같은 방어).
Range 는 단일 범위만 받는다 — 미디어 탐색에 충분하고, 다중 범위는 multipart 응답이 필요해 v1 범위 밖이다.

`content_handler` 의 파일 열기·seek·읽기는 이벤트 루프 스레드에서 돌지 않는다 — sqlite 조회와 마찬가지로
`run_blocking` 으로 워커 스레드에 보낸다. 스트리밍 자체는 유지한다(파일 전체를 메모리에 올리지 않는다).
"""

import asyncio
import base64
import contextlib
import json
import time
import urllib.parse

from aiohttp import web

from . import artifacts_links as links
from . import artifacts_policy as policy
from . import artifacts_store as store
from .artifacts_tool import artifact_upload_max_bytes
from .common import RequestError, guarded, json_error, log_event, read_json_object, run_blocking
from .contract_fields import ARTIFACT_SUMMARY_KEYS, ARTIFACT_VERSION_KEYS
from .kanban_common import project

USER_HEADER = "X-DeskRPG-User"
LIMIT_DEFAULT, LIMIT_MAX = 50, 200
UPLOAD_CHUNK = 1024 * 1024
NOTE_READ_CHUNK = 4096
_ALLOWED_EDIT_KEYS = {"content", "filename", "note"}


class _TooLarge(Exception):
    """`_read_edit_body`/`store_artifact_version` 이 상한 초과를 알리는 내부 신호.

    `RequestError` 를 쓰지 않는다 — `guarded` 의 기본 413 모양(`{error, detail}`)에는
    `max_bytes` 가 없고, 스펙 ⑤(413 + max_bytes)를 만족하려면 핸들러가 직접
    `_too_large()` 로 응답을 만들어야 하기 때문이다.
    """


def _too_large(limit: int) -> web.Response:
    return json_error(413, "artifact_too_large", str(limit), max_bytes=limit)


def _user(request) -> str:
    """`X-DeskRPG-User` → `human:<이름>`. 인쇄 불가 문자(제어문자·외톨이 서로게이트 포함)를 지우고 128자로 자른다.

    이 값은 `created_by`/`deleted_by` 로 sqlite 에 들어가고 JSON 으로 다시 나간다 — 외톨이 서로게이트가
    남으면 UTF-8 인코딩에서 터진다.
    """
    raw = "".join(ch for ch in (request.headers.get(USER_HEADER) or "") if ch.isprintable())
    raw = raw.encode("utf-8", "replace").decode("utf-8").strip()[:128]
    return f"human:{raw or 'unknown'}"


def _is_ascii_digits(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


def _summary(api, row, versions) -> dict:
    head = versions[-1] if versions else None
    d = {k: row[k] for k in row.keys() if k not in ("title_norm", "deleted_at", "deleted_by")}
    if head is not None:
        d.update(filename=head["filename"], mime=head["mime"], size=head["size"], sha256=head["sha256"])
        path = store.blob_path_for(api, head)
        if path is None or not path.is_file():
            d["missing"] = True
    return project(d, ARTIFACT_SUMMARY_KEYS)


def _version(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    if d.get("pruned_at") is None:
        d.pop("pruned_at", None)  # 정리된 버전에만 싣는다
    return project(d, ARTIFACT_VERSION_KEYS)


def _live(conn, artifact_id: str):
    row = store.get_artifact(conn, artifact_id)
    if row is None:
        raise RequestError(404, "artifact_not_found", artifact_id)
    if row["deleted_at"] is not None:
        raise RequestError(410, "artifact_deleted", artifact_id)
    return row


def _cursor_encode(row) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["updated_at"], row["id"]]).encode()).decode().rstrip("=")


def _cursor_decode(token):
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not (isinstance(value, list) and len(value) == 2):
            raise ValueError("cursor shape")
        return (int(value[0]), str(value[1]))
    except (ValueError, TypeError, IndexError, KeyError):
        raise RequestError(400, "unknown_cursor")


def list_handler(api):
    @guarded
    async def handler(request):
        q = request.query
        raw_limit = q.get("limit")
        limit = max(1, min(int(raw_limit), LIMIT_MAX)) if _is_ascii_digits(raw_limit) else LIMIT_DEFAULT
        kind_raw, source = q.get("kind") or None, q.get("source") or None
        kind = None
        if kind_raw:
            tokens = [t for t in kind_raw.split(",") if t]
            for token in tokens:
                if token not in policy.KINDS:
                    raise RequestError(400, "artifact_bad_kind", token)
            kind = tokens[0] if len(tokens) == 1 else tokens
        if source and source not in ("chat", "kanban", "cron"):
            raise RequestError(400, "invalid_field", "source")
        profiles = [p for p in (q.get("profiles") or "").split(",") if p] or None
        before = _cursor_decode(q.get("cursor"))
        task_id = q.get("task_id") or None
        if task_id is not None and len(task_id) > 128:
            raise RequestError(400, "invalid_field", "task_id")

        def work():
            with contextlib.closing(store.open_registry(api)) as conn:
                rows = store.list_artifacts(conn, profiles=profiles, board=q.get("board") or None, kind=kind,
                                            source=source, task_id=task_id, q=q.get("q") or None, before=before, limit=limit + 1)
                page, has_more = rows[:limit], len(rows) > limit
                items = [_summary(api, r, store.list_versions(conn, r["id"])) for r in page]
                cursor = _cursor_encode(page[-1]) if page else (q.get("cursor") or "")
                return {"artifacts": items, "cursor": cursor, "has_more": has_more}

        return web.json_response(await run_blocking(work))

    return handler


def get_handler(api):
    @guarded
    async def handler(request):
        artifact_id = request.match_info["artifact_id"]

        def work():
            with contextlib.closing(store.open_registry(api)) as conn:
                row = _live(conn, artifact_id)
                versions = store.list_versions(conn, artifact_id)
                return {"artifact": _summary(api, row, versions), "versions": [_version(v) for v in versions]}

        return web.json_response(await run_blocking(work))

    return handler


def _disposition(kind: str, filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_") or "artifact"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"


def _parse_range(header: str, size: int):
    """`bytes=a-b` / `bytes=a-` / `bytes=-n` 단일 범위. 못 만족하면 416."""
    if not header.startswith("bytes="):
        raise RequestError(416, "range_not_satisfiable")
    spec = header[6:]
    if "," in spec:
        raise RequestError(416, "range_not_satisfiable", "단일 범위만 받는다")
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            n = int(end_s)
            start, end = max(size - n, 0), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        raise RequestError(416, "range_not_satisfiable")
    if start >= size or end < start:
        raise RequestError(416, "range_not_satisfiable")
    return start, min(end, size - 1)


def _open_and_seek(path, start: int):
    """`path.open("rb")` + `seek` — 워커 스레드에서만 부른다(R14: 이벤트 루프에 블로킹 금지)."""
    fh = path.open("rb")
    fh.seek(start)
    return fh


async def _open_blob(path, start: int):
    """`_open_and_seek` 을 워커 스레드에서. 기다리는 쪽이 취소돼도 이미 열린(또는 곧 열릴) 핸들을 닫는다.

    `to_thread` 는 취소돼도 스레드가 끝까지 돌므로, 그대로 두면 열린 핸들이 주인 없이 남는다.
    """
    task = asyncio.ensure_future(run_blocking(_open_and_seek, path, start))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def _close_orphan(done):
            if not done.cancelled() and done.exception() is None:
                asyncio.ensure_future(run_blocking(done.result().close))
        task.add_done_callback(_close_orphan)
        raise


def content_handler(api):
    @guarded
    async def handler(request):
        artifact_id, v = request.match_info["artifact_id"], request.match_info["v"]
        if not _is_ascii_digits(v):
            raise RequestError(400, "invalid_field", "v")

        def work():
            with contextlib.closing(store.open_registry(api)) as conn:
                _live(conn, artifact_id)
                row = conn.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?",
                                   (artifact_id, int(v))).fetchone()
                if row is None:
                    raise RequestError(404, "artifact_version_not_found", v)
                if row["pruned_at"] is not None:
                    raise RequestError(404, "artifact_version_pruned", v)
                path = store.blob_path_for(api, row)
                if path is None:
                    raise RequestError(403, "artifact_path_outside_root", artifact_id)
                if not path.is_file():
                    raise RequestError(410, "artifact_blob_missing", artifact_id)
                return row, path, path.stat().st_size

        row, path, size = await run_blocking(work)
        disposition = "attachment" if request.query.get("download") == "1" else "inline"
        headers = {
            "Content-Disposition": _disposition(disposition, row["filename"]),
            "Accept-Ranges": "bytes",
            # DeskRPG 는 HTML/SVG 아티팩트를 iframe srcdoc 으로 텍스트째 렌더링하고, 이 URL 로
            # 직접 이동하지 않는다. 그래도 채널 멤버가 이 URL 을 브라우저에 직접 열면 저장된
            # mime(text/html·image/svg+xml …)이 이 오리진의 쿠키를 쥔 채 스크립트를 실행할 수
            # 있다 — sandbox 로 불투명 오리진·스크립트 금지를 강제하고, nosniff 로 브라우저의
            # content-type 추측을 막는다. inline/attachment 여부와 무관하게 항상 붙인다.
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        }
        rng = request.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng:
            start, end = _parse_range(rng, size)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        # 응답을 시작하기 **전에** 연다 — stat 과 open 사이에 blob 이 사라졌으면 아직 410 JSON 을 보낼 수 있다.
        # prepare() 뒤에는 상태 줄이 이미 나갔으므로 @guarded 가 두 번째 응답을 만들면 안 된다.
        try:
            fh = await _open_blob(path, start)
        except FileNotFoundError:
            raise RequestError(410, "artifact_blob_missing", artifact_id)
        try:
            resp = web.StreamResponse(status=status, headers=headers)
            resp.content_type = row["mime"] or "application/octet-stream"
            resp.content_length = end - start + 1
            await resp.prepare(request)
            try:
                remaining = end - start + 1
                while remaining > 0:
                    chunk = await run_blocking(fh.read, min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    await resp.write(chunk)
                    remaining -= len(chunk)
                await resp.write_eof()
            except ConnectionError as exc:
                # 클라이언트가 끊었다(ConnectionResetError·BrokenPipeError, aiohttp 의 ClientConnectionResetError 도 그 하위).
                # 헤더는 이미 나갔으니 조용히 끝낸다.
                log_event("artifact.content_aborted", artifact_id=artifact_id, error=type(exc).__name__)
                resp.force_close()  # 반쯤 쓴 연결을 재사용하지 않는다
            return resp
        finally:
            # 취소(CancelledError)로 빠져나가도 닫히도록 shield — 바깥 await 가 취소돼도 닫기 작업은 끝까지 돈다.
            await asyncio.shield(run_blocking(fh.close))

    return handler


async def _read_edit_body(request, limit: int):
    """JSON `{content, filename, note}` 또는 multipart `file`+`note` → (data, filename, note).

    `limit` 을 넘는 순간 읽기를 멈추고 `_TooLarge` 를 던진다 — `kanban_files._read_file_part`
    와 같은 방어다. aiohttp 의 `client_max_size` 는 `request.multipart()` 스트림에는
    적용되지 않으므로, 여기서 직접 끊지 않으면 채널 멤버가(DeskRPG 프록시를 거쳐) 게이트웨이에
    임의 크기의 메모리를 물릴 수 있다. `note` 파트도 `part.text()`(무제한)가 아니라
    `read_chunk` 로 한 번만, 작은 상한(4 KiB)까지만 읽는다.
    """
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        data, filename, note = None, None, None
        async for part in reader:
            if part.name == "file":
                if data is not None:
                    raise RequestError(400, "invalid_field", "file")
                filename = part.filename or "artifact"
                buf = bytearray()
                total = 0
                while True:
                    chunk = await part.read_chunk(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise _TooLarge()
                    buf.extend(chunk)
                data = bytes(buf)
            elif part.name == "note":
                chunk = await part.read_chunk(NOTE_READ_CHUNK)
                note = chunk.decode("utf-8", "replace")[:400] if chunk else None
        if data is None:
            raise RequestError(400, "missing_field", "file")
        return data, filename, note
    body = await read_json_object(request)
    unknown = set(body) - _ALLOWED_EDIT_KEYS
    if unknown:
        raise RequestError(400, "unsupported_field", sorted(unknown)[0])
    content, filename = body.get("content"), body.get("filename")
    if not isinstance(content, str) or not isinstance(filename, str) or not filename.strip():
        raise RequestError(400, "missing_field", "content, filename")
    note = body.get("note")
    data = content.encode("utf-8")
    # JSON 본문은 aiohttp 의 요청 크기 상한(MAX_REQUEST_BYTES)으로 이미 유계다 — 여기선
    # 아티팩트 상한(더 작을 수 있다)만 다시 확인하면 된다. 레지스트리에 닿기 전에 끊는다.
    if len(data) > limit:
        raise _TooLarge()
    return data, filename.strip(), (note[:400] if isinstance(note, str) else None)


def add_version_handler(api):
    @guarded
    async def handler(request):
        artifact_id = request.match_info["artifact_id"]
        limit = artifact_upload_max_bytes(api)  # 본문을 읽기 전에 상한부터 구한다 — 끊을 기준이 먼저다.
        try:
            data, filename, note = await _read_edit_body(request, limit)
        except _TooLarge:
            return _too_large(limit)
        user = _user(request)

        def work():
            with contextlib.closing(store.open_registry(api)) as conn:
                row = _live(conn, artifact_id)
                body, name, mime = data, filename, policy.mime_for_filename(filename)
                summary, identity = row["summary"] or "", None
                if row["kind"] == "link":
                    # 링크 편집은 URL 한 줄만 받는다 — 저장본은 정리한 URL. 파일명은 기존 버전 것을 잇는다.
                    url = links.canonical_url(data.decode("utf-8", "replace").strip())
                    if url is None:
                        raise RequestError(422, "artifact_incomplete", "link 는 http(s) 주소 한 줄이어야 한다")
                    head = store.list_versions(conn, artifact_id)[-1]
                    body, name, mime = links.url_blob(url), head["filename"], links.LINK_MIME
                    # 정체성·요약은 새 URL 로 옮기고 제목은 사람이 보던 그대로 둔다(store 의 UPDATE 규칙).
                    summary, identity = url, url
                meta = store.ArtifactMeta(
                    kind=row["kind"], title=row["title"], summary=summary, filename=name,
                    mime=mime, profile=row["profile"], source_kind=row["source_kind"],
                    session_id=row["session_id"], created_by=user, captured_via="edit", board=row["board"],
                    task_id=row["task_id"], job_id=row["job_id"], run_id=row["run_id"], note=note, supersedes=artifact_id,
                    identity=identity,
                )
                try:
                    out = store.store_artifact_version(api, conn, meta=meta, data=body, max_bytes=limit)
                except store.ArtifactTooLarge:
                    raise _TooLarge()
                version = conn.execute("SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?",
                                       (out.artifact_id, out.version)).fetchone()
                return _version(version)

        try:
            version = await run_blocking(work)
        except _TooLarge:
            return _too_large(limit)
        log_event("artifact.edit", artifact_id=artifact_id, version=version["version"])
        return web.json_response({"version": version}, status=201)

    return handler


def rework_handler(api):
    @guarded
    async def handler(request):
        # 3번 스펙(수정 루프)에서 구현한다. 라우트는 계약 자리를 잡아 두기 위해 지금 둔다(결정 0005 의 견적 501 과 같은 이유).
        raise RequestError(501, "not_implemented", "rework 는 아직 구현되지 않았다")

    return handler


def delete_handler(api):
    @guarded
    async def handler(request):
        artifact_id = request.match_info["artifact_id"]
        user = _user(request)

        def work():
            with contextlib.closing(store.open_registry(api)) as conn:
                _live(conn, artifact_id)
                return store.soft_delete(api, conn, artifact_id, deleted_by=user, now=int(time.time()))

        failed = await run_blocking(work)
        log_event("artifact.delete", artifact_id=artifact_id, failed_paths=len(failed))
        if failed:
            def note():
                with contextlib.closing(store.open_registry(api)) as conn, conn:
                    store._append_event(conn, int(time.time()), "artifact.delete_partial",
                                        {"artifact_id": artifact_id, "failed_paths_count": len(failed)})
            await run_blocking(note)
        return web.json_response({"ok": True})

    return handler
