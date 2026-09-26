import json
import pytest
from aiohttp import web

from deskrpg_plugin import routes as _routes


def _app(fake_api, *, authorized=True):
    """라우트 표를 그대로 붙인 aiohttp 앱. 기존 라우트 테스트와 같은 방식이다."""
    from tests.conftest import FakeAdapter  # 같은 어댑터를 쓴다

    app = web.Application()
    _routes.attach(app, FakeAdapter(authorized=authorized), fake_api)
    return app


@pytest.fixture(autouse=True)
def _swarm_profiles(tmp_path):
    """`fake_api` 는 `tmp_path/profiles/<name>` 디렉터리로 프로필 존재를 판정한다."""
    for name in ("nova", "luna", "sophie", "dante"):
        (tmp_path / "profiles" / name).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def swarm_body():
    return {
        "goal": "다음 분기 로드맵 초안을 만든다",
        "workers": [
            {"profile": "nova", "title": "경쟁사 조사", "skills": ["research"]},
            {"profile": "luna", "title": "사용자 인터뷰 정리"},
        ],
        "verifier": "sophie",
        "synthesizer": "dante",
    }


async def test_스웜을_만들면_아이디를_돌려준다(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    res = await client.post("/deskrpg/kanban/swarm?board=default", json=swarm_body)
    assert res.status == 200
    body = await res.json()
    assert body == {
        "root_id": "t_root",
        "worker_ids": ["t_w0", "t_w1"],
        "verifier_id": "t_ver",
        "synthesizer_id": "t_syn",
    }


async def test_본문이_그대로_create_swarm_에_넘어간다(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    await client.post("/deskrpg/kanban/swarm?board=default", json=swarm_body)
    call = fake_api.swarm_calls[-1]
    assert call["goal"] == "다음 분기 로드맵 초안을 만든다"
    assert call["verifier_assignee"] == "sophie"
    assert call["synthesizer_assignee"] == "dante"
    assert [w.profile for w in call["workers"]] == ["nova", "luna"]
    assert [w.title for w in call["workers"]] == ["경쟁사 조사", "사용자 인터뷰 정리"]
    # body 를 안 주면 title 을 쓴다 — `parse_worker_arg` 와 같은 규칙.
    assert call["workers"][1].body == "사용자 인터뷰 정리"
    assert call["workers"][0].skills == ["research"]


async def test_목표가_비면_400(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    res = await client.post("/deskrpg/kanban/swarm?board=default", json={**swarm_body, "goal": "  "})
    assert res.status == 400
    assert (await res.json())["error"] == "invalid_field"


async def test_워커가_없으면_400(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    res = await client.post("/deskrpg/kanban/swarm?board=default", json={**swarm_body, "workers": []})
    assert res.status == 400
    assert (await res.json())["error"] == "workers_required"


async def test_없는_프로필이면_400(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    body = {**swarm_body, "verifier": "ghost"}
    res = await client.post("/deskrpg/kanban/swarm?board=default", json=body)
    assert res.status == 400
    assert (await res.json())["error"] == "profile_not_found"


async def test_블랙보드를_읽는다(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    created = await client.post("/deskrpg/kanban/swarm?board=default", json=swarm_body)
    root_id = (await created.json())["root_id"]
    res = await client.get(f"/deskrpg/kanban/tasks/{root_id}/blackboard?board=default")
    assert res.status == 200
    assert (await res.json())["blackboard"]["topology"] == {"goal": "g"}


async def test_없는_카드의_블랙보드는_404(aiohttp_client, fake_api):
    client = await aiohttp_client(_app(fake_api))
    res = await client.get("/deskrpg/kanban/tasks/ghost-task/blackboard?board=default")
    assert res.status == 404
    assert (await res.json())["error"] == "task_not_found"


def test_심볼이_없으면_라우트가_없다(fake_api):
    fake_api.create_swarm = None
    fake_api.latest_blackboard = None
    paths = [p for _m, p, _h, _s in _routes.routes_for(fake_api)]
    assert "/deskrpg/kanban/swarm" not in paths
    assert "/deskrpg/kanban/tasks/{id}/blackboard" not in paths


def test_심볼이_없으면_capability_에도_없다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    assert "swarm" in capabilities(fake_api)
    fake_api.create_swarm = None
    assert "swarm" not in capabilities(fake_api)


# --- Approval-policy boards (swarm_review_policy) ---------------------------------------------------------------

import types  # noqa: E402

from deskrpg_plugin import kanban_swarm as _kanban_swarm  # noqa: E402

HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}


def _policy_hermes(monkeypatch, *, swarm_policy):
    monkeypatch.setattr(_kanban_swarm, "has_review_policy", lambda api: True)
    monkeypatch.setattr(_kanban_swarm, "has_swarm_policy_symbols", lambda api: swarm_policy)


async def test_a_policy_hermes_without_the_swarm_internals_refuses_new_swarms(
    aiohttp_client, fake_api, swarm_body, monkeypatch
):
    _policy_hermes(monkeypatch, swarm_policy=False)
    client = await aiohttp_client(_app(fake_api))
    calls = len(fake_api.swarm_calls)
    res = await client.post("/deskrpg/kanban/swarm?board=default", json={**swarm_body, "review_policy": HUMAN})
    assert res.status == 428
    assert (await res.json())["error"] == "swarm_review_policy_unsupported"
    assert len(fake_api.swarm_calls) == calls  # Hermes was not written to


async def test_a_policy_hermes_requires_a_policy_for_the_workers(aiohttp_client, fake_api, swarm_body, monkeypatch):
    _policy_hermes(monkeypatch, swarm_policy=True)
    client = await aiohttp_client(_app(fake_api))
    calls = len(fake_api.swarm_calls)
    res = await client.post("/deskrpg/kanban/swarm?board=default", json=swarm_body)
    assert res.status == 400
    assert len(fake_api.swarm_calls) == calls


async def test_a_policy_swarm_goes_through_the_plugin_assembly_not_create_swarm(
    aiohttp_client, fake_api, swarm_body, monkeypatch
):
    _policy_hermes(monkeypatch, swarm_policy=True)
    seen = {}

    def fake_assembly(api, conn, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(as_dict=lambda: {"root_id": "t_root"})

    monkeypatch.setattr(_kanban_swarm, "create_swarm_with_policy", fake_assembly)
    client = await aiohttp_client(_app(fake_api))
    calls = len(fake_api.swarm_calls)
    res = await client.post("/deskrpg/kanban/swarm?board=default", json={**swarm_body, "review_policy": HUMAN})
    assert res.status == 200, await res.text()
    assert seen["worker_policy"] == HUMAN
    assert seen["verifier_assignee"] == "sophie" and seen["synthesizer_assignee"] == "dante"
    assert len(fake_api.swarm_calls) == calls  # plain create_swarm (no policies) is never used


async def test_a_hermes_without_policies_rejects_a_policy_it_cannot_keep(aiohttp_client, fake_api, swarm_body):
    client = await aiohttp_client(_app(fake_api))
    res = await client.post("/deskrpg/kanban/swarm?board=default", json={**swarm_body, "review_policy": HUMAN})
    assert res.status == 428
    assert (await res.json())["error"] == "review_policy_required"
