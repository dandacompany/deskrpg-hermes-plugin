"""POST /p/{profile}/deskrpg/skill-invocation — expand `/skill … instruction` with Hermes' public builders."""

from pathlib import Path

import pytest
from aiohttp import web

from deskrpg_plugin import contract_fields, routes
from tests.conftest import FakeAdapter

URL = "/p/{}/deskrpg/skill-invocation"


class FakeSkillCommands:
    """The upstream skill-command functions, scoped by `get_hermes_home()` like the real ones.

    Each call records the home override in force at that moment, so a test can tell whether the
    lookup ran in the requested profile's scope.
    """

    def __init__(self, api):
        self.api = api
        self.skills: dict[str, set[str]] = {}  # profile home -> skill names
        self.disabled: dict[str, set[str]] = {}
        self.broken: set[str] = set()  # keys whose payload fails to load
        self.calls: list[tuple] = []

    def _home(self) -> str:
        return str(Path(self.api.get_hermes_home()))

    def seed(self, profile, *names, disabled=()):
        home = str(self.api.get_profile_dir(profile))
        self.skills.setdefault(home, set()).update(names, disabled)
        self.disabled.setdefault(home, set()).update(disabled)

    def get_skill_commands(self):
        # Hermes drops disabled skills from the slash-command map while scanning.
        home = self._home()
        self.calls.append(("get_skill_commands", home))
        names = self.skills.get(home, set()) - self.disabled.get(home, set())
        return {f"/{n}": {"name": n, "skill_dir": f"{home}/skills/{n}"} for n in names}

    def resolve_skill_command_key(self, command):
        key = f"/{command.replace('_', '-')}"
        return key if key in self.get_skill_commands() else None

    def get_disabled_skill_names(self, platform=None):
        self.calls.append(("get_disabled_skill_names", self._home(), platform))
        return set(self.disabled.get(self._home(), set()))

    def build_skill_invocation_message(self, cmd_key, user_instruction=""):
        self.calls.append(("single", self._home(), cmd_key, user_instruction))
        if cmd_key in self.broken:
            return None
        return f"[skill {cmd_key}] {user_instruction}"

    def build_stacked_skill_invocation_message(self, cmd_keys, user_instruction=""):
        self.calls.append(("stacked", self._home(), list(cmd_keys), user_instruction))
        loaded = [k.lstrip("/") for k in cmd_keys if k not in self.broken]
        missing = [k.lstrip("/") for k in cmd_keys if k in self.broken]
        if not loaded:
            return None
        return (f"[stack {' '.join(cmd_keys)}] {user_instruction}", loaded, missing)

    def install(self):
        for name in ("get_skill_commands", "resolve_skill_command_key", "get_disabled_skill_names",
                     "build_skill_invocation_message", "build_stacked_skill_invocation_message"):
            setattr(self.api, name, getattr(self, name))
        return self


@pytest.fixture
def fake(fake_api):
    fake_api.create_profile("alpha")
    fake_api.create_profile("beta")
    return FakeSkillCommands(fake_api).install()


async def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


async def _post(client, profile, body):
    resp = await client.post(URL.format(profile), json=body)
    return resp.status, await resp.json()


async def test_single_skill_expands_with_the_upstream_builder(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research")
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["research"], "instruction": "  go  "})
    assert status == 200
    assert body == {"message": "[skill /research] go", "loaded": ["research"], "missing": []}
    assert [c for c in fake.calls if c[0] in ("single", "stacked")] == [
        ("single", str(fake_api.get_profile_dir("alpha")), "/research", "go"),
    ]


async def test_stacked_skills_use_the_stacked_builder_in_order(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research", "write-report")
    client = await _client(aiohttp_client, fake_api)
    # A leading slash is allowed and an empty instruction is fine.
    status, body = await _post(client, "alpha", {"skills": ["/write-report", "research"], "instruction": ""})
    assert status == 200
    assert body == {"message": "[stack /write-report /research] ", "loaded": ["write-report", "research"],
                    "missing": []}
    assert [c[0] for c in fake.calls if c[0] in ("single", "stacked")] == ["stacked"]


async def test_unknown_skill_is_404_with_missing(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research")
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["research", "nope"], "instruction": "x"})
    assert status == 404
    assert body["error"] == "skill_not_found" and body["missing"] == ["nope"]
    assert not [c for c in fake.calls if c[0] in ("single", "stacked")]


async def test_disabled_skill_is_409_with_disabled(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research", disabled=("old-report",))
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["research", "old-report"], "instruction": "x"})
    assert status == 409
    assert body["error"] == "skill_disabled" and body["disabled"] == ["old-report"]
    # DeskRPG chat runs on the API server, so its platform-specific disabled list applies.
    assert ("get_disabled_skill_names", str(fake_api.get_profile_dir("alpha")), "api_server") in fake.calls
    assert not [c for c in fake.calls if c[0] in ("single", "stacked")]


async def test_skill_resolved_but_disabled_for_the_api_server_is_409(aiohttp_client, fake_api, fake):
    # The slash-command scan runs without a platform, so a skill switched off only for the API server
    # still resolves. It must not be expanded into a chat that runs there.
    fake.seed("alpha", "research")
    real = fake.get_disabled_skill_names
    fake_api.get_disabled_skill_names = lambda platform=None: real(platform) | (
        {"research"} if platform == "api_server" else set())
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["research"], "instruction": "x"})
    assert status == 409 and body["disabled"] == ["research"]


async def test_six_skills_are_rejected(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "a", "b", "c", "d", "e", "f")
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": list("abcdef"), "instruction": "x"})
    assert status == 400 and body["error"] == "too_many_skills"
    status, _ = await _post(client, "alpha", {"skills": list("abcde"), "instruction": "x"})
    assert status == 200


@pytest.mark.parametrize("body", [
    {"skills": ["research", "research"], "instruction": "x"},
    {"skills": ["research", "/research"], "instruction": "x"},
    {"skills": ["write_report", "write-report"], "instruction": "x"},
    {"skills": ["../x"], "instruction": "x"},
    {"skills": ["a b"], "instruction": "x"},
    {"skills": [""], "instruction": "x"},
    {"skills": [3], "instruction": "x"},
    {"skills": [], "instruction": "x"},
    {"skills": "research", "instruction": "x"},
    {"instruction": "x"},
    {"skills": ["research"], "instruction": 3},
])
async def test_duplicate_or_malformed_skills_are_rejected(aiohttp_client, fake_api, fake, body):
    fake.seed("alpha", "research")
    client = await _client(aiohttp_client, fake_api)
    status, resp = await _post(client, "alpha", body)
    assert status == 400 and resp["error"] == "invalid_skills"
    assert not fake.calls


async def test_home_override_scopes_the_lookup_to_the_profile(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research")
    before = fake_api.get_hermes_home()
    client = await _client(aiohttp_client, fake_api)
    status, _ = await _post(client, "alpha", {"skills": ["research"], "instruction": "x"})
    assert status == 200
    alpha_home = str(fake_api.get_profile_dir("alpha"))
    assert fake.calls and all(call[1] == alpha_home for call in fake.calls)
    assert fake_api.get_hermes_home() == before


async def test_home_override_is_reset_when_the_lookup_fails(aiohttp_client, fake_api, fake):
    before = fake_api.get_hermes_home()
    client = await _client(aiohttp_client, fake_api)
    status, _ = await _post(client, "alpha", {"skills": ["nope"], "instruction": "x"})
    assert status == 404
    assert fake_api.get_hermes_home() == before


async def test_a_skill_of_another_profile_is_not_found(aiohttp_client, fake_api, fake):
    fake.seed("beta", "beta-only")
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["beta-only"], "instruction": "x"})
    assert status == 404 and body["missing"] == ["beta-only"]
    status, _ = await _post(client, "beta", {"skills": ["beta-only"], "instruction": "x"})
    assert status == 200


async def test_a_skill_that_fails_to_load_is_422(aiohttp_client, fake_api, fake):
    fake.seed("alpha", "research", "write-report")
    fake.broken.add("/write-report")
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "alpha", {"skills": ["write-report"], "instruction": "x"})
    assert status == 422 and body["error"] == "skill_load_failed"
    # A stack that loads only part of its skills is a failure too: never send a silently thinner prompt.
    status, body = await _post(client, "alpha", {"skills": ["research", "write-report"], "instruction": "x"})
    assert status == 422 and body["error"] == "skill_load_failed" and body["missing"] == ["write-report"]


async def test_unknown_profile_is_404(aiohttp_client, fake_api, fake):
    client = await _client(aiohttp_client, fake_api)
    status, body = await _post(client, "ghost", {"skills": ["research"], "instruction": "x"})
    assert status == 404 and body["error"] == "profile_not_found"


def test_capability_is_announced_only_with_every_symbol(fake_api, fake):
    assert "skill_invocation" in contract_fields.capabilities(fake_api)
    for name in ("build_skill_invocation_message", "build_stacked_skill_invocation_message",
                 "resolve_skill_command_key", "get_skill_commands", "get_disabled_skill_names"):
        saved = getattr(fake_api, name)
        setattr(fake_api, name, None)
        assert "skill_invocation" not in contract_fields.capabilities(fake_api), name
        setattr(fake_api, name, saved)


def _without_symbols(api):
    for name in ("build_skill_invocation_message", "build_stacked_skill_invocation_message",
                 "resolve_skill_command_key", "get_skill_commands", "get_disabled_skill_names"):
        setattr(api, name, None)


def test_capability_is_absent_on_a_build_without_the_symbols(fake_api):
    _without_symbols(fake_api)
    assert "skill_invocation" not in contract_fields.capabilities(fake_api)


async def test_the_route_is_not_registered_on_a_build_without_the_symbols(aiohttp_client, fake_api):
    _without_symbols(fake_api)
    fake_api.create_profile("alpha")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post(URL.format("alpha"), json={"skills": ["research"], "instruction": "x"})
    assert resp.status == 404
