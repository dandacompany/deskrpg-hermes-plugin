"""가짜 Hermes MCP 심볼(0.17.0) — 현재 home(`api.get_hermes_home()`)의 config.yaml·.env 를 실제로 쓴다.

프로필 스코프가 맞는지 확인하려고 가짜도 home 오버라이드를 따른다. 값(비밀)은 저장소에 두지 않는다.
테스트가 특정 동작을 원하면 `fake_api.fake_mcp.<필드>` 를 바꾸거나 `fake_api.<이름> = …` 로 덮는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class FakeTool:
    name: str
    description: str = ""
    annotations: object = None


@dataclass
class FakeServer:
    _tools: list

    async def shutdown(self):
        return None


@dataclass
class FakeCatalogEnv:
    name: str
    prompt: str = ""
    required: bool = True
    secret: bool = True


@dataclass
class FakeMcp:
    api: object
    tools_by_server: dict = field(default_factory=dict)  # name -> [FakeTool]
    connect_errors: dict = field(default_factory=dict)  # name -> Exception
    reload_calls: list = field(default_factory=list)
    oauth_flows: dict = field(default_factory=dict)  # session_id -> dict
    catalog: list = field(default_factory=list)
    installs: list = field(default_factory=list)

    def _cfg_path(self) -> Path:
        return Path(self.api.get_hermes_home()) / "config.yaml"

    def _load(self) -> dict:
        path = self._cfg_path()
        return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}

    def _dump(self, data: dict) -> None:
        self._cfg_path().write_text(yaml.safe_dump(data))

    def get_servers(self, config=None):
        return dict((config or self._load()).get("mcp_servers") or {})

    def save_server(self, name, cfg):
        if self.validate(name, cfg):
            return False
        data = self._load()
        data.setdefault("mcp_servers", {})[name] = cfg
        self._dump(data)
        return True

    def remove_server(self, name):
        data = self._load()
        servers = data.get("mcp_servers") or {}
        if name not in servers:
            return False
        del servers[name]
        if not servers:
            data.pop("mcp_servers", None)
        self._dump(data)
        return True

    @staticmethod
    def validate(name, entry):
        text = " ".join(str(v) for v in [entry.get("command"), *(entry.get("args") or [])])
        return ["known malicious pattern"] if "evil.example" in text else []


def install_fake_mcp(api) -> FakeMcp:
    fake = FakeMcp(api)

    async def connect(name, cfg):
        if name in fake.connect_errors:
            raise fake.connect_errors[name]
        return FakeServer(list(fake.tools_by_server.get(name, [])))

    def run_on_loop(coro, timeout=None):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @contextlib.contextmanager
    def runtime_scope(profile_home, prepared_secret_scope=None, *, hydrate_secrets=True):
        token = api.set_hermes_home_override(str(profile_home))
        try:
            yield
        finally:
            api.reset_hermes_home_override(token)

    class Registry:
        def current_scope_key(self):
            return str(api.get_hermes_home())

    class Flow:
        def __init__(self, fid):
            self.flow_id = fid

    class Attempt:
        def __init__(self, url, fid):
            self.auth_url, self.flow = url, Flow(fid)

    def oauth_start(server, *, client_redirect_uri=None, cfg=None, **_):
        fid = f"flow-{len(fake.oauth_flows) + 1}"
        fake.oauth_flows[fid] = {"server": server, "redirect": client_redirect_uri, "state": "st-" + fid,
                                 "status": "pending", "home": str(api.get_hermes_home())}
        return Attempt(f"https://auth.example/authorize?state=st-{fid}", fid)

    def deliver(session_id, server, *, code, state, error=None, iss=None):
        flow = fake.oauth_flows.get(session_id)
        if not flow or flow["server"] != server or state != flow["state"] or flow["status"] != "pending":
            return {"ok": False, "error_message": "state mismatch"}
        flow["status"] = "approved"
        return {"ok": True, "session_id": session_id}

    def poll(session_id, server):
        flow = fake.oauth_flows[session_id]
        return {"status": flow["status"], "tools": ["t1"] if flow["status"] == "approved" else []}

    def cancel(session_id, server, hermes_home):
        return {"ok": fake.oauth_flows.pop(session_id, None) is not None}

    def env_key(name):
        return "MCP_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_API_KEY"

    api._get_mcp_servers = fake.get_servers
    api._save_mcp_server = fake.save_server
    api._remove_mcp_server = fake.remove_server
    api._env_key_for_server = env_key
    api._bearer_auth_headers = lambda n: {"Authorization": "Bearer ${" + env_key(n) + "}"}
    api._oauth_tokens_present = lambda n: (Path(api.get_hermes_home()) / "mcp-tokens" / f"{n}.json").exists()
    api.redact_mcp_probe_text = lambda t: re.sub(r"(Bearer\s+)\S+", r"\1***", str(t))
    api._resolve_mcp_server_config = lambda cfg: dict(cfg)
    api.validate_mcp_server_entry = fake.validate
    api._ensure_mcp_loop = lambda: None
    api._run_on_mcp_loop = run_on_loop
    api._connect_server = connect
    api._stop_mcp_loop_if_idle = lambda: None
    api.shutdown_mcp_servers = lambda scope=None: fake.reload_calls.append(("shutdown", scope))
    api.discover_mcp_tools = lambda: fake.reload_calls.append(("discover", None)) or []
    api.reprobe_tool_availability = lambda: fake.reload_calls.append(("reprobe", None))
    api.mcp_registry = Registry()
    api._profile_runtime_scope = runtime_scope
    api.mcp_list_catalog = lambda: list(fake.catalog)
    api.mcp_get_catalog_entry = lambda n: next((e for e in fake.catalog if e.name == n), None)
    def card_install_config(entry):
        # Hermes `card_install_config` 와 같은 모양 — `_build_server_config` + enabled, 프롬프트·탐침 없음.
        fake.installs.append(entry.name)
        t, auth = entry.transport, entry.auth
        cfg = {}
        if t.type == "stdio":
            cfg["command"] = t.command
            if getattr(t, "args", None):
                cfg["args"] = list(t.args)
            if getattr(t, "env", None):
                cfg["env"] = dict(t.env)
        else:
            cfg["url"] = t.url
            if auth.type == "oauth":
                cfg["auth"] = "oauth"
            elif auth.type == "api_key":
                cfg["headers"] = api._bearer_auth_headers(entry.name)
        cfg["enabled"] = True
        return cfg

    api.mcp_card_install_config = card_install_config
    api.mcp_oauth_start = oauth_start
    api.mcp_oauth_cancel_attempt = lambda flow: False
    api.deliver_callback_flow = deliver
    api.poll_flow = poll
    api.cancel_flow = cancel
    return fake
