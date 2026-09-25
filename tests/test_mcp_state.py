import json

import pytest

from deskrpg_plugin import _hermes_api, contract_fields, mcp_state
from deskrpg_plugin.common import RequestError


def test_mcp_admin_capability_requires_every_symbol(fake_api):
    assert contract_fields.has_mcp_admin_symbols(fake_api)
    assert "profile_mcp_admin" in contract_fields.capabilities(fake_api)
    fake_api._connect_server = None
    assert not contract_fields.has_mcp_admin_symbols(fake_api)
    assert "profile_mcp_admin" not in contract_fields.capabilities(fake_api)


def test_capability_symbols_match_optional_spec_block():
    assert set(contract_fields._MCP_ADMIN_SYMBOLS) <= set(_hermes_api.OPTIONAL)


def test_aliases_resolve_to_flat_names():
    assert "mcp_oauth_start" in _hermes_api.OPTIONAL
    assert "start" not in _hermes_api.OPTIONAL
    assert "mcp_registry" in _hermes_api.OPTIONAL


@pytest.mark.parametrize("bad", ["", "A", "a/b", "../x", "a b", "한글", "x" * 65, "-a"])
def test_require_name_rejects(bad):
    with pytest.raises(RequestError) as exc:
        mcp_state.require_name(bad)
    assert exc.value.code == "invalid_name"


def test_require_name_accepts():
    assert mcp_state.require_name("github-2_x") == "github-2_x"


def test_revision_is_order_independent():
    assert mcp_state.revision({"a": 1, "b": 2}) == mcp_state.revision({"b": 2, "a": 1})
    assert len(mcp_state.revision({})) == 16


def test_view_never_carries_secret_values(fake_api):
    home = fake_api.create_profile("sophie")
    (home / ".env").write_text("MCP_GH_API_KEY=ghp_secretvalue\nGH_TOKEN=abc\n")
    entry = {"url": "https://mcp.example.com/v1?token=zzz", "headers": {"Authorization": "Bearer ${MCP_GH_API_KEY}"},
             "env": {"GH_TOKEN": "${GH_TOKEN}", "EMPTY": "${NOPE}"}}
    view = mcp_state.server_view(fake_api, home, "gh", entry, {})
    text = json.dumps(view)
    assert "ghp_secretvalue" not in text and "zzz" not in text and "abc" not in text
    assert view["endpointSummary"] == "mcp.example.com/v1"
    assert view["auth"] == "bearer"
    assert {"key": "MCP_GH_API_KEY", "hasValue": True} in view["secrets"]
    assert {"key": "NOPE", "hasValue": False} in view["secrets"]


def test_stdio_view_hides_args(fake_api):
    home = fake_api.create_profile("sophie")
    view = mcp_state.server_view(fake_api, home, "fs", {"command": "/usr/bin/npx", "args": ["-y", "--token=abc"]}, {})
    assert view["endpointSummary"] == "npx (2 args)" and "abc" not in json.dumps(view)


def test_detail_carries_names_not_values(fake_api):
    home = fake_api.create_profile("sophie")
    entry = {"url": "https://a.example/x?k=v", "headers": {"X-Key": "${K}"}, "tools": {"include": ["a"], "prompts": False}}
    detail = mcp_state.server_detail(fake_api, home, "a", entry, {})
    assert detail["url"] == "https://a.example/x" and detail["headerKeys"] == ["X-Key"]
    assert detail["toolFilter"] == {"include": ["a"]}


def test_checks_roundtrip_and_delete(fake_api):
    home = fake_api.create_profile("sophie")
    mcp_state.write_check(home, "gh", {"at": "2026-09-25T00:00:00Z", "ok": True})
    assert mcp_state.read_checks(home)["gh"]["ok"] is True
    mcp_state.write_check(home, "gh", None)
    assert "gh" not in mcp_state.read_checks(home)


def test_audit_appends_jsonl(fake_api):
    home = fake_api.create_profile("sophie")
    mcp_state.audit(home, "u-1", "create", "gh")
    mcp_state.audit(home, None, "delete", "gh")
    rows = [json.loads(line) for line in (home / "plugin-data" / "deskrpg" / "mcp-audit.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["create", "delete"] and rows[0]["actor"] == "u-1"


def test_request_error_extra_reaches_body():
    resp = RequestError(422, "mcp_security_rejected", "x").with_extra(reasons=["r1"]).response()
    assert json.loads(resp.body)["reasons"] == ["r1"]


def test_env_refs_cover_oauth_args_and_url():
    entry = {"url": "https://x.example/${TENANT}/mcp", "args": ["--key", "${API_K}"],
             "oauth": {"client_id": "abc", "client_secret": "${ASANA_CLIENT_SECRET}"},
             "env": {"A": "${A}"}}
    assert mcp_state.env_refs(entry) == ["A", "ASANA_CLIENT_SECRET", "API_K", "TENANT"]
