"""칸반 동작 라우트 — `POST /deskrpg/kanban/tasks/{id}/<action>?board=`.

라우트 테이블(`routes.py`)은 다른 태스크 몫이라 여기서는 핸들러를 직접 붙인다. 인증 래퍼는
실제와 같이 `require_auth(FakeAdapter, Scope.DEFAULT, …)` 를 거친다.
"""

import types

import pytest
from aiohttp import web

from deskrpg_plugin import kanban_actions
from deskrpg_plugin.auth import Scope, require_auth
from deskrpg_plugin.contract_fields import KANBAN_TASK_ACTIONS, KANBAN_TASK_FULL_KEYS
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
    for action in KANBAN_TASK_ACTIONS:
        app.router.add_route(
            "POST",
            f"/deskrpg/kanban/tasks/{{id}}/{action}",
            require_auth(adapter, Scope.DEFAULT, kanban_actions.action_handler(fake_api, action)),
        )
    return aiohttp_client(app)


def _conn(kanban):
    return kanban.connect(board=BOARD)


def _task(kanban, **kw):
    """카드를 만들고 **객체**를 돌려준다. Hermes(와 base 가짜)의 `create_task` 는 id 문자열을 준다."""
    kw.setdefault("title", "카드")
    conn = _conn(kanban)
    return kanban.get_task(conn, kanban.create_task(conn, **kw))


def _called(kanban, name, **kw):
    """base 가짜의 호출 기록(`calls[name]`)에 kwargs 가 부분 일치하는 항목이 있는가."""
    return any(all(rec.get(k) == v for k, v in kw.items()) for rec in kanban.calls.get(name, []))


async def _post(client, task_id, action, body=None, *, board=BOARD, headers=None):
    return await client.post(
        f"/deskrpg/kanban/tasks/{task_id}/{action}?board={board}", json=body if body is not None else {},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


async def test_인증_없으면_401(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api, authorized=False)
    resp = await _post(client, task.id, "reclaim")
    assert resp.status == 401


async def test_board_가_없으면_400(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/kanban/tasks/t0001/archive", json={})
    assert resp.status == 400
    assert (await resp.json())["error"] == "board_required"


async def test_모르는_보드면_404(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, "t0001", "archive", board="nope")
    assert resp.status == 404
    assert (await resp.json())["error"] == "board_not_found"


@pytest.mark.parametrize("action", [a for a in KANBAN_TASK_ACTIONS if a != "estimate"])
async def test_카드가_없으면_404(aiohttp_client, fake_api, kanban, action):
    client = await _client(aiohttp_client, fake_api)
    body = {"profile": "default"} if action == "reassign" else {"comment": "x"} if action == "request-changes" else {}
    resp = await _post(client, "t9999", action, body)
    assert resp.status == 404, await resp.text()
    assert (await resp.json())["error"] == "task_not_found"


def test_모르는_동작_이름은_구성_시점에_거절된다(fake_api):
    with pytest.raises(ValueError):
        kanban_actions.action_handler(fake_api, "explode")


async def test_응답의_task_는_계약_키_밖으로_새지_않는다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "archive")
    body = await resp.json()
    assert resp.status == 200
    assert set(body) == {"task"}
    assert set(body["task"]) <= KANBAN_TASK_FULL_KEYS
    assert body["task"]["id"] == task.id and body["task"]["status"] == "archived"


# ---------------------------------------------------------------------------
# reassign / reclaim
# ---------------------------------------------------------------------------


async def test_reassign_은_프로필을_바꾼다(aiohttp_client, fake_api, kanban):
    fake_api.create_profile("mia")
    task = _task(kanban, assignee="sophie")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {"profile": "mia"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["assignee"] == "mia"
    assert _called(kanban, "reassign_task", profile="mia", reclaim_first=False, reason=None)


async def test_reassign_은_없는_프로필이면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {"profile": "ghost"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "profile_not_found"


async def test_reassign_은_profile_이_없으면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {})
    assert resp.status == 400
    assert (await resp.json())["error"] == "missing_field"


async def test_reassign_은_실행_중이면_409_invalid_transition(aiohttp_client, fake_api, kanban):
    fake_api.create_profile("mia")
    task = _task(kanban, assignee="sophie")
    kanban.start_run(_conn(kanban), task.id)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {"profile": "mia"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


async def test_reassign_은_reclaim_first_로_실행을_되찾은_뒤_바꾼다(aiohttp_client, fake_api, kanban):
    fake_api.create_profile("mia")
    task = _task(kanban, assignee="sophie")
    kanban.start_run(_conn(kanban), task.id)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {"profile": "mia", "reclaim_first": True})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["assignee"] == "mia" and body["task"]["status"] == "ready"


async def test_reassign_의_reclaim_first_는_불리언이어야_한다(aiohttp_client, fake_api, kanban):
    fake_api.create_profile("mia")
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reassign", {"profile": "mia", "reclaim_first": "yes"})
    assert resp.status == 400


async def test_reclaim_은_고정_사유로_되찾는다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    kanban.start_run(_conn(kanban), task.id)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reclaim")
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["status"] == "ready"
    assert _called(kanban, "reclaim_task", reason="reclaimed by deskrpg")


async def test_reclaim_은_실행_중이_아니면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "reclaim")
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


# ---------------------------------------------------------------------------
# specify / decompose / estimate
# ---------------------------------------------------------------------------


def _outcome(**kw):
    return types.SimpleNamespace(**kw)


async def test_specify_는_보드를_고정한_채_Hermes_를_부르고_task_와_outcome_을_준다(aiohttp_client, fake_api, kanban):
    task = _task(kanban, triage=True)
    seen = {}

    def specify_task(task_id, *, author=None, timeout=None):
        seen.update(task_id=task_id, author=author, timeout=timeout, board=kanban.get_current_board())
        t = kanban.get_task(_conn(kanban), task_id)
        t.title, t.status = "명세된 제목", "todo"
        return _outcome(task_id=task_id, ok=True, reason="specified", new_title="명세된 제목")

    fake_api.specify_task = specify_task
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "specify", headers={"X-DeskRPG-Actor": "dante"})
    body = await resp.json()
    assert resp.status == 200, body
    assert seen == {
        "task_id": task.id, "author": "deskrpg:dante",
        "timeout": kanban_actions.SPECIFY_TIMEOUT_SECONDS, "board": BOARD,
    }
    assert kanban.get_current_board() == "default"  # 스코프가 복원됐다
    assert body["task"]["title"] == "명세된 제목"
    assert body["outcome"] == {"changed_fields": ["title", "body", "status"]}


async def test_specify_가_ok_False_면_409_llm_refused(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    fake_api.specify_task = lambda task_id, **kw: _outcome(task_id=task_id, ok=False, reason="task is not in triage")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "specify")
    body = await resp.json()
    assert resp.status == 409
    assert body == {"error": "llm_refused", "detail": "task is not in triage"}


async def test_specify_의_reason_이_timeout_이면_504_llm_timeout(aiohttp_client, fake_api, kanban):
    task = _task(kanban, triage=True)
    fake_api.specify_task = lambda task_id, **kw: _outcome(task_id=task_id, ok=False, reason="aux call timed out after 120s")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "specify")
    body = await resp.json()
    assert resp.status == 504
    assert body["error"] == "llm_timeout"


async def test_decompose_는_child_ids_를_outcome_에_싣는다(aiohttp_client, fake_api, kanban):
    task = _task(kanban, triage=True)
    seen = {}

    def decompose_task(task_id, *, author=None, timeout=None):
        seen.update(timeout=timeout, author=author)
        c1 = kanban.create_task(_conn(kanban), title="자식1", parents=[task_id])
        c2 = kanban.create_task(_conn(kanban), title="자식2", parents=[task_id])
        return _outcome(task_id=task_id, ok=True, reason="decomposed", fanout=True, child_ids=[c1, c2])

    fake_api.decompose_task = decompose_task
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "decompose")
    body = await resp.json()
    assert resp.status == 200, body
    assert seen == {"timeout": kanban_actions.DECOMPOSE_TIMEOUT_SECONDS, "author": "deskrpg"}
    assert len(body["outcome"]["child_ids"]) == 2


async def test_decompose_가_ok_False_면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    fake_api.decompose_task = lambda task_id, **kw: _outcome(task_id=task_id, ok=False, reason="empty reply", child_ids=None)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "decompose")
    assert resp.status == 409
    assert (await resp.json())["error"] == "llm_refused"


async def test_estimate_는_501(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "estimate")
    assert resp.status == 501
    assert (await resp.json())["error"] == "not_implemented"


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


async def test_approve_는_review_카드를_done_으로(aiohttp_client, fake_api, kanban):
    task = _task(kanban, assignee="sophie")
    kanban.request_review(_conn(kanban), task.id, reviewer="mia")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "approve", {"result": "좋다", "summary": "승인"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["status"] == "done" and body["task"]["result"] == "좋다"
    assert _called(kanban, "complete_task", result="좋다", summary="승인")


async def test_approve_는_ready_카드도_받는다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    task.status = "ready"
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "approve")
    assert resp.status == 200
    assert (await resp.json())["task"]["status"] == "done"


async def test_approve_가_거절되면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban, triage=True)  # triage — Hermes 가 받지 않는 상태
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "approve")
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


async def test_approve_의_result_가_문자열이_아니면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "approve", {"result": 3})
    assert resp.status == 400


# ---------------------------------------------------------------------------
# request-changes — 세 갈래
# ---------------------------------------------------------------------------


async def test_request_changes_는_comment_가_없으면_400(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {})
    assert resp.status == 400
    assert (await resp.json())["error"] == "missing_field"
    assert kanban.list_comments(_conn(kanban), task.id) == []


async def test_request_changes_검토자_run_이_돌면_댓글_후_request_changes(aiohttp_client, fake_api, kanban):
    task = _task(kanban, assignee="sophie")
    conn = _conn(kanban)
    kanban.request_review(conn, task.id, reviewer="mia")
    kanban.start_run(conn, task.id, source_status="review", profile="mia")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {"comment": "테스트가 빠졌다"}, headers={"X-DeskRPG-Actor": "dante"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["outcome"] == "changes_requested"
    assert body["task"]["status"] == "ready" and body["task"]["assignee"] == "sophie"
    comments = kanban.list_comments(conn, task.id)
    assert [(c.author, c.body) for c in comments] == [("deskrpg:dante", "테스트가 빠졌다")]
    # 댓글이 먼저, 전이는 그 다음이다 — 사건 순서로 본다.
    kinds = [e.kind for e in kanban.list_events(conn, task.id)]
    assert kinds.index("commented") < kinds.index("changes_requested")
    assert _called(kanban, "request_changes", reason="테스트가 빠졌다")


async def test_request_changes_에서_Hermes_가_거절하면_409_에_사유(aiohttp_client, fake_api, kanban):
    task = _task(kanban, assignee="sophie")
    conn = _conn(kanban)
    task.status = "review"  # review_requested 사건 없이 review — implementer 출처가 없다
    kanban.start_run(conn, task.id, source_status="review", profile="mia")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {"comment": "다시"})
    body = await resp.json()
    assert resp.status == 409
    assert body["error"] == "invalid_transition"
    assert "implementer" in body["detail"]


async def test_request_changes_활성_run_없이_review_면_reopen(aiohttp_client, fake_api, kanban):
    task = _task(kanban, assignee="sophie")
    conn = _conn(kanban)
    kanban.request_review(conn, task.id, reviewer="mia")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {"comment": "다시 봐 달라"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["outcome"] == "reopened"
    assert body["task"]["status"] == "ready"
    assert _called(kanban, "reopen_review_task")
    assert [c.body for c in kanban.list_comments(conn, task.id)] == ["다시 봐 달라"]


async def test_request_changes_구현자_run_이_돌면_409_implementer_running(aiohttp_client, fake_api, kanban):
    task = _task(kanban, assignee="sophie")
    conn = _conn(kanban)
    kanban.start_run(conn, task.id, source_status="ready")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {"comment": "멈춰"})
    body = await resp.json()
    assert resp.status == 409
    assert body["error"] == "implementer_running"
    assert "request_changes" not in kanban.calls
    # 댓글은 이미 남았다 — 순서가 (1) 댓글 (2) 판정이다.
    assert [c.body for c in kanban.list_comments(conn, task.id)] == ["멈춰"]


async def test_request_changes_run_도_없고_review_도_아니면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "request-changes", {"comment": "?"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


# ---------------------------------------------------------------------------
# unblock / terminate / archive
# ---------------------------------------------------------------------------


async def test_unblock_은_댓글을_먼저_남기고_푼다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    conn = _conn(kanban)
    kanban.block_task(conn, task.id, reason="자격 만료")
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "unblock", {"comment": "키 갱신함"})
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["status"] == "ready"
    assert [c.body for c in kanban.list_comments(conn, task.id)] == ["키 갱신함"]


async def test_unblock_은_댓글_없이도_된다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    conn = _conn(kanban)
    kanban.block_task(conn, task.id)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "unblock")
    assert resp.status == 200
    assert kanban.list_comments(conn, task.id) == []


async def test_unblock_은_막히지_않은_카드면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "unblock")
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


async def test_terminate_는_활성_run_을_되찾는다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    kanban.start_run(_conn(kanban), task.id, worker_pid=4242)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "terminate")
    body = await resp.json()
    assert resp.status == 200, body
    assert body["task"]["status"] == "ready"
    assert _called(kanban, "reclaim_task", reason="terminated by deskrpg")
    assert [pid for pid, _lock in kanban.signals] == [4242]  # 워커에 실제 종료 신호가 갔다


async def test_terminate_는_활성_run_이_없으면_409_no_active_run(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "terminate")
    body = await resp.json()
    assert resp.status == 409
    assert body["error"] == "no_active_run"
    assert "reclaim_task" not in kanban.calls


async def test_archive_는_보관한다(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "archive")
    assert resp.status == 200
    assert (await resp.json())["task"]["status"] == "archived"


async def test_archive_는_이미_보관됐으면_409(aiohttp_client, fake_api, kanban):
    task = _task(kanban)
    task.status = "archived"
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "archive")
    assert resp.status == 409


async def test_Hermes_가_RuntimeError_를_던지면_409_invalid_transition(aiohttp_client, fake_api, kanban):
    task = _task(kanban)

    def boom(conn, task_id):
        raise RuntimeError("still running")

    fake_api.archive_task = boom
    client = await _client(aiohttp_client, fake_api)
    resp = await _post(client, task.id, "archive")
    body = await resp.json()
    assert resp.status == 409
    assert body == {"error": "invalid_transition", "detail": "still running"}
