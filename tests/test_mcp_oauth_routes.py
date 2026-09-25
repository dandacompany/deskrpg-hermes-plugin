"""MCP 커넥터 관리 — OAuth 리다이렉트 URL 붙여넣기 흐름(0.17.0)."""

import pytest
from aiohttp import web

from deskrpg_plugin import mcp_oauth_routes, routes
from tests.conftest import FakeAdapter

ACTOR = {"X-DeskRPG-Actor": "u-1"}


@pytest.fixture
async def client(aiohttp_client, fake_api):
    mcp_oauth_routes._SESSIONS.clear()
    fake_api.create_profile("sophie")
    fake_api.create_profile("bob")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    c = await aiohttp_client(app)
    for p in ("sophie", "bob"):
        res = await c.post(f"/p/{p}/deskrpg/mcp/servers", json={"name": "canva", "transport": "http",
                                                                "url": "https://mcp.canva.com/mcp", "auth": "oauth"},
                           headers=ACTOR)
        assert res.status == 201
    return c


async def _start(client, profile="sophie"):
    res = await client.post(f"/p/{profile}/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    assert res.status == 200, await res.text()
    return (await res.json())["sessionId"]


async def test_start_uses_fixed_loopback_redirect(client, fake_api):
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    body = await res.json()
    assert res.status == 200 and body["sessionId"] == "flow-1" and body["authUrl"].startswith("https://auth.example/")
    assert fake_api.fake_mcp.oauth_flows["flow-1"]["redirect"] == "http://127.0.0.1:8412/callback"
    assert fake_api.fake_mcp.oauth_flows["flow-1"]["home"].endswith("sophie")


async def test_start_reports_already_approved(client, fake_api):
    class Done:
        auth_url, flow = "", None

    fake_api.mcp_oauth_start = lambda name, **_: Done()
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    assert res.status == 200 and await res.json() == {"status": "approved"}


async def test_start_failure_is_redacted_502(client, fake_api):
    def boom(name, **_):
        raise RuntimeError("provider said Bearer ghp_leak")

    fake_api.mcp_oauth_start = boom
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    body = await res.json()
    assert res.status == 502 and body["error"] == "oauth_start_failed" and "ghp_leak" not in str(body)


async def test_callback_then_poll_approved(client):
    sid = await _start(client)
    res = await client.post(f"/p/sophie/deskrpg/mcp/oauth/{sid}/callback", json={"code": "c", "state": "st-" + sid},
                            headers=ACTOR)
    assert res.status == 200 and await res.json() == {"ok": True}
    poll = await (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).json()
    assert poll == {"status": "approved", "tools": ["t1"]}


async def test_poll_tool_dicts_become_names(client, fake_api):
    sid = await _start(client)
    fake_api.poll_flow = lambda s, n: {"status": "approved", "tools": [{"name": "a", "description": "x"}, "b", {}]}
    poll = await (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).json()
    assert poll["tools"] == ["a", "b"]


async def test_poll_error_is_redacted(client, fake_api):
    sid = await _start(client)
    fake_api.poll_flow = lambda s, n: {"status": "error", "error_message": "denied Bearer ghp_leak"}
    poll = await (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).json()
    assert poll["status"] == "error" and "ghp_leak" not in poll["error"]


async def test_poll_pending_keeps_session(client):
    sid = await _start(client)
    assert (await (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).json()) == {"status": "pending"}
    assert (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).status == 200


async def test_callback_state_mismatch(client):
    sid = await _start(client)
    res = await client.post(f"/p/sophie/deskrpg/mcp/oauth/{sid}/callback", json={"code": "c", "state": "nope"},
                            headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "oauth_callback_invalid"


async def test_callback_requires_code_and_state(client):
    sid = await _start(client)
    res = await client.post(f"/p/sophie/deskrpg/mcp/oauth/{sid}/callback", json={"state": "x"}, headers=ACTOR)
    assert res.status == 400


async def test_other_profile_cannot_touch_session(client):
    sid = await _start(client)
    for method, path, kw in (("post", f"/p/bob/deskrpg/mcp/oauth/{sid}/callback", {"json": {"code": "c", "state": "x"}}),
                             ("get", f"/p/bob/deskrpg/mcp/oauth/{sid}", {}),
                             ("delete", f"/p/bob/deskrpg/mcp/oauth/{sid}", {})):
        res = await getattr(client, method)(path, headers=ACTOR, **kw)
        assert res.status == 400 and (await res.json())["error"] == "oauth_session_mismatch"


async def test_cancel_passes_profile_home_and_forgets_session(client, fake_api):
    sid = await _start(client)
    seen = {}

    def cancel(s, n, home):
        seen["home"] = home
        return {"ok": True}

    fake_api.cancel_flow = cancel
    res = await client.delete(f"/p/sophie/deskrpg/mcp/oauth/{sid}", headers=ACTOR)
    assert res.status == 200 and await res.json() == {"ok": True}
    assert seen["home"].endswith("sophie")
    assert (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).status == 404


async def test_start_requires_oauth_server(client):
    await client.post("/p/sophie/deskrpg/mcp/servers", json={"name": "plain", "transport": "http",
                                                             "url": "https://a.example/x", "auth": "none"}, headers=ACTOR)
    res = await client.post("/p/sophie/deskrpg/mcp/servers/plain/oauth", headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "oauth_not_configured"


async def test_unknown_session_404(client):
    res = await client.get("/p/sophie/deskrpg/mcp/oauth/nope")
    assert res.status == 404 and (await res.json())["error"] == "oauth_session_not_found"


async def test_audit_records_start_without_urls(client, fake_api):
    await _start(client)
    log = (fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "mcp-audit.jsonl").read_text()
    assert '"action": "oauth_start"' in log and "auth.example" not in log
