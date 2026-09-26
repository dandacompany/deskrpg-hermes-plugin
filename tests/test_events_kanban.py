"""E3 · §5.3 — 칸반 task_events 매핑과 삭제 기록 tail."""

import json

import pytest

from deskrpg_plugin import events
from deskrpg_plugin.contract_fields import EVENT_KINDS, PLUGIN_EVENT_KEYS, TASK_STATUS_PAYLOAD_KEYS
from tests.fakes_events import append_deleted, install_fake_events


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def conn(kanban):
    return kanban.connect(board="default")


def _tail(fake_api, conn, since=0, limit=500):
    return [events.public_event(e) for e in events.kanban_tail(fake_api, conn, "default", since, limit)]


def _only(kanban, conn, fake_api):
    """마지막으로 심은 행 하나에서 나온 사건들."""
    last = kanban.boards["default"].events[-1].id
    return _tail(fake_api, conn, since=last - 1)


# ---------------------------------------------------------------------------
# kind 매핑
# ---------------------------------------------------------------------------


def test_created_는_task_created_로_나오고_id_ts_board_task_id_를_단다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드", assignee="sophie", triage=True)
    [ev] = _tail(fake_api, conn)
    assert ev["kind"] == "task.created"
    assert ev["id"] == f"k:{kanban.boards['default'].events[0].id}"
    assert ev["board"] == "default"
    assert ev["task_id"] == task.id
    assert ev["ts"] == kanban.boards["default"].events[0].created_at
    assert ev["payload"]["status"] == "triage"
    assert set(ev) <= PLUGIN_EVENT_KEYS
    assert ev["kind"] in EVENT_KINDS


def test_commented_는_task_comment(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "commented", {"author": "dante", "len": 3})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["kind"] == "task.comment"
    assert ev["payload"]["author"] == "dante"


@pytest.mark.parametrize("kind", ["linked", "unlinked"])
def test_linked_unlinked_는_task_link_에_action_을_붙인다(fake_api, kanban, conn, kind):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, kind, {"parent_id": "t0001"})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["kind"] == "task.link"
    assert ev["payload"] == {"parent_id": "t0001", "action": kind}


def test_spawned_는_task_run_started_에_run_id_를_싣고_claimed_는_무시된다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    created_id = kanban.boards["default"].events[-1].id
    kanban.emit("default", task.id, "claimed", {"profile": "sophie"}, run_id=7)
    kanban.emit("default", task.id, "spawned", {"pid": 123}, run_id=7)
    evs = _tail(fake_api, conn, since=created_id)
    assert [e["kind"] for e in evs] == ["task.run.started"]
    assert evs[0]["run_id"] == 7
    assert evs[0]["payload"] == {"pid": 123}


@pytest.mark.parametrize(
    "kind",
    sorted(events.IGNORED_KINDS) + ["decomposed", "totally_unknown_kind"],
)
def test_무시_목록과_모르는_kind_는_사건을_내지_않는다(fake_api, kanban, conn, kind):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, kind, {"x": 1})
    assert _only(kanban, conn, fake_api) == []


@pytest.mark.parametrize("kind", sorted(events.STATUS_KINDS))
def test_상태_kind_는_전부_task_status_로_나온다(fake_api, kanban, conn, kind):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, kind, None)
    [ev] = _only(kanban, conn, fake_api)
    assert ev["kind"] == "task.status"
    assert set(ev["payload"]) == TASK_STATUS_PAYLOAD_KEYS


@pytest.mark.parametrize(
    "kind,payload,expected_fields",
    [
        ("attached", {"filename": "a.png", "size": 3}, ["attachments"]),
        ("attachment_removed", {"filename": "a.png"}, ["attachments"]),
        ("edited", {"fields": ["title", "body"]}, ["title", "body"]),
        ("edited", None, []),
        ("reprioritized", {"priority": 5}, ["priority"]),
        ("assigned", {"assignee": "sophie"}, ["assignee"]),
    ],
)
def test_갱신_kind_는_task_updated_에_fields_를_싣는다(fake_api, kanban, conn, kind, payload, expected_fields):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, kind, payload)
    [ev] = _only(kanban, conn, fake_api)
    assert ev["kind"] == "task.updated"
    assert ev["payload"]["fields"] == expected_fields


# ---------------------------------------------------------------------------
# task.status 의 from / to / parent_count
# ---------------------------------------------------------------------------


def test_to_는_payload_의_status_가_우선이다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드", triage=True)
    kanban.set_status("default", task.id, "running")  # 현재 상태는 다르게 둔다
    kanban.emit("default", task.id, "status", {"status": "review"})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"]["to"] == "review"
    assert ev["payload"]["from"] == "triage"  # created 의 status


def test_payload_없는_promoted_는_ready_로_보고_현재_상태에_의존하지_않는다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드", triage=True)
    kanban.emit("default", task.id, "promoted", None)
    kanban.set_status("default", task.id, "done")  # 그 뒤에 또 바뀌었다
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"] == {"from": "triage", "to": "ready", "parent_count": 0, "title": "카드", "assignee": None}


def test_기본표에_없는_kind_는_현재_tasks_status_를_to_로_쓴다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.set_status("default", task.id, "review")
    kanban.emit("default", task.id, "specified", {"changed_fields": ["body"]})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"]["to"] == "review"


def test_from_은_같은_task_의_더_작은_id_상태_사건의_to_이다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드", triage=True)
    other = kanban.make_task(conn, title="다른 카드")
    kanban.emit("default", task.id, "status", {"status": "ready"})
    kanban.emit("default", other.id, "status", {"status": "blocked"})  # 다른 카드는 섞이지 않는다
    kanban.emit("default", task.id, "status", {"status": "running"})
    evs = _tail(fake_api, conn)
    mine = [e for e in evs if e["task_id"] == task.id and e["kind"] == "task.status"]
    assert [(e["payload"]["from"], e["payload"]["to"]) for e in mine] == [("triage", "ready"), ("ready", "running")]


def test_from_은_created_가_없으면_null(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.boards["default"].events.clear()  # 오래된 이력이 잘린 카드
    kanban.emit("default", task.id, "status", {"status": "ready"})
    [ev] = _tail(fake_api, conn)
    assert ev["payload"]["from"] is None


def test_parent_count_는_task_links_로_센다(fake_api, kanban, conn):
    p1 = kanban.make_task(conn, title="부모1")
    p2 = kanban.make_task(conn, title="부모2")
    child = kanban.make_task(conn, title="자식", parents=[p1.id, p2.id])
    kanban.emit("default", child.id, "status", {"status": "ready"})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"]["parent_count"] == 2


def test_카드가_이미_지워졌으면_to_는_null_parent_count_는_0(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "specified", None)
    kanban.boards["default"].tasks.pop(task.id)  # 카드만 없고 사건은 남은 경계 상황
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"]["to"] is None
    assert ev["payload"]["parent_count"] == 0
    assert ev["payload"]["title"] is None


def test_title_assignee_는_현재_카드에서_읽는다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="원래 제목", assignee="sophie")
    kanban.boards["default"].tasks[task.id].title = "바뀐 제목"
    kanban.emit("default", task.id, "status", {"status": "ready"})
    [ev] = _only(kanban, conn, fake_api)
    assert ev["payload"]["title"] == "바뀐 제목"
    assert ev["payload"]["assignee"] == "sophie"


# ---------------------------------------------------------------------------
# run.finished + task.status 쌍
# ---------------------------------------------------------------------------


def test_completed_는_run_finished_와_status_변화_한_건을_같이_낸다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "status", {"status": "running"})
    kanban.set_status("default", task.id, "done")
    row = kanban.emit("default", task.id, "completed", {"summary": "끝"}, run_id=3)
    evs = _only(kanban, conn, fake_api)
    assert [e["kind"] for e in evs] == ["task.run.finished", "task.status"]
    assert evs[0]["id"] == f"k:{row.id}"
    assert evs[0]["run_id"] == 3
    assert evs[0]["payload"] == {"summary": "끝", "outcome": "completed"}
    assert evs[1]["id"] == f"k:{row.id}:status"
    assert evs[1]["payload"]["from"] == "running"
    assert evs[1]["payload"]["to"] == "done"


@pytest.mark.parametrize(
    "kind", ["reclaimed", "gave_up", "timed_out", "crashed", "stale", "spawn_failed", "rate_limited"],
)
def test_상태가_그대로면_run_finished_만_낸다(fake_api, kanban, conn, kind):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "status", {"status": "ready"})
    kanban.set_status("default", task.id, "ready")  # 되돌아온 상태가 직전과 같다
    kanban.emit("default", task.id, kind, {"reason": "x"}, run_id=9)
    evs = _only(kanban, conn, fake_api)
    assert [e["kind"] for e in evs] == ["task.run.finished"]
    assert evs[0]["payload"]["outcome"] == kind


def test_run_finished_의_status_가_payload_에_있으면_그것을_to_로_쓴다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "timed_out", {"status": "blocked", "retry_status": "blocked"})
    evs = _only(kanban, conn, fake_api)
    assert [e["kind"] for e in evs] == ["task.run.finished", "task.status"]
    assert evs[1]["payload"]["to"] == "blocked"


def test_run_finished_뒤의_상태_사건은_그_전이를_from_으로_본다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.set_status("default", task.id, "done")
    kanban.emit("default", task.id, "completed", None)
    kanban.emit("default", task.id, "archived", None)
    evs = _tail(fake_api, conn)
    assert evs[-1]["payload"] == {"from": "done", "to": "archived", "parent_count": 0, "title": "카드", "assignee": None}


def test_카드가_지워졌으면_run_finished_만_내고_status_는_내지_않는다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    kanban.emit("default", task.id, "crashed", None)
    kanban.boards["default"].tasks.pop(task.id)
    evs = _only(kanban, conn, fake_api)
    assert [e["kind"] for e in evs] == ["task.run.finished"]


# ---------------------------------------------------------------------------
# tail 경계 · payload 파싱
# ---------------------------------------------------------------------------


def test_tail_은_since_id_초과만_id_순으로_limit_행까지_읽는다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    for i in range(5):
        kanban.emit("default", task.id, "commented", {"i": i})
    first = kanban.boards["default"].events[0].id
    evs = _tail(fake_api, conn, since=first, limit=3)
    assert [e["payload"]["i"] for e in evs] == [0, 1, 2]
    assert events.max_event_id(conn) == first + 5


def test_깨진_payload_는_빈_dict_로_본다(fake_api, kanban, conn):
    task = kanban.make_task(conn, title="카드")
    ev = kanban.emit("default", task.id, "commented", None)
    ev.payload = None
    # JSON 이 아닌 문자열이 들어 있는 행을 흉내 낸다 — 가짜 연결이 dumps 하므로 문자열 payload 는 "\"...\"" 이 된다.
    ev.payload = "not-a-dict"
    [out] = _only(kanban, conn, fake_api)
    assert out["payload"] == {}


# ---------------------------------------------------------------------------
# §5.3 — 삭제 기록
# ---------------------------------------------------------------------------


def test_deleted_tail_은_after_n_초과_줄을_task_deleted_로_낸다(fake_api, kanban):
    append_deleted(fake_api, "default", "t0001", "첫째", 1_700_000_001)
    append_deleted(fake_api, "default", "t0002", "둘째", 1_700_000_002)
    append_deleted(fake_api, "default", "t0003", "셋째", "2023-11-14T22:13:23Z")
    evs = [events.public_event(e) for e in events.deleted_tail(fake_api, "default", 1)]
    assert [e["id"] for e in evs] == ["d:2", "d:3"]
    assert evs[0] == {
        "id": "d:2", "ts": 1_700_000_002, "kind": "task.deleted", "board": "default", "task_id": "t0002",
        "payload": {"task_id": "t0002", "title": "둘째"},
    }
    assert evs[1]["ts"] == pytest.approx(1_700_000_003)  # ISO 도 epoch 으로 맞춘다
    assert events.deleted_log_position(fake_api, "default") == 3


def test_deleted_tail_은_파일이_없으면_빈_목록_위치는_0(fake_api, kanban):
    assert events.deleted_tail(fake_api, "default", 0) == []
    assert events.deleted_log_position(fake_api, "default") == 0


def test_deleted_tail_은_깨진_줄을_건너뛰고_n_없는_줄은_줄_번호로_본다(fake_api, kanban):
    path = events.deleted_log_path(fake_api, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"task_id": "a", "title": "A", "ts": 1}) + "\n"
        + "{broken\n"
        + "\n"
        + json.dumps({"n": 4, "task_id": "b", "title": "B", "ts": 2}) + "\n",
        encoding="utf-8",
    )
    evs = events.deleted_tail(fake_api, "default", 0)
    assert [e["id"] for e in evs] == ["d:1", "d:4"]
    assert events.deleted_tail(fake_api, "default", 1)[0]["id"] == "d:4"


def test_삭제_기록_경로는_board_dir_아래_deskrpg_deleted_jsonl_이다(fake_api, kanban):
    assert events.deleted_log_path(fake_api, "default") == kanban.board_dir("default") / "deskrpg_deleted.jsonl"


@pytest.mark.parametrize("kind", ["spawn_failed", "rate_limited"])
def test_spawn_failed_and_rate_limited_close_the_run_with_its_run_id(fake_api, kanban, conn, kind):
    # Both end a run in Hermes. Dropping them left the NPC "working" and the run's end invisible.
    task = kanban.make_task(conn, title="card")
    kanban.emit("default", task.id, "status", {"status": "ready"})
    kanban.set_status("default", task.id, "ready")
    kanban.emit("default", task.id, kind, {"error": "boom", "failures": 1, "retry_status": "ready"}, run_id=12)
    evs = _only(kanban, conn, fake_api)
    assert [e["kind"] for e in evs] == ["task.run.finished"]
    assert evs[0]["run_id"] == 12
    assert evs[0]["payload"]["outcome"] == kind
    assert evs[0]["payload"]["error"] == "boom"
