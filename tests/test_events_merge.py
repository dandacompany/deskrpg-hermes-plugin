"""E5 · E6 — 병합 순서·tie-break·limit 절단과 출처별 커서 전진, 그리고 여러 호출에 걸친 무중복·무누락."""

import time
from datetime import datetime

import pytest

from deskrpg_plugin import events
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import append_deleted, events_client, install_fake_events


def _k(ts, row_id, sub=0):
    return {"id": f"k:{row_id}" if sub == 0 else f"k:{row_id}:status", "ts": ts, "kind": "task.status",
            "payload": {}, "_src": "k", "_pos": (row_id, sub)}


def _d(ts, n):
    return {"id": f"d:{n}", "ts": ts, "kind": "task.deleted", "payload": {}, "_src": "d", "_pos": (n,)}


def _c(ts, exec_id, phase, profile="p", claimed="2026-01-01T00:00:00+09:00", index=0):
    return {"id": f"c:{profile}:{exec_id}:{phase}", "ts": ts, "kind": f"cron.run.{phase}", "payload": {},
            "_src": "c", "_profile": profile, "_exec": exec_id, "_claimed": claimed,
            "_pos": (index, 0 if phase == "started" else 1)}


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_ts_오름차순으로_합친다():
    emitted, has_more, by = events.merge([_k(30, 1)], [_d(10, 1)], [_c(20, "e", "started")], 10)
    assert [e["id"] for e in emitted] == ["d:1", "c:p:e:started", "k:1"]
    assert has_more is False
    assert [e["id"] for e in by["k"]] == ["k:1"]


def test_같은_ts_는_k_d_c_순_그다음_출처_안의_위치():
    emitted, _, _ = events.merge(
        [_k(5, 10), _k(5, 9), _k(5, 9, sub=1)], [_d(5, 2), _d(5, 1)],
        [_c(5, "b", "finished", index=1), _c(5, "a", "started", index=0)], 10,
    )
    assert [e["id"] for e in emitted] == ["k:9", "k:9:status", "k:10", "d:1", "d:2", "c:p:a:started", "c:p:b:finished"]


def test_limit_에서_자르면_has_more():
    emitted, has_more, by = events.merge([_k(1, 1), _k(3, 2)], [_d(2, 1)], [], 2)
    assert [e["id"] for e in emitted] == ["k:1", "d:1"]
    assert has_more is True
    assert by["k"] == [emitted[0]] and by["d"] == [emitted[1]] and by["c"] == []


def test_limit_이_같은_행의_쌍_사이에_떨어지면_쌍을_통째로_넘긴다():
    emitted, has_more, _ = events.merge([_k(1, 1), _k(2, 2), _k(2, 2, sub=1), _k(3, 3)], [], [], 2)
    assert [e["id"] for e in emitted] == ["k:1"]
    assert has_more is True


def test_쌍만_남았고_limit_이_1_이면_쌍을_통째로_싣는다():
    emitted, has_more, _ = events.merge([_k(2, 2), _k(2, 2, sub=1)], [], [], 1)
    assert [e["id"] for e in emitted] == ["k:2", "k:2:status"]
    assert has_more is False


# ---------------------------------------------------------------------------
# 출처별 전진 — 실은 것까지만
# ---------------------------------------------------------------------------


def test_k_는_행의_사건이_전부_실린_곳까지만_전진한다():
    k_events = [_k(1, 1), _k(2, 2), _k(2, 2, sub=1), _k(3, 3)]
    emitted = {"k:1", "k:2"}  # k:2:status 가 잘렸다
    assert events.advance_kanban(0, [1, 2, 3], k_events, emitted) == 1
    assert events.advance_kanban(0, [1, 2, 3], k_events, {"k:1", "k:2", "k:2:status"}) == 2
    assert events.advance_kanban(0, [1, 2, 3], k_events, set()) == 0


def test_k_는_사건을_내지_않은_행도_본_것으로_친다():
    # 행 2 는 무시된 kind 라 사건이 없다 — 그래도 커서는 넘어가야 매번 다시 읽지 않는다.
    assert events.advance_kanban(0, [1, 2, 3], [_k(1, 1), _k(3, 3)], {"k:1"}) == 2
    assert events.advance_kanban(5, [6, 7], [], set()) == 7


def test_d_는_실은_줄까지만():
    d_events = [_d(1, 1), _d(2, 2), _d(3, 3)]
    assert events.advance_deleted(0, d_events, {"d:1", "d:2"}) == 2
    assert events.advance_deleted(0, d_events, {"d:2"}) == 0
    assert events.advance_deleted(4, [], set()) == 4


def test_c_의_t_는_전부_실린_실행까지만_o_는_미완료만():
    c_events = [
        _c(1, "e1", "started", claimed="T1", index=0), _c(2, "e1", "finished", claimed="T1", index=0),
        _c(3, "e2", "started", claimed="T2", index=1),
        _c(4, "e3", "started", claimed="T3", index=2), _c(5, "e3", "finished", claimed="T3", index=2),
    ]
    info = {"p": {"t": "T0", "o": {}, "candidates": [
        ("T1", "e1", "completed", False), ("T2", "e2", "running", False), ("T3", "e3", "completed", False),
    ]}}
    # e3 의 finished 가 잘렸다 → t 는 T2 까지, e3 는 관측자가 본 대로 running 으로 `o` 에 남는다(started 재발행 금지).
    out = events.advance_cron(info, c_events, {e["id"] for e in c_events} - {"c:p:e3:finished"})
    assert out == {"p": {"t": "T2", "o": {"e2": "running", "e3": "running"}}}
    # e3 는 아예 못 실었다 → `o` 에 없던 것이니 넣지 않는다(다음에 다시 페이지된다).
    out = events.advance_cron(info, c_events, {e["id"] for e in c_events} - {"c:p:e3:started", "c:p:e3:finished"})
    assert out == {"p": {"t": "T2", "o": {"e2": "running"}}}
    # 전부 실리면 t 는 T3, e3 는 끝났으니 빠진다.
    out = events.advance_cron(info, c_events, {e["id"] for e in c_events})
    assert out == {"p": {"t": "T3", "o": {"e2": "running"}}}
    # 아무것도 못 실었으면 그대로.
    out = events.advance_cron(info, c_events, set())
    assert out == {"p": {"t": "T0", "o": {}}}


def test_c_는_t_가_못_넘은_끝난_실행을_본_것으로_o_에_남겼다가_넘으면_지운다():
    """앞선 실행이 잘려 t 가 멈추면, 뒤의 끝난 실행은 페이지에 다시 나온다 — `o` 의 표식이 재발행을 막는다."""
    c_events = [
        _c(1, "e1", "started", claimed="T1", index=0),
        _c(2, "e2", "started", claimed="T2", index=1), _c(3, "e2", "finished", claimed="T2", index=1),
    ]
    info = {"p": {"t": None, "o": {}, "candidates": [("T1", "e1", "running", False), ("T2", "e2", "completed", False)]}}
    out = events.advance_cron(info, c_events, {"c:p:e2:started", "c:p:e2:finished"})
    assert out == {"p": {"t": None, "o": {"e2": "completed"}}}
    # 다음 호출: e1 이 실리고 e2 는 `o` 재조회로 왔다(사건 없음) → t 가 T2 까지 가고 표식은 빠진다.
    info = {"p": {"t": None, "o": {"e2": "completed"}, "candidates": [("T2", "e2", "completed", True), ("T1", "e1", "running", False)]}}
    out = events.advance_cron(info, [_c(1, "e1", "started", claimed="T1", index=1)], {"c:p:e1:started"})
    assert out == {"p": {"t": "T2", "o": {"e1": "running"}}}


def test_c_의_o_에서_온_실행의_finished_가_잘리면_o_에_그대로_남긴다():
    c_events = [_c(2, "e1", "finished", claimed="T1", index=0)]
    info = {"p": {"t": "T5", "o": {"e1": "running"}, "candidates": [("T1", "e1", "completed", True)]}}
    assert events.advance_cron(info, c_events, set()) == {"p": {"t": "T5", "o": {"e1": "running"}}}
    assert events.advance_cron(info, c_events, {"c:p:e1:finished"}) == {"p": {"t": "T5", "o": {}}}


def test_c_는_같은_claimed_at_묶음에_잘린_것이_있으면_그_시각을_넘지_않는다():
    c_events = [_c(1, "a", "started", claimed="T1", index=0), _c(1, "b", "started", claimed="T1", index=1)]
    info = {"p": {"t": None, "o": {}, "candidates": [("T1", "a", "running", False), ("T1", "b", "running", False)]}}
    out = events.advance_cron(info, c_events, {"c:p:a:started"})
    assert out == {"p": {"t": None, "o": {"a": "running"}}}  # a 는 실렸으니 `o` 로 기억한다


# ---------------------------------------------------------------------------
# 여러 호출에 걸친 통합 — 잘린 사건은 다시 나오고, 전체는 정확히 한 번씩
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


async def test_작은_limit_으로_여러_번_불러도_모든_사건이_정확히_한_번_시간순으로_나온다(aiohttp_client, fake_api, kanban, store):
    base = int(time.time()) - 1000
    conn = kanban.connect(board="default")
    task = kanban.make_task(conn, title="카드")
    created = kanban.boards["default"].events[0]
    created.created_at = base + 1
    running = kanban.emit("default", task.id, "status", {"status": "running"}, ts=base + 3)
    kanban.set_status("default", task.id, "done")
    completed = kanban.emit("default", task.id, "completed", {"summary": "끝"}, ts=base + 7)  # run.finished + status 쌍
    ignored = kanban.emit("default", task.id, "claimed", {}, ts=base + 8)  # 무시
    append_deleted(fake_api, "default", "t0099", "지움", base + 5)
    fake_api.create_profile("sophie")
    home = fake_api.get_profile_dir("sophie")
    store.add_job(home, id="job001", name="잡")
    store.add_execution(home, id="ex1", job_id="job001", status="completed", claimed_at=_iso(base + 2),
                        started_at=_iso(base + 2), finished_at=_iso(base + 6))
    store.add_execution(home, id="ex2", job_id="job001", status="running", claimed_at=_iso(base + 9), started_at=_iso(base + 9))

    client = await events_client(aiohttp_client, fake_api)
    cursor = events.encode_cursor({"k": 0, "d": 0, "c": {"sophie": {"t": None, "o": {}}}})
    seen = []
    pages = 0
    while True:
        body = await (await client.get(f"/deskrpg/events?board=default&cursor={cursor}&limit=2")).json()
        pages += 1
        assert len(body["events"]) <= 2
        seen.extend(body["events"])
        cursor = body["cursor"]
        if not body["has_more"]:
            break
        assert pages < 20

    ids = [e["id"] for e in seen]
    assert ids == [
        f"k:{created.id}",  # created (ts+1)
        "c:sophie:ex1:started",  # ts+2
        f"k:{running.id}",  # status running (ts+3)
        "d:1",  # deleted (ts+5)
        "c:sophie:ex1:finished",  # ts+6
        f"k:{completed.id}",  # run.finished (ts+7)
        f"k:{completed.id}:status",  # 같은 행의 status 쌍
        "c:sophie:ex2:started",  # ts+9
    ]
    assert len(ids) == len(set(ids))
    assert [e["ts"] for e in seen] == sorted(e["ts"] for e in seen)
    assert pages == 5  # 8 개를 2 개씩, 마지막에 has_more 가 꺼지는 빈 페이지 없이 4 페이지 + 확인 1 페이지 이하
    assert not any("_src" in e or "_pos" in e for e in seen)

    # 다 읽은 커서로 다시 부르면 비어 있고, ex2 는 `o` 에 남아 있다.
    body = await (await client.get(f"/deskrpg/events?board=default&cursor={cursor}")).json()
    assert body["events"] == [] and body["has_more"] is False
    state = events.decode_cursor(body["cursor"])
    assert state["k"] == ignored.id and state["d"] == 1  # 무시된 행도 본 것으로 넘어간다
    assert state["c"]["sophie"]["o"] == {"ex2": "running"}


async def test_잘린_사건은_다음_호출에_다시_나온다(aiohttp_client, fake_api, kanban, store):
    base = int(time.time()) - 100
    conn = kanban.connect(board="default")
    task = kanban.make_task(conn, title="카드")
    created = kanban.boards["default"].events[0]
    created.created_at = base + 1
    c1 = kanban.emit("default", task.id, "commented", {"i": 1}, ts=base + 2)
    c2 = kanban.emit("default", task.id, "commented", {"i": 2}, ts=base + 3)

    client = await events_client(aiohttp_client, fake_api)
    cursor = events.encode_cursor({"k": 0, "d": 0, "c": {}})
    first = await (await client.get(f"/deskrpg/events?board=default&cursor={cursor}&limit=1")).json()
    assert [e["id"] for e in first["events"]] == [f"k:{created.id}"] and first["has_more"] is True
    assert events.decode_cursor(first["cursor"])["k"] == created.id
    second = await (await client.get(f"/deskrpg/events?board=default&cursor={first['cursor']}&limit=5")).json()
    assert [e["id"] for e in second["events"]] == [f"k:{c1.id}", f"k:{c2.id}"]
    assert second["has_more"] is False


async def test_커서에_없는_프로필의_위치는_유지된다(aiohttp_client, fake_api, kanban, store):
    """프로필이 잠시 없어졌다 돌아와도 그 위치를 잃지 않는다 — 삭제된 프로필의 항목도 그냥 남는다."""
    client = await events_client(aiohttp_client, fake_api)
    cursor = events.encode_cursor({"k": 0, "d": 0, "c": {"gone": {"t": "T9", "o": {"x": "running"}}}})
    body = await (await client.get(f"/deskrpg/events?board=default&cursor={cursor}")).json()
    assert events.decode_cursor(body["cursor"])["c"] == {"gone": {"t": "T9", "o": {"x": "running"}}}
