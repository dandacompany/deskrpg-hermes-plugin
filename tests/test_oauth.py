"""OAuth 디바이스 로그인 — Hermes 의 세션·폴러·저장을 그대로 쓰고 모양만 옮긴다."""

import threading

import pytest
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter

TOKEN = "eyJ-SECRET-ACCESS-TOKEN-should-never-leak"


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@pytest.fixture
def oauth_api(fake_api):
    sessions = {}
    calls = {}

    async def start(provider_id, profile=None):
        calls["start"] = (provider_id, profile)
        if provider_id not in fake_api._DEVICE_CODE_STARTERS:
            raise FakeHTTPException(400, "unsupported")
        sessions["sid-1"] = {"session_id": "sid-1", "provider": provider_id, "profile": profile,
                             "status": "pending", "error_message": None, "expires_at": 1.0e9,
                             "access_token": TOKEN, "account_email": "me@example.com"}
        return {"session_id": "sid-1", "flow": "device_code", "user_code": "ABCD-1234",
                "verification_url": "https://auth.openai.com/codex/device", "expires_in": 900, "poll_interval": 5}

    async def poll(provider_id, session_id, profile=None):
        sess = sessions.get(session_id)
        if not sess:
            raise FakeHTTPException(404, "Session not found or expired")
        if sess["provider"] != provider_id:
            raise FakeHTTPException(400, "Provider mismatch for session")
        if sess["profile"] != profile:
            raise FakeHTTPException(400, "OAuth session profile mismatch")
        return {"session_id": session_id, "status": sess["status"], "error_message": sess["error_message"],
                "expires_at": sess["expires_at"], "reason": "x", "account_email": sess["account_email"],
                "model": "gpt", "retryable": None, "retry_after": None}

    cleared = []
    fake_api._start_device_code_flow = start
    fake_api.poll_oauth_session = poll
    fake_api._oauth_sessions = sessions
    fake_api._oauth_sessions_lock = threading.Lock()
    fake_api.clear_provider_auth = lambda pid=None: cleared.append(pid) or True
    fake_api._calls = calls
    fake_api._cleared = cleared
    fake_api.create_profile("noah")
    return fake_api


def _client(aiohttp_client, api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), api)
    return aiohttp_client(app)


async def test_시작하면_코드와_주소만_준다(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    assert resp.status == 200
    assert await resp.json() == {"sessionId": "sid-1", "userCode": "ABCD-1234",
                                 "verificationUrl": "https://auth.openai.com/codex/device",
                                 "expiresIn": 900, "pollInterval": 5}
    assert oauth_api._calls["start"] == ("openai-codex", "noah")


async def test_폴링은_상태만_옮기고_토큰_이메일을_싣지_않는다(aiohttp_client, oauth_api, caplog):
    client = await _client(aiohttp_client, oauth_api)
    await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    oauth_api._oauth_sessions["sid-1"]["status"] = "approved"
    resp = await client.get("/p/noah/deskrpg/oauth/openai-codex/sessions/sid-1")
    text = await resp.text()
    assert TOKEN not in text and "me@example.com" not in text and TOKEN not in caplog.text
    assert await resp.json() == {"status": "approved", "error": None, "expiresAt": 1.0e9,
                                 "retryable": None, "retryAfter": None}


async def test_다른_프로필의_세션은_불일치다(aiohttp_client, oauth_api):
    oauth_api.create_profile("mia")
    client = await _client(aiohttp_client, oauth_api)
    await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    resp = await client.get("/p/mia/deskrpg/oauth/openai-codex/sessions/sid-1")
    assert resp.status == 400
    assert (await resp.json())["error"] == "oauth_session_mismatch"


async def test_없는_세션은_404(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.get("/p/noah/deskrpg/oauth/openai-codex/sessions/ghost")
    assert resp.status == 404
    assert (await resp.json())["error"] == "oauth_session_not_found"


async def test_취소하면_cancelled_표시_후_세션을_지운다(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    sess = oauth_api._oauth_sessions["sid-1"]
    resp = await client.delete("/p/noah/deskrpg/oauth/sessions/sid-1")
    assert await resp.json() == {"ok": True}
    assert sess["cancelled"] is True and "sid-1" not in oauth_api._oauth_sessions


async def test_다른_프로필의_세션은_취소할_수_없다(aiohttp_client, oauth_api):
    oauth_api.create_profile("mia")
    client = await _client(aiohttp_client, oauth_api)
    await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    resp = await client.delete("/p/mia/deskrpg/oauth/sessions/sid-1")
    assert resp.status == 400
    assert (await resp.json())["error"] == "oauth_session_mismatch"
    assert "sid-1" in oauth_api._oauth_sessions
    assert "cancelled" not in oauth_api._oauth_sessions["sid-1"]


async def test_디바이스_로그인이_없는_프로바이더는_400(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/noah/deskrpg/oauth/qwen-oauth/start")
    assert resp.status == 400
    assert (await resp.json())["error"] == "oauth_flow_unsupported"


async def test_Hermes_시작_실패는_502_와_잘린_사유(aiohttp_client, oauth_api):
    async def boom(provider_id, profile=None):
        raise FakeHTTPException(500, "device-auth failed: " + "x" * 500)
    oauth_api._start_device_code_flow = boom
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    assert resp.status == 502
    body = await resp.json()
    assert body["error"] == "oauth_start_failed" and len(body["detail"]) <= 200


async def test_연결_끊기는_그_프로필_홈에서_지운다(aiohttp_client, oauth_api):
    seen = []
    oauth_api.clear_provider_auth = lambda pid=None: seen.append((pid, str(oauth_api.get_hermes_home()))) or True
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.delete("/p/noah/deskrpg/oauth/openai-codex")
    assert await resp.json() == {"ok": True}
    assert seen == [("openai-codex", str(oauth_api.get_profile_dir("noah")))]


async def test_심볼이_없는_빌드에는_라우트와_capability_가_없다(aiohttp_client, oauth_api):
    oauth_api._start_device_code_flow = None
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/noah/deskrpg/oauth/openai-codex/start")).status == 404
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert "profile_oauth" not in caps


async def test_시작_전에_Hermes_처럼_만료_세션을_치운다(aiohttp_client, oauth_api):
    order = []
    inner = oauth_api._start_device_code_flow
    oauth_api._gc_oauth_sessions = lambda: order.append("gc")

    async def start(provider_id, profile=None):
        order.append("start")
        return await inner(provider_id, profile=profile)

    oauth_api._start_device_code_flow = start
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/noah/deskrpg/oauth/openai-codex/start")).status == 200
    assert order == ["gc", "start"]


@pytest.mark.parametrize("provider_id", ["nous", "xai-oauth", "minimax-oauth"])
async def test_Codex_외_디바이스_로그인은_앱에서_시작도_연결_끊기도_못한다(aiohttp_client, oauth_api, provider_id):
    # Hermes 의 xAI·MiniMax 폴러는 취소된 세션의 토큰을 default 프로필에 저장할 수 있다
    # (hermes_cli/web_server_oauth.py:397, :424) — 앱 안 로그인은 Codex 만 허용한다.
    oauth_api._DEVICE_CODE_STARTERS = {"openai-codex": object(), provider_id: object()}
    client = await _client(aiohttp_client, oauth_api)
    start = await client.post(f"/p/noah/deskrpg/oauth/{provider_id}/start")
    assert start.status == 400 and (await start.json())["error"] == "oauth_flow_unsupported"
    assert "start" not in oauth_api._calls and oauth_api._oauth_sessions == {}
    gone = await client.delete(f"/p/noah/deskrpg/oauth/{provider_id}")
    assert gone.status == 400 and (await gone.json())["error"] == "oauth_flow_unsupported"
    assert oauth_api._cleared == []


async def test_디바이스_로그인이_아닌_프로바이더는_연결_끊기도_400(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.delete("/p/noah/deskrpg/oauth/anthropic")
    assert resp.status == 400
    assert (await resp.json())["error"] == "oauth_flow_unsupported"
    assert oauth_api._cleared == []


async def test_없는_프로필은_404(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/ghost/deskrpg/oauth/openai-codex/start")
    assert resp.status == 404
    assert "start" not in oauth_api._calls


async def test_default_프로필은_Hermes_에_None_으로_넘긴다(aiohttp_client, oauth_api):
    oauth_api.create_profile("default")
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/default/deskrpg/oauth/openai-codex/start")).status == 200
    assert oauth_api._calls["start"] == ("openai-codex", None)


async def test_시작_타임아웃은_504(aiohttp_client, oauth_api):
    async def slow(provider_id, profile=None):
        raise FakeHTTPException(504, "device-auth timed out")
    oauth_api._start_device_code_flow = slow
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    assert resp.status == 504
    assert (await resp.json())["error"] == "oauth_start_timeout"


async def _assert_cross_profile_rejected(client, api, path_profile):
    poll = await client.get(f"/p/{path_profile}/deskrpg/oauth/openai-codex/sessions/sid-1")
    assert poll.status == 400
    assert (await poll.json())["error"] == "oauth_session_mismatch"
    cancel = await client.delete(f"/p/{path_profile}/deskrpg/oauth/sessions/sid-1")
    assert cancel.status == 400
    assert (await cancel.json())["error"] == "oauth_session_mismatch"
    assert "sid-1" in api._oauth_sessions and "cancelled" not in api._oauth_sessions["sid-1"]


async def test_default_세션은_이름있는_프로필에서_폴링도_취소도_못한다(aiohttp_client, oauth_api):
    oauth_api.create_profile("default")
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/default/deskrpg/oauth/openai-codex/start")).status == 200
    await _assert_cross_profile_rejected(client, oauth_api, "noah")


async def test_이름있는_세션은_default_경로에서_폴링도_취소도_못한다(aiohttp_client, oauth_api):
    oauth_api.create_profile("default")
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/noah/deskrpg/oauth/openai-codex/start")).status == 200
    await _assert_cross_profile_rejected(client, oauth_api, "default")


async def test_다른_프로바이더로_폴링하면_불일치다(aiohttp_client, oauth_api):
    client = await _client(aiohttp_client, oauth_api)
    await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    resp = await client.get("/p/noah/deskrpg/oauth/nous/sessions/sid-1")
    assert resp.status == 400
    assert (await resp.json())["error"] == "oauth_session_mismatch"


# `current` 는 Hermes 에서 유효한 프로필 이름이지만 `_oauth_profile_name` 은 그것을 None(=default)으로 바꾼다.
# 그대로 넘기면 `current` 프로필이 default 의 세션을 보고, Codex 토큰이 default 의 auth.json 에 저장된다.
async def test_current_프로필은_시작을_거절한다(aiohttp_client, oauth_api):
    oauth_api.create_profile("current")
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/current/deskrpg/oauth/openai-codex/start")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_profile"
    assert "start" not in oauth_api._calls and oauth_api._oauth_sessions == {}


async def test_current_프로필은_default_세션을_폴링도_취소도_못한다(aiohttp_client, oauth_api):
    oauth_api.create_profile("default")
    oauth_api.create_profile("current")
    client = await _client(aiohttp_client, oauth_api)
    assert (await client.post("/p/default/deskrpg/oauth/openai-codex/start")).status == 200
    poll = await client.get("/p/current/deskrpg/oauth/openai-codex/sessions/sid-1")
    assert poll.status == 400 and (await poll.json())["error"] == "invalid_profile"
    cancel = await client.delete("/p/current/deskrpg/oauth/sessions/sid-1")
    assert cancel.status == 400 and (await cancel.json())["error"] == "invalid_profile"
    assert "sid-1" in oauth_api._oauth_sessions and "cancelled" not in oauth_api._oauth_sessions["sid-1"]


async def test_디바이스_지원_프로바이더의_Hermes_400_은_거절로_구분한다(aiohttp_client, oauth_api):
    async def refuse(provider_id, profile=None):
        raise FakeHTTPException(400, "Already signed in to Nous Portal. " + "y" * 400)
    oauth_api._start_device_code_flow = refuse
    client = await _client(aiohttp_client, oauth_api)
    resp = await client.post("/p/noah/deskrpg/oauth/openai-codex/start")
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "oauth_start_rejected"
    assert body["detail"].startswith("Already signed in") and len(body["detail"]) <= 200
