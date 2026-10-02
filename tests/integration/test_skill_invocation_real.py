"""Skill invocation on a real Hermes: its builders must see the requesting profile's skills and config."""

import pytest
import yaml

from deskrpg_plugin.contract_fields import capabilities, has_skill_invocation_symbols

pytestmark = pytest.mark.integration

URL = "/p/{}/deskrpg/skill-invocation"


def _skill(home, name, body):
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} probe\n---\n{body}\n", encoding="utf-8")


async def test_the_installed_hermes_has_the_public_builders(api):
    assert has_skill_invocation_symbols(api)
    assert "skill_invocation" in capabilities(api)


async def test_expansion_is_scoped_to_the_requesting_profile(client, make_profile):
    noah = make_profile("noah")
    make_profile("mia")
    _skill(noah, "invoice-check", "Compare every amount with the ledger.")
    _skill(noah, "tax-memo", "Write the memo in plain words.")

    one = await client.post(URL.format("noah"), json={"skills": ["invoice-check"], "instruction": "check March"})
    assert one.status == 200, await one.text()
    body = await one.json()
    assert body["loaded"] == ["invoice-check"] and body["missing"] == []
    assert "Compare every amount with the ledger." in body["message"] and "check March" in body["message"]

    two = await client.post(URL.format("noah"), json={"skills": ["/tax-memo", "invoice-check"], "instruction": ""})
    assert two.status == 200, await two.text()
    message = (await two.json())["message"]
    assert "Write the memo in plain words." in message and "Compare every amount with the ledger." in message

    other = await client.post(URL.format("mia"), json={"skills": ["invoice-check"], "instruction": "x"})
    assert other.status == 404
    body = await other.json()
    assert body["error"] == "skill_not_found" and body["missing"] == ["invoice-check"]


async def test_a_skill_disabled_in_the_profile_config_is_409(client, make_profile):
    noah = make_profile("noah")
    _skill(noah, "invoice-check", "Compare every amount with the ledger.")
    _skill(noah, "tax-memo", "Write the memo in plain words.")
    (noah / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"platform_disabled": {"api_server": ["tax-memo"]}}}), encoding="utf-8")

    resp = await client.post(URL.format("noah"), json={"skills": ["invoice-check", "tax-memo"], "instruction": "x"})
    assert resp.status == 409, await resp.text()
    assert (await resp.json())["disabled"] == ["tax-memo"]
    ok = await client.post(URL.format("noah"), json={"skills": ["invoice-check"], "instruction": "x"})
    assert ok.status == 200
