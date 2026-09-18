"""실제 Hermes 위의 키 입력·OAuth 배선. 네트워크는 쓰지 않는다 — Codex 시작은 가짜 워커로 막는다."""

import pytest

pytestmark = pytest.mark.integration

SECRET = "sk-it-KEYINPUT-0123456789abcdef"


async def test_키를_넣으면_재시작_없이_카탈로그가_인증됨으로_바뀐다(client, make_profile):
    make_profile("noah")
    before = {r["id"]: r for r in (await (await client.get("/p/noah/deskrpg/catalog")).json())["providers"]}
    pid = next(i for i, r in before.items() if r["authType"] == "api_key" and not r["authenticated"])
    resp = await client.put(f"/p/noah/deskrpg/provider-keys/{pid}", json={"value": SECRET})
    assert resp.status == 200 and SECRET not in await resp.text()
    after = {r["id"]: r for r in (await (await client.get("/p/noah/deskrpg/catalog")).json())["providers"]}
    assert after[pid]["authenticated"] is True
    assert (await client.delete(f"/p/noah/deskrpg/provider-keys/{pid}")).status == 200
    again = {r["id"]: r for r in (await (await client.get("/p/noah/deskrpg/catalog")).json())["providers"]}
    assert again[pid]["authenticated"] is False


async def test_Codex_는_oauth_device_이고_qwen_은_external_이다(client, make_profile):
    make_profile("noah")
    rows = {r["id"]: r for r in (await (await client.get("/p/noah/deskrpg/catalog")).json())["providers"]}
    assert rows["openai-codex"]["authType"] == "oauth_device"
    assert rows["qwen-oauth"]["authType"] == "external"
    assert rows["qwen-oauth"]["cliCommand"].startswith("hermes -p noah ")


async def test_OAuth_시작_폴링_취소가_Hermes_세션을_거친다(client, make_profile, monkeypatch):
    make_profile("noah")
    import hermes_cli.web_routers.oauth as hermes_oauth
    from hermes_cli import web_server_oauth as wso

    def fake_worker(session_id):  # 네트워크 없이 사용자 코드만 채운다
        with wso._oauth_sessions_lock:
            wso._oauth_sessions[session_id].update(user_code="TEST-CODE", verification_url="https://auth.openai.com/codex/device")

    monkeypatch.setattr(hermes_oauth, "_codex_full_login_worker", fake_worker)
    start = await (await client.post("/p/noah/deskrpg/oauth/openai-codex/start")).json()
    assert start["userCode"] == "TEST-CODE"
    sid = start["sessionId"]
    assert wso._oauth_sessions[sid]["profile"] == "noah"
    poll = await (await client.get(f"/p/noah/deskrpg/oauth/openai-codex/sessions/{sid}")).json()
    assert poll["status"] == "pending"
    assert (await (await client.delete(f"/p/noah/deskrpg/oauth/sessions/{sid}")).json()) == {"ok": True}
    assert sid not in wso._oauth_sessions


async def test_current_이라는_이름의_프로필은_OAuth_시작에서_400_invalid_profile_이다(client, api):
    from hermes_cli import profiles as hermes_profiles

    try:
        hermes_profiles.validate_profile_name("current")
    except ValueError as exc:
        pytest.skip(f"Hermes 가 'current' 를 프로필 이름으로 허용하지 않는다: {exc}")
    api.create_profile("current", no_alias=True)
    resp = await client.post("/p/current/deskrpg/oauth/openai-codex/start")
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_profile"


async def test_xAI_디바이스_로그인은_400_이고_Hermes_세션을_만들지_않는다(client, make_profile):
    # xAI 폴러는 취소 뒤 세션이 사라지면 default 프로필에 토큰을 저장한다(hermes_cli/web_server_oauth.py:424).
    make_profile("noah")
    from hermes_cli import web_server_oauth as wso

    before = set(wso._oauth_sessions)
    resp = await client.post("/p/noah/deskrpg/oauth/xai-oauth/start")
    assert resp.status == 400 and (await resp.json())["error"] == "oauth_flow_unsupported"
    assert set(wso._oauth_sessions) == before
    rows = {r["id"]: r for r in (await (await client.get("/p/noah/deskrpg/catalog")).json())["providers"]}
    for pid in ("nous", "xai-oauth", "minimax-oauth"):
        assert rows[pid]["authType"] == "external"
        assert rows[pid]["cliCommand"] == f"hermes -p noah auth add {pid}"
