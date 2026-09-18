"""아티팩트 사건이 기존 커서에 네 번째 출처로 합쳐진다. 실린 것까지만 전진한다."""
import contextlib
import json
import types

import pytest

from deskrpg_plugin import artifacts_store as store
from deskrpg_plugin import artifacts_hook, events
from deskrpg_plugin.contract_fields import EVENT_KINDS, PLUGIN_EVENT_KEYS, PLUGIN_EVENT_REQUIRED
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import events_client, install_fake_events


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    return types.SimpleNamespace(get_hermes_home=lambda: str(home))


def _emit(api, event_kind, ts, **payload):
    with contextlib.closing(store.open_registry(api)) as conn, conn:
        store._append_event(conn, ts, event_kind, payload)


def test_읽기는_after_id_이후를_오름차순으로_주고_모양이_계약대로다(api):
    _emit(api, "artifact.created", 10, artifact_id="x", version=1, profile="sophie")
    _emit(api, "artifact.deleted", 11, artifact_id="x", deleted_by="human:u")
    rows = events.read_artifact_events(api, after_id=0, limit=10)
    assert [r["kind"] for r in rows] == ["artifact.created", "artifact.deleted"]
    assert rows[0]["id"] == "a:1" and rows[0]["ts"] == 10 and rows[0]["artifact_id"] == "x"
    assert rows[0]["_src"] == "a" and rows[0]["_pos"] == (1,)
    assert events.read_artifact_events(api, after_id=1, limit=10)[0]["id"] == "a:2"


def test_레지스트리가_없으면_빈_목록이고_만들지_않는다(api):
    assert events.read_artifact_events(api, after_id=0, limit=10) == []
    assert not store.registry_path(api).exists()


def test_병합은_ts_같으면_k_d_c_a_순이고_전진은_실린_것까지만():
    k = [{"id": "k:1", "ts": 5, "_src": "k", "_pos": (1, 0)}]
    a = [{"id": "a:1", "ts": 5, "_src": "a", "_pos": (1,)}, {"id": "a:2", "ts": 6, "_src": "a", "_pos": (2,)}]
    emitted, has_more, by_src = events.merge(k, [], [], a, limit=2)
    assert [e["id"] for e in emitted] == ["k:1", "a:1"] and has_more
    assert events.advance_artifacts(0, a, {e["id"] for e in emitted}) == 1


def test_커서에_a_가_없으면_지금_위치로_본다(api):
    _emit(api, "artifact.created", 1, artifact_id="old")
    state = {"k": 0, "d": 0, "c": {}}
    assert events.artifact_position(api, state) == 1


# ---------------------------------------------------------------------------
# 라우트 — ts 단위(초)와 include=artifacts 옵트인(R17)
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def cron_store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


def _cursor(**state):
    return events.encode_cursor({"k": 0, "d": 0, "c": {}, **state})


async def test_아티팩트_ts_는_초라서_칸반_사건과_실제_단위로_섞인다(aiohttp_client, fake_api, kanban, cron_store):
    conn = kanban.connect(board="default")
    task = kanban.make_task(conn, title="카드")
    k_start = max(e.id for e in kanban.boards["default"].events)
    kanban.emit("default", task.id, "commented", {"author": "dante", "len": 3}, ts=100)
    _emit(fake_api, "artifact.created", 50, artifact_id="x", version=1)
    client = await events_client(aiohttp_client, fake_api)
    body = await (await client.get(
        f"/deskrpg/events?board=default&include=artifacts&cursor={_cursor(k=k_start, a=0)}")).json()
    assert [(e["kind"], e["ts"]) for e in body["events"]] == [("artifact.created", 50), ("task.comment", 100)]


async def test_include_가_없으면_아티팩트_사건을_싣지_않고_a_를_그대로_넘긴다(aiohttp_client, fake_api, kanban, cron_store):
    client = await events_client(aiohttp_client, fake_api)
    _emit(fake_api, "artifact.created", 50, artifact_id="x", version=1)
    body = await (await client.get(f"/deskrpg/events?board=default&cursor={_cursor(a=0)}")).json()
    assert body["events"] == []
    assert events.decode_cursor(body["cursor"])["a"] == 0


async def test_include_가_없고_커서에_a_가_없으면_a_를_만들지_않는다(aiohttp_client, fake_api, kanban, cron_store):
    _emit(fake_api, "artifact.created", 50, artifact_id="x", version=1)
    client = await events_client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/events?board=default&cursor={_cursor()}")).json()
    assert body["events"] == []
    assert "a" not in events.decode_cursor(body["cursor"])
    first = await (await client.get("/deskrpg/events?board=default")).json()
    assert "a" not in events.decode_cursor(first["cursor"])


@pytest.mark.parametrize("include", ["artifacts", "foo,artifacts", "artifacts,foo"])
async def test_include_artifacts_면_아티팩트_사건을_싣는다(aiohttp_client, fake_api, kanban, cron_store, include):
    client = await events_client(aiohttp_client, fake_api)
    start = await (await client.get(f"/deskrpg/events?board=default&include={include}")).json()
    assert events.decode_cursor(start["cursor"])["a"] == 0
    _emit(fake_api, "artifact.created", 50, artifact_id="x", version=1)
    body = await (await client.get(
        f"/deskrpg/events?board=default&include={include}&cursor={start['cursor']}")).json()
    assert [e["kind"] for e in body["events"]] == ["artifact.created"]
    assert events.decode_cursor(body["cursor"])["a"] == 1


async def test_include_artifacts_면_a_없는_구커서는_지금_위치에서_시작한다(aiohttp_client, fake_api, kanban, cron_store):
    _emit(fake_api, "artifact.created", 50, artifact_id="old", version=1)
    client = await events_client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/events?board=default&include=artifacts&cursor={_cursor()}")).json()
    assert body["events"] == []
    assert events.decode_cursor(body["cursor"])["a"] == 1


# ---------------------------------------------------------------------------
# 계약 — kind 집합과 키 집합(I1), null 키를 내지 않는다
# ---------------------------------------------------------------------------


def test_아티팩트_사건_다섯_kind_가_계약_kind_와_키_집합을_지킨다(api):
    _emit(api, "artifact.created", 1, artifact_id="x", version=1, kind="document", title="t", profile="sophie",
          source_kind="kanban", board="default", task_id="t1", captured_via="tool")
    _emit(api, "artifact.versioned", 2, artifact_id="x", version=2, kind="document", title="t", profile="sophie",
          source_kind="chat", board=None, task_id=None, captured_via="hook")
    _emit(api, "artifact.deleted", 3, artifact_id="x", deleted_by="human:u")
    _emit(api, "artifact.delete_partial", 4, artifact_id="x", failed_paths_count=1)
    artifacts_hook.record_capture_failure(api, tool_name="write_file", reason="OSError")
    rows = [events.public_event(r) for r in events.read_artifact_events(api, after_id=0, limit=10)]
    assert [r["kind"] for r in rows] == [
        "artifact.created", "artifact.versioned", "artifact.deleted", "artifact.delete_partial",
        "artifact.capture_failed",
    ]
    for ev in rows:
        assert PLUGIN_EVENT_REQUIRED <= set(ev) <= PLUGIN_EVENT_KEYS, ev
        assert ev["kind"] in EVENT_KINDS


def test_capture_failed_는_artifact_id_를_null_로_내지_않는다(api):
    artifacts_hook.record_capture_failure(api, tool_name="write_file", reason="OSError")
    (ev,) = [events.public_event(r) for r in events.read_artifact_events(api, after_id=0, limit=10)]
    assert "artifact_id" not in ev
    assert None not in ev.values()
