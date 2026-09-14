"""K9 — 템플릿(블루프린트) 목록과 인스턴스화."""

import pytest
from aiohttp import web

from deskrpg_plugin.contract_fields import AUTOMATION_BLUEPRINT_REQUIRED, BLUEPRINT_FIELD_KEYS
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


async def test_목록은_계약_모양이고_deliver_옵션을_배달처_목록으로_바꾼다(aiohttp_client, fake_api, store, profile):
    store.delivery_targets = [{"id": "slack", "name": "Slack", "home_target_set": True, "home_env_var": None}]
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/cron/blueprints")
    assert resp.status == 200
    entries = (await resp.json())["blueprints"]
    assert [e["key"] for e in entries] == ["morning-brief", "mood"]
    for entry in entries:
        assert AUTOMATION_BLUEPRINT_REQUIRED <= set(entry)
        for field in entry["fields"]:
            assert set(field) <= BLUEPRINT_FIELD_KEYS
    deliver = next(f for f in entries[0]["fields"] if f["name"] == "deliver")
    assert deliver["options"] == ["local", "slack"]
    # 다른 필드의 options 는 건드리지 않는다.
    mood = next(f for f in entries[1]["fields"] if f["name"] == "mood")
    assert mood["options"] == ["happy", "sad"]


async def test_인스턴스화는_201_과_잡을_주고_origin_에_템플릿_키를_남긴다(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(
        "/p/sophie/deskrpg/cron/blueprints/instantiate",
        json={"blueprint": "morning-brief", "values": {"time": "07:30"}},
    )
    assert resp.status == 201, await resp.text()
    job = (await resp.json())["job"]
    assert job["name"] == "Morning briefing"
    assert job["schedule"]["expr"] == "30 7 * * *"
    assert job["skills"] == ["google-workspace"]
    assert job["deliver"] == "local"
    assert job["state"] == "scheduled"
    assert store.jobs_by_home[profile][0]["origin"] == {"source": "deskrpg", "blueprint": "morning-brief"}
    assert store.scheduler.on_jobs_changed_calls == 1


async def test_인스턴스화는_모르는_키에_404(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"blueprint": "nope", "values": {}})
    assert resp.status == 404
    assert (await resp.json())["error"] == "blueprint_not_found"


async def test_인스턴스화는_값_오류에_422(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"blueprint": "mood", "values": {"mood": "angry"}})
    assert resp.status == 422
    body = await resp.json()
    assert body["error"] == "invalid_blueprint_values"
    assert "angry" in body["detail"]
    assert store.jobs_by_home.get(profile, []) == []

    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"blueprint": "mood", "values": {"tiem": "x"}})
    assert resp.status == 422


async def test_인스턴스화는_blueprint_가_없거나_values_가_객체가_아니면_400(aiohttp_client, fake_api, store, profile):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"values": {}})
    assert resp.status == 400
    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"blueprint": "mood", "values": []})
    assert resp.status == 400


async def test_인스턴스화도_스케줄러_등록_실패면_424(aiohttp_client, fake_api, store, profile):
    store.registration_fails = True
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/cron/blueprints/instantiate", json={"blueprint": "morning-brief", "values": {}})
    assert resp.status == 424


async def test_인스턴스화는_없는_프로필에_404(aiohttp_client, fake_api, store):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/nobody/deskrpg/cron/blueprints/instantiate", json={"blueprint": "mood", "values": {}})
    assert resp.status == 404
