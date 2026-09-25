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


# ---- 0.17.1 — 같은 서버의 OAuth 시도를 겹치지 않게 한다 ----------------------------------------
# Hermes 는 새 시도가 옛 시도를 취소하게 두는데, 새 시도가 먼저 끝난 뒤 옛 워커가 끝나면 옛 워커의
# 롤백이 시작 전 토큰 스냅샷을 되돌려 방금 받은 인증을 지운다(스테이징 실측). 플러그인이 겹침 자체를 막는다.


async def test_second_start_while_open_is_409_in_progress(client, fake_api):
    sid = await _start(client)
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    body = await res.json()
    assert res.status == 409 and body["error"] == "oauth_in_progress" and body["sessionId"] == sid
    assert list(fake_api.fake_mcp.flow_objs) == [sid]  # Hermes start 를 다시 부르지 않았다


async def test_other_profile_or_server_is_not_blocked(client):
    await _start(client)
    assert (await client.post("/p/bob/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)).status == 200


async def test_restart_cancels_and_waits_then_starts_new(client, fake_api):
    old = await _start(client)
    seen = []
    fake_api.cancel_flow = lambda s, n, home: seen.append(s) or {"ok": True}
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", json={"restart": True}, headers=ACTOR)
    body = await res.json()
    assert res.status == 200 and body["sessionId"] != old
    assert fake_api.fake_mcp.cancel_attempts == [old] and seen == [old]
    assert fake_api.fake_mcp.flow_objs[old].worker_done is True
    assert (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{old}")).status == 404


async def test_restart_when_worker_does_not_exit_is_409_busy(client, fake_api, monkeypatch):
    monkeypatch.setattr(mcp_oauth_routes, "_WORKER_WAIT", 0.1)
    old = await _start(client)
    fake_api.fake_mcp.worker_exits_on_cancel = False
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", json={"restart": True}, headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "oauth_busy"
    assert list(fake_api.fake_mcp.flow_objs) == [old]  # 새 시도를 시작하지 않았다
    # 워커가 나중에 끝나면 그때는 새로 시작할 수 있다.
    fake_api.fake_mcp.flow_objs[old].worker_done = True
    assert (await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)).status == 200


async def test_finished_worker_does_not_block_new_start(client, fake_api):
    old = await _start(client)
    fake_api.fake_mcp.flow_objs[old].worker_done = True  # 붙여넣지 않고 끝난 시도(시간 초과 등)
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    assert res.status == 200 and (await res.json())["sessionId"] != old


async def test_approved_then_new_start_is_allowed(client):
    sid = await _start(client)
    await client.post(f"/p/sophie/deskrpg/mcp/oauth/{sid}/callback", json={"code": "c", "state": "st-" + sid},
                      headers=ACTOR)
    assert (await (await client.get(f"/p/sophie/deskrpg/mcp/oauth/{sid}")).json())["status"] == "approved"
    assert (await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)).status == 200


async def test_cancel_waits_for_worker(client, fake_api):
    sid = await _start(client)
    res = await client.delete(f"/p/sophie/deskrpg/mcp/oauth/{sid}", headers=ACTOR)
    assert res.status == 200 and (await res.json())["ok"] is True
    assert fake_api.fake_mcp.cancel_attempts == [sid] and fake_api.fake_mcp.flow_objs[sid].worker_done
    assert (await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)).status == 200


async def test_cancel_with_stuck_worker_keeps_blocking(client, fake_api, monkeypatch):
    monkeypatch.setattr(mcp_oauth_routes, "_WORKER_WAIT", 0.1)
    sid = await _start(client)
    fake_api.fake_mcp.worker_exits_on_cancel = False
    res = await client.delete(f"/p/sophie/deskrpg/mcp/oauth/{sid}", headers=ACTOR)
    assert res.status == 200 and (await res.json()) == {"ok": True, "workerDone": False}
    res = await client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "oauth_in_progress"


async def test_concurrent_starts_call_hermes_once(client, fake_api):
    import asyncio
    results = await asyncio.gather(*[client.post("/p/sophie/deskrpg/mcp/servers/canva/oauth", headers=ACTOR)
                                     for _ in range(3)])
    assert sorted(r.status for r in results) == [200, 409, 409]
    assert len(fake_api.fake_mcp.flow_objs) == 1
