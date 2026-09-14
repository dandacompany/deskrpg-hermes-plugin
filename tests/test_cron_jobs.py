"""K1·K2·K4–K8 — 프로필 스코프 잡 CRUD·일시정지·재개·실행·삭제·배달처."""

import pytest
from aiohttp import web

from deskrpg_plugin import cron
from deskrpg_plugin.common import RequestError
from deskrpg_plugin.contract_fields import CRON_JOB_REQUIRED, DELIVERY_TARGET_KEYS
from tests.conftest import FakeAdapter
from tests.fakes_cron import FakeExternalCronScheduler, install_fake_cron, mount_cron_routes


@pytest.fixture
def store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


@pytest.fixture
def profile(fake_api):
    fake_api.create_profile("sophie")
    return fake_api.get_profile_dir("sophie")


async def _client(aiohttp_client, fake_api, authorized=True):
    app = web.Application()
    mount_cron_routes(app, FakeAdapter(authorized=authorized), fake_api)
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# K1 — 스코프
# ---------------------------------------------------------------------------


def test_cron_scope_는_홈_오버라이드와_저장소를_같이_걸고_나가며_원상복구한다(fake_api, store, profile):
    with cron.cron_scope(fake_api, "sophie") as home:
        assert home == profile
        assert store.current_home == profile
        assert store.home_override_stack == [profile]
    assert store.current_home is None
    assert store.home_override_stack == []


def test_cron_scope_는_없는_프로필에_404_를_던진다(fake_api, store):
    with pytest.raises(RequestError) as info:
        with cron.cron_scope(fake_api, "nobody"):
            pass
    assert info.value.status == 404


def test_cron_scope_는_이름_형식_오류에_400_을_던진다(fake_api, store):
    with pytest.raises(RequestError) as info:
        with cron.cron_scope(fake_api, "../etc"):
            pass
    assert info.value.status == 400


def test_cron_scope_는_대문자_이름을_정규화해서_찾는다(fake_api, store, profile):
    with cron.cron_scope(fake_api, "Sophie") as home:
        assert home == profile


async def test_잡은_프로필마다_따로_보인다(aiohttp_client, fake_api, store, profile):
    fake_api.create_profile("noah")
    store.add_job(profile, id="s1", name="sophie's")
    store.add_job(fake_api.get_profile_dir("noah"), id="n1", name="noah's")
    client = await _client(aiohttp_client, fake_api)

    body = await (await client.get("/p/sophie/deskrpg/cron/jobs")).json()
    assert [j["id"] for j in body["jobs"]] == ["s1"]
    body = await (await client.get("/p/noah/deskrpg/cron/jobs")).json()
    assert [j["id"] for j in body["jobs"]] == ["n1"]


async def test_없는_프로필은_404(aiohttp_client, fake_api, store):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/nobody/deskrpg/cron/jobs")
    assert resp.status == 404
    assert (await resp.json())["error"] == "profile_not_found"


async def test_인증_실패는_핸들러에_닿지_않는다(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api, authorized=False)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs")
    assert resp.status == 401
    assert store.scope_log == []


# ---------------------------------------------------------------------------
# K2 — 목록·단건
# ---------------------------------------------------------------------------


async def test_목록은_기본으로_비활성_잡을_빼고_include_disabled_면_넣는다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="on")
    store.add_job(profile, id="off", enabled=False, state="paused")
    client = await _client(aiohttp_client, fake_api)

    body = await (await client.get("/p/sophie/deskrpg/cron/jobs")).json()
    assert [j["id"] for j in body["jobs"]] == ["on"]
    body = await (await client.get("/p/sophie/deskrpg/cron/jobs?include_disabled=true")).json()
    assert [j["id"] for j in body["jobs"]] == ["on", "off"]


async def test_잡_레코드는_계약_필수_키를_전부_갖고_state_는_effective_로_덮인다(aiohttp_client, fake_api, store, profile):
    # 저장된 state 는 paused 인데 enabled 가 true 인 모순 레코드 — Hermes 규칙은 scheduled 다.
    store.add_job(profile, id="j1", state="paused", enabled=True)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs/j1")
    assert resp.status == 200
    job = (await resp.json())["job"]
    assert CRON_JOB_REQUIRED <= set(job)
    assert job["state"] == "scheduled"


async def test_없는_잡은_404(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/jobs/nope")
    assert resp.status == 404
    assert (await resp.json())["error"] == "job_not_found"


# ---------------------------------------------------------------------------
# K4 — 생성
# ---------------------------------------------------------------------------


async def test_생성은_201_과_잡을_주고_origin_은_deskrpg_다(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(
        "/p/sophie/deskrpg/cron/jobs",
        json={"schedule": "every 10 m", "prompt": "안부 묻기", "name": "hello", "skills": ["a", "b"], "paused": True},
    )
    assert resp.status == 201, await resp.text()
    job = (await resp.json())["job"]
    assert job["name"] == "hello"
    assert job["deliver"] == "local"
    assert job["skills"] == ["a", "b"]
    assert job["state"] == "paused"
    stored = store.jobs_by_home[profile][0]
    assert stored["origin"] == {"source": "deskrpg"}
    assert store.scheduler.on_jobs_changed_calls == 1


async def test_생성은_schedule_이_없으면_400(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"prompt": "x"})
    assert resp.status == 400
    assert (await resp.json()) == {"error": "missing_field", "detail": "schedule"}


async def test_생성은_script_없이_prompt_가_없으면_400(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 5 m"})
    assert resp.status == 400
    assert (await resp.json())["detail"] == "prompt"


async def test_생성은_script_가_있으면_prompt_없이도_된다(aiohttp_client, fake_api, store, profile):
    scripts = profile / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 5 m", "script": "run.sh"})
    assert resp.status == 201, await resp.text()
    assert store.jobs_by_home[profile][0]["script"] == "run.sh"


async def test_생성의_script_는_샌드박스_밖이면_400(aiohttp_client, fake_api, store, profile, tmp_path):
    outside = tmp_path / "evil.sh"
    outside.write_text("#!/bin/sh\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 5 m", "script": str(outside)})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_script"


async def test_생성의_스케줄_문법_오류는_400_invalid_schedule(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "whenever", "prompt": "x"})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_schedule"
    assert "detail" in body
    assert store.jobs_by_home.get(profile, []) == []


async def test_생성의_스케줄러_등록_실패는_424_이고_저장된_잡을_싣는다(aiohttp_client, fake_api, store, profile):
    store.registration_fails = True
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 5 m", "prompt": "x"})
    assert resp.status == 424
    body = await resp.json()
    assert body["error"] == "scheduler_registration_failed"
    assert body["job"]["id"] == store.jobs_by_home[profile][0]["id"]


async def test_생성_본문이_JSON_객체가_아니면_400(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", data="nope", headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_생성의_repeat_는_불리언을_거절한다(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs", json={"schedule": "every 5 m", "prompt": "x", "repeat": True})
    assert resp.status == 400


# ---------------------------------------------------------------------------
# K5 — 수정
# ---------------------------------------------------------------------------


async def test_수정은_허용_키만_받고_정규화해서_update_job_에_넘긴다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/cron/jobs/j1",
        json={"updates": {"deliver": "", "model": "  gpt  ", "provider": None, "skills": "a, b\nc", "enabled": False, "name": "new"}},
    )
    assert resp.status == 200, await resp.text()
    job = (await resp.json())["job"]
    assert job["deliver"] == "local"
    assert job["model"] == "gpt"
    assert job["provider"] is None
    assert job["skills"] == ["a", "b", "c"]
    assert job["enabled"] is False
    # Hermes 의 update_job 은 enabled=false 만으로 state 를 바꾸지 않는다 — 우리는 그대로 지나가게 한다.
    assert job["state"] == fake_api.effective_job_state(store.jobs_by_home[profile][0])
    assert store.scheduler.on_jobs_changed_calls == 1


async def test_수정은_모르는_키에_400(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/cron/jobs/j1", json={"updates": {"id": "j2"}})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_field"
    assert store.jobs_by_home[profile][0]["id"] == "j1"


async def test_수정은_updates_가_없으면_400(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/cron/jobs/j1", json={"schedule": "every 1 m"})
    assert resp.status == 400
    assert (await resp.json())["detail"] == "updates"


async def test_수정은_없는_잡에_404(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/cron/jobs/nope", json={"updates": {"name": "x"}})
    assert resp.status == 404


async def test_수정의_스케줄_문법_오류는_400(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/cron/jobs/j1", json={"updates": {"schedule": "whenever"}})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_update"


async def test_수정의_script_는_샌드박스_기준_상대_경로로_저장된다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    (profile / "scripts" / "sub").mkdir(parents=True)
    target = profile / "scripts" / "sub" / "go.sh"
    target.write_text("#!/bin/sh\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/cron/jobs/j1", json={"updates": {"script": str(target)}})
    assert resp.status == 200, await resp.text()
    assert store.jobs_by_home[profile][0]["script"] == "sub/go.sh"


def test_normalize_updates_는_enabled_에_불리언만_받는다(profile):
    with pytest.raises(RequestError) as info:
        cron.normalize_updates({"enabled": "yes"}, profile)
    assert info.value.code == "invalid_field"


# ---------------------------------------------------------------------------
# K6 — pause / resume / run
# ---------------------------------------------------------------------------


async def test_pause_와_resume_는_잡을_돌려준다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)

    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/pause")
    assert resp.status == 200
    assert (await resp.json())["job"]["state"] == "paused"

    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/resume")
    assert resp.status == 200
    assert (await resp.json())["job"]["state"] == "scheduled"
    assert store.scheduler.on_jobs_changed_calls == 2


async def test_resume_의_지난_일회성은_409_job_terminal(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1", schedule="once 2020-01-01T09:00:00", past_oneshot=True, enabled=False, state="paused")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/resume")
    assert resp.status == 409
    assert (await resp.json())["error"] == "job_terminal"


async def test_pause_는_없는_잡에_404(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs/nope/pause")
    assert resp.status == 404


async def test_run_은_202_accepted_와_잡을_주고_동기_실행하지_않는다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/run")
    assert resp.status == 202
    body = await resp.json()
    assert body["accepted"] is True
    assert body["job"]["id"] == "j1"
    assert store.jobs_by_home[profile][0]["manual_run_at"]
    assert store.jobs_by_home[profile][0]["last_run_at"] is None


async def test_run_은_일시정지된_잡을_409_job_paused_로_막고_재개시키지_않는다(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1", enabled=False, state="paused")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/run")
    assert resp.status == 409
    assert (await resp.json())["error"] == "job_paused"
    # trigger_job 은 재개까지 해 버린다 — 부르지 않았어야 한다.
    assert store.jobs_by_home[profile][0]["enabled"] is False


async def test_run_은_종료된_일회성에_409_job_terminal(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1", state="completed", enabled=False)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/jobs/j1/run")
    assert resp.status == 409
    assert (await resp.json())["error"] == "job_terminal"


# ---------------------------------------------------------------------------
# K7 — 삭제 · 프로바이더 재조정
# ---------------------------------------------------------------------------


async def test_삭제는_ok_true_이고_없으면_404(aiohttp_client, fake_api, store, profile):
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/p/sophie/deskrpg/cron/jobs/j1")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}
    assert store.jobs_by_home[profile] == []
    assert store.scheduler.on_jobs_changed_calls == 1

    resp = await client.delete("/p/sophie/deskrpg/cron/jobs/j1")
    assert resp.status == 404


async def test_재조정은_외부_프로바이더_프로필_둘_이상이면_건너뛴다(aiohttp_client, fake_api, store, profile):
    fake_api.create_profile("noah")
    store.scheduler = FakeExternalCronScheduler()
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/p/sophie/deskrpg/cron/jobs/j1")
    assert resp.status == 200
    assert store.scheduler.on_jobs_changed_calls == 0


async def test_재조정은_외부_프로바이더라도_프로필이_하나면_부른다(aiohttp_client, fake_api, store, profile):
    store.scheduler = FakeExternalCronScheduler()
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    await client.delete("/p/sophie/deskrpg/cron/jobs/j1")
    assert store.scheduler.on_jobs_changed_calls == 1


async def test_재조정_실패는_요청을_실패시키지_않는다(aiohttp_client, fake_api, store, profile):
    def boom():
        raise RuntimeError("provider exploded")

    fake_api.resolve_cron_scheduler = boom
    store.add_job(profile, id="j1")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/p/sophie/deskrpg/cron/jobs/j1")
    assert resp.status == 200
    assert store.jobs_by_home[profile] == []


async def test_삭제의_경로_탈출_id_는_400(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.delete("/p/sophie/deskrpg/cron/jobs/..")
    assert resp.status in (400, 404)


# ---------------------------------------------------------------------------
# K8 — 배달처
# ---------------------------------------------------------------------------


async def test_배달처는_local_을_앞에_붙인다(aiohttp_client, fake_api, store, profile):
    store.delivery_targets = [{"id": "slack", "name": "Slack", "home_target_set": False, "home_env_var": "SLACK_HOME_CHANNEL"}]
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/delivery-targets")
    assert resp.status == 200
    targets = (await resp.json())["targets"]
    assert [t["id"] for t in targets] == ["local", "slack"]
    assert targets[0] == {"id": "local", "name": "Local", "home_target_set": True, "home_env_var": None}
    for target in targets:
        assert set(target) == DELIVERY_TARGET_KEYS
