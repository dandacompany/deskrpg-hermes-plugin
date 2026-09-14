"""E4 · E6 — 크론 사건: 장부 tail, `o` 운반, 잡 레코드 폴백, 장부 없는 프로필."""

import time
from datetime import datetime

import pytest

from deskrpg_plugin import events
from deskrpg_plugin.contract_fields import (
    CRON_RUN_FINISHED_PAYLOAD_KEYS,
    CRON_RUN_STARTED_PAYLOAD_KEYS,
    PLUGIN_EVENT_KEYS,
)
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import install_fake_events


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


@pytest.fixture
def home(fake_api):
    fake_api.create_profile("sophie")
    return fake_api.get_profile_dir("sophie")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def _tz(fake_api):
    return fake_api.get_timezone()


def _run(fake_api, cursor_c):
    """cron_tail 을 돌리고 (공개 사건, 새 c) 를 돌려준다 — 전부 실렸다고 보고 전진한다."""
    evs, info = events.cron_tail(fake_api, cursor_c, _tz(fake_api))
    new_c = events.advance_cron(info, evs, {e["id"] for e in evs})
    return [events.public_event(e) for e in evs], new_c


# ---------------------------------------------------------------------------
# 장부가 있는 프로필
# ---------------------------------------------------------------------------


def test_running_진입은_started_한_번만_낸다(fake_api, store, home):
    base = time.time() - 60
    store.add_job(home, id="job001", name="아침 브리핑")
    store.add_execution(home, id="exec1", job_id="job001", status="running", claimed_at=_iso(base), started_at=_iso(base + 1))

    evs, c = _run(fake_api, {})
    assert [e["kind"] for e in evs] == ["cron.run.started"]
    ev = evs[0]
    assert ev["id"] == "c:sophie:exec1:started"
    assert ev["profile"] == "sophie"
    assert ev["job_id"] == "job001"
    assert ev["ts"] == pytest.approx(base + 1)
    assert set(ev) <= PLUGIN_EVENT_KEYS
    assert set(ev["payload"]) == CRON_RUN_STARTED_PAYLOAD_KEYS
    assert ev["payload"]["job_name"] == "아침 브리핑"
    assert ev["payload"]["session_id"] is None
    assert c["sophie"] == {"t": _iso(base), "o": {"exec1": "running"}}

    # 같은 상태로 다시 부르면 아무것도 안 나오고 `o` 는 그대로 운반된다.
    again, c2 = _run(fake_api, c)
    assert again == []
    assert c2 == c


def test_completed_진입은_finished_를_내고_세션_결과_본문을_싣는다(fake_api, store, home):
    base = time.time() - 300
    store.add_job(home, id="job001", name="브리핑")
    store.add_execution(home, id="exec1", job_id="job001", status="running", claimed_at=_iso(base), started_at=_iso(base + 1))
    _, c = _run(fake_api, {})

    session = store.add_session(home, id="cron_job001_abc", started_at=base + 3, ended_at=base + 50)
    store.add_message(home, session["id"], "user", "해줘")
    store.add_message(home, session["id"], "assistant", "오늘의 브리핑입니다.")
    store.executions_by_home[home][0].update(status="completed", finished_at=_iso(base + 50))

    evs, c2 = _run(fake_api, c)
    assert [e["kind"] for e in evs] == ["cron.run.finished"]
    ev = evs[0]
    assert ev["id"] == "c:sophie:exec1:finished"
    assert ev["ts"] == pytest.approx(base + 50)
    assert set(ev["payload"]) == CRON_RUN_FINISHED_PAYLOAD_KEYS
    assert ev["payload"]["status"] == "ok"
    assert ev["payload"]["session_id"] == "cron_job001_abc"
    assert ev["payload"]["result_text"] == "오늘의 브리핑입니다."
    assert ev["payload"]["started_at"] == _iso(base + 1)
    assert ev["payload"]["ended_at"] == _iso(base + 50)
    # 끝난 실행은 `o` 에서 빠진다(E6).
    assert c2["sophie"]["o"] == {}
    assert c2["sophie"]["t"] == _iso(base)


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_failed_unknown_은_status_error(fake_api, store, home, status):
    base = time.time() - 100
    store.add_job(home, id="job001")
    store.add_execution(home, id="exec1", job_id="job001", status=status, claimed_at=_iso(base), started_at=_iso(base + 1), finished_at=_iso(base + 9))
    evs, _ = _run(fake_api, {})
    assert [e["kind"] for e in evs] == ["cron.run.started", "cron.run.finished"]
    assert evs[1]["payload"]["status"] == "error"
    assert evs[1]["payload"]["result_text"] == ""


def test_한_번도_안_본_완료_실행은_started_와_finished_를_둘_다_낸다(fake_api, store, home):
    base = time.time() - 100
    store.add_job(home, id="job001")
    store.add_execution(home, id="exec1", job_id="job001", status="completed", claimed_at=_iso(base), started_at=_iso(base + 1), finished_at=_iso(base + 2))
    evs, c = _run(fake_api, {"sophie": {"t": None, "o": {}}})
    assert [e["id"] for e in evs] == ["c:sophie:exec1:started", "c:sophie:exec1:finished"]
    assert c["sophie"]["o"] == {}


def test_claimed_만_된_실행은_사건_없이_o_에_남았다가_running_이_되면_started(fake_api, store, home):
    base = time.time() - 100
    store.add_job(home, id="job001")
    store.add_execution(home, id="exec1", job_id="job001", status="claimed", claimed_at=_iso(base), started_at=None)
    evs, c = _run(fake_api, {})
    assert evs == []
    assert c["sophie"] == {"t": _iso(base), "o": {"exec1": "claimed"}}

    store.executions_by_home[home][0].update(status="running", started_at=_iso(base + 5))
    evs, c2 = _run(fake_api, c)
    assert [e["id"] for e in evs] == ["c:sophie:exec1:started"]
    assert c2["sophie"]["o"] == {"exec1": "running"}


def test_t_이후_행만_페이지하고_t_는_가장_늦은_claimed_at_으로_간다(fake_api, store, home):
    base = time.time() - 1000
    store.add_job(home, id="job001")
    for i in range(3):
        store.add_execution(home, id=f"exec{i}", job_id="job001", status="completed",
                            claimed_at=_iso(base + i * 100), started_at=_iso(base + i * 100 + 1), finished_at=_iso(base + i * 100 + 2))
    evs, c = _run(fake_api, {"sophie": {"t": _iso(base), "o": {}}})
    assert sorted({e["id"].split(":")[2] for e in evs}) == ["exec1", "exec2"]
    assert c["sophie"]["t"] == _iso(base + 200)


def test_o_의_실행이_장부에서_사라지면_조용히_잊는다(fake_api, store, home):
    store.add_execution(home, id="other", job_id="job001", status="completed", claimed_at=_iso(time.time() - 5000))
    evs, c = _run(fake_api, {"sophie": {"t": _iso(time.time()), "o": {"ghost": "running"}}})
    assert evs == []
    assert c["sophie"]["o"] == {}


def test_페이지는_500_행_단위로_before_claimed_at_을_넘기며_이어_읽는다(fake_api, store, home, monkeypatch):
    base = time.time() - 100_000
    store.add_job(home, id="job001")
    for i in range(3):
        store.add_execution(home, id=f"exec{i}", job_id="job001", status="completed",
                            claimed_at=_iso(base + i * 100), started_at=_iso(base + i * 100 + 1), finished_at=_iso(base + i * 100 + 2))
    monkeypatch.setattr(events.cron_results, "EXECUTIONS_PAGE_LIMIT", 1)
    calls = []
    real = fake_api.list_executions

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    fake_api.list_executions = spy
    evs, c = _run(fake_api, {"sophie": {"t": _iso(base), "o": {}}})
    assert sorted({e["id"].split(":")[2] for e in evs}) == ["exec1", "exec2"]
    assert [k.get("before_claimed_at") for k in calls] == [None, _iso(base + 200), _iso(base + 100)]


# ---------------------------------------------------------------------------
# 장부가 없는 프로필
# ---------------------------------------------------------------------------


def test_장부_없는_프로필은_잡_레코드_폴백_fire_claim_은_started_한_번(fake_api, store, home):
    at = _iso(time.time() - 30)
    store.add_job(home, id="job001", name="폴백", fire_claim={"at": at, "by": "m:1"})
    assert not (home / "cron" / "executions.db").exists()

    evs, c = _run(fake_api, {})
    assert [e["id"] for e in evs] == [f"c:sophie:job001@{at}:started"]
    assert evs[0]["payload"]["job_name"] == "폴백"
    assert evs[0]["payload"]["started_at"] == at
    assert c["sophie"]["o"] == {f"job001@{at}": "running"}

    again, c2 = _run(fake_api, c)
    assert again == []
    assert c2 == c


def test_장부_없는_프로필은_last_run_at_전진을_finished_로_본다(fake_api, store, home):
    at = _iso(time.time() - 60)
    done = _iso(time.time() - 10)
    store.add_job(home, id="job001", fire_claim={"at": at, "by": "m:1"})
    _, c = _run(fake_api, {})

    job = store.jobs_by_home[home][0]
    job.update(fire_claim=None, last_run_at=done, last_status="ok")
    evs, c2 = _run(fake_api, c)
    assert [e["id"] for e in evs] == [f"c:sophie:job001@{done}:finished"]
    assert evs[0]["payload"]["status"] == "ok"
    assert evs[0]["payload"]["started_at"] == at
    assert evs[0]["payload"]["ended_at"] == done
    assert c2["sophie"] == {"t": done, "o": {}}

    # last_run_at 이 그대로면 다시 내지 않는다.
    again, _ = _run(fake_api, c2)
    assert again == []


def test_폴백_finished_의_error_상태(fake_api, store, home):
    done = _iso(time.time() - 10)
    store.add_job(home, id="job001", last_run_at=done, last_status="error")
    evs, _ = _run(fake_api, {"sophie": {"t": _iso(time.time() - 100), "o": {}}})
    assert evs[0]["payload"]["status"] == "error"


def test_장부_없는_프로필은_장부_파일을_절대_만들지_않고_list_executions_를_부르지_않는다(fake_api, store, home):
    store.add_job(home, id="job001")

    def forbidden(**kwargs):
        raise AssertionError("장부가 없는 프로필에서 list_executions 를 불렀다")

    fake_api.list_executions = forbidden
    fake_api.get_execution = lambda execution_id: (_ for _ in ()).throw(AssertionError("get_execution 호출"))
    evs, c = _run(fake_api, {"sophie": {"t": None, "o": {"stale": "running"}}})
    assert evs == []
    assert not (home / "cron").exists()
    assert c["sophie"] == {"t": None, "o": {}}


def test_지금_위치는_장부_있는_프로필과_없는_프로필을_각각_계산한다(fake_api, store):
    fake_api.create_profile("alpha")
    fake_api.create_profile("beta")
    alpha = fake_api.get_profile_dir("alpha")
    beta = fake_api.get_profile_dir("beta")
    store.add_execution(alpha, id="a1", job_id="job001", status="running", claimed_at="2026-01-01T00:00:00+09:00")
    store.add_execution(alpha, id="a2", job_id="job001", status="completed", claimed_at="2026-01-02T00:00:00+09:00")
    store.add_job(beta, id="job009", last_run_at="2026-01-03T00:00:00+09:00", fire_claim={"at": "2026-01-04T00:00:00+09:00", "by": "x"})

    now = events.cron_now_positions(fake_api)
    assert now["alpha"] == {"t": "2026-01-02T00:00:00+09:00", "o": {"a1": "running"}}
    assert now["beta"] == {"t": "2026-01-03T00:00:00+09:00", "o": {"job009@2026-01-04T00:00:00+09:00": "running"}}


def test_한_프로필의_실패는_다른_프로필을_막지_않는다(fake_api, store):
    fake_api.create_profile("good")
    fake_api.create_profile("bad")
    good = fake_api.get_profile_dir("good")
    store.add_job(good, id="job001")
    store.add_execution(good, id="g1", job_id="job001", status="running", claimed_at=_iso(time.time() - 5), started_at=_iso(time.time() - 4))
    store.add_execution(fake_api.get_profile_dir("bad"), id="b1", job_id="job001", status="running", claimed_at=_iso(time.time() - 5))
    real = fake_api.list_executions

    def flaky(**kwargs):
        if store.current_home == fake_api.get_profile_dir("bad"):
            raise RuntimeError("장부 손상")
        return real(**kwargs)

    fake_api.list_executions = flaky
    evs, c = _run(fake_api, {})
    assert [e["profile"] for e in evs] == ["good"]
    assert "bad" not in c  # 실패한 프로필의 위치는 건드리지 않는다


def test_프로필_스코프_안에서만_장부를_읽는다(fake_api, store, home):
    store.add_execution(home, id="e1", job_id="job001", status="running", claimed_at=_iso(time.time() - 5), started_at=_iso(time.time() - 4))
    _run(fake_api, {})
    assert store.scope_log and all(entry == (home, home) for entry in store.scope_log)
    assert store.current_home is None and store.home_override_stack == []


# ---------------------------------------------------------------------------
# job_name 은 null 이 되지 않는다 — 잡이 없거나 이름이 비면 잡 id
# ---------------------------------------------------------------------------


def test_장부_사건의_job_name_은_잡이_지워졌으면_잡_id_다(fake_api, store, home):
    base = time.time() - 60
    # 잡 레코드 없이 장부만 — 실행 뒤 잡을 지운 경우.
    store.add_execution(home, id="exec1", job_id="gone01", status="running", claimed_at=_iso(base), started_at=_iso(base + 1))
    evs, _ = _run(fake_api, {})
    assert evs[0]["payload"]["job_name"] == "gone01"


def test_장부_사건의_job_name_은_이름이_비어_있으면_잡_id_다(fake_api, store, home):
    base = time.time() - 60
    store.add_job(home, id="job001", name="")
    store.add_execution(home, id="exec1", job_id="job001", status="running", claimed_at=_iso(base), started_at=_iso(base + 1))
    evs, _ = _run(fake_api, {})
    assert evs[0]["payload"]["job_name"] == "job001"


def test_잡_레코드_폴백의_job_name_도_이름이_없으면_잡_id_다(fake_api, store, home):
    at = _iso(time.time() - 30)
    store.add_job(home, id="job001", name=None, fire_claim={"at": at, "by": "m:1"})
    evs, _ = _run(fake_api, {})
    assert evs[0]["payload"]["job_name"] == "job001"
