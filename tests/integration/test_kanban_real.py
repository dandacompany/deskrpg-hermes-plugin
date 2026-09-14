"""실제 `hermes_cli.kanban_db` 위의 칸반·사건 시나리오(spec §9 T2).

가짜 단위 테스트가 고정한 계약이 **진짜 Hermes 의 sqlite·전이 규칙** 위에서도 같은 상태 코드·모양으로
나오는지 본다. 보드는 `HERMES_KANBAN_HOME=<tmp>/kanban` 아래에만 생긴다.
"""

import io
import json

import pytest

from deskrpg_plugin import deleted_log
from deskrpg_plugin import contract_fields as cf

pytestmark = pytest.mark.integration

BOARD = "deskrpg-0123456789abcdef0123456789abcdef"
B = f"?board={BOARD}"


async def _board(client, slug=BOARD):
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": slug, "name": "통합"})
    assert resp.status in (200, 201), await resp.text()
    return await resp.json()


async def _task(client, title="카드", **extra):
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": title, **extra})
    assert resp.status == 201, await resp.text()
    return (await resp.json())["task"]


async def _events(client, cursor=None, limit=None):
    url = f"/deskrpg/events{B}"
    if cursor is not None:
        url += f"&cursor={cursor}"
    if limit is not None:
        url += f"&limit={limit}"
    resp = await client.get(url)
    assert resp.status == 200, await resp.text()
    return await resp.json()


# ---------------------------------------------------------------------------
# §5.1 보드
# ---------------------------------------------------------------------------


async def test_보드_생성은_201_이고_다시_만들면_200_에_기존_보드다(client, api, hermes_env):
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": BOARD, "name": "첫 이름"})
    assert resp.status == 201, await resp.text()
    first = (await resp.json())["board"]
    assert first["slug"] == BOARD and first["name"] == "첫 이름"
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": BOARD, "name": "다른 이름"})
    assert resp.status == 200
    assert (await resp.json())["board"]["name"] == "첫 이름"  # 멱등 — name 은 바꾸지 않는다
    # 실제 파일이 임시 kanban 홈 아래에만 생겼다.
    assert (hermes_env["kanban"] / "kanban" / "boards" / BOARD).is_dir()
    assert api.get_current_board() == "default"  # 새 보드를 현재 보드로 바꾸지 않는다
    listed = await (await client.get("/deskrpg/kanban/boards")).json()
    assert BOARD in {b["slug"] for b in listed["boards"]}


async def test_보드_slug_형식_오류는_400_없는_보드_PATCH_는_404(client):
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": "Bad Slug", "name": "x"})
    assert resp.status == 400
    resp = await client.patch("/deskrpg/kanban/boards/deskrpg-none", json={"name": "x"})
    assert resp.status == 404


async def test_보드_PATCH_는_이름과_설명을_바꾼다(client):
    await _board(client)
    resp = await client.patch(f"/deskrpg/kanban/boards/{BOARD}", json={"name": "바뀐", "description": "설명"})
    assert resp.status == 200, await resp.text()
    board = (await resp.json())["board"]
    assert board["name"] == "바뀐" and board["description"] == "설명"


# ---------------------------------------------------------------------------
# §5.2–5.3 카드·보드 보기·댓글·링크
# ---------------------------------------------------------------------------


async def test_카드_생성은_보드_보기의_ready_열에_나타나고_dispatcher_missing_경고가_붙는다(client):
    await _board(client)
    resp = await client.post(
        f"/deskrpg/kanban/tasks{B}", json={"title": "일", "body": "본문", "priority": 2},
        headers={"X-DeskRPG-Actor": "dante"},
    )
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    task = body["task"]
    assert cf.KANBAN_TASK_FULL_REQUIRED <= set(task) <= cf.KANBAN_TASK_FULL_KEYS, set(task) ^ cf.KANBAN_TASK_FULL_KEYS
    assert task["status"] == "ready" and task["created_by"] == "deskrpg:dante"
    # 임시 홈에는 게이트웨이가 없다 — 실제 `_check_dispatcher_presence` 가 거짓을 준다.
    assert body.get("warning") == "dispatcher_missing"

    view = await (await client.get(f"/deskrpg/kanban/board{B}")).json()
    columns = {c["name"]: c["tasks"] for c in view["columns"]}
    assert list(c["name"] for c in view["columns"]) == [
        "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
    ]
    [card] = columns["ready"]
    assert card["id"] == task["id"]
    assert cf.KANBAN_TASK_REQUIRED <= set(card) <= cf.KANBAN_TASK_KEYS, set(card) ^ cf.KANBAN_TASK_KEYS
    assert view["latest_event_id"] >= 1 and isinstance(view["now"], int)


async def test_보드_없음은_404_board_not_found(client):
    resp = await client.get("/deskrpg/kanban/board?board=deskrpg-none")
    assert resp.status == 404 and (await resp.json())["error"] == "board_not_found"


async def test_카드_조회는_댓글_사건_링크_실행을_함께_준다(client):
    await _board(client)
    parent = await _task(client, "부모")
    child = await _task(client, "자식")
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": parent["id"], "child_id": child["id"]})
    assert resp.status == 200 and (await resp.json()) == {"ok": True}
    resp = await client.post(f"/deskrpg/kanban/tasks/{child['id']}/comments{B}", json={"body": "댓글", "author": "dante"})
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["comment"]["author"] == "dante"

    detail = await (await client.get(f"/deskrpg/kanban/tasks/{child['id']}{B}")).json()
    assert detail["task"]["status"] == "todo"  # 부모가 끝나지 않았으니 게이트된다
    assert detail["links"] == {"parents": [parent["id"]], "children": []}
    assert [c["body"] for c in detail["comments"]] == ["댓글"]
    assert {e["kind"] for e in detail["events"]} >= {"created", "commented"}
    assert detail["attachments"] == [] and detail["runs"] == []

    # 순환은 400 cycle, 없는 카드는 404
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": child["id"], "child_id": parent["id"]})
    assert resp.status == 400 and (await resp.json())["error"] == "link_cycle"
    resp = await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": "nope", "child_id": parent["id"]})
    assert resp.status == 404
    resp = await client.delete(f"/deskrpg/kanban/links{B}", json={"parent_id": parent["id"], "child_id": child["id"]})
    assert resp.status == 200


async def test_없는_카드는_404(client):
    await _board(client)
    assert (await client.get(f"/deskrpg/kanban/tasks/nope{B}")).status == 404
    assert (await client.patch(f"/deskrpg/kanban/tasks/nope{B}", json={"title": "x"})).status == 404
    assert (await client.delete(f"/deskrpg/kanban/tasks/nope{B}")).status == 404


# ---------------------------------------------------------------------------
# §5.3 PATCH — 실제 전이 규칙
# ---------------------------------------------------------------------------


async def _patch(client, task_id, **fields):
    resp = await client.patch(f"/deskrpg/kanban/tasks/{task_id}{B}", json=fields)
    return resp.status, await resp.json()


async def test_PATCH_제목_본문_우선순위는_바뀌고_사건이_남는다(client):
    await _board(client)
    task = await _task(client)
    status, body = await _patch(client, task["id"], title="새 제목", body="새 본문", priority=7)
    assert status == 200, body
    assert body["task"]["title"] == "새 제목" and body["task"]["body"] == "새 본문" and body["task"]["priority"] == 7
    detail = await (await client.get(f"/deskrpg/kanban/tasks/{task['id']}{B}")).json()
    assert {"edited", "reprioritized"} <= {e["kind"] for e in detail["events"]}


async def test_PATCH_status_갈래_block_unblock_review_reopen_done_archive(client):
    await _board(client)
    task = await _task(client)
    tid = task["id"]
    # ready → blocked (block_task)
    status, body = await _patch(client, tid, status="blocked")
    assert (status, body["task"]["status"]) == (200, "blocked"), body
    # blocked → ready (unblock_task)
    status, body = await _patch(client, tid, status="ready")
    assert (status, body["task"]["status"]) == (200, "ready"), body
    # ready → review (request_review force=True)
    status, body = await _patch(client, tid, status="review")
    assert (status, body["task"]["status"]) == (200, "review"), body
    # review → ready (reopen_review_task)
    status, body = await _patch(client, tid, status="ready")
    assert (status, body["task"]["status"]) == (200, "ready"), body
    # ready → scheduled (schedule_task) → ready (unblock)
    status, body = await _patch(client, tid, status="scheduled")
    assert (status, body["task"]["status"]) == (200, "scheduled"), body
    status, body = await _patch(client, tid, status="ready")
    assert (status, body["task"]["status"]) == (200, "ready"), body
    # ready → done (complete_task)
    status, body = await _patch(client, tid, status="done")
    assert (status, body["task"]["status"]) == (200, "done"), body
    # done → archived (archive_task)
    status, body = await _patch(client, tid, status="archived")
    assert (status, body["task"]["status"]) == (200, "archived"), body
    # archived 에서 blocked 로는 Hermes 가 거절한다 → 409 invalid_transition
    status, body = await _patch(client, tid, status="blocked")
    assert status == 409 and body["error"] == "invalid_transition", body
    # 모르는 키는 400
    status, body = await _patch(client, tid, bogus=1)
    assert status == 400


async def test_PATCH_assignee_와_model_override_reasoning_effort(client, profile):
    await _board(client)
    task = await _task(client)
    status, body = await _patch(client, task["id"], assignee=profile)
    assert (status, body["task"]["assignee"]) == (200, profile), body
    status, body = await _patch(client, task["id"], model_override="gpt-x", provider_override="openai")
    assert status == 200 and body["task"]["model_override"] == "gpt-x" and body["task"]["provider_override"] == "openai"
    status, body = await _patch(client, task["id"], reasoning_effort="high")
    assert status == 200 and body["task"]["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# §5.3 삭제 → 삭제 기록 → §6 사건 task.deleted
# ---------------------------------------------------------------------------


async def test_삭제는_삭제_기록을_남기고_사건_스트림에_task_deleted_가_나온다(client, api, hermes_env):
    await _board(client)
    now = await _events(client)
    assert now["events"] == [] and now["has_more"] is False
    task = await _task(client, "지울 카드")
    resp = await client.delete(f"/deskrpg/kanban/tasks/{task['id']}{B}")
    assert resp.status == 200 and (await resp.json()) == {"ok": True}
    # 파일은 실제 board_dir 아래(spec §5.3), 한 줄 `{n, task_id, title, ts}`
    path = deleted_log.log_path(api, BOARD)
    assert path == hermes_env["kanban"] / "kanban" / "boards" / BOARD / "deskrpg_deleted.jsonl"
    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["n"] == 1 and record["task_id"] == task["id"] and record["title"] == "지울 카드"
    # Hermes 는 task_events 도 같이 지우므로 created 는 안 나오고, 삭제 기록에서 합성한 task.deleted 만 나온다.
    tail = await _events(client, now["cursor"])
    kinds = [(e["kind"], e["id"]) for e in tail["events"]]
    assert kinds == [("task.deleted", "d:1")], kinds
    assert tail["events"][0]["payload"] == {"task_id": task["id"], "title": "지울 카드"}
    assert (await client.get(f"/deskrpg/kanban/tasks/{task['id']}{B}")).status == 404


# ---------------------------------------------------------------------------
# §6 사건 — 실제 task_events 에서의 kind 매핑·커서 왕복·페이징
# ---------------------------------------------------------------------------


async def test_사건_매핑_created_linked_commented_status_와_커서_왕복(client):
    await _board(client)
    now = await _events(client)
    parent = await _task(client, "부모")
    child = await _task(client, "자식")
    await client.post(f"/deskrpg/kanban/links{B}", json={"parent_id": parent["id"], "child_id": child["id"]})
    await client.post(f"/deskrpg/kanban/tasks/{child['id']}/comments{B}", json={"body": "댓글"})
    await _patch(client, parent["id"], status="blocked")
    await _patch(client, parent["id"], status="ready")

    tail = await _events(client, now["cursor"])
    kinds = [e["kind"] for e in tail["events"]]
    assert kinds[:2] == ["task.created", "task.created"]
    assert "task.link" in kinds and "task.comment" in kinds
    statuses = [e for e in tail["events"] if e["kind"] == "task.status" and e["task_id"] == parent["id"]]
    assert [(s["payload"]["from"], s["payload"]["to"]) for s in statuses] == [("ready", "blocked"), ("blocked", "ready")]
    assert statuses[0]["payload"]["title"] == "부모" and statuses[0]["payload"]["parent_count"] == 0
    for ev in tail["events"]:
        assert ev["id"].startswith("k:") and ev["board"] == BOARD and isinstance(ev["ts"], int)

    # 커서 왕복 — 다시 부르면 비어 있고, 새 사건은 그 뒤에 나온다.
    again = await _events(client, tail["cursor"])
    assert again["events"] == [] and again["has_more"] is False
    await _task(client, "셋째")
    third = await _events(client, again["cursor"])
    assert [e["kind"] for e in third["events"]] == ["task.created"]


async def test_사건_페이징_limit_과_has_more(client):
    await _board(client)
    now = await _events(client)
    for i in range(5):
        await _task(client, f"카드{i}")
    page = await _events(client, now["cursor"], limit=2)
    assert len(page["events"]) == 2 and page["has_more"] is True
    seen = [e["id"] for e in page["events"]]
    while page["has_more"]:
        page = await _events(client, page["cursor"], limit=2)
        seen.extend(e["id"] for e in page["events"])
    assert len(seen) == 5 and len(set(seen)) == 5


async def test_모르는_커서는_400_unknown_cursor_보드_없음은_404(client):
    await _board(client)
    resp = await client.get(f"/deskrpg/events{B}&cursor=v0.zzz")
    assert resp.status == 400 and (await resp.json())["error"] == "unknown_cursor"
    resp = await client.get("/deskrpg/events?board=deskrpg-none")
    assert resp.status == 404 and (await resp.json())["error"] == "board_not_found"


# ---------------------------------------------------------------------------
# §5.4 동작 — 실제 전이
# ---------------------------------------------------------------------------


async def _action(client, task_id, name, body=None):
    resp = await client.post(f"/deskrpg/kanban/tasks/{task_id}/{name}{B}", json=body or {})
    return resp.status, await resp.json()


async def test_동작_approve_archive_unblock_terminate_estimate(client):
    await _board(client)
    task = await _task(client)
    status, body = await _action(client, task["id"], "terminate")
    assert status == 409 and body["error"] == "no_active_run", body
    status, body = await _action(client, task["id"], "estimate")
    assert status == 501
    status, body = await _action(client, task["id"], "unblock", {"comment": "풀어"})
    assert status == 409 and body["error"] == "invalid_transition", body  # ready 는 unblock 대상이 아니다
    status, body = await _action(client, task["id"], "approve", {"summary": "좋다"})
    assert (status, body["task"]["status"]) == (200, "done"), body
    status, body = await _action(client, task["id"], "archive")
    assert (status, body["task"]["status"]) == (200, "archived"), body
    status, body = await _action(client, task["id"], "frobnicate")
    assert status == 404


async def test_동작_request_changes_는_review_카드를_되돌린다(client):
    await _board(client)
    task = await _task(client)
    await _patch(client, task["id"], status="review")
    status, body = await _action(client, task["id"], "request-changes", {"comment": "다시"})
    assert status == 200, body
    assert body["outcome"] == "reopened" and body["task"]["status"] == "ready"
    detail = await (await client.get(f"/deskrpg/kanban/tasks/{task['id']}{B}")).json()
    assert [c["body"] for c in detail["comments"]] == ["다시"]
    status, body = await _action(client, task["id"], "request-changes", {})
    assert status == 400


async def test_동작_reassign_은_존재하는_프로필만(client, profile):
    await _board(client)
    task = await _task(client)
    status, body = await _action(client, task["id"], "reassign", {"profile": "nobody"})
    assert status == 400, body
    status, body = await _action(client, task["id"], "reassign", {"profile": profile})
    assert (status, body["task"]["assignee"]) == (200, profile), body


# ---------------------------------------------------------------------------
# §5.5 첨부·로그·디스패치 · §5.6 운영 설정·프로필
# ---------------------------------------------------------------------------


async def test_첨부_업로드_목록_내려받기_삭제(client, api, hermes_env):
    import aiohttp

    await _board(client)
    task = await _task(client)
    form = aiohttp.FormData()
    form.add_field("file", io.BytesIO(b"hello attachment"), filename="a.txt", content_type="text/plain")
    resp = await client.post(f"/deskrpg/kanban/tasks/{task['id']}/attachments{B}", data=form)
    assert resp.status == 201, await resp.text()
    att = (await resp.json())["attachment"]
    assert att["filename"] == "a.txt" and att["size"] == len(b"hello attachment")
    listed = await (await client.get(f"/deskrpg/kanban/tasks/{task['id']}/attachments{B}")).json()
    assert [a["id"] for a in listed["attachments"]] == [att["id"]]
    resp = await client.get(f"/deskrpg/kanban/attachments/{att['id']}{B}")
    assert resp.status == 200 and (await resp.read()) == b"hello attachment"
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert "a.txt" in resp.headers["Content-Disposition"]
    resp = await client.delete(f"/deskrpg/kanban/attachments/{att['id']}{B}")
    assert resp.status == 200 and (await resp.json()) == {"ok": True}
    assert (await client.get(f"/deskrpg/kanban/attachments/{att['id']}{B}")).status == 404


async def test_워커_로그_없으면_exists_false(client):
    await _board(client)
    task = await _task(client)
    body = await (await client.get(f"/deskrpg/kanban/tasks/{task['id']}/log{B}")).json()
    assert body["exists"] is False and body["content"] == "" and body["truncated"] is False


async def test_디스패치는_빈_보드에서_아무것도_스폰하지_않는다(client):
    await _board(client)
    resp = await client.post(f"/deskrpg/kanban/dispatch{B}&max=2")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["spawned"] == [] and body["skipped_locked"] is False


def test_dispatch_once_dry_run_은_ready_카드를_스폰하지_않고_장부만_읽는다(api, hermes_env):
    """실제 스폰은 절대 하지 않는다(spec T2) — `dry_run=True` 로 디스패처 경로만 지나간다."""
    from deskrpg_plugin.common import board_conn

    api.create_board(BOARD, name="통합")
    with board_conn(api, BOARD) as conn:
        tid = api.create_task(conn, title="준비된 카드", created_by="deskrpg")
        result = api.dispatch_once(conn, board=BOARD, dry_run=True, max_spawn=1)
        assert api.get_task(conn, tid).status == "ready"
        assert list(getattr(result, "spawned", [])) == [] or all(isinstance(x, str) for x in result.spawned)


async def test_운영_설정_조회와_갱신(client, profile):
    body = await (await client.get("/deskrpg/kanban/orchestration")).json()
    # max_in_progress* 는 설정에 정수로 있을 때만 실린다(계약의 optional) — 새 홈의 기본 config 는 null 이다.
    assert cf.ORCHESTRATION_SETTINGS_REQUIRED <= set(body) and "dispatch_in_gateway" in body
    assert "max_in_progress" not in body
    resp = await client.put("/deskrpg/kanban/orchestration", json={"default_assignee": profile, "max_in_progress": 3})
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["default_assignee"] == profile and body["max_in_progress"] == 3 and body["restart_required"] is True
    resp = await client.put("/deskrpg/kanban/orchestration", json={"default_assignee": "nobody"})
    assert resp.status == 400


async def test_칸반_프로필_목록은_default_와_만든_프로필을_준다(client, profile):
    body = await (await client.get("/deskrpg/kanban/profiles")).json()
    names = {p["name"]: p for p in body["profiles"]}
    assert set(names) == {"default", profile}
    assert names["default"]["is_default"] is True and names[profile]["is_default"] is False
