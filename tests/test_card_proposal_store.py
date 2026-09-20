"""카드 제안 저장소 — id·사건 append 와 "한 번만 해소" 보장."""

import deskrpg_plugin.card_proposal_store as store


def test_create_returns_id_and_appends_event(tmp_api):
    pid = store.create(tmp_api, profile="noah", title="주간 보고 정리",
                       summary="매주 금요일 보고서를 모아 정리한다", body=None, acceptance=None)
    assert pid
    events = tmp_api.events()
    assert [e["kind"] for e in events] == ["card_proposal.created"]
    assert events[0]["payload"]["proposal_id"] == pid
    assert events[0]["payload"]["title"] == "주간 보고 정리"
    assert events[0]["profile"] == "noah"


def test_resolve_is_once_only(tmp_api):
    pid = store.create(tmp_api, profile="noah", title="t", summary="s", body=None, acceptance=None)
    assert store.resolve(tmp_api, pid, "card", "task-1") is True
    assert store.resolve(tmp_api, pid, "card", "task-2") is False
    assert store.get(tmp_api, pid)["resolved_choice"] == "card"
    assert store.get(tmp_api, pid)["resolved_task_id"] == "task-1"


def test_resolve_unknown_id_returns_false(tmp_api):
    assert store.resolve(tmp_api, "nope", "card", None) is False


def test_get_unknown_id_returns_none(tmp_api):
    assert store.get(tmp_api, "nope") is None
