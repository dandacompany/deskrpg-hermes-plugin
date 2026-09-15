"""첨부·워커 로그 라우트.

- `GET|POST /deskrpg/kanban/tasks/{id}/attachments?board=`
- `GET|DELETE /deskrpg/kanban/attachments/{id}?board=`
- `GET /deskrpg/kanban/tasks/{id}/log?board=&tail=`
"""

import pytest
from aiohttp import FormData, web

from deskrpg_plugin import kanban_files
from deskrpg_plugin.auth import Scope, require_auth
from deskrpg_plugin.contract_fields import KANBAN_ATTACHMENT_REQUIRED, WORKER_LOG_KEYS
from tests.conftest import FakeAdapter
from tests.fakes_kanban_actions import install_fake_kanban_actions

BOARD = "deskrpg-abc"


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


def _client(aiohttp_client, fake_api, *, authorized=True):
    app = web.Application()
    adapter = FakeAdapter(authorized=authorized)
    wrap = lambda h: require_auth(adapter, Scope.DEFAULT, h)  # noqa: E731
    app.router.add_route("GET", "/deskrpg/kanban/tasks/{id}/attachments", wrap(kanban_files.list_attachments_handler(fake_api)))
    app.router.add_route("POST", "/deskrpg/kanban/tasks/{id}/attachments", wrap(kanban_files.upload_attachment_handler(fake_api)))
    app.router.add_route("GET", "/deskrpg/kanban/attachments/{id}", wrap(kanban_files.download_attachment_handler(fake_api)))
    app.router.add_route("DELETE", "/deskrpg/kanban/attachments/{id}", wrap(kanban_files.delete_attachment_handler(fake_api)))
    app.router.add_route("GET", "/deskrpg/kanban/tasks/{id}/log", wrap(kanban_files.worker_log_handler(fake_api)))
    return aiohttp_client(app)


def _conn(kanban):
    return kanban.connect(board=BOARD)


def _task(kanban, **kw):
    """카드를 만들고 **객체**를 돌려준다. Hermes(와 base 가짜)의 `create_task` 는 id 문자열을 준다."""
    kw.setdefault("title", "카드")
    conn = _conn(kanban)
    return kanban.get_task(conn, kanban.create_task(conn, **kw))


def _form(data: bytes, *, filename="보고서.txt", content_type="text/plain", field="file"):
    form = FormData(quote_fields=False)
    form.add_field(field, data, filename=filename, content_type=content_type)
    return form


async def _upload(client, task_id, data, **kw):
    headers = kw.pop("headers", None)
    return await client.post(f"/deskrpg/kanban/tasks/{task_id}/attachments?board={BOARD}", data=_form(data, **kw), headers=headers)


# ---------------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------------


async def test_인증_없으면_401(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api, authorized=False)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/attachments?board={BOARD}")
    assert resp.status == 401


async def test_업로드하면_201_과_attachment(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, task.id, b"hello", headers={"X-DeskRPG-Actor": "dante"})
    body = await resp.json()
    assert resp.status == 201, body
    att = body["attachment"]
    assert KANBAN_ATTACHMENT_REQUIRED <= set(att)
    assert att["filename"] == "보고서.txt" and att["size"] == 5
    # Hermes 단일 쓰기 경로에 보드·상한·업로더가 그대로 전달된다.
    call = kanban.calls["store_attachment_bytes"][-1]
    assert call["board"] == BOARD and call["uploaded_by"] == "deskrpg:dante" and call["content_type"] == "text/plain"
    assert call["max_bytes"] == min(fake_api.MAX_REQUEST_BYTES, fake_api.KANBAN_ATTACHMENT_MAX_BYTES)
    # blob 은 그 보드의 attachments_root 아래에 있다
    stored = kanban.get_attachment(_conn(kanban), att["id"]).stored_path
    assert str(kanban.attachments_root(BOARD).resolve()) in stored


async def test_업로드_상한을_넘으면_413_attachment_too_large(aiohttp_client, fake_api, kanban):
    fake_api.MAX_REQUEST_BYTES = 10  # 두 상한 중 작은 쪽이 실효 상한이다
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, task.id, b"x" * 11)
    body = await resp.json()
    assert resp.status == 413, body
    assert body["error"] == "attachment_too_large" and body["max_bytes"] == 10
    assert kanban.list_attachments(_conn(kanban), task.id) == []


async def test_Hermes_가_AttachmentTooLarge_를_던져도_413(aiohttp_client, fake_api, kanban):
    task = _task(kanban)

    def store(*a, **k):
        raise fake_api.AttachmentTooLarge("attachment exceeds 25 MB limit")

    fake_api.store_attachment_bytes = store
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, task.id, b"x")
    assert resp.status == 413
    assert (await resp.json())["error"] == "attachment_too_large"


async def test_file_파트가_없으면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, task.id, b"x", field="document")
    body = await resp.json()
    assert resp.status == 400
    assert body["error"] == "missing_file"


async def test_multipart_가_아니면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks/{task.id}/attachments?board={BOARD}", json={"file": "x"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_파일_이름이_쓸모없으면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, task.id, b"x", filename="...")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_filename"


async def test_없는_카드에_올리면_404(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await _upload(client, "t9999", b"x")
    assert resp.status == 404
    assert (await resp.json())["error"] == "task_not_found"


# ---------------------------------------------------------------------------
# 목록·내려받기·삭제
# ---------------------------------------------------------------------------


async def test_목록은_attachments_봉투다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    kanban.store_attachment_bytes(_conn(kanban), task.id, "a.txt", b"a", board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/attachments?board={BOARD}")
    body = await resp.json()
    assert resp.status == 200
    assert set(body) == {"attachments"} and [a["filename"] for a in body["attachments"]] == ["a.txt"]


async def test_내려받기는_바이트와_헤더를_준다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    att_id = kanban.store_attachment_bytes(_conn(kanban), task.id, "보고서.txt", b"\x00binary\xff", content_type="text/plain", board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/{att_id}?board={BOARD}")
    assert resp.status == 200
    assert await resp.read() == b"\x00binary\xff"
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C.txt" in resp.headers["Content-Disposition"]


async def test_내려받기_content_type_이_없으면_octet_stream(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    att_id = kanban.store_attachment_bytes(_conn(kanban), task.id, "blob", b"x", board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/{att_id}?board={BOARD}")
    assert resp.headers["Content-Type"] == "application/octet-stream"


async def test_저장_경로가_attachments_root_밖이면_404(aiohttp_client, fake_api, kanban, tmp_path):
    # 변조된 DB 행 — 경로가 보드의 첨부 루트 밖을 가리킨다. 바이트를 내주면 임의 파일 읽기다.
    task = _task(kanban)
    secret = tmp_path / "secret.txt"
    secret.write_text("비밀", encoding="utf-8")
    att_id = kanban.store_attachment_bytes(_conn(kanban), task.id, "a.txt", b"a", board=BOARD)
    kanban.get_attachment(_conn(kanban), att_id).stored_path = str(secret)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/{att_id}?board={BOARD}")
    assert resp.status == 404
    assert (await resp.json())["error"] == "attachment_unavailable"


async def test_blob_이_디스크에_없으면_404(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    att_id = kanban.store_attachment_bytes(_conn(kanban), task.id, "a.txt", b"a", board=BOARD)
    import os

    os.unlink(kanban.get_attachment(_conn(kanban), att_id).stored_path)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/{att_id}?board={BOARD}")
    assert resp.status == 404
    assert (await resp.json())["error"] == "attachment_missing"


async def test_없는_첨부는_404(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/999?board={BOARD}")
    assert resp.status == 404
    resp = await client.delete(f"/deskrpg/kanban/attachments/999?board={BOARD}")
    assert resp.status == 404


async def test_첨부_id_가_정수가_아니면_400(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/attachments/abc?board={BOARD}")
    assert resp.status == 400


async def test_삭제하면_ok_이고_blob_도_사라진다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    conn = _conn(kanban)
    att_id = kanban.store_attachment_bytes(conn, task.id, "a.txt", b"a", board=BOARD)
    stored = kanban.get_attachment(conn, att_id).stored_path
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete(f"/deskrpg/kanban/attachments/{att_id}?board={BOARD}")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    assert kanban.get_attachment(conn, att_id) is None
    import os

    assert not os.path.exists(stored)


# ---------------------------------------------------------------------------
# 워커 로그
# ---------------------------------------------------------------------------


async def test_로그가_없으면_exists_False(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}")
    body = await resp.json()
    assert resp.status == 200
    assert body == {"exists": False, "size_bytes": 0, "content": "", "truncated": False}


async def test_로그_전체가_tail_안에_들면_그대로(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    kanban.write_worker_log(task.id, "한 줄\n두 줄\n", board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}")
    body = await resp.json()
    assert set(body) == WORKER_LOG_KEYS
    assert body["exists"] is True and body["truncated"] is False
    assert body["content"] == "한 줄\n두 줄\n"
    assert body["size_bytes"] == len("한 줄\n두 줄\n".encode("utf-8"))


async def test_tail_보다_크면_잘리고_truncated(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    text = "".join(f"line {i:04d}\n" for i in range(100))
    kanban.write_worker_log(task.id, text, board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}&tail=50")
    body = await resp.json()
    assert body["truncated"] is True and body["size_bytes"] == len(text)
    assert body["content"].endswith("line 0099\n") and len(body["content"].encode()) <= 50
    assert body["content"].startswith("line ")  # 첫 잘린 줄은 건너뛴다


async def test_tail_기본값과_상한(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    assert kanban_files.DEFAULT_LOG_TAIL_BYTES == 16384
    assert kanban_files.MAX_LOG_TAIL_BYTES == 1024 * 1024
    kanban.write_worker_log(task.id, "x" * 20000, board=BOARD)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}")
    body = await resp.json()
    assert body["truncated"] is True and len(body["content"]) == 16384
    # 상한을 넘겨 요청하면 상한으로 조여진다(거절하지 않는다).
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}&tail=99999999")
    assert resp.status == 200 and (await resp.json())["truncated"] is False


async def test_tail_이_양의_정수가_아니면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    for bad in ("0", "-1", "abc"):
        resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/log?board={BOARD}&tail={bad}")
        assert resp.status == 400, bad


async def test_로그는_없는_카드면_404(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/t9999/log?board={BOARD}")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 단일 직렬화기 · 500
# ---------------------------------------------------------------------------


async def test_첨부_직렬화는_상세_목록_업로드가_같은_모양이고_계약_키의_상위집합이다(aiohttp_client, fake_api, kanban):
    from deskrpg_plugin.contract_fields import KANBAN_ATTACHMENT_KEYS
    from deskrpg_plugin.kanban_common import attachment_payload

    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    uploaded = (await (await _upload(client, task.id, b"hello")).json())["attachment"]
    listed = (await (await client.get(f"/deskrpg/kanban/tasks/{task.id}/attachments?board={BOARD}")).json())["attachments"]
    direct = attachment_payload(kanban.get_attachment(_conn(kanban), uploaded["id"]))
    assert set(uploaded) == set(listed[0]) == set(direct) == {"id", "filename", "size", "content_type", "created_at"}
    assert KANBAN_ATTACHMENT_KEYS <= set(uploaded)
    assert uploaded == listed[0] == direct
    assert uploaded["content_type"] == "text/plain" and isinstance(uploaded["created_at"], int)


async def test_Hermes_가_예상_못_한_예외를_던지면_500_internal_error_JSON(aiohttp_client, fake_api, kanban):
    task = _task(kanban)

    def boom(*a, **kw):
        raise RuntimeError("secret stored_path")

    fake_api.list_attachments = boom
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{task.id}/attachments?board={BOARD}")
    assert resp.status == 500
    body = await resp.json()
    assert body == {"error": "internal_error", "detail": "RuntimeError"}
    assert "secret" not in await resp.text()
