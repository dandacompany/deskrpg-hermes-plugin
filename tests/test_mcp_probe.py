"""MCP 커넥터 관리 — 연결 테스트 작업과 도구 목록(0.17.0)."""

import asyncio

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import mcp_probe, mcp_state, routes
from tests.conftest import FakeAdapter
from tests.fakes_mcp import FakeTool

ACTOR = {"X-DeskRPG-Actor": "u-1"}
BASE = "/p/sophie/deskrpg/mcp"


class Ann:
    def __init__(self, ro=None, de=None):
        self.readOnlyHint, self.destructiveHint = ro, de


@pytest.fixture
async def client(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    c = await aiohttp_client(app)
    res = await c.post(f"{BASE}/servers", json={"name": "gh", "transport": "http", "url": "https://a.example/x",
                                                "auth": "none"}, headers=ACTOR)
    assert res.status == 201
    return c


async def wait_job(client, job):
    for _ in range(200):
        body = await (await client.get(f"{BASE}/jobs/{job}")).json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


def _slow_server(delay=0.2):
    async def slow(name, cfg):
        await asyncio.sleep(delay)
        return type("S", (), {"_tools": [], "shutdown": staticmethod(lambda: asyncio.sleep(0))})()

    return slow


def test_tool_rows_include_wins_and_globs():
    tools = [{"name": "read_a"}, {"name": "write_b"}]
    rows = mcp_probe.tool_rows({"tools": {"include": ["read_*"], "exclude": ["read_a"]}}, tools)
    assert [r["on"] for r in rows] == [True, False]
    assert [r["on"] for r in mcp_probe.tool_rows({"tools": {"exclude": ["write_*"]}}, tools)] == [True, False]
    assert [r["on"] for r in mcp_probe.tool_rows({"tools": {"include": []}}, tools)] == [False, False]
    assert [r["on"] for r in mcp_probe.tool_rows({}, tools)] == [True, True]


async def test_probe_records_tools_with_hints(client, fake_api):
    fake_api.fake_mcp.tools_by_server["gh"] = [FakeTool("read_a", "reads", Ann(ro=True)),
                                               FakeTool("drop_b", "drops", Ann(de=True))]
    res = await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)
    assert res.status == 202
    done = await wait_job(client, (await res.json())["jobId"])
    assert done["state"] == "succeeded" and done["ok"] is True
    tools = await (await client.get(f"{BASE}/servers/gh/tools")).json()
    assert tools["tools"][0] == {"name": "read_a", "description": "reads", "readOnlyHint": True,
                                 "destructiveHint": None, "on": True}
    listed = await (await client.get(f"{BASE}/servers")).json()
    assert listed["servers"][0]["lastCheck"]["ok"] is True
    assert listed["servers"][0]["tools"] == {"total": 2, "enabled": 2}


async def test_probe_failure_is_redacted(client, fake_api):
    fake_api.fake_mcp.connect_errors["gh"] = RuntimeError("401 with Bearer ghp_leak")
    job = (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"]
    done = await wait_job(client, job)
    assert done["ok"] is False and "ghp_leak" not in done["error"]
    listed = await (await client.get(f"{BASE}/servers")).json()
    assert listed["servers"][0]["lastCheck"]["ok"] is False
    assert "ghp_leak" not in str(listed)


async def test_failed_probe_keeps_last_known_tools(client, fake_api):
    fake_api.fake_mcp.tools_by_server["gh"] = [FakeTool("read_a")]
    await wait_job(client, (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"])
    fake_api.fake_mcp.connect_errors["gh"] = RuntimeError("down")
    await wait_job(client, (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"])
    tools = await (await client.get(f"{BASE}/servers/gh/tools")).json()
    assert [t["name"] for t in tools["tools"]] == ["read_a"]


async def test_oauth_server_without_token_is_not_green(client, fake_api):
    await client.post(f"{BASE}/servers", json={"name": "canva", "transport": "http",
                                               "url": "https://mcp.canva.com/mcp", "auth": "oauth"}, headers=ACTOR)
    fake_api.fake_mcp.tools_by_server["canva"] = [FakeTool("t1")]
    job = (await (await client.post(f"{BASE}/servers/canva/test", headers=ACTOR)).json())["jobId"]
    done = await wait_job(client, job)
    assert done["ok"] is False and "OAuth" in done["error"]


async def test_tools_unknown_before_first_test(client):
    res = await client.get(f"{BASE}/servers/gh/tools")
    assert res.status == 404 and (await res.json())["error"] == "tools_unknown"


async def test_test_unknown_server_404(client):
    res = await client.post(f"{BASE}/servers/nope/test", headers=ACTOR)
    assert res.status == 404 and (await res.json())["error"] == "server_not_found"


async def test_job_of_other_profile_is_unknown(client, fake_api):
    fake_api.create_profile("bob")
    fake_api._connect_server = _slow_server(0.05)
    job = (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"]
    res = await client.get(f"/p/bob/deskrpg/mcp/jobs/{job}")
    assert res.status == 404 and (await res.json())["error"] == "job_unknown"
    await wait_job(client, job)


async def test_probe_does_not_resurrect_deleted_server(client, fake_api):
    fake_api._connect_server = _slow_server()
    job = (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"]
    await client.delete(f"{BASE}/servers/gh", headers=ACTOR)
    await wait_job(client, job)
    assert "gh" not in mcp_state.read_checks(fake_api.get_profile_dir("sophie"))


async def test_second_test_while_running_is_busy(client, fake_api):
    fake_api._connect_server = _slow_server()
    first = (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"]
    res = await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "job_busy"
    await wait_job(client, first)


async def test_tool_filter_change_recomputes_on_flags(client, fake_api):
    fake_api.fake_mcp.tools_by_server["gh"] = [FakeTool("read_a"), FakeTool("drop_b")]
    await wait_job(client, (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"])
    rev = (await (await client.get(f"{BASE}/servers/gh")).json())["revision"]
    res = await client.put(f"{BASE}/servers/gh/tools", json={"include": ["read_*"], "baseRevision": rev}, headers=ACTOR)
    assert (await res.json())["tools"] == {"total": 2, "enabled": 1}


async def test_probe_uses_profile_config(client, fake_api):
    seen = {}

    async def connect(name, cfg):
        seen["home"] = str(fake_api.get_hermes_home())
        seen["cfg"] = cfg
        return type("S", (), {"_tools": [], "shutdown": staticmethod(lambda: asyncio.sleep(0))})()

    fake_api._connect_server = connect
    await wait_job(client, (await (await client.post(f"{BASE}/servers/gh/test", headers=ACTOR)).json())["jobId"])
    assert seen["home"].endswith("sophie") and seen["cfg"]["url"] == "https://a.example/x"
    raw = yaml.safe_load((fake_api.get_profile_dir("sophie") / "config.yaml").read_text())
    assert "connect_timeout" not in raw["mcp_servers"]["gh"]


def test_hint_reads_camel_snake_and_dict_annotations():
    from types import SimpleNamespace as NS

    assert mcp_probe._hint(NS(annotations=NS(readOnlyHint=True)), "readOnlyHint") is True
    assert mcp_probe._hint(NS(annotations=NS(read_only_hint=True)), "readOnlyHint") is True
    assert mcp_probe._hint(NS(annotations={"destructiveHint": True}), "destructiveHint") is True
    assert mcp_probe._hint(NS(annotations=None), "readOnlyHint") is None
    assert mcp_probe._hint(NS(annotations=NS(readOnlyHint="yes")), "readOnlyHint") is None
