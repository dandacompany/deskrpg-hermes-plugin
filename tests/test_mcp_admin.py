"""MCP 커넥터 관리 — 서버 목록·추가·수정·삭제·비밀값(0.17.0)."""

from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter

ACTOR = {"X-DeskRPG-Actor": "u-1"}
BASE = "/p/sophie/deskrpg/mcp"


@pytest.fixture
async def client(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def cfg(fake_api):
    path = fake_api.get_profile_dir("sophie") / "config.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


async def _http(client, name="gh", auth="none", url="https://a.example/x"):
    res = await client.post(f"{BASE}/servers", json={"name": name, "transport": "http", "url": url, "auth": auth},
                            headers=ACTOR)
    assert res.status == 201, await res.text()
    return await res.json()


async def test_create_http_and_list(client, fake_api):
    res = await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http",
                                                     "url": "https://mcp.example.com/x", "auth": "none"}, headers=ACTOR)
    assert res.status == 201
    body = await (await client.get(f"{BASE}/servers")).json()
    assert [s["name"] for s in body["servers"]] == ["gh"]
    assert cfg(fake_api)["mcp_servers"]["gh"]["url"] == "https://mcp.example.com/x"


async def test_list_is_empty_without_servers(client):
    res = await client.get(f"{BASE}/servers")
    assert res.status == 200 and await res.json() == {"servers": []}


async def test_list_marks_catalog_entries(client, fake_api):
    fake_api.fake_mcp.catalog.append(SimpleNamespace(name="gh"))
    await _http(client)
    body = await (await client.get(f"{BASE}/servers")).json()
    assert body["servers"][0]["kind"] == "catalog"


async def test_writes_go_to_the_requested_profile_only(client, fake_api):
    fake_api.create_profile("other")
    await _http(client)
    other = fake_api.get_profile_dir("other") / "config.yaml"
    assert not other.exists() or "gh" not in (yaml.safe_load(other.read_text()) or {}).get("mcp_servers", {})


async def test_create_duplicate_is_409(client):
    payload = {"name": "gh", "transport": "http", "url": "https://a.example/x", "auth": "none"}
    await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)
    res = await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "name_taken"


@pytest.mark.parametrize("bad", ["A", "a/b", "../x", "a b", "한글", "-a"])
async def test_create_rejects_bad_names(client, bad):
    res = await client.post(f"{BASE}/servers", json={"name": bad, "transport": "http",
                                                     "url": "https://a.example/x", "auth": "none"}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "invalid_name"


async def test_path_name_is_validated(client):
    res = await client.get(f"{BASE}/servers/UPPER")
    assert res.status == 400 and (await res.json())["error"] == "invalid_name"


async def test_detail_404_for_unknown_server(client):
    res = await client.get(f"{BASE}/servers/nope")
    assert res.status == 404 and (await res.json())["error"] == "server_not_found"


async def test_http_url_must_be_http_scheme(client):
    res = await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http",
                                                     "url": "file:///etc/passwd", "auth": "none"}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "invalid_field"


async def test_stdio_requires_confirmation_and_defaults_untrusted(client, fake_api):
    payload = {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "@x/fs"], "auth": "none"}
    res = await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "confirmation_required"
    res = await client.post(f"{BASE}/servers", json={**payload, "confirmName": "fs"}, headers=ACTOR)
    assert res.status == 201
    assert cfg(fake_api)["mcp_servers"]["fs"]["trust"] == "untrusted"


async def test_stdio_explicit_trust_is_kept(client, fake_api):
    payload = {"name": "fs", "transport": "stdio", "command": "npx", "args": [], "auth": "none",
               "trust": "full", "confirmName": "fs"}
    assert (await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)).status == 201
    assert cfg(fake_api)["mcp_servers"]["fs"]["trust"] == "full"


async def test_stdio_env_values_are_never_stored(client, fake_api):
    payload = {"name": "fs", "transport": "stdio", "command": "npx", "auth": "env", "confirmName": "fs",
               "env": {"FS_TOKEN": "ghp_abc"}, "passthroughEnv": ["HOME_DIR"]}
    assert (await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)).status == 201
    env = cfg(fake_api)["mcp_servers"]["fs"]["env"]
    assert env == {"FS_TOKEN": "${FS_TOKEN}", "HOME_DIR": "${HOME_DIR}"}
    assert "ghp_abc" not in (fake_api.get_profile_dir("sophie") / "config.yaml").read_text()


async def test_security_rejection_is_422_and_not_saved(client, fake_api):
    payload = {"name": "bad", "transport": "stdio", "command": "sh", "args": ["-c", "curl evil.example"],
               "auth": "none", "confirmName": "bad"}
    res = await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)
    body = await res.json()
    assert res.status == 422 and body["error"] == "mcp_security_rejected" and body["reasons"]
    assert "bad" not in (cfg(fake_api).get("mcp_servers") or {})


async def test_save_failure_without_reasons_is_500_save_failed(client, fake_api):
    fake_api._save_mcp_server = lambda name, entry: False
    res = await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http",
                                                     "url": "https://a.example/x", "auth": "none"}, headers=ACTOR)
    assert res.status == 500 and (await res.json())["error"] == "mcp_save_failed"


async def test_update_revision_conflict(client):
    await _http(client)
    res = await client.put(f"{BASE}/servers/gh", json={"url": "https://b.example/x", "baseRevision": "0" * 16},
                           headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "revision_conflict"


async def test_update_with_current_revision(client, fake_api):
    created = await _http(client)
    res = await client.put(f"{BASE}/servers/gh", json={"url": "https://b.example/y",
                                                       "baseRevision": created["revision"]}, headers=ACTOR)
    assert res.status == 200
    assert cfg(fake_api)["mcp_servers"]["gh"]["url"] == "https://b.example/y"


async def test_update_stdio_command_needs_confirmation(client, fake_api):
    payload = {"name": "fs", "transport": "stdio", "command": "npx", "args": [], "auth": "none", "confirmName": "fs"}
    created = await (await client.post(f"{BASE}/servers", json=payload, headers=ACTOR)).json()
    res = await client.put(f"{BASE}/servers/fs", json={"command": "uvx", "baseRevision": created["revision"]},
                           headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "confirmation_required"
    res = await client.put(f"{BASE}/servers/fs", json={"command": "uvx", "baseRevision": created["revision"],
                                                       "confirmName": "fs"}, headers=ACTOR)
    assert res.status == 200 and cfg(fake_api)["mcp_servers"]["fs"]["command"] == "uvx"


async def test_bearer_secret_goes_to_env_only(client, fake_api):
    await _http(client, auth="bearer")
    res = await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "Bearer ghp_abc"}, headers=ACTOR)
    assert res.status == 200 and await res.json() == {"key": "MCP_GH_API_KEY", "hasValue": True}
    env = (fake_api.get_profile_dir("sophie") / ".env").read_text()
    assert "MCP_GH_API_KEY=ghp_abc" in env
    assert "ghp_abc" not in (fake_api.get_profile_dir("sophie") / "config.yaml").read_text()
    listed = await (await client.get(f"{BASE}/servers")).text()
    assert "ghp_abc" not in listed
    detail = await (await client.get(f"{BASE}/servers/gh")).text()
    assert "ghp_abc" not in detail


async def test_secret_key_must_be_referenced(client):
    await _http(client)
    res = await client.put(f"{BASE}/servers/gh/secrets/PATH", json={"value": "x"}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "secret_key_not_referenced"


async def test_secret_value_rejects_newline(client):
    await _http(client, auth="bearer")
    res = await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "a\nEVIL=1"}, headers=ACTOR)
    assert res.status == 400 and (await res.json())["error"] == "invalid_field"


async def test_secret_delete(client, fake_api):
    await _http(client, auth="bearer")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "v"}, headers=ACTOR)
    res = await client.delete(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", headers=ACTOR)
    assert res.status == 200 and await res.json() == {"key": "MCP_GH_API_KEY", "hasValue": False}
    assert "MCP_GH_API_KEY" not in (fake_api.get_profile_dir("sophie") / ".env").read_text()


async def test_env_duplicate_lines_collapse(client, fake_api):
    home = fake_api.get_profile_dir("sophie")
    (home / ".env").write_text("MCP_GH_API_KEY=old1\nMCP_GH_API_KEY=old2\n")
    await _http(client, auth="bearer")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "new"}, headers=ACTOR)
    assert (home / ".env").read_text().count("MCP_GH_API_KEY=") == 1


async def test_delete_removes_env_tokens_and_check(client, fake_api):
    from deskrpg_plugin import mcp_state
    home = fake_api.get_profile_dir("sophie")
    (home / ".env").write_text("KEEP_ME=1\n")
    await _http(client, auth="bearer")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "v"}, headers=ACTOR)
    mcp_state.write_check(home, "gh", {"at": "2026-09-25T00:00:00Z", "ok": True})
    (home / "mcp-tokens").mkdir()
    (home / "mcp-tokens" / "gh.json").write_text("{}")
    (home / "mcp-tokens" / "gh.client.json").write_text("{}")
    (home / "mcp-tokens" / "ghx.json").write_text("{}")
    res = await client.delete(f"{BASE}/servers/gh", headers=ACTOR)
    assert res.status == 200
    env = (home / ".env").read_text()
    assert "MCP_GH_API_KEY" not in env and "KEEP_ME=1" in env
    assert sorted(p.name for p in (home / "mcp-tokens").iterdir()) == ["ghx.json"]
    assert "gh" not in mcp_state.read_checks(home)
    assert "gh" not in (cfg(fake_api).get("mcp_servers") or {})


async def test_delete_unknown_is_404(client):
    res = await client.delete(f"{BASE}/servers/nope", headers=ACTOR)
    assert res.status == 404


async def test_enabled_trust_and_tools_filter(client, fake_api):
    await _http(client)
    await client.put(f"{BASE}/servers/gh/enabled", json={"enabled": False}, headers=ACTOR)
    await client.put(f"{BASE}/servers/gh/trust", json={"trust": "untrusted"}, headers=ACTOR)
    rev = (await (await client.get(f"{BASE}/servers/gh")).json())["revision"]
    res = await client.put(f"{BASE}/servers/gh/tools", json={"include": ["read_*"], "baseRevision": rev}, headers=ACTOR)
    assert res.status == 200
    entry = cfg(fake_api)["mcp_servers"]["gh"]
    assert entry["enabled"] is False and entry["trust"] == "untrusted" and entry["tools"]["include"] == ["read_*"]


async def test_tools_filter_revision_conflict(client):
    await _http(client)
    res = await client.put(f"{BASE}/servers/gh/tools", json={"include": [], "baseRevision": "0" * 16}, headers=ACTOR)
    assert res.status == 409 and (await res.json())["error"] == "revision_conflict"


async def test_trust_rejects_unknown_value(client):
    await _http(client)
    res = await client.put(f"{BASE}/servers/gh/trust", json={"trust": "yolo"}, headers=ACTOR)
    assert res.status == 400


async def test_audit_has_no_values(client, fake_api):
    await _http(client, auth="bearer", url="https://a.example/x?k=zz")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "topsecret"}, headers=ACTOR)
    log = (fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "mcp-audit.jsonl").read_text()
    assert "topsecret" not in log and "k=zz" not in log and '"actor": "u-1"' in log


async def test_routes_absent_without_mcp_symbols(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    del fake_api._connect_server
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    client = await aiohttp_client(app)
    assert (await client.get(f"{BASE}/servers")).status == 404


async def test_views_read_raw_templates_not_expanded_values(client, fake_api, monkeypatch):
    """실제 Hermes `_get_mcp_servers()` 는 load_config 를 거쳐 `${VAR}` 를 값으로 펼친다 — 응답은 원본을 읽어야 한다."""
    await _http(client, auth="bearer")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "ghp_abc"}, headers=ACTOR)
    raw = fake_api._get_mcp_servers

    def expanded(config=None):
        return {n: {**e, "headers": {k: v.replace("${MCP_GH_API_KEY}", "ghp_abc") for k, v in
                                     (e.get("headers") or {}).items()}} for n, e in raw(config).items()}

    monkeypatch.setattr(fake_api, "_get_mcp_servers", expanded)
    listed = await (await client.get(f"{BASE}/servers")).json()
    detail = await (await client.get(f"{BASE}/servers/gh")).text()
    assert "ghp_abc" not in str(listed) and "ghp_abc" not in detail
    assert listed["servers"][0]["secrets"] == [{"key": "MCP_GH_API_KEY", "hasValue": True}]
    res = await client.put(f"{BASE}/servers/gh/enabled", json={"enabled": False}, headers=ACTOR)
    assert res.status == 200
    assert "ghp_abc" not in (fake_api.get_profile_dir("sophie") / "config.yaml").read_text()


async def test_plain_header_values_move_to_env(client, fake_api):
    res = await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http", "url": "https://a.example/x",
                                                     "auth": "env", "headers": {"X-Api-Key": "ghp_abc",
                                                                                "X-Ref": "${ALREADY_SET}"}},
                            headers=ACTOR)
    assert res.status == 201
    headers = cfg(fake_api)["mcp_servers"]["gh"]["headers"]
    assert headers == {"X-Api-Key": "${MCP_GH_X_API_KEY}", "X-Ref": "${ALREADY_SET}"}
    home = fake_api.get_profile_dir("sophie")
    assert "MCP_GH_X_API_KEY=ghp_abc" in (home / ".env").read_text()
    body = await res.json()
    assert "ghp_abc" not in str(body)
    assert {"key": "MCP_GH_X_API_KEY", "hasValue": True} in body["secrets"]


async def test_header_with_newline_rejected(client):
    res = await client.post(f"{BASE}/servers", json={"name": "gh", "transport": "http", "url": "https://a.example/x",
                                                     "auth": "none", "headers": {"X-A": "a\nb"}}, headers=ACTOR)
    assert res.status == 400


async def test_delete_keeps_keys_other_servers_use(client, fake_api):
    home = fake_api.get_profile_dir("sophie")
    await _http(client, name="gh", auth="bearer")
    await _http(client, name="gh_x", auth="bearer")
    await client.put(f"{BASE}/servers/gh/secrets/MCP_GH_API_KEY", json={"value": "a"}, headers=ACTOR)
    await client.put(f"{BASE}/servers/gh_x/secrets/MCP_GH_X_API_KEY", json={"value": "b"}, headers=ACTOR)
    assert (await client.delete(f"{BASE}/servers/gh", headers=ACTOR)).status == 200
    env = (home / ".env").read_text()
    assert "MCP_GH_API_KEY" not in env and "MCP_GH_X_API_KEY=b" in env
