"""무인 실행 정책(0.18.0) — 크론·칸반 워커의 승인 모드와 허용 목록."""

import json

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import contract_fields, routes
from tests.conftest import FakeAdapter

ACTOR = {"X-DeskRPG-Actor": "u-1"}
BASE = "/p/sophie/deskrpg/approval-policy"


@pytest.fixture
async def client(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def _cfg_path(fake_api):
    return fake_api.get_profile_dir("sophie") / "config.yaml"


def _write(fake_api, data):
    _cfg_path(fake_api).write_text(yaml.safe_dump(data))


def _read(fake_api):
    return yaml.safe_load(_cfg_path(fake_api).read_text()) or {}


def test_capability_is_advertised(fake_api):
    assert "profile_approval_policy" in contract_fields.capabilities(fake_api)


async def test_defaults_without_config(client):
    res = await client.get(BASE)
    assert res.status == 200
    assert await res.json() == {"cronMode": "deny", "singleQueryMode": "deny", "allowlist": [],
                                "timeoutSeconds": 300, "workerPropagation": False}


async def test_reads_modes_like_hermes(client, fake_api):
    # Hermes 는 approve·off·allow·yes 를 approve 로, 그 밖(따옴표 없는 YAML off=False 포함)은 deny 로 읽는다.
    _write(fake_api, {"approvals": {"cron_mode": "Allow", "single_query_mode": False, "timeout": 90},
                      "command_allowlist": ["recursive delete", "git push --force"]})
    body = await (await client.get(BASE)).json()
    assert body["cronMode"] == "approve" and body["singleQueryMode"] == "deny"
    assert body["timeoutSeconds"] == 90
    assert body["allowlist"] == ["recursive delete", "git push --force"]


async def test_bad_timeout_and_malformed_allowlist_fall_back(client, fake_api):
    _write(fake_api, {"approvals": {"timeout": "soon"}, "command_allowlist": [1, "x"]})
    body = await (await client.get(BASE)).json()
    assert body["timeoutSeconds"] == 300 and body["allowlist"] == []


async def test_legacy_string_allowlist_is_recovered(client, fake_api):
    _write(fake_api, {"command_allowlist": "['recursive delete']"})
    assert (await (await client.get(BASE)).json())["allowlist"] == ["recursive delete"]


async def test_worker_propagation_is_reported(client, monkeypatch):
    monkeypatch.setenv("DESKRPG_WORKER_PROPAGATION", "1")
    assert (await (await client.get(BASE)).json())["workerPropagation"] is True


async def test_partial_put_keeps_other_config(client, fake_api):
    _write(fake_api, {"approvals": {"mode": "manual", "timeout": 120},
                      "mcp_servers": {"gh": {"url": "https://a.example/x"}}, "model": {"default": "m"}})
    res = await client.put(BASE, json={"cronMode": "approve"}, headers=ACTOR)
    assert res.status == 200
    assert (await res.json())["cronMode"] == "approve"
    data = _read(fake_api)
    assert data["approvals"] == {"mode": "manual", "timeout": 120, "cron_mode": "approve"}
    assert data["mcp_servers"] == {"gh": {"url": "https://a.example/x"}} and data["model"] == {"default": "m"}
    res = await client.put(BASE, json={"singleQueryMode": "approve", "cronMode": "deny"}, headers=ACTOR)
    assert _read(fake_api)["approvals"]["single_query_mode"] == "approve"
    assert _read(fake_api)["approvals"]["cron_mode"] == "deny"


@pytest.mark.parametrize("body", [{"cronMode": "always"}, {"cronMode": "off"}, {"singleQueryMode": True},
                                  {"unattendedMode": "approve"}, {}, {"cronMode": None}])
async def test_put_rejects_unknown_values(client, fake_api, body):
    res = await client.put(BASE, json=body, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "invalid_field"
    assert not _cfg_path(fake_api).exists()


async def test_allowlist_add_dedupes_and_delete(client, fake_api):
    _write(fake_api, {"command_allowlist": ["recursive delete"], "approvals": {"cron_mode": "deny"}})
    res = await client.post(f"{BASE}/allowlist", json={"entry": "git push --force"}, headers=ACTOR)
    assert res.status == 200 and (await res.json())["allowlist"] == ["recursive delete", "git push --force"]
    res = await client.post(f"{BASE}/allowlist", json={"entry": "git push --force"}, headers=ACTOR)
    assert (await res.json())["allowlist"] == ["recursive delete", "git push --force"]
    res = await client.delete(f"{BASE}/allowlist", json={"entry": "recursive delete"}, headers=ACTOR)
    assert res.status == 200 and (await res.json())["allowlist"] == ["git push --force"]
    assert _read(fake_api)["command_allowlist"] == ["git push --force"]
    assert _read(fake_api)["approvals"] == {"cron_mode": "deny"}


async def test_delete_missing_entry_is_noop(client, fake_api):
    res = await client.delete(f"{BASE}/allowlist", json={"entry": "never there"}, headers=ACTOR)
    assert res.status == 200 and (await res.json())["allowlist"] == []


@pytest.mark.parametrize("entry", ["", "x" * 201, "a\nb", "a\rb", "tab\tin", 5, None, "nul\x00"])
async def test_allowlist_entry_format(client, entry):
    res = await client.post(f"{BASE}/allowlist", json={"entry": entry}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "invalid_allowlist_entry"


async def test_entry_is_trimmed(client):
    res = await client.post(f"{BASE}/allowlist", json={"entry": "  recursive delete  "}, headers=ACTOR)
    assert (await res.json())["allowlist"] == ["recursive delete"]


async def test_unreadable_config_is_409_and_untouched(client, fake_api):
    _cfg_path(fake_api).write_text("approvals: [unclosed\n")
    assert (await client.get(BASE)).status == 409
    res = await client.put(BASE, json={"cronMode": "approve"}, headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "config_unreadable"
    assert _cfg_path(fake_api).read_text() == "approvals: [unclosed\n"


async def test_audit_records_actor_and_change(client, fake_api):
    await client.put(BASE, json={"cronMode": "approve"}, headers=ACTOR)
    await client.post(f"{BASE}/allowlist", json={"entry": "recursive delete"}, headers=ACTOR)
    path = fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "policy-audit.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["action"] for r in rows] == ["modes", "allowlist_add"]
    assert rows[0]["actor"] == "u-1" and rows[0]["cronMode"] == "approve"
    assert rows[1]["entry"] == "recursive delete"


async def test_unknown_profile_404(client):
    assert (await client.get("/p/nobody/deskrpg/approval-policy")).status == 404
