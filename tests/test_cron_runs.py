"""K3 · E4 — 실행 이력과 결과 본문(cron_results)."""

import time
from datetime import datetime, timedelta

import pytest
from aiohttp import web

from deskrpg_plugin import cron_results
from deskrpg_plugin.contract_fields import CRON_RUN_KEYS
from tests.conftest import FakeAdapter
from tests.fakes_cron import install_fake_cron, mount_cron_routes


@pytest.fixture
def store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


@pytest.fixture
def profile(fake_api):
    fake_api.create_profile("sophie")
    return fake_api.get_profile_dir("sophie")


async def _client(aiohttp_client, fake_api):
    app = web.Application()
    mount_cron_routes(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


# ---------------------------------------------------------------------------
# 순수 도우미
# ---------------------------------------------------------------------------


def test_to_epoch_는_epoch_ISO_datetime_을_같은_값으로_본다():
    now = 1_700_000_000.0
    assert cron_results.to_epoch(now) == now
    assert cron_results.to_epoch(_iso(now)) == pytest.approx(now)
    assert cron_results.to_epoch(datetime.fromtimestamp(now).astimezone()) == pytest.approx(now)
    assert cron_results.to_epoch("2023-11-14T22:13:20Z") == pytest.approx(now)
    assert cron_results.to_epoch(None) is None
    assert cron_results.to_epoch("garbage") is None
    assert cron_results.to_epoch(True) is None


def test_run_status_for_는_completed_ok_failed_unknown_error_그외_None():
    assert cron_results.run_status_for({"status": "completed"}) == "ok"
    assert cron_results.run_status_for({"status": "failed"}) == "error"
    assert cron_results.run_status_for({"status": "unknown"}) == "error"
    assert cron_results.run_status_for({"status": "running"}) is None
    assert cron_results.run_status_for(None) is None


def test_truncate_result_는_상한을_넘으면_자르고_말줄임표를_붙인다():
    assert cron_results.truncate_result("가" * 5, max_chars=5) == "가" * 5
    assert cron_results.truncate_result("가" * 6, max_chars=5) == "가" * 5 + "…"
    assert cron_results.truncate_result(None) == ""


def test_clamp_runs_limit_는_1과_50_사이로_죈다():
    assert cron_results.clamp_runs_limit(None) == 20
    assert cron_results.clamp_runs_limit("abc") == 20
    assert cron_results.clamp_runs_limit("0") == 1
    assert cron_results.clamp_runs_limit("999") == 50
    assert cron_results.clamp_runs_limit("7") == 7


def test_match_execution_은_창_안에서_가장_가까운_실행을_고른다():
    base = 1_700_000_000.0
    far = {"id": "far", "started_at": _iso(base + 100)}
    near = {"id": "near", "started_at": _iso(base + 5)}
    out = {"id": "out", "started_at": _iso(base + 500)}
    assert cron_results.match_execution([far, near, out], base)["id"] == "near"
    assert cron_results.match_execution([out], base) is None
    # started_at 이 비면 claimed_at 으로 잰다.
    claimed = {"id": "c", "started_at": None, "claimed_at": _iso(base + 1)}
    assert cron_results.match_execution([claimed], base)["id"] == "c"


def test_find_session_for_execution_은_접두사와_시각_창으로_찾는다(fake_api, store, profile):
    base = 1_700_000_000.0
    store.add_session(profile, id="cron_j1_a", started_at=base + 3)
    store.add_session(profile, id="cron_j1_b", started_at=base + 900)
    store.add_session(profile, id="cron_j2_c", started_at=base)
    sdb = cron_results.open_session_db(fake_api, profile)
    assert sdb.read_only is True
    assert cron_results.find_session_for_execution(sdb, "j1", _iso(base)) == "cron_j1_a"
    assert cron_results.find_session_for_execution(sdb, "j1", _iso(base + 2000)) is None
    assert cron_results.find_session_for_execution(sdb, "j9", base) is None


def test_last_assistant_text_는_본문_있는_마지막_assistant_를_고른다(fake_api, store, profile):
    store.add_session(profile, id="cron_j1_a")
    store.add_message(profile, "cron_j1_a", "user", "질문")
    store.add_message(profile, "cron_j1_a", "assistant", "첫 답")
    store.add_message(profile, "cron_j1_a", "assistant", [{"type": "text", "text": "블록 답"}])
    store.add_message(profile, "cron_j1_a", "assistant", "")  # 도구 호출만 있는 행
    store.add_message(profile, "cron_j1_a", "tool", "결과")
    sdb = cron_results.open_session_db(fake_api, profile)
    assert cron_results.last_assistant_text(sdb, "cron_j1_a") == "블록 답"
    assert cron_results.last_assistant_text(sdb, "none") == ""


def test_output_file_text_는_실행_창_안의_가장_늦은_파일을_읽는다(fake_api, store, profile):
    start = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
    store.write_output_file(profile, "j1", start - timedelta(hours=1), "옛날 것")
    store.write_output_file(profile, "j1", start + timedelta(seconds=30), "이번 것")
    store.write_output_file(profile, "j1", start + timedelta(seconds=90), "이번 것 마지막")
    with fake_api.use_cron_store(profile):
        text = cron_results.output_file_text(fake_api, "j1", start.timestamp(), (start + timedelta(minutes=2)).timestamp())
        assert text == "이번 것 마지막"
        assert cron_results.output_file_text(fake_api, "j1", (start - timedelta(days=1)).timestamp(), (start - timedelta(days=1)).timestamp()) == ""
        assert cron_results.output_file_text(fake_api, "none", start.timestamp()) == ""


def test_result_text_for_는_세션_다음_파일_다음_빈문자열_순이고_상한을_적용한다(fake_api, store, profile, monkeypatch):
    base = time.time() - 600
    store.add_session(profile, id="cron_j1_a", started_at=base)
    store.add_message(profile, "cron_j1_a", "assistant", "세션 답")
    store.write_output_file(profile, "j1", datetime.fromtimestamp(base + 10), "파일 답")
    sdb = cron_results.open_session_db(fake_api, profile)
    with fake_api.use_cron_store(profile):
        assert cron_results.result_text_for(fake_api, sdb, "j1", base) == "세션 답"
        # 세션에 assistant 가 없으면 파일로.
        store.add_session(profile, id="cron_j2_b", started_at=base)
        store.write_output_file(profile, "j2", datetime.fromtimestamp(base + 10), "j2 파일")
        assert cron_results.result_text_for(fake_api, sdb, "j2", base) == "j2 파일"
        # 둘 다 없으면 빈 문자열. sdb 가 None 이어도 죽지 않는다.
        assert cron_results.result_text_for(fake_api, None, "j3", base) == ""
        # 상한.
        monkeypatch.setattr(cron_results, "RESULT_TEXT_MAX_CHARS", 3)
        store.add_message(profile, "cron_j1_a", "assistant", "12345")
        assert cron_results.result_text_for(fake_api, sdb, "j1", base) == "123…"


# ---------------------------------------------------------------------------
# K3 — GET jobs/{id}/runs
# ---------------------------------------------------------------------------


async def test_runs_는_계약_모양이고_상태_규칙을_따른다(aiohttp_client, fake_api, store, profile):
    now = time.time()
    store.add_job(profile, id="j1")
    # 1) 살아 있는 실행: ended_at 없음, 최근 활동.
    store.add_session(profile, id="cron_j1_live", started_at=now - 60, last_active=now - 10, title="live run")
    # 2) 끝난 실행, 장부에 completed.
    store.add_session(profile, id="cron_j1_ok", started_at=now - 3600, ended_at=now - 3500, title="ok run")
    store.add_message(profile, "cron_j1_ok", "assistant", "잘 됐다")
    store.add_execution(profile, job_id="j1", status="completed", started_at=_iso(now - 3598), claimed_at=_iso(now - 3599))
    # 3) 끝난 실행, 장부에 failed.
    store.add_session(profile, id="cron_j1_bad", started_at=now - 7200, ended_at=now - 7100, preview="bad preview")
    store.add_execution(profile, job_id="j1", status="failed", started_at=_iso(now - 7199), claimed_at=_iso(now - 7200))
    # 4) ended_at 없지만 오래 조용함 + 대응 실행 없음 → unknown.
    store.add_session(profile, id="cron_j1_stale", started_at=now - 90000, last_active=now - 89000)
    # 다른 잡의 세션은 섞이지 않는다.
    store.add_session(profile, id="cron_j2_x", started_at=now)

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs/j1/runs?limit=10")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["limit"] == 10
    runs = {r["id"]: r for r in body["runs"]}
    assert list(runs) == ["cron_j1_live", "cron_j1_ok", "cron_j1_bad", "cron_j1_stale"]
    for run in runs.values():
        assert set(run) == CRON_RUN_KEYS
    assert runs["cron_j1_live"]["status"] == "running"
    assert runs["cron_j1_live"]["summary"] == "live run"
    assert runs["cron_j1_ok"]["status"] == "ok"
    assert runs["cron_j1_ok"]["result_text"] == "잘 됐다"
    assert runs["cron_j1_bad"]["status"] == "error"
    assert runs["cron_j1_bad"]["summary"] == "bad preview"
    assert runs["cron_j1_bad"]["result_text"] == ""
    assert runs["cron_j1_stale"]["status"] == "unknown"
    # 세션 DB 는 읽기 전용으로 열고 요청 뒤 닫았다.
    assert store.opened_session_dbs and all(db.read_only and db.closed for db in store.opened_session_dbs)


async def test_runs_는_limit_을_50_으로_죈다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/cron/jobs/j1/runs?limit=500")).json()
    assert body["limit"] == 50


async def test_runs_는_state_db_가_없으면_빈_목록(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/cron/jobs/j1/runs")).json()
    assert body == {"runs": [], "limit": 20}
    assert store.opened_session_dbs == []


async def test_runs_는_장부_파일이_없으면_열지_않고_unknown_으로_둔다(aiohttp_client, fake_api, store, profile):
    now = time.time()
    store.add_job(profile, id="j1")
    store.add_session(profile, id="cron_j1_done", started_at=now - 3600, ended_at=now - 3500)
    calls = []
    original = fake_api.list_executions

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    fake_api.list_executions = spy
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/cron/jobs/j1/runs")).json()
    assert body["runs"][0]["status"] == "unknown"
    assert calls == []
    assert not (profile / "cron" / "executions.db").exists()


async def test_runs_는_없는_잡에_404(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs/nope/runs")
    assert resp.status == 404


async def test_runs_는_장부_읽기_실패에도_이력을_준다(aiohttp_client, fake_api, store, profile):
    now = time.time()
    store.add_job(profile, id="j1")
    store.add_session(profile, id="cron_j1_done", started_at=now - 3600, ended_at=now - 3500)
    store.add_execution(profile, job_id="j1")

    def boom(**kwargs):
        raise RuntimeError("ledger locked")

    fake_api.list_executions = boom
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs/j1/runs")
    assert resp.status == 200
    assert (await resp.json())["runs"][0]["status"] == "unknown"
