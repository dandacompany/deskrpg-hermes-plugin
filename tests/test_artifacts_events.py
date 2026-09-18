"""아티팩트 사건이 기존 커서에 네 번째 출처로 합쳐진다. 실린 것까지만 전진한다."""
import contextlib
import json
import types

import pytest

from deskrpg_plugin import artifacts_store as store
from deskrpg_plugin import events


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    return types.SimpleNamespace(get_hermes_home=lambda: str(home))


def _emit(api, kind, ts, **payload):
    with contextlib.closing(store.open_registry(api)) as conn, conn:
        store._append_event(conn, ts, kind, payload)


def test_읽기는_after_id_이후를_오름차순으로_주고_모양이_계약대로다(api):
    _emit(api, "artifact.created", 10, artifact_id="x", version=1, profile="sophie")
    _emit(api, "artifact.deleted", 11, artifact_id="x", deleted_by="human:u")
    rows = events.read_artifact_events(api, after_id=0, limit=10)
    assert [r["kind"] for r in rows] == ["artifact.created", "artifact.deleted"]
    assert rows[0]["id"] == "a:1" and rows[0]["ts"] == 10_000 and rows[0]["artifact_id"] == "x"
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
