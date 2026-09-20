"""카드 상세·생성·수정·삭제·댓글·링크(§5.3, §5.5 링크)."""

import json

import pytest

from deskrpg_plugin import contract_fields as cf
from deskrpg_plugin import deleted_log
from tests.kanban_app import make_app

B = "?board=default"


def _client(aiohttp_client, fake_api):
    return aiohttp_client(make_app(fake_api))


def _conn(fake_api):
    return fake_api.kanban.connect(board="default")


def _events(fake_api, task_id):
    return [e.kind for e in fake_api.kanban.list_events(_conn(fake_api), task_id)]


# ---------------------------------------------------------------------------
# 상세
# ---------------------------------------------------------------------------


async def test_카드_상세_모양은_계약과_같다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    parent = db.create_task(conn, title="p")
    t = db.create_task(conn, title="t", parents=[parent], body="본문", assignee="sophie")
    child = db.create_task(conn, title="c", parents=[t])
    db.add_comment(conn, t, "deskrpg:dante", "댓글")
    db.start_run(conn, t, profile="sophie", summary="요약")
    db.add_attachment(conn, t, filename="a.txt", size=3)

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/{t}{B}")
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == cf.KANBAN_TASK_DETAIL_KEYS
    assert cf.KANBAN_TASK_FULL_REQUIRED <= set(body["task"]) <= cf.KANBAN_TASK_FULL_KEYS
    assert body["task"]["latest_summary"] == "요약"
    assert body["task"]["comment_count"] == 1
    assert body["task"]["link_counts"] == {"parents": 1, "children": 1}
    assert body["links"] == {"parents": [parent], "children": [child]}
    assert [set(c) for c in body["comments"]] == [cf.KANBAN_COMMENT_KEYS]
    assert body["comments"][0]["author"] == "deskrpg:dante"
    assert all(set(e) == cf.KANBAN_EVENT_KEYS for e in body["events"])
    # 첨부는 단일 직렬화기(`kanban_common.attachment_payload`) — 계약 키의 상위집합(content_type·created_at 포함)
    assert len(body["attachments"]) == 1 and cf.KANBAN_ATTACHMENT_KEYS <= set(body["attachments"][0])
    assert {"content_type", "created_at"} <= set(body["attachments"][0])
    assert len(body["runs"]) == 1
    assert cf.KANBAN_RUN_REQUIRED <= set(body["runs"][0]) <= cf.KANBAN_RUN_KEYS


async def test_카드_상세는_진단_목록을_싣는다(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="t")

    class _Diag:
        severity = "critical"

        def to_dict(self):
            return {
                "kind": "crash", "severity": "critical", "title": "t", "detail": "d",
                "actions": [{"kind": "reclaim", "label": "되찾기"}], "count": 1, "last_seen_at": 1, "data": {},
            }

    fake_api.compute_task_diagnostics = lambda *a, **k: [_Diag()]
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/tasks/{t}{B}")).json()
    assert body["task"]["warnings"] == {"count": 1, "highest_severity": "critical"}
    assert [set(d) for d in body["task"]["diagnostics"]] == [cf.DIAGNOSTIC_KEYS]


async def test_카드_상세는_없는_카드에_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/tasks/nope{B}")
    assert resp.status == 404
    assert (await resp.json())["error"] == "task_not_found"


# ---------------------------------------------------------------------------
# 생성
# ---------------------------------------------------------------------------


async def test_카드_생성은_201_이고_created_by_는_actor_헤더를_따른다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(
        f"/deskrpg/kanban/tasks{B}",
        json={"title": "일", "body": "본문", "priority": 2, "skills": ["a"], "goal_mode": True},
        headers={"X-DeskRPG-Actor": "dante"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert set(body) <= cf.ENVELOPES["task"] | {"warning"}
    assert "warning" not in body
    task = body["task"]
    assert cf.KANBAN_TASK_FULL_REQUIRED <= set(task) <= cf.KANBAN_TASK_FULL_KEYS
    assert task["created_by"] == "deskrpg:dante"
    assert task["priority"] == 2
    assert task["status"] == "ready"  # 부모 없음 → Hermes 규칙대로 ready

    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "둘"})
    assert (await resp.json())["task"]["created_by"] == "deskrpg"


async def test_카드_생성은_디스패처가_없으면_경고를_싣는다(aiohttp_client, fake_api):
    probed = []

    def probe(hermes_home=None):
        probed.append(hermes_home)
        return (False, "no gateway")

    fake_api._check_dispatcher_presence = probe
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "x"})).json()
    assert body["warning"] == "dispatcher_missing"
    assert probed == [fake_api.get_hermes_home()]


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "missing_field"),
        ({"title": ""}, "invalid_field"),
        ({"title": "x", "priority": "high"}, "invalid_field"),
        ({"title": "x", "priority": True}, "invalid_field"),
        ({"title": "x", "parents": "t1"}, "invalid_field"),
        ({"title": "x", "skills": [1]}, "invalid_field"),
        ({"title": "x", "triage": "yes"}, "invalid_field"),
        ({"title": "x", "workspace_kind": "cloud"}, "invalid_field"),
        ({"title": "x", "max_runtime_seconds": 0}, "invalid_field"),
        ({"title": "x", "initial_status": "triage"}, "invalid_field"),
        ({"title": "x", "initial_status": "ready"}, "invalid_field"),
        ({"title": "x", "bogus": 1}, "unknown_field"),
    ],
)
async def test_카드_생성_본문_검증(aiohttp_client, fake_api, payload, code):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json=payload)
    assert resp.status == 400
    assert (await resp.json())["error"] == code


async def test_initial_status_blocked_는_카드를_승인_대기로_세운다(aiohttp_client, fake_api):
    """실행 전 승인 관문의 토대 — `blocked` 는 사람이 풀어 줄 때까지 디스패치되지 않는다.

    `triage` 를 쓸 수 없는 이유는 게이트웨이가 매 틱 triage 카드를 자동 분해하기 때문이다.
    """
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(
        f"/deskrpg/kanban/tasks{B}", json={"title": "승인 대기 과업", "initial_status": "blocked"}
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["task"]["status"] == "blocked"


async def test_initial_status_를_주지_않으면_예전과_같다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "보통 과업"})
    assert resp.status == 201
    assert (await resp.json())["task"]["status"] == "ready"


async def test_카드_생성은_Hermes_의_ValueError_를_400_으로(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "x", "parents": ["ghost"]})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_task"
    assert "ghost" in body["detail"]


async def test_카드_생성은_본문이_JSON_이_아니면_400(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", data=b"not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_json"


# ---------------------------------------------------------------------------
# PATCH — 필드
# ---------------------------------------------------------------------------


async def test_PATCH_제목_본문은_edited_사건과_notify_를_남긴다(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="old", body="b")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"title": "  new ", "body": ""})
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == cf.ENVELOPES["task"]
    assert body["task"]["title"] == "new"
    assert body["task"]["body"] == ""
    assert _events(fake_api, t)[-1] == "edited"
    assert db.notified[-1] == (t, ("title", "body"), "default")


async def test_PATCH_빈_제목은_400(aiohttp_client, fake_api):
    t = fake_api.kanban.create_task(_conn(fake_api), title="old")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"title": "  "})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_field"


async def test_PATCH_우선순위는_reprioritized_사건을_남긴다(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"priority": 5})).json()
    assert body["task"]["priority"] == 5
    ev = db.list_events(_conn(fake_api), t)[-1]
    assert ev.kind == "reprioritized" and ev.payload == {"priority": 5}
    assert db.notified[-1] == (t, ("priority",), "default")


async def test_PATCH_담당자_변경과_실행_중_409(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="x")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"assignee": "sophie"})).json()
    assert body["task"]["assignee"] == "sophie"
    db.start_run(conn, t, profile="sophie")
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"assignee": "mia"})
    assert resp.status == 409
    assert (await resp.json())["error"] == "invalid_transition"


async def test_PATCH_모델_추론_오버라이드와_null_로_비우기(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    body = await (
        await client.patch(
            f"/deskrpg/kanban/tasks/{t}{B}",
            json={"model_override": "gpt-5", "provider_override": "openai", "reasoning_effort": "high"},
        )
    ).json()
    assert body["task"]["model_override"] == "gpt-5"
    assert body["task"]["provider_override"] == "openai"
    assert body["task"]["reasoning_effort"] == "high"
    body = await (
        await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"model_override": None, "reasoning_effort": None})
    ).json()
    assert body["task"]["model_override"] is None
    assert body["task"]["provider_override"] is None
    assert body["task"]["reasoning_effort"] is None


async def test_PATCH_공급자만_주면_Hermes_ValueError_가_400(aiohttp_client, fake_api):
    t = fake_api.kanban.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"provider_override": "openai"})
    assert resp.status == 400


async def test_PATCH_알_수_없는_키와_미지원_키는_400(aiohttp_client, fake_api):
    t = fake_api.kanban.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"nope": 1})
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_field"
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"idempotency_key": "k"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_field"
    # 계약의 키지만 PATCH 로 바꿀 수 없는 것 — 조용히 버리지 않는다.
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"tenant": "acme"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "unsupported_field"


async def test_PATCH_없는_카드는_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/ghost{B}", json={"title": "x"})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# PATCH — 상태 전이
# ---------------------------------------------------------------------------


async def _patch_status(client, task_id, status):
    resp = await client.patch(f"/deskrpg/kanban/tasks/{task_id}{B}", json={"status": status})
    return resp.status, await resp.json()


async def test_상태_done_은_complete_task(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="x")
    db.get_task(_conn(fake_api), t).status = "ready"
    client = await _client(aiohttp_client, fake_api)
    status, body = await _patch_status(client, t, "done")
    assert status == 200 and body["task"]["status"] == "done"
    assert "completed" in _events(fake_api, t)


async def test_상태_blocked_scheduled_review_archived(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    client = await _client(aiohttp_client, fake_api)

    t = db.create_task(conn, title="b")
    db.get_task(conn, t).status = "ready"
    status, body = await _patch_status(client, t, "blocked")
    assert (status, body["task"]["status"]) == (200, "blocked")
    assert _events(fake_api, t)[-1] == "blocked"

    t = db.create_task(conn, title="s")
    status, body = await _patch_status(client, t, "scheduled")
    assert (status, body["task"]["status"]) == (200, "scheduled")
    assert _events(fake_api, t)[-1] == "scheduled"

    t = db.create_task(conn, title="r")
    db.start_run(conn, t, profile="sophie")  # 실행 중 — force=True 라 사람이 덮어쓴다
    status, body = await _patch_status(client, t, "review")
    assert (status, body["task"]["status"]) == (200, "review")
    assert _events(fake_api, t)[-1] == "review_requested"
    assert db.calls["request_review"][-1]["force"] is True

    t = db.create_task(conn, title="a")
    status, body = await _patch_status(client, t, "archived")
    assert (status, body["task"]["status"]) == (200, "archived")
    assert _events(fake_api, t)[-1] == "archived"


async def test_상태_blocked_에서_ready_todo_triage_는_unblock_task(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    client = await _client(aiohttp_client, fake_api)
    for target in ("ready", "todo", "triage"):
        t = db.create_task(conn, title=target)
        db.get_task(conn, t).status = "blocked"
        status, body = await _patch_status(client, t, target)
        assert status == 200, target
        assert body["task"]["status"] == "ready"  # unblock 은 재개 상태로 보낸다
        assert _events(fake_api, t)[-1] == "unblocked"
    t = db.create_task(conn, title="sched")
    db.get_task(conn, t).status = "scheduled"
    status, body = await _patch_status(client, t, "ready")
    assert status == 200 and _events(fake_api, t)[-1] == "unblocked"


async def test_상태_review_에서_다른_상태는_reopen_review_task(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    client = await _client(aiohttp_client, fake_api)
    t = db.create_task(conn, title="r")
    db.get_task(conn, t).status = "review"
    status, body = await _patch_status(client, t, "ready")
    assert status == 200 and body["task"]["status"] == "ready"
    assert _events(fake_api, t)[-1] == "review_reopened"


async def test_상태_직접_전이는_status_사건을_남기고_running_에서_나오면_run_을_닫는다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    killed = []
    fake_api._terminate_reclaimed_worker = lambda pid, lock: killed.append((pid, lock))
    client = await _client(aiohttp_client, fake_api)

    t = db.create_task(conn, title="todo->ready")
    db.get_task(conn, t).status = "todo"
    status, body = await _patch_status(client, t, "ready")
    assert status == 200 and body["task"]["status"] == "ready"
    ev = db.list_events(conn, t)[-1]
    assert ev.kind == "status" and ev.payload == {"status": "ready", "requested_status": "ready"}

    t = db.create_task(conn, title="running->todo")
    run_id = db.start_run(conn, t, profile="sophie", worker_pid=4242, claim_lock="lock-1")
    status, body = await _patch_status(client, t, "todo")
    assert status == 200 and body["task"]["status"] == "todo"
    run = db.get_run(conn, run_id)
    assert run.status == "reclaimed" and run.ended_at is not None
    assert killed == [(4242, "lock-1")]
    assert body["task"]["worker_pid"] is None
    assert db.list_events(conn, t)[-1].payload["status"] == "todo"


async def test_상태_running_에서_ready_는_검토_run_이면_review_로_간다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    client = await _client(aiohttp_client, fake_api)
    t = db.create_task(conn, title="reviewer")
    db.start_run(conn, t, profile="sophie", source_status="review")
    status, body = await _patch_status(client, t, "ready")
    assert status == 200 and body["task"]["status"] == "review"


async def test_상태_ready_는_부모가_안_끝났으면_409(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    parent = db.create_task(conn, title="p")
    child = db.create_task(conn, title="c", parents=[parent])
    client = await _client(aiohttp_client, fake_api)
    status, body = await _patch_status(client, child, "ready")
    assert status == 409
    assert body["error"] == "invalid_transition"
    assert "p" in body["detail"]


async def test_상태_done_을_다시_열면_후손을_무효화하고_recompute_한다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    parent = db.create_task(conn, title="p")
    child = db.create_task(conn, title="c", parents=[parent])
    db.get_task(conn, parent).status = "done"
    db.get_task(conn, child).status = "ready"
    client = await _client(aiohttp_client, fake_api)
    status, body = await _patch_status(client, parent, "todo")
    assert status == 200
    assert db.get_task(conn, child).status == "todo"
    assert db.calls["invalidate_descendants_for_parent_reopen"][-1]["author"] == "deskrpg"


async def test_상태_running_직접_지정과_거절은_409_모르는_상태는_400(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    client = await _client(aiohttp_client, fake_api)
    t = db.create_task(conn, title="x")
    db.get_task(conn, t).status = "triage"
    status, body = await _patch_status(client, t, "running")
    assert status == 409 and body["error"] == "invalid_transition"
    # triage 에서 done 은 Hermes 가 거절한다(False)
    status, body = await _patch_status(client, t, "done")
    assert status == 409 and body["error"] == "invalid_transition"
    status, body = await _patch_status(client, t, "flying")
    assert status == 400 and body["error"] == "invalid_field"


async def test_상태_review_와_담당자를_함께_주면_구현자를_먼저_기록한다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="x", assignee="sophie")
    db.get_task(conn, t).status = "ready"
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch(f"/deskrpg/kanban/tasks/{t}{B}", json={"status": "review", "assignee": "mia"})
    assert resp.status == 200
    body = await resp.json()
    assert body["task"]["status"] == "review"
    assert body["task"]["assignee"] == "mia"
    assert db.calls["request_review"][-1]["reviewer"] == "mia"
    assert "assigned" not in _events(fake_api, t)  # assign_task 를 따로 부르지 않았다


# ---------------------------------------------------------------------------
# 삭제
# ---------------------------------------------------------------------------


async def test_삭제는_ok_를_주고_삭제_장부에_한_줄_남긴다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    t1 = db.create_task(conn, title="첫째")
    t2 = db.create_task(conn, title="둘째")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete(f"/deskrpg/kanban/tasks/{t1}{B}")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    assert db.get_task(conn, t1) is None
    await client.delete(f"/deskrpg/kanban/tasks/{t2}{B}")

    path = fake_api.board_dir("default") / "deskrpg_deleted.jsonl"
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert [(l["n"], l["task_id"], l["title"]) for l in lines] == [(1, t1, "첫째"), (2, t2, "둘째")]
    assert all(isinstance(l["ts"], int) for l in lines)
    assert deleted_log.deleted_count(fake_api, "default") == 2
    assert [d["task_id"] for d in deleted_log.read_deleted_since(fake_api, "default", 1)] == [t2]
    assert deleted_log.read_deleted_since(fake_api, "default", 2) == []


async def test_삭제_장부는_없는_보드_폴더도_만들고_없으면_0(fake_api):
    assert deleted_log.deleted_count(fake_api, "default") == 0
    assert deleted_log.read_deleted_since(fake_api, "default", 0) == []
    assert deleted_log.append_deleted(fake_api, "default", "t1", "x") == 1
    assert deleted_log.append_deleted(fake_api, "default", "t2", "y") == 2


async def test_삭제_장부는_깨진_줄을_건너뛰되_줄_번호를_보존한다(fake_api):
    deleted_log.append_deleted(fake_api, "default", "t1", "x")
    path = fake_api.board_dir("default") / "deskrpg_deleted.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{broken\n")
    assert deleted_log.append_deleted(fake_api, "default", "t3", "z") == 3
    assert [d["n"] for d in deleted_log.read_deleted_since(fake_api, "default", 0)] == [1, 3]


async def test_삭제는_없는_카드에_404_이고_장부를_쓰지_않는다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete(f"/deskrpg/kanban/tasks/ghost{B}")
    assert resp.status == 404
    assert (await resp.json())["error"] == "task_not_found"
    assert not (fake_api.board_dir("default") / "deskrpg_deleted.jsonl").exists()


# ---------------------------------------------------------------------------
# 댓글
# ---------------------------------------------------------------------------


async def test_댓글은_201_이고_작성자_기본은_deskrpg_100자_절단(aiohttp_client, fake_api):
    db = fake_api.kanban
    t = db.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks/{t}/comments{B}", json={"body": "안녕"})
    assert resp.status == 201
    body = await resp.json()
    assert set(body) == cf.ENVELOPES["comment"]
    assert set(body["comment"]) == cf.KANBAN_COMMENT_KEYS
    assert body["comment"]["author"] == "deskrpg"
    assert body["comment"]["body"] == "안녕"

    long_author = "deskrpg:" + "n" * 200
    body = await (
        await client.post(f"/deskrpg/kanban/tasks/{t}/comments{B}", json={"author": long_author, "body": "b"})
    ).json()
    assert body["comment"]["author"] == long_author[:100]
    assert _events(fake_api, t)[-1] == "commented"


async def test_댓글_본문_없으면_400_카드_없으면_404(aiohttp_client, fake_api):
    t = fake_api.kanban.create_task(_conn(fake_api), title="x")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/tasks/{t}/comments{B}", json={"body": " "})
    assert resp.status == 400
    resp = await client.post(f"/deskrpg/kanban/tasks/ghost/comments{B}", json={"body": "x"})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 링크
# ---------------------------------------------------------------------------


async def test_링크_추가_삭제(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    a = db.create_task(conn, title="a")
    b = db.create_task(conn, title="b")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": b})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    assert db.parent_ids(conn, b) == [a]
    resp = await client.delete(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": b})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    assert db.parent_ids(conn, b) == []
    # 없던 링크를 지우면 ok:false — 오류는 아니다.
    resp = await client.delete(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": b})
    assert await resp.json() == {"ok": False}


async def test_링크_순환은_400_link_cycle_없는_카드는_404(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    a = db.create_task(conn, title="a")
    b = db.create_task(conn, title="b")
    db.link_tasks(conn, a, b)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": b, "child_id": a})
    assert resp.status == 400
    assert (await resp.json())["error"] == "link_cycle"
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": a})
    assert resp.status == 400
    assert (await resp.json())["error"] == "link_cycle"
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": a, "child_id": "ghost"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "task_not_found"
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": a})
    assert resp.status == 400


async def test_모든_카드_라우트는_board_슬러그를_요구한다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    for method, path in [
        ("GET", "/deskrpg/kanban/tasks/t1"),
        ("POST", "/deskrpg/kanban/tasks"),
        ("PATCH", "/deskrpg/kanban/tasks/t1"),
        ("DELETE", "/deskrpg/kanban/tasks/t1"),
        ("POST", "/deskrpg/kanban/tasks/t1/comments"),
        ("POST", "/deskrpg/kanban/links"),
        ("DELETE", "/deskrpg/kanban/links"),
    ]:
        resp = await client.request(method, path, json={})
        assert resp.status == 400, (method, path)
        assert (await resp.json())["error"] == "board_required"
        resp = await client.request(method, path + "?board=deskrpg-none", json={})
        assert resp.status == 404, (method, path)
        assert (await resp.json())["error"] == "board_not_found"


async def test_인증_실패는_401(aiohttp_client, fake_api):
    client = await aiohttp_client(make_app(fake_api, authorized=False))
    resp = await client.get("/deskrpg/kanban/boards")
    assert resp.status == 401
