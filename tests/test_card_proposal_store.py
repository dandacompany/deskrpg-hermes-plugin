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


def test_unresolve_는_카드가_없는_해소만_되돌린다(tmp_api):
    pid = store.create(tmp_api, profile="noah", title="t", summary="s", body=None, acceptance=None)
    assert store.unresolve(tmp_api, pid) is False           # 아직 해소되지 않았다
    assert store.resolve(tmp_api, pid, "card", None) is True
    assert store.unresolve(tmp_api, pid) is True
    assert store.unresolve(tmp_api, pid) is False           # 두 번 되돌리지 않는다
    assert store.resolve(tmp_api, pid, "card", "task-1") is True
    assert store.unresolve(tmp_api, pid) is False           # 카드가 기록된 제안은 다시 열지 않는다
    assert store.get(tmp_api, pid)["resolved_task_id"] == "task-1"


def test_unresolve_unknown_id_returns_false(tmp_api):
    assert store.unresolve(tmp_api, "nope") is False


def test_record_task_는_해소된_제안에_한_번만_적는다(tmp_api):
    pid = store.create(tmp_api, profile="noah", title="t", summary="s", body=None, acceptance=None)
    assert store.record_task(tmp_api, pid, "t-1") is False   # 해소되지 않았다
    assert store.resolve(tmp_api, pid, "card", None) is True
    assert store.record_task(tmp_api, pid, "t-1") is True
    assert store.record_task(tmp_api, pid, "t-2") is False   # 덮지 않는다
    assert store.get(tmp_api, pid)["resolved_task_id"] == "t-1"
    assert store.unresolve(tmp_api, pid) is False            # 가드가 살아 있다
    assert store.record_task(tmp_api, "nope", "t-1") is False
