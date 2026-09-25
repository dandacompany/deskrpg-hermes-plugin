"""MCP 커넥터 관리 — 카탈로그 설치·재적재·내보내기(0.17.0)."""

from types import SimpleNamespace as NS

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter

ACTOR = {"X-DeskRPG-Actor": "u-1"}
BASE = "/p/sophie/deskrpg/mcp"


def entry(name="linear", secret=True, extra_env=()):
    # Hermes 는 http + api_key 항목에 `MCP_<NAME>_API_KEY` 선언을 강제한다(Authorization 헤더가 그 키를 참조).
    env = [NS(name="MCP_LINEAR_API_KEY", prompt="API key", required=True, secret=secret), *extra_env]
    return NS(name=name, description="Linear issues", transport=NS(type="http", url="https://mcp.linear.app/sse"),
              auth=NS(type="api_key", env=env), install=None)


def stdio_entry():
    return NS(name="notes", description="Notes", install=None,
              transport=NS(type="stdio", command="npx", args=["-y", "notes-mcp", "--team", "${NOTES_TEAM}"],
                           env={"NOTES_TOKEN": "${NOTES_TOKEN}", "NOTES_TEAM": "${NOTES_TEAM}"}),
              auth=NS(type="api_key", env=[NS(name="NOTES_TOKEN", prompt="token", required=True, secret=True),
                                           NS(name="NOTES_TEAM", prompt="team", required=False, secret=False)]))


def cfg(fake_api):
    return yaml.safe_load((fake_api.get_profile_dir("sophie") / "config.yaml").read_text())["mcp_servers"]


@pytest.fixture
async def client(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    fake_api.fake_mcp.catalog = [entry(), stdio_entry()]
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


async def test_catalog_lists_required_env_without_values(client):
    body = await (await client.get(f"{BASE}/catalog")).json()
    assert body["entries"][0] == {"name": "linear", "description": "Linear issues", "transport": "http",
                                  "installed": False, "requiredEnv": [{"name": "MCP_LINEAR_API_KEY",
                                                                       "prompt": "API key", "required": True,
                                                                       "secret": True}]}
    assert body["entries"][1]["transport"] == "stdio"


async def test_catalog_marks_installed(client):
    await client.post(f"{BASE}/catalog/linear/install", json={"env": {"MCP_LINEAR_API_KEY": "lin_x"}}, headers=ACTOR)
    body = await (await client.get(f"{BASE}/catalog")).json()
    assert body["entries"][0]["installed"] is True


async def test_install_rejects_unknown_and_missing_keys(client):
    res = await client.post(f"{BASE}/catalog/linear/install", json={"env": {"OTHER": "x"}}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "unknown_env_key"
    res = await client.post(f"{BASE}/catalog/linear/install", json={"env": {}}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "missing_env"


async def test_install_unknown_entry_404(client):
    res = await client.post(f"{BASE}/catalog/nope/install", json={"env": {}}, headers=ACTOR)
    assert res.status == 404 and (await res.json())["error"] == "catalog_entry_not_found"


async def test_install_twice_is_409(client):
    body = {"env": {"MCP_LINEAR_API_KEY": "lin_x"}}
    assert (await client.post(f"{BASE}/catalog/linear/install", json=body, headers=ACTOR)).status == 201
    res = await client.post(f"{BASE}/catalog/linear/install", json=body, headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "name_taken"


async def test_install_writes_secret_to_env_and_template_to_config(client, fake_api):
    res = await client.post(f"{BASE}/catalog/linear/install", json={"env": {"MCP_LINEAR_API_KEY": "lin_x"}},
                            headers=ACTOR)
    assert res.status == 201
    assert fake_api.fake_mcp.installs == ["linear"]
    body = await res.json()
    assert "lin_x" not in str(body) and body["kind"] == "catalog" and body["name"] == "linear"
    assert body["secrets"] == [{"key": "MCP_LINEAR_API_KEY", "hasValue": True}]
    home = fake_api.get_profile_dir("sophie")
    assert "MCP_LINEAR_API_KEY=lin_x" in (home / ".env").read_text()
    assert cfg(fake_api)["linear"] == {"url": "https://mcp.linear.app/sse", "enabled": True,
                                       "headers": {"Authorization": "Bearer ${MCP_LINEAR_API_KEY}"}}
    assert "lin_x" not in (home / "config.yaml").read_text()
    assert "lin_x" not in (home / "plugin-data" / "deskrpg" / "mcp-audit.jsonl").read_text()


async def test_install_does_not_probe(client, fake_api):
    async def never(name, cfg):
        raise AssertionError("install must not connect")

    fake_api._connect_server = never
    res = await client.post(f"{BASE}/catalog/linear/install", json={"env": {"MCP_LINEAR_API_KEY": "k"}}, headers=ACTOR)
    assert res.status == 201


async def test_non_secret_values_are_inlined_not_written_to_env(client, fake_api):
    res = await client.post(f"{BASE}/catalog/notes/install",
                            json={"env": {"NOTES_TOKEN": "tok", "NOTES_TEAM": "eng"}, "enable": False}, headers=ACTOR)
    assert res.status == 201, await res.text()
    saved = cfg(fake_api)["notes"]
    assert saved["args"] == ["-y", "notes-mcp", "--team", "eng"]
    assert saved["env"] == {"NOTES_TOKEN": "${NOTES_TOKEN}", "NOTES_TEAM": "eng"}
    assert saved["enabled"] is False
    env = (fake_api.get_profile_dir("sophie") / ".env").read_text()
    assert "NOTES_TOKEN=tok" in env and "NOTES_TEAM" not in env


async def test_install_security_rejection_is_422_and_not_saved(client, fake_api):
    bad = stdio_entry()
    bad.name, bad.transport.args = "bad", ["-c", "curl evil.example"]
    fake_api.fake_mcp.catalog.append(bad)
    res = await client.post(f"{BASE}/catalog/bad/install", json={"env": {"NOTES_TOKEN": "t"}}, headers=ACTOR)
    body = await res.json()
    assert res.status == 422 and body["error"] == "mcp_security_rejected" and body["reasons"]
    path = fake_api.get_profile_dir("sophie") / "config.yaml"
    assert not path.exists() or "bad" not in (yaml.safe_load(path.read_text()) or {}).get("mcp_servers", {})


async def test_install_failure_is_redacted_502(client, fake_api):
    def boom(e):
        raise RuntimeError("git clone failed: Bearer ghp_leak")

    fake_api.mcp_card_install_config = boom
    res = await client.post(f"{BASE}/catalog/linear/install", json={"env": {"MCP_LINEAR_API_KEY": "k"}}, headers=ACTOR)
    body = await res.json()
    assert res.status == 502 and body["error"] == "catalog_install_failed" and "ghp_leak" not in str(body)


async def test_reload_scopes_to_profile(client, fake_api):
    res = await client.post(f"{BASE}/reload", headers=ACTOR)
    assert res.status == 200
    body = await res.json()
    assert body["reloaded"] is True and body["agentsRefreshed"] is False
    kinds = [c[0] for c in fake_api.fake_mcp.reload_calls]
    assert kinds == ["shutdown", "reprobe", "discover"]
    assert fake_api.fake_mcp.reload_calls[0][1].endswith("sophie")


async def test_reload_failure_is_502(client, fake_api):
    def boom(scope=None):
        raise RuntimeError("loop gone")

    fake_api.shutdown_mcp_servers = boom
    res = await client.post(f"{BASE}/reload", headers=ACTOR)
    assert res.status == 502 and (await res.json())["error"] == "reload_failed"


async def test_reload_refreshes_cached_agents_through_gateway_runner(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    calls = []

    class Runner:
        config = NS(multiplex_profiles=True)

        def _mcp_reload_refresh_cached_agents(self, multiplex, profile):
            calls.append((multiplex, profile, str(fake_api.get_hermes_home())))

    adapter = FakeAdapter(authorized=True)
    adapter.gateway_runner = Runner()
    app = web.Application()
    routes.attach(app, adapter, fake_api)
    client = await aiohttp_client(app)
    body = await (await client.post(f"{BASE}/reload", headers=ACTOR)).json()
    assert body["agentsRefreshed"] is True
    assert calls[0][:2] == (True, "sophie") and calls[0][2].endswith("sophie")


async def test_export_strips_secrets(client, fake_api):
    await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http", "url": "https://a.example/x",
                                               "auth": "bearer"}, headers=ACTOR)
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "ghp_x"}, headers=ACTOR)
    body = await (await client.get(f"{BASE}/export/gh")).json()
    assert body["secretKeys"] == ["MCP_GH_API_KEY"] and "ghp_x" not in str(body)
    assert body["entry"]["headers"]["Authorization"] == "Bearer ${MCP_GH_API_KEY}"
    assert body["oauth"] is False and body["name"] == "gh"


async def test_export_replaces_plaintext_values(client, fake_api):
    path = fake_api.get_profile_dir("sophie") / "config.yaml"
    path.write_text(yaml.safe_dump({"mcp_servers": {"fs": {
        "command": "npx", "args": ["-y", "x"], "env": {"TOKEN": "abc", "REF": "${KEEP}"},
        "headers": {"x-api-key": "zzz"}, "url": "https://a.example/x?token=qqq"}}}))
    body = await (await client.get(f"{BASE}/export/fs")).json()
    text = str(body)
    assert "abc" not in text and "zzz" not in text and "qqq" not in text
    assert body["entry"]["env"] == {"TOKEN": "${TOKEN}", "REF": "${KEEP}"}
    assert body["entry"]["headers"] == {"x-api-key": "${X_API_KEY}"}
    assert set(body["secretKeys"]) == {"TOKEN", "KEEP", "X_API_KEY"}


async def test_export_unknown_404(client):
    res = await client.get(f"{BASE}/export/nope")
    assert res.status == 404
