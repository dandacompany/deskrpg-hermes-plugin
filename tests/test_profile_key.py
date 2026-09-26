"""Key for a profile that already exists — `POST /deskrpg/profiles/{name}/key`.

A profile made outside DeskRPG (`hermes profile create` on the host) has no API key DeskRPG can
learn, and this plugin issues keys only when it creates a profile. This route issues one for an
existing profile on the owner key. It never hands back a key that is already there: an existing
key is replaced only when the caller asks for it, because that cuts off whatever else uses it.
"""

import logging

from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def _env(fake_api, name):
    return (fake_api.get_profile_dir(name) / ".env").read_text(encoding="utf-8")


def _set_env(fake_api, name, text):
    (fake_api.get_profile_dir(name) / ".env").write_text(text, encoding="utf-8")


async def test_a_profile_without_a_key_gets_one_once(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    _set_env(fake_api, "noah", "# Per-profile secrets\nOPENAI_API_KEY=sk-keep-me\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key", json={})
    body = await resp.json()
    assert resp.status == 201
    assert body["name"] == "noah" and body["issued"] is True and body["rotated"] is False
    assert len(body["apiKey"]) >= 16
    env = _env(fake_api, "noah")
    assert f"API_SERVER_KEY={body['apiKey']}" in env
    assert "OPENAI_API_KEY=sk-keep-me" in env, "other lines stay"


async def test_an_empty_key_line_counts_as_no_key(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    _set_env(fake_api, "noah", "API_SERVER_KEY=\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key")
    assert resp.status == 201
    assert _env(fake_api, "noah").count("API_SERVER_KEY=") == 1


async def test_an_existing_key_is_refused_and_never_read_back(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    _set_env(fake_api, "noah", "API_SERVER_KEY=existing-key-abcdefghijklmnop\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key", json={})
    assert resp.status == 409
    assert (await resp.json()) == {"error": "key_exists", "name": "noah"}
    assert "existing-key-abcdefghijklmnop" in _env(fake_api, "noah"), "the key is left as it was"


async def test_rotate_replaces_an_existing_key(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    _set_env(fake_api, "noah", "API_SERVER_KEY=existing-key-abcdefghijklmnop\n")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key", json={"rotate": True})
    body = await resp.json()
    assert resp.status == 201
    assert body["rotated"] is True
    env = _env(fake_api, "noah")
    assert "existing-key-abcdefghijklmnop" not in env
    assert f"API_SERVER_KEY={body['apiKey']}" in env


async def test_rotate_must_be_true(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    for bad in ({"rotate": "yes"}, {"rotate": 1}, ["rotate"]):
        resp = await client.post("/deskrpg/profiles/noah/key", json=bad)
        assert resp.status == 400, bad


async def test_unknown_profile_is_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/ghost/key")
    assert resp.status == 404
    assert (await resp.json())["error"] == "no_profile"


async def test_the_default_profile_is_refused(aiohttp_client, fake_api):
    # The default profile's key is the owner key itself — minting it here would hand the whole
    # gateway to whoever holds the response.
    fake_api.create_profile("default")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/default/key")
    assert resp.status == 400
    assert (await resp.json())["error"] == "default_profile"


async def test_an_external_secret_reference_is_left_alone(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    for ref in ("${VAULT_NOAH}", "op://vault/noah/key", "bw://noah"):
        _set_env(fake_api, "noah", f"API_SERVER_KEY={ref}\n")
        resp = await client.post("/deskrpg/profiles/noah/key", json={"rotate": True})
        assert resp.status == 409, ref
        assert (await resp.json())["error"] == "external_secret_provider"
        assert ref in _env(fake_api, "noah")


async def test_a_profile_with_an_enabled_secrets_provider_is_left_alone(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    (fake_api.get_profile_dir("noah") / "config.yaml").write_text(
        "secrets:\n  onepassword:\n    enabled: true\n", encoding="utf-8"
    )
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key")
    assert resp.status == 409
    assert (await resp.json())["error"] == "external_secret_provider"


async def test_invalid_name_is_400(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/..bad/key")
    assert resp.status in (400, 404)


async def test_a_write_failure_says_why_without_the_value(aiohttp_client, fake_api, monkeypatch):
    from deskrpg_plugin import keyissue

    def boom(_dir):
        raise keyissue.KeyIssueFailed(".env write failed: PermissionError")

    monkeypatch.setattr(keyissue, "issue", boom)
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/profiles/noah/key")
    body = await resp.json()
    assert resp.status == 500
    assert body == {"error": "key_issue_failed", "reason": ".env write failed: PermissionError", "name": "noah"}


async def test_the_key_value_never_reaches_the_log(aiohttp_client, fake_api, caplog):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    with caplog.at_level(logging.DEBUG):
        body = await (await client.post("/deskrpg/profiles/noah/key")).json()
        rotated = await (await client.post("/deskrpg/profiles/noah/key", json={"rotate": True})).json()
    assert body["apiKey"] not in caplog.text
    assert rotated["apiKey"] not in caplog.text
    assert "noah" in caplog.text, "the event itself is logged by name"


async def test_the_route_is_advertised_as_a_capability(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert "profile_key_issue" in caps
