"""실제 `cron.jobs`·`cron.executions`·`hermes_state.SessionDB` 위의 크론 시나리오(spec §9 T2).

프로필은 임시 홈 아래 실제 `create_profile` 로 만든다. 잡은 `<home>/profiles/sophie/cron/jobs.json` 에,
실행 장부는 같은 프로필의 `cron/executions.db` 에, 세션은 `state.db` 에 생긴다 — 전부 `tmp_path` 아래다.
스케줄러 틱은 돌지 않으므로 잡이 실제로 실행되는 일은 없다. 실행 장부 행은 Hermes 의 공개 기록 함수
(`create_execution`·`mark_execution_running`·`finish_execution`)로 직접 쓴다.
"""

import time

import pytest

from deskrpg_plugin import contract_fields as cf
from deskrpg_plugin import cron as _cron

pytestmark = pytest.mark.integration


def _p(profile, path):
    return f"/p/{profile}/deskrpg/cron/{path}"


async def _create(client, profile, **body):
    payload = {"schedule": "every 10 m", "prompt": "안부 묻기", "name": "hello", **body}
    resp = await client.post(_p(profile, "jobs"), json=payload)
    assert resp.status == 201, await resp.text()
    return (await resp.json())["job"]


# ---------------------------------------------------------------------------
# K1–K2 스코프·목록·조회
# ---------------------------------------------------------------------------


async def test_없는_프로필은_404_이고_잡_목록은_프로필_파일에서_온다(client, profile, hermes_env):
    resp = await client.get(_p("nobody", "jobs"))
    assert resp.status == 404, await resp.text()
    assert (await (await client.get(_p(profile, "jobs"))).json()) == {"jobs": []}
    job = await _create(client, profile)
    assert cf.CRON_JOB_REQUIRED <= set(job), cf.CRON_JOB_REQUIRED - set(job)
    assert job["origin"] == {"source": "deskrpg"} and job["state"] == "scheduled"
    # 잡 파일은 그 프로필의 cron/ 아래에만 생겼다 — default 홈에는 없다.
    assert (hermes_env["home"] / "profiles" / profile / "cron" / "jobs.json").is_file()
    assert not (hermes_env["home"] / "cron" / "jobs.json").exists()
    listed = await (await client.get(_p(profile, "jobs") + "?include_disabled=true")).json()
    assert [j["id"] for j in listed["jobs"]] == [job["id"]]
    got = await (await client.get(_p(profile, f"jobs/{job['id']}"))).json()
    assert got["job"]["id"] == job["id"]
    assert (await client.get(_p(profile, "jobs/nope"))).status == 404


# ---------------------------------------------------------------------------
# K4–K7 생성 규칙·수정·일시정지·재개·실행·삭제
# ---------------------------------------------------------------------------


async def test_생성_규칙_prompt_또는_script_와_스케줄_문법(client, profile):
    resp = await client.post(_p(profile, "jobs"), json={"schedule": "every 5 m"})
    assert resp.status == 400, await resp.text()  # prompt 도 script 도 없다
    resp = await client.post(_p(profile, "jobs"), json={"schedule": "whenever", "prompt": "x"})
    assert resp.status == 400 and (await resp.json())["error"] == "invalid_schedule", await resp.text()
    resp = await client.post(_p(profile, "jobs"), json={"schedule": "every 5 m", "prompt": "x", "paused": True})
    assert resp.status == 201
    assert (await resp.json())["job"]["state"] == "paused"


async def test_수정_일시정지_재개_실행_삭제(client, profile):
    job = await _create(client, profile)
    jid = job["id"]
    resp = await client.put(_p(profile, f"jobs/{jid}"), json={"updates": {"name": "renamed", "schedule": "every 1 h"}})
    assert resp.status == 200, await resp.text()
    updated = (await resp.json())["job"]
    assert updated["name"] == "renamed"
    assert updated["schedule"]["kind"] == "interval" and updated["schedule"]["minutes"] == 60
    resp = await client.put(_p(profile, f"jobs/{jid}"), json={"updates": {"bogus": 1}})
    assert resp.status == 400
    resp = await client.put(_p(profile, "jobs/nope"), json={"updates": {"name": "x"}})
    assert resp.status == 404

    resp = await client.post(_p(profile, f"jobs/{jid}/pause"))
    assert resp.status == 200 and (await resp.json())["job"]["state"] == "paused", await resp.text()
    # 일시정지 중 run 은 409 job_paused — trigger_job 이 재개까지 해 버리므로 막는다(K6)
    resp = await client.post(_p(profile, f"jobs/{jid}/run"))
    assert resp.status == 409 and (await resp.json())["error"] == "job_paused"
    resp = await client.post(_p(profile, f"jobs/{jid}/resume"))
    assert resp.status == 200 and (await resp.json())["job"]["state"] == "scheduled", await resp.text()
    resp = await client.post(_p(profile, f"jobs/{jid}/run"))
    assert resp.status == 202, await resp.text()
    body = await resp.json()
    assert body["accepted"] is True and body["job"]["id"] == jid
    assert body["job"].get("manual_run_at")  # 다음 틱에 돌도록 표시만 했다 — 동기 실행 없음

    resp = await client.delete(_p(profile, f"jobs/{jid}"))
    assert resp.status == 200 and (await resp.json()) == {"ok": True}
    assert (await client.get(_p(profile, f"jobs/{jid}"))).status == 404
    assert (await client.delete(_p(profile, f"jobs/{jid}"))).status == 404


async def test_끝난_일회성_잡은_resume_run_이_409_job_terminal(client, profile, api):
    # 과거 시각의 일회성은 Hermes 가 생성 자체를 거절한다(실측: "more than 120s in the past") — 미래로 만든 뒤
    # 실제 저장소 함수로 "이미 돌아서 끝났다" 상태를 만든다.
    import datetime as dt

    at = (dt.datetime.now().astimezone() + dt.timedelta(days=1)).replace(microsecond=0).isoformat()
    job = await _create(client, profile, schedule=at)
    jid = job["id"]
    assert job["schedule"]["kind"] == "once"
    with _cron.cron_scope(api, profile):
        api.update_job(jid, {"state": "completed", "enabled": False, "next_run_at": None, "last_run_at": at})
    assert (await client.get(_p(profile, f"jobs/{jid}"))).status == 200
    resp = await client.post(_p(profile, f"jobs/{jid}/run"))
    assert resp.status == 409 and (await resp.json())["error"] == "job_terminal", await resp.text()
    resp = await client.post(_p(profile, f"jobs/{jid}/resume"))
    assert resp.status == 409 and (await resp.json())["error"] == "job_terminal", await resp.text()


# ---------------------------------------------------------------------------
# K8–K9 배달처·템플릿
# ---------------------------------------------------------------------------


async def test_배달처_목록은_local_을_먼저_준다(client, profile):
    body = await (await client.get(_p(profile, "delivery-targets"))).json()
    assert body["targets"][0]["id"] == "local"
    for target in body["targets"]:
        assert cf.DELIVERY_TARGET_KEYS <= set(target), target


async def test_템플릿_목록과_인스턴스화(client, profile):
    body = await (await client.get(_p(profile, "blueprints"))).json()
    assert body["blueprints"], "실제 Hermes 의 템플릿 카탈로그가 비어 있다"
    first = body["blueprints"][0]
    assert cf.AUTOMATION_BLUEPRINT_REQUIRED <= set(first), cf.AUTOMATION_BLUEPRINT_REQUIRED - set(first)
    for field in first["fields"]:
        assert cf.BLUEPRINT_FIELD_REQUIRED <= set(field) <= cf.BLUEPRINT_FIELD_KEYS, field
    # deliver 필드의 options 는 K8 의 배달처 id 로 바뀌어 있다 — local 은 항상 들어 있다.
    deliver = next((f for f in first["fields"] if f["name"] == "deliver"), None)
    if deliver is not None:
        assert "local" in deliver["options"]
    key = first["key"]
    # 값 타입이 틀리면 422 invalid_blueprint_values.
    bad = {f["name"]: "이건 시각이 아니다" for f in first["fields"] if f["type"] == "time"}
    if bad:
        resp = await client.post(_p(profile, "blueprints/instantiate"), json={"blueprint": key, "values": bad})
        assert resp.status == 422, await resp.text()
        assert (await resp.json())["error"] == "invalid_blueprint_values"
    # 기본값으로 채우면 201 — deliver 는 local 로 고정해 외부 플랫폼을 건드리지 않는다.
    values = {f["name"]: f["default"] for f in first["fields"] if f.get("default") not in (None, "")}
    if deliver is not None:
        values["deliver"] = "local"
    resp = await client.post(_p(profile, "blueprints/instantiate"), json={"blueprint": key, "values": values})
    assert resp.status == 201, await resp.text()
    job = (await resp.json())["job"]
    assert job["origin"] == {"source": "deskrpg", "blueprint": key}
    resp = await client.post(_p(profile, "blueprints/instantiate"), json={"blueprint": "no-such-blueprint", "values": {}})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# K3 · E4 — 실행 장부 + 실제 세션 → runs 와 사건 스트림
# ---------------------------------------------------------------------------


def _write_execution(api, profile, job_id, *, finish=None):
    """프로필 스코프 안에서 실제 장부에 claimed→running(→completed) 행을 쓴다. 실행 id 를 돌려준다."""
    from cron import executions

    with _cron.cron_scope(api, profile):
        record = executions.create_execution(job_id, source="scheduled")
        exec_id = record["id"]
        assert executions.mark_execution_running(exec_id) is not None
        if finish is not None:
            assert executions.finish_execution(exec_id, success=finish) is not None
    return exec_id


def _write_session(api, profile, job_id, text):
    """`cron_<job_id>_<ts>` 세션(source='cron')과 assistant 메시지를 실제 state.db 에 쓴다."""
    home = _cron.resolve_profile_home(api, profile)
    sdb = api.SessionDB(home / "state.db")
    try:
        session_id = f"cron_{job_id}_{int(time.time())}"
        sdb.create_session(session_id, "cron")
        sdb.append_message(session_id, "user", "안부 묻기")
        sdb.append_message(session_id, "assistant", text)
    finally:
        sdb.close()
    return session_id


async def test_실행_장부와_세션이_있으면_runs_와_사건이_result_text_를_싣는다(client, profile, api, hermes_env):
    board = "deskrpg-cron-events"
    assert (await client.post("/deskrpg/kanban/boards", json={"slug": board, "name": "x"})).status == 201
    now = await (await client.get(f"/deskrpg/events?board={board}")).json()
    assert now["events"] == []

    job = await _create(client, profile)
    jid = job["id"]
    # 장부 파일은 아직 없다 — 사건은 잡 레코드 폴백으로 빈 목록.
    assert not (hermes_env["home"] / "profiles" / profile / "cron" / "executions.db").exists()

    exec_id = _write_execution(api, profile, jid)
    session_id = _write_session(api, profile, jid, "결과 본문입니다")
    assert (hermes_env["home"] / "profiles" / profile / "cron" / "executions.db").is_file()

    started = await (await client.get(f"/deskrpg/events?board={board}&cursor={now['cursor']}")).json()
    kinds = [(e["kind"], e["id"]) for e in started["events"]]
    assert kinds == [("cron.run.started", f"c:{profile}:{exec_id}:started")], kinds
    payload = started["events"][0]["payload"]
    assert payload["job_id"] == jid and payload["job_name"] == "hello" and payload["profile"] == profile
    assert payload["session_id"] == session_id
    assert set(payload) == cf.CRON_RUN_STARTED_PAYLOAD_KEYS

    # runs — 세션은 아직 열려 있고(ended_at 없음) 방금 활동했으니 running
    runs = await (await client.get(_p(profile, f"jobs/{jid}/runs"))).json()
    assert [r["id"] for r in runs["runs"]] == [session_id]
    assert runs["runs"][0]["status"] == "running" and runs["runs"][0]["result_text"] == "결과 본문입니다"

    with _cron.cron_scope(api, profile):
        from cron import executions

        assert executions.finish_execution(exec_id, success=True) is not None
    finished = await (await client.get(f"/deskrpg/events?board={board}&cursor={started['cursor']}")).json()
    kinds = [(e["kind"], e["id"]) for e in finished["events"]]
    assert kinds == [("cron.run.finished", f"c:{profile}:{exec_id}:finished")], kinds
    payload = finished["events"][0]["payload"]
    assert payload["status"] == "ok" and payload["result_text"] == "결과 본문입니다" and payload["session_id"] == session_id
    assert set(payload) == cf.CRON_RUN_FINISHED_PAYLOAD_KEYS

    # 다시 부르면 비어 있다 — 끝난 실행은 커서의 o 에서 빠졌다.
    again = await (await client.get(f"/deskrpg/events?board={board}&cursor={finished['cursor']}")).json()
    assert again["events"] == [] and again["has_more"] is False

    # runs 의 status 도 장부에서 ok 로 대응된다(세션은 끝났다고 표시).
    home = _cron.resolve_profile_home(api, profile)
    sdb = api.SessionDB(home / "state.db")
    try:
        sdb.end_session(session_id, "completed")
    finally:
        sdb.close()
    runs = await (await client.get(_p(profile, f"jobs/{jid}/runs?limit=5"))).json()
    assert runs["limit"] == 5 and runs["runs"][0]["status"] == "ok"


async def test_실패한_실행은_status_error_로_나온다(client, profile, api):
    board = "deskrpg-cron-fail"
    assert (await client.post("/deskrpg/kanban/boards", json={"slug": board, "name": "x"})).status == 201
    now = await (await client.get(f"/deskrpg/events?board={board}")).json()
    job = await _create(client, profile)
    exec_id = _write_execution(api, profile, job["id"], finish=False)
    tail = await (await client.get(f"/deskrpg/events?board={board}&cursor={now['cursor']}")).json()
    kinds = [e["kind"] for e in tail["events"]]
    assert kinds == ["cron.run.started", "cron.run.finished"], kinds
    assert tail["events"][1]["payload"]["status"] == "error"
    assert tail["events"][1]["payload"]["result_text"] == ""  # 세션도 출력 파일도 없다
